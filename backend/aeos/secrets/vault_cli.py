"""`aeos vault ...` - local OpenBao lifecycle (§07 order 3, §26). ASCII output only.

install  shows the plan (official release, asset, checksum, target, privilege, cost, rollback) and
         needs --yes or an interactive confirmation; user level only, never system-wide.
start    generated loopback-only config, detached server, pid recorded.
stop     stops the recorded server process.
status   health/sealed/initialised, AppRole presence, interim mode.
init     init + unseal + KV v2 + policies + AppRole; keeps the unseal key share(s) in the OS keyring and never
         prints them (ADR-0123); the root token is revoked, or kept in the keyring only while setup is incomplete.
         On Windows (export-recovery refused) it also shows the recovery material ONCE on a temporary screen.
         --show-unseal-once: the former flow, the keys ONCE on a temporary terminal screen (refused outside a
         terminal unless --plain-output).
unseal   applies the unseal key(s) from the OS keyring when OpenBao is sealed, otherwise prompts (hidden).
import-unseal-key  one-time migration: an existing unseal key from a hidden prompt into the OS keyring;
                   --with-approle also restores the AppRole credentials from a recovery copy (lost keyring).
export-recovery    the stored key(s) and AppRole credentials to a new owner-only (0600) file outside every
                   repository; prints the path.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import sys
import textwrap
import time

import httpx
import typer

app = typer.Typer(help="Local OpenBao vault (install/start/stop/status/init/unseal/import-unseal-key/"
                       "export-recovery).", no_args_is_help=True)

# Alternate screen buffer (xterm/VT): what is written there never reaches the scrollback, so the unseal
# material cannot be copied later together with the rest of the terminal output. No erase-in-display is sent
# while it is active (iTerm2 saves an erased screen into the scrollback by default) and nothing may scroll off
# its top: rows are wiped with erase-in-line and a wrong answer is asked again on the same row.
ALT_SCREEN_ON = "\x1b[?1049h\x1b[H"  # entering mode 1049 clears the alternate screen
ALT_SCREEN_OFF = "\x1b[?1049l"
ASK_AGAIN = "\x1b[1A\x1b[2K"  # cursor up one row, erase that row
STORED_WORD = "stored"


def _err(msg: str) -> None:
    typer.secho(msg, fg="red", err=True)


def _vt_terminal() -> bool:
    """True when stdin and stdout are an interactive terminal that understands VT sequences."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    return os.environ.get("TERM", "") != "dumb"


def _show_on_temporary_screen(lines: list[str], what: str) -> bool:
    """Show `lines` on the alternate screen until the owner types STORED_WORD.

    False when the screen closed before that (Ctrl+C or end of input); the material is gone either way."""
    typer.echo(ALT_SCREEN_ON, nl=False, color=True)  # color=True: never strip the sequences
    try:
        for line in lines:
            typer.echo(line)
        while True:
            answer = typer.prompt(f"Type '{STORED_WORD}' after you have stored {what} offline", default="",
                                  show_default=False)
            if answer.strip().lower() == STORED_WORD:
                return True
            typer.echo(ASK_AGAIN, nl=False, color=True)
    except (typer.Abort, EOFError, KeyboardInterrupt):
        return False
    finally:
        rows = shutil.get_terminal_size().lines
        wipe = "".join(f"\x1b[{row};1H\x1b[2K" for row in range(1, rows + 1))
        typer.echo(wipe + ALT_SCREEN_OFF, nl=False, color=True)


def _show_material_once(res, *, threshold: int, shares: int, vt: bool, kept_in: str | None = None,
                        approle: tuple[str, str] | None = None) -> tuple[bool | None, str]:
    """Show the init material ONCE (temporary screen, or stdout with --plain-output). Returns (shown, what).

    shown is None for plain output, False when the temporary screen closed before STORED_WORD. `kept_in` names the
    OS keyring when the material is also stored there (Windows: this screen replaces export-recovery)."""
    incomplete = not res.root_revoked  # a revoked root token is useless; shown only while the owner still needs it
    if approle:
        what = ("the unseal key(s), the AppRole credentials and the root token" if incomplete
                else "the unseal key(s) and the AppRole credentials")
    else:
        what = "the unseal key(s) and the root token" if incomplete else "the unseal key(s)"
    banner = ["=" * 72,
              f"SHOWN ONCE - store {what} OFFLINE (password manager / paper). Never put them in the repo,",
              "a config file, a chat or an AI assistant, and never take a screenshot of them:"]
    if res.error:
        banner.append(f"SETUP INCOMPLETE ({res.error}): you need the root token below to finish.")
    banner += [f"  Unseal key {i}: {k}" for i, k in enumerate(res.keys, 1)]
    if approle:
        banner += [f"  AppRole role_id  : {approle[0]}", f"  AppRole secret_id: {approle[1]}"]
    if incomplete:
        banner.append(f"  Root token  : {res.root_token}  (NOT revoked: revoke it when the setup is finished)")
    if kept_in:
        from aeos.secrets.unseal import lost_keyring_line

        banner += [f"  (also kept in the {kept_in}: aeos serve and aeos vault unseal use them after a restart;",
                   "   this is your offline copy for a lost keyring.)",
                   *textwrap.wrap(lost_keyring_line(len(res.keys), approle_included=approle is not None), 70,
                                  initial_indent="  ", subsequent_indent="   ", break_on_hyphens=False)]
    else:
        banner.append(f"  ({threshold} of {shares} keys are needed after every restart: aeos vault unseal)")
    banner.append("=" * 72)
    shown = _show_on_temporary_screen(banner, what) if vt else None
    typer.echo("")
    if shown is None:
        for line in banner:
            typer.echo(line)
        typer.secho("Clear or close this terminal after storing them; never copy this output.", fg="yellow")
    else:
        typer.echo("The material was shown on a temporary screen; it is not written to this terminal's scrollback.")
    return shown, what


def _settings():
    from aeos.core.config import get_settings

    return get_settings()


def _observe(status: str, version: str | None, detail: dict, *, dimension: str = "installation",
             identity: str | None = None) -> None:
    """Best-effort inventory observation (installation / functional dimension)."""
    try:
        from aeos.cli import ensure_database
        from aeos.core.db import session_scope
        from aeos.inventory.facade import record_observation

        ensure_database()
        with session_scope() as s:
            record_observation(s, component_id="openbao", dimension=dimension, status=status,
                               observer="aeos.secrets.vault_cli", observed_version=version, detail=detail,
                               observed_identity=identity,
                               ttl_seconds=None if dimension == "installation" else 7 * 86400)
    except (ImportError, ModuleNotFoundError):
        pass
    except Exception as exc:  # pragma: no cover
        typer.secho(f"warning: inventory observation not recorded ({type(exc).__name__})", fg="yellow", err=True)


@app.command("install")
def install_cmd(
    version: str = typer.Option(None, help="Exact OpenBao version (default: the install-manifest approved_pin; "
                                           "'latest' is never implied)"),
    yes: bool = typer.Option(False, "--yes", help="Confirm the shown plan without prompting"),
) -> None:
    """Install the official OpenBao binary into <data dir>/bin after showing the plan."""
    from aeos.secrets import installer

    # §07: installers are pinned. The default is the reviewed manifest pin; without one the owner must name
    # an exact version - an unpinned "latest" download is never the default.
    if version is not None and version.strip().lower() == "latest":
        _err("'latest' is not a pin. Pass an exact version (e.g. --version 2.7.1) or record approved_pin for "
             "openbao in install-manifest/manifest.yaml.")
        raise typer.Exit(1)
    pin_source = "explicit --version (owner choice; not the manifest pin)"
    if version is None:
        version = installer.manifest_pin()
        pin_source = "install-manifest approved_pin"
        if version is None:
            _err("install-manifest/manifest.yaml has no approved_pin for openbao. Pass an exact version you "
                 "reviewed (--version X.Y.Z) or record the pin in the manifest first.")
            raise typer.Exit(1)
    settings = _settings()
    with httpx.Client(headers={"User-Agent": "dly-aeos-vault-install"}, follow_redirects=False) as client:
        try:
            release = installer.resolve_release(client, version)
            plan = {**installer.install_plan(settings, release), "pin_source": pin_source}
        except (installer.InstallError, httpx.HTTPError) as exc:
            _err(f"cannot plan the install: {exc}")
            raise typer.Exit(1) from exc
        typer.echo("Install plan (nothing has been changed yet):")
        for k, v in plan.items():
            typer.echo(f"  {k:<14} {v}")
        if not yes and not typer.confirm("Proceed with this user-level install?", default=False):
            typer.echo("Aborted - nothing installed.")
            raise typer.Exit(1)
        try:
            result = installer.install(settings, client, plan)
        except (installer.InstallError, httpx.HTTPError, OSError) as exc:
            _err(f"install failed, nothing installed: {exc}")
            _observe("INSTALL_FAILED", plan["version"], {"error": str(exc)[:200]})
            raise typer.Exit(1) from exc
    _observe("INSTALLED", result["version"],
             {k: result.get(k) for k in ("archive_sha256", "binary_sha256", "verification", "digest_status",
                                         "digest_platform", "signature")},
             identity=result["binary_digest"])  # the executable digest the inventory drift check compares
    typer.echo(f"Installed OpenBao {result['version']} -> {result['installed']}")
    typer.echo("Signature (GPG/sigstore) NOT verified; SHA256 matched checksums.txt.")
    target = result.get("digest_platform") or "this platform"
    if result["digest_status"] == "MATCH":
        typer.echo(f"Binary digest matched the install-manifest approved_digest ({target}).")
    else:
        typer.secho(f"Binary digest NOT checked against a reviewed digest ({result['digest_status']}): record "
                    f"approved_digest {result['binary_digest']} for this pin under {target} in "
                    "install-manifest/manifest.yaml (approved_digest: {" + str(target) + ": sha256:...}) through a "
                    "reviewed PR.", fg="yellow")
    typer.echo("Next: aeos vault start  then  aeos vault init")


@app.command("start")
def start_cmd(wait: float = typer.Option(10.0, help="Seconds to wait for the listener")) -> None:
    """Start the local OpenBao server (loopback only) in the background."""
    from aeos.secrets import server
    from aeos.secrets.openbao import OpenBaoClient, OpenBaoUnavailable

    settings = _settings()
    binary = server.bin_path(settings)
    if binary is None:
        found = server.bin_lookup(settings)
        if found.rejected:
            # the settings override wins over <data>/bin, so `aeos vault install` would not change anything
            _err(f"settings openbao_binary={found.rejected} is not an executable file; fix the setting or remove it")
        else:
            _err("OpenBao binary not found. Run: aeos vault install")
        raise typer.Exit(1)
    try:
        info = server.start(settings, binary)
    except (OpenBaoUnavailable, OSError) as exc:
        _err(f"cannot start OpenBao: {exc}")
        raise typer.Exit(1) from exc
    if info.get("already_running"):
        typer.echo(f"OpenBao already running (pid {info['pid']}).")
        return
    deadline = time.monotonic() + wait
    health = None
    while time.monotonic() < deadline:
        try:
            health = OpenBaoClient(settings.openbao_addr).health()
            break
        except OpenBaoUnavailable:
            time.sleep(0.5)
    typer.echo(f"OpenBao started (pid {info['pid']}), config {info['config']}, log {info['log']}")
    if health is None:
        typer.secho("listener not reachable yet - check the log", fg="yellow")
    elif not health["initialized"]:
        typer.echo("Not initialised yet. Next: aeos vault init")
    elif health["sealed"]:
        typer.echo("Sealed. Next: aeos vault unseal")


@app.command("stop")
def stop_cmd() -> None:
    """Stop the OpenBao server started by `aeos vault start`."""
    from aeos.secrets import server

    typer.echo("Stopped." if server.stop(_settings()) else "No running OpenBao recorded.")


def _verify(settings) -> int:
    """Functional roundtrip with the controller AppRole token (write/read/delete, no values shown)."""
    from aeos.secrets.provider import OpenBaoProvider, record_verification, select_backend

    sel = select_backend(settings)
    if not isinstance(sel.provider, OpenBaoProvider):
        _err(f"verify: OpenBao is not the active store (backend {sel.backend}); " + "; ".join(sel.reasons))
        from aeos.core.timeutil import utcnow

        record_verification(settings, {"status": "BLOCKED", "steps": [], "checked_at": utcnow().isoformat(),
                                       "backend": sel.backend, "ref": None})
        return 1
    res = sel.provider.verify_roundtrip()
    record_verification(settings, res)
    tok = res.get("token") or {}
    typer.echo(f"functional check {res['status']} (write/read/delete {res['ref']})")
    for st in res["steps"]:
        typer.echo(f"  {st['id']:<8} {st['status']} {st.get('detail', '')}".rstrip())
    if tok.get("present"):
        typer.echo(f"  token    ttl={tok.get('ttl_seconds')}s renewable={tok.get('renewable')} "
                   f"policies={','.join(tok.get('policies') or []) or 'UNKNOWN'}")
    _observe("VERIFIED" if res["status"] == "VERIFIED" else "FAILED", None,
             {"dimension_note": "functional roundtrip", "status": res["status"]}, dimension="functional")
    return 0 if res["status"] == "VERIFIED" else 1


def _unseal_key_location(settings) -> str:
    """Where the unseal key lives (ADR-0123): a count and the store, never a value."""
    from aeos.core.errors import AeosError
    from aeos.secrets.provider import bootstrap_store, keyring_backend
    from aeos.secrets.unseal import keyring_location

    if not keyring_backend()[0]:
        return "UNKNOWN (OS keyring unavailable)"
    try:
        count = bootstrap_store(settings).unseal_key_count()
    except AeosError:
        return "UNKNOWN (OS keyring unavailable)"
    if not count:
        return "not stored (unseal by hand after a restart: aeos vault unseal)"
    return f"{count} share(s) in the {keyring_location(settings)}"


@app.command("status")
def status_cmd(verify: bool = typer.Option(False, "--verify", help="Also prove write/read/delete with the "
                                           "controller token (functional check)")) -> None:
    """Vault health, sealed/initialised, AppRole presence and interim-mode warning (no values)."""
    from aeos.secrets.health import store_health

    settings = _settings()
    code = _verify(settings) if verify else 0
    h = store_health(settings)
    b = h["openbao"]
    typer.echo(f"store status     {h['status']} (backend {h['backend']})")
    typer.echo(f"openbao          addr={b['addr']} reachable={b['reachable']} initialized={b['initialized']} "
               f"sealed={b['sealed']} version={b['version'] or 'UNKNOWN'}")
    typer.echo(f"binary           {'installed' if b['binary_installed'] else 'missing'}; server pid alive={b['server_pid_alive']}")
    typer.echo(f"approle keyring  {b['approle_in_keyring']}; login={b['login']}")
    typer.echo(f"unseal key       {_unseal_key_location(settings)}")
    typer.echo(f"os keyring       {h['keyring']['available']} ({h['keyring']['backend']})")
    if h["interim"]["active"]:
        typer.secho(f"WARNING interim  {h['interim']['residual_risk']}", fg="yellow")
    for r in h["reasons"]:
        typer.echo(f"reason           {r}")
    tok = b.get("token") or {}
    if tok.get("present"):
        typer.echo(f"token            ttl={tok.get('ttl_seconds')}s policies={','.join(tok.get('policies') or [])}")
    fc = h["functional_check"]
    typer.echo(f"functional check {fc.get('status')} at {fc.get('checked_at') or '-'}")
    for rem in h["remedies"]:
        typer.echo(f"remedy           {rem['command']}  ({rem['why']})")
    typer.echo(f"evidence limit   {h['evidence_limit']}")
    if code:
        raise typer.Exit(code)


@app.command("init")
def init_cmd(
    shares: int = typer.Option(1, help="Number of unseal key shares"),
    threshold: int = typer.Option(1, help="Keys required to unseal"),
    store_unseal_in_keyring: bool = typer.Option(
        True, "--store-unseal-in-keyring/--show-unseal-once",
        help="Default: keep the unseal key share(s) in the OS keyring, never printed; aeos serve and aeos vault "
             "unseal unseal from there (ADR-0123); on Windows they are also shown once for the offline copy. "
             "--show-unseal-once shows them once on a temporary screen instead"),
    plain_output: bool = typer.Option(
        False, "--plain-output",
        help="With --show-unseal-once (or on Windows) outside an interactive terminal, print the material to stdout "
             "(whatever captures it keeps it)"),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation"),
) -> None:
    """Initialise OpenBao for DLY AEOS (policies + AppRole) and revoke the root token afterwards."""
    from aeos.core.redaction import register_secret
    from aeos.secrets import server, unseal
    from aeos.secrets.openbao import OpenBaoError, OpenBaoUnavailable
    from aeos.secrets.provider import UNSEAL_KEYRING_RISK, bootstrap_store, reset_provider_cache
    from aeos.secrets.setup import CONTROLLER_POLICIES, POLICY_NAMESPACES, run_init

    settings = _settings()
    vt = _vt_terminal()
    # Windows: export-recovery refuses (owner-only ACL NOT-PROVEN), so the keyring default also shows the recovery
    # material once here; otherwise the Credential Manager would hold the only copy of the unseal key.
    copy_once = store_unseal_in_keyring and not unseal.recovery_export_supported()
    if (copy_once or not store_unseal_in_keyring) and not vt and not plain_output:
        if copy_once:
            _err("On Windows vault init also shows the recovery material (unseal key(s), AppRole credentials) once, "
                 "on a temporary terminal screen, because vault export-recovery is refused there (owner-only ACL "
                 "NOT-PROVEN), and this is not an interactive terminal. Run it in a terminal, or pass "
                 "--plain-output to print it to stdout. Nothing was changed.")
        else:
            _err("vault init --show-unseal-once shows the unseal key(s) once, on a temporary terminal screen, and this "
                 "is not an interactive terminal. Run it in a terminal, or pass --plain-output to print them to "
                 "stdout. Nothing was changed.")
        raise typer.Exit(1)
    if store_unseal_in_keyring and not vt and not plain_output:
        # Nothing could show the material if the keyring refused it after sys/init (the fallback needs the owner's
        # eyes), so prove a keyring write first and keep "nothing was changed" when it fails (ADR-0123 decision 1).
        why = bootstrap_store(settings).write_probe()
        if why:
            _err(f"The OS keyring did not take a test write ({why}). vault init stores the unseal key there; if it "
                 "refused that write after init, the material would have to be shown once, and this is not an "
                 "interactive terminal. Fix the keyring, run it in a terminal, or pass --plain-output. Nothing was "
                 "changed.")
            raise typer.Exit(1)
    location = unseal.keyring_location(settings)
    typer.echo("This will initialise the local OpenBao at " + settings.openbao_addr)
    typer.echo(f"  key shares/threshold : {shares}/{threshold}")
    typer.echo(f"  policies             : {', '.join(POLICY_NAMESPACES)}")
    typer.echo(f"  approle              : aeos-controller -> {', '.join(CONTROLLER_POLICIES)} (office NOT bound)")
    typer.echo("  bootstrap credentials: OS keyring service " + bootstrap_store(settings).service)
    if copy_once:
        typer.echo(f"  unseal key(s)        : {location}, and shown once on a temporary terminal screen for your "
                   "offline copy (export-recovery is refused on Windows)")
    else:
        typer.echo("  unseal key(s)        : " + (f"{location} (never printed)" if store_unseal_in_keyring
                                                else "shown once on a temporary terminal screen"))
    if not yes and not typer.confirm("Continue?", default=False):
        raise typer.Exit(1)
    reset = (f"reset the still empty vault: aeos vault stop, delete {server.server_dir(settings) / 'raft'}, "
             "aeos vault start, aeos vault init")
    try:
        res = run_init(settings.openbao_addr, shares=shares, threshold=threshold, bootstrap=bootstrap_store(settings),
                       store_unseal_in_keyring=store_unseal_in_keyring)
    except (OpenBaoError, ValueError) as exc:
        _err(f"init refused: {exc}")
        if isinstance(exc, OpenBaoUnavailable):
            _err("If `aeos vault status` now reports initialized=True, the keys were never received and the vault is "
                 f"unusable: {reset}.")
        raise typer.Exit(1) from exc
    for k in res.keys:
        register_secret(k)
    register_secret(res.root_token)
    # ADR-0123: the material is safe when the keys are in the keyring and the root token is revoked or kept there
    if res.unseal_stored and (res.root_revoked or res.root_stored):
        shown: bool | None = None
        what = ""
        if copy_once:
            approle = None
            if res.approle_stored:
                from aeos.core.errors import AeosError

                try:
                    approle = bootstrap_store(settings).approle()
                except AeosError:
                    approle = None
            shown, what = _show_material_once(res, threshold=threshold, shares=shares, vt=vt, kept_in=location,
                                              approle=approle)
        for step in res.steps:
            typer.echo(f"  done: {step}")
        reset_provider_cache()
        if copy_once:
            typer.echo(f"Unseal key share(s): {len(res.keys)} stored in the {location}. aeos serve and aeos vault "
                       "unseal unseal OpenBao from there after a restart. The material shown once is your offline "
                       "recovery copy (aeos vault export-recovery is refused on Windows).")
        else:
            typer.echo(f"Unseal key share(s): {len(res.keys)} stored in the {location}; never printed. aeos serve "
                       "and aeos vault unseal unseal OpenBao from there after a restart.")
            typer.echo("Make an offline recovery copy now, to a file outside every repository, then move it offline: "
                       "aeos vault export-recovery <file>")
        typer.secho(UNSEAL_KEYRING_RISK, fg="yellow")
        if shown is False:
            _err(f"The temporary screen closed before you typed '{STORED_WORD}'. The {location} now holds the only "
                 f"copy; if {what} are not stored offline, {reset}.")
        if res.error:
            _err(f"setup incomplete: {res.error}")
            where = ("it was shown once above to finish the setup" if copy_once else
                     "a recovery copy (aeos vault export-recovery <file>) contains it to finish the setup")
            _err(f"The root token was NOT revoked. It is kept in the OS keyring only; {where}. Otherwise {reset}.")
            raise typer.Exit(1)
        typer.echo("Root token revoked; the controller now uses AppRole with a 1h token TTL.")
        if shown is False:
            raise typer.Exit(1)
        return
    if store_unseal_in_keyring:
        _err(f"The OS keyring did not take the unseal material ({res.store_error or res.error}); it is shown once "
             "instead.")
        if not res.keys:
            _err(f"init returned no unseal key: {reset}.")
            raise typer.Exit(1)
        if not vt and not plain_output:
            _err("This is not an interactive terminal and --plain-output was not given, so the material cannot be "
                 f"shown and is lost: {reset}.")
            raise typer.Exit(1)
    shown, what = _show_material_once(res, threshold=threshold, shares=shares, vt=vt)
    for step in res.steps:
        typer.echo(f"  done: {step}")
    reset_provider_cache()
    if shown is False:
        _err(f"The temporary screen closed before you typed '{STORED_WORD}'. If {what} are not stored offline, "
             f"{reset}.")
    if res.error:
        _err(f"setup incomplete: {res.error}")
        _err(f"The root token was NOT revoked. If you stored it, finish or retry the setup and revoke it; otherwise "
             f"{reset}.")
        raise typer.Exit(1)
    typer.echo("Root token revoked; the controller now uses AppRole with a 1h token TTL.")
    if shown is False:
        raise typer.Exit(1)


@app.command("unseal")
def unseal_cmd(from_keyring: bool = typer.Option(
        False, "--from-keyring", help="Use only the unseal key(s) in the OS keyring; fail instead of prompting")) -> None:
    """Unseal OpenBao after a restart: from the OS keyring when the key is stored there (ADR-0123), else prompted."""
    from aeos.core.redaction import register_secret
    from aeos.secrets import unseal
    from aeos.secrets.openbao import OpenBaoClient, OpenBaoError
    from aeos.secrets.provider import reset_provider_cache

    settings = _settings()
    out = unseal.unseal_from_keyring(settings, trigger="cli")
    if out.status == unseal.UNREACHABLE:
        _err(f"unseal failed: {out.detail}")
        raise typer.Exit(1)
    if out.status == unseal.NOT_INITIALIZED:
        _err("OpenBao is not initialised. Run: aeos vault init")
        raise typer.Exit(1)
    if out.status == unseal.ALREADY_UNSEALED:
        typer.echo("Already unsealed.")
        return
    if out.status == unseal.UNSEALED:
        reset_provider_cache()
        typer.echo(f"Unsealed with the key from the {unseal.keyring_location(settings)}.")
        return
    if out.status == unseal.FAILED:
        _err(f"unseal with the key from the OS keyring failed: {out.detail}. If the stored key is wrong, store the "
             "right one: aeos vault import-unseal-key --replace")
        raise typer.Exit(1)
    if from_keyring:
        _err("no unseal keys stored in the OS keyring" if out.status == unseal.NO_KEY_IN_KEYRING
             else f"the unseal key(s) from the OS keyring were not enough ({out.status}: {out.detail})".rstrip())
        raise typer.Exit(1)
    st = out.seal or {}
    c = OpenBaoClient(settings.openbao_addr)
    applied = 0
    try:
        need = max(int(st.get("t") or 1) - int(st.get("progress") or 0), 1)
        for i in range(need):
            key = getpass.getpass(f"Unseal key {i + 1}/{need} (hidden): ")
            register_secret(key)
            st = c.unseal(key)
            applied += 1
            if not st.get("sealed"):
                break
    except OpenBaoError as exc:
        _err(f"unseal failed: {exc}")
        unseal.audit("openbao.unseal", "FAILED", {"source": "prompt", "trigger": "cli", "keys_applied": applied},
                     prepare_db=True)
        raise typer.Exit(1) from exc
    finally:
        c.close()
    reset_provider_cache()
    unseal.audit("openbao.unseal", "FAILED" if st.get("sealed") else "SUCCESS",
                 {"source": "prompt", "trigger": "cli", "keys_applied": applied}, prepare_db=True)
    typer.echo("Unsealed." if not st.get("sealed") else "Still sealed - more keys needed.")


# an unseal key share is one word of base64 (keys_base64) or hex (keys); anything else is a typo or a paste accident
_UNSEAL_KEY_SHAPE = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")
# an AppRole role_id / secret_id as OpenBao issues them (UUIDs): one word of letters, digits and '-'
_APPROLE_ID_SHAPE = re.compile(r"^[A-Za-z0-9-]{8,128}$")


def _hidden_twice(label: str, repeat: str, shape: re.Pattern[str], shape_hint: str) -> str:
    """One value from a hidden prompt, entered twice; Exit(1) with 'Nothing stored.' on a bad entry."""
    from aeos.core.redaction import register_secret

    value = getpass.getpass(f"{label} (hidden): ").strip()
    register_secret(value)
    if not shape.match(value):
        _err(f"That is not {shape_hint}. Nothing stored.")
        raise typer.Exit(1)
    again = getpass.getpass(f"{repeat} (hidden): ").strip()
    if again != value:
        _err("The two entries differ. Nothing stored.")
        raise typer.Exit(1)
    return value


@app.command("import-unseal-key")
def import_unseal_key_cmd(
    shares: int = typer.Option(1, "--shares", help="How many unseal key shares to enter (enter all of them, so a "
                                                  "recovery copy is complete)"),
    replace: bool = typer.Option(False, "--replace", help="Replace the share(s) (and with --with-approle the AppRole "
                                                         "credentials) already stored in the OS keyring"),
    with_approle: bool = typer.Option(
        False, "--with-approle", help="Also restore the AppRole role_id and secret_id from your recovery copy (hidden "
                                      "prompts), after the OS keyring was lost"),
) -> None:
    """Store an existing unseal key in the OS keyring from a hidden prompt (one-time migration, ADR-0123)."""
    from aeos.core.errors import AeosError
    from aeos.secrets import unseal
    from aeos.secrets.provider import UNSEAL_KEYRING_RISK, bootstrap_store

    settings = _settings()
    store = bootstrap_store(settings)
    location = unseal.keyring_location(settings)
    if not 1 <= shares <= 10:
        _err("--shares must be between 1 and 10. Nothing stored.")
        raise typer.Exit(2)
    ok, backend = store.available()
    if not ok:
        _err(f"OS keyring unavailable ({backend}). Nothing stored.")
        raise typer.Exit(1)
    try:
        existing = store.unseal_key_count()
        approle_existing = with_approle and store.approle() is not None
    except AeosError as exc:
        _err(f"{exc.message}. Nothing stored.")
        raise typer.Exit(1) from exc
    if existing and not replace:
        _err(f"{existing} unseal key share(s) are already stored in the {location}; pass --replace to overwrite "
             "them. Nothing stored.")
        raise typer.Exit(1)
    if approle_existing and not replace:
        _err(f"AppRole credentials are already stored in the {location}; pass --replace to overwrite them. "
             "Nothing stored.")
        raise typer.Exit(1)
    keys: list[str] = []
    approle: tuple[str, str] | None = None
    try:
        for i in range(1, shares + 1):
            key = _hidden_twice(f"Unseal key {i}/{shares}", f"Repeat unseal key {i}/{shares}", _UNSEAL_KEY_SHAPE,
                                "an unseal key share (one word of at least 16 base64 or hex characters)")
            if key in keys:
                _err("This share was entered already. Nothing stored.")
                raise typer.Exit(1)
            keys.append(key)
        if with_approle:
            hint = "an AppRole {} (one word of 8 to 128 letters, digits or '-', as in your recovery copy)"
            role_id = _hidden_twice("AppRole role_id", "Repeat AppRole role_id", _APPROLE_ID_SHAPE,
                                    hint.format("role_id"))
            secret_id = _hidden_twice("AppRole secret_id", "Repeat AppRole secret_id", _APPROLE_ID_SHAPE,
                                      hint.format("secret_id"))
            if role_id == secret_id or role_id in keys:
                _err("The role_id and secret_id must be two different values from the recovery copy. Nothing stored.")
                raise typer.Exit(1)
            approle = (role_id, secret_id)
    except (EOFError, KeyboardInterrupt) as exc:
        _err("Input ended. Nothing stored.")
        raise typer.Exit(1) from exc
    details: dict = {"unseal_key_shares": len(keys)}
    if with_approle:
        details["approle_restored"] = True
    try:
        store.set_unseal_keys(keys)
        if approle:
            store.set_approle(*approle)
    except AeosError as exc:
        _err(f"{exc.message}. Run the command again.")
        unseal.audit("openbao.unseal_key_import", "FAILED", details, prepare_db=True)
        raise typer.Exit(1) from exc
    unseal.audit("openbao.unseal_key_import", "SUCCESS", {**details, "replaced": bool(existing or approle_existing)},
                 prepare_db=True)
    typer.echo(f"Stored {len(keys)} unseal key share(s){' and the AppRole credentials' if approle else ''} in the "
               f"{location}.")
    typer.echo("The key is checked the next time OpenBao is sealed (aeos serve or aeos vault unseal).")
    if approle:
        typer.echo("Prove the AppRole login once OpenBao is unsealed: aeos vault status --verify")
    if unseal.recovery_export_supported():
        typer.echo("Next: aeos vault export-recovery <file outside every repository>, then move that file offline.")
    else:
        typer.echo("Keep your offline copy of the key(s): aeos vault export-recovery is refused on Windows (owner-only "
                   "ACL NOT-PROVEN).")
    typer.secho(UNSEAL_KEYRING_RISK, fg="yellow")


@app.command("export-recovery")
def export_recovery_cmd(path: str = typer.Argument(
        ..., help="New file outside every git repository and outside the AEOS data dir; created owner-only (0600), "
                  "never overwritten")) -> None:
    """Write the unseal key share(s) and AppRole credentials from the OS keyring to a new owner-only file."""
    from aeos.core.errors import AeosError
    from aeos.secrets.unseal import RecoveryRefused, write_recovery_file

    try:
        target = write_recovery_file(path, _settings())
    except RecoveryRefused as exc:
        _err(exc.message)
        raise typer.Exit(1) from exc
    except AeosError as exc:
        _err(f"{exc.message}. Nothing was written.")
        raise typer.Exit(1) from exc
    except OSError as exc:
        _err(f"cannot write the recovery copy ({type(exc).__name__}). Nothing was kept.")
        raise typer.Exit(1) from exc
    typer.echo(str(target))
