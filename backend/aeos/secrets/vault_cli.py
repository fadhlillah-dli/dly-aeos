"""`aeos vault ...` - local OpenBao lifecycle (§07 order 3, §26). ASCII output only.

install  shows the plan (official release, asset, checksum, target, privilege, cost, rollback) and
         needs --yes or an interactive confirmation; user level only, never system-wide.
start    generated loopback-only config, detached server, pid recorded.
stop     stops the recorded server process.
status   health/sealed/initialised, AppRole presence, interim mode.
init     init + unseal + KV v2 + policies + AppRole; shows the unseal key(s) ONCE, on a temporary terminal
         screen when interactive; the root token only when setup is incomplete (otherwise it is revoked).
unseal   applies unseal keys (prompted, or from the OS keyring when the owner stored them there).
"""

from __future__ import annotations

import getpass
import os
import sys
import time

import httpx
import typer

app = typer.Typer(help="Local OpenBao vault (install/start/stop/status/init/unseal).", no_args_is_help=True)

# Alternate screen buffer (xterm/VT): what is written there never reaches the scrollback, so the unseal
# material cannot be copied later together with the rest of the terminal output.
ALT_SCREEN_ON = "\x1b[?1049h\x1b[H\x1b[2J"
ALT_SCREEN_OFF = "\x1b[2J\x1b[?1049l"
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


def _show_on_temporary_screen(lines: list[str]) -> bool | None:
    """Show `lines` on the alternate screen until the owner types STORED_WORD.

    None: no VT terminal, nothing shown. True: confirmed. False: the screen closed before confirmation
    (Ctrl+C or end of input); the material is gone from the screen either way."""
    if not _vt_terminal():
        return None
    typer.echo(ALT_SCREEN_ON, nl=False, color=True)  # color=True: never strip the sequence
    try:
        for line in lines:
            typer.echo(line)
        while True:
            answer = typer.prompt(f"Type '{STORED_WORD}' after you have stored the key(s) offline",
                                  default="", show_default=False)
            if answer.strip().lower() == STORED_WORD:
                return True
    except (typer.Abort, EOFError, KeyboardInterrupt):
        return False
    finally:
        typer.echo(ALT_SCREEN_OFF, nl=False, color=True)


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
        False, "--store-unseal-in-keyring",
        help="Also store the threshold unseal keys in the OS keyring (convenience; residual risk)"),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation"),
) -> None:
    """Initialise OpenBao for DLY AEOS (policies + AppRole) and revoke the root token afterwards."""
    from aeos.core.redaction import register_secret
    from aeos.secrets.openbao import OpenBaoError, OpenBaoUnavailable
    from aeos.secrets.provider import bootstrap_store, reset_provider_cache
    from aeos.secrets.setup import CONTROLLER_POLICIES, POLICY_NAMESPACES, run_init

    settings = _settings()
    typer.echo("This will initialise the local OpenBao at " + settings.openbao_addr)
    typer.echo(f"  key shares/threshold : {shares}/{threshold}")
    typer.echo(f"  policies             : {', '.join(POLICY_NAMESPACES)}")
    typer.echo(f"  approle              : aeos-controller -> {', '.join(CONTROLLER_POLICIES)} (office NOT bound)")
    typer.echo("  bootstrap credentials: OS keyring service " + bootstrap_store(settings).service)
    if not yes and not typer.confirm("Continue?", default=False):
        raise typer.Exit(1)
    try:
        res = run_init(settings.openbao_addr, shares=shares, threshold=threshold, bootstrap=bootstrap_store(settings),
                       store_unseal_in_keyring=store_unseal_in_keyring)
    except (OpenBaoError, ValueError) as exc:
        _err(f"init refused: {exc}")
        if isinstance(exc, OpenBaoUnavailable):
            from aeos.secrets import server

            _err("If `aeos vault status` now reports initialized=True, the keys were never received: the vault is "
                 f"unusable. Stop it (aeos vault stop), delete {server.server_dir(settings) / 'raft'} (no secrets were "
                 "stored yet) and run `aeos vault init` again.")
        raise typer.Exit(1) from exc
    for k in res.keys:
        register_secret(k)
    register_secret(res.root_token)
    banner = ["=" * 72,
              "SHOWN ONCE - store the unseal key(s) OFFLINE (password manager / paper). Never put them in the",
              "repo, a config file, a chat or an AI assistant, and never take a screenshot of them:"]
    banner += [f"  Unseal key {i}: {k}" for i, k in enumerate(res.keys, 1)]
    # a revoked root token is useless; it is shown only while setup is incomplete and the owner still needs it
    if not res.root_revoked:
        banner.append(f"  Root token  : {res.root_token}  (NOT revoked: setup is incomplete; revoke it when done)")
    banner += [f"  ({threshold} of {shares} keys are needed after every restart: aeos vault unseal)", "=" * 72]
    shown = _show_on_temporary_screen(banner)
    typer.echo("")
    if shown is None:
        for line in banner:
            typer.echo(line)
        typer.secho("Clear or close this terminal after storing the key(s); never copy this output.", fg="yellow")
    else:
        typer.echo("The unseal key(s) were shown on a temporary screen and are not in this terminal's scrollback.")
    for step in res.steps:
        typer.echo(f"  done: {step}")
    reset_provider_cache()
    if shown is False:
        from aeos.secrets import server

        _err("The key screen closed before you typed 'stored'. If the key(s) are not stored offline, reset the "
             f"still empty vault: aeos vault stop, delete {server.server_dir(settings) / 'raft'}, aeos vault start, "
             "aeos vault init.")
    if res.error:
        _err(f"setup incomplete: {res.error}")
        _err("The root token was NOT revoked so you can finish/retry; revoke it when done.")
        raise typer.Exit(1)
    typer.echo("Root token revoked; the controller now uses AppRole with a 1h token TTL.")
    if res.unseal_stored:
        typer.secho("Unseal keys are also in the OS keyring (your choice): anyone with your OS account can unseal.",
                    fg="yellow")
    if shown is False:
        raise typer.Exit(1)


@app.command("unseal")
def unseal_cmd(from_keyring: bool = typer.Option(False, "--from-keyring", help="Use unseal keys stored in the OS keyring")) -> None:
    """Unseal OpenBao after a restart."""
    from aeos.core.redaction import register_secret
    from aeos.secrets.openbao import OpenBaoClient, OpenBaoError
    from aeos.secrets.provider import bootstrap_store, reset_provider_cache

    settings = _settings()
    c = OpenBaoClient(settings.openbao_addr)
    try:
        st = c.seal_status()
        if not st.get("initialized"):
            _err("OpenBao is not initialised. Run: aeos vault init")
            raise typer.Exit(1)
        if not st.get("sealed"):
            typer.echo("Already unsealed.")
            return
        keys = bootstrap_store(settings).unseal_keys() if from_keyring else []
        if from_keyring and not keys:
            _err("no unseal keys stored in the OS keyring")
            raise typer.Exit(1)
        need = int(st.get("t") or 1) - int(st.get("progress") or 0)
        for i in range(need):
            key = keys[i] if i < len(keys) else getpass.getpass(f"Unseal key {i + 1}/{need} (hidden): ")
            register_secret(key)
            st = c.unseal(key)
            if not st.get("sealed"):
                break
    except OpenBaoError as exc:
        _err(f"unseal failed: {exc}")
        raise typer.Exit(1) from exc
    finally:
        c.close()
    reset_provider_cache()
    typer.echo("Unsealed." if not st.get("sealed") else "Still sealed - more keys needed.")
