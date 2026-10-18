"""Deterministic fake probe runner shared by inventory/doctor tests (no real binaries, no network)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from aeos.inventory.probes import HttpResult, ProbeRefused, RunResult, is_loopback_url, set_runner_override

FAKE_BIN = "/opt/fake/bin"


def ok(stdout: str = "", stderr: str = "") -> RunResult:
    return RunResult(0, stdout, stderr, None, 1)


def fail(code: int = 1, stderr: str = "error") -> RunResult:
    return RunResult(code, "", stderr, None, 1)


class FakeRunner:
    def __init__(
        self, *, binaries: Mapping[str, str] | None = None,
        outputs: Mapping[Any, RunResult | Callable[[list[str]], RunResult] | Exception] | None = None,
        http: Mapping[str, HttpResult | Exception] | None = None, env: Mapping[str, str] | None = None,
        stats: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        self.binaries = {k: v for k, v in (binaries or {}).items()}
        self.outputs = dict(outputs or {})
        self.http = dict(http or {})
        self._env = dict(env or {})
        self.stats = dict(stats or {})
        self.calls: list[dict[str, Any]] = []
        self.which_calls: list[str] = []
        self.http_calls: list[dict[str, Any]] = []

    def add_binary(self, name: str, version_out: str | None = None) -> str:
        path = f"{FAKE_BIN}/{name}"
        self.binaries[name] = path
        if version_out is not None:
            self.outputs[name] = ok(version_out)
        return path

    def _name_for(self, path: str) -> str:
        for name, p in self.binaries.items():
            if p == path:
                return name
        return Path(path).stem.lower()

    def which(self, name: str) -> str | None:
        self.which_calls.append(name)
        return self.binaries.get(name)

    def run(self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None,
            cwd: Path | None = None) -> RunResult:
        argv = list(argv)
        self.calls.append({"argv": argv, "env": dict(env or {}), "cwd": cwd, "timeout": timeout})
        name = self._name_for(argv[0])
        r = self.outputs.get((name, tuple(argv[1:]))) or self.outputs.get(name)
        if callable(r) and not isinstance(r, RunResult):
            r = r(argv)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, RunResult) else RunResult(1, "", "not configured", None, 1)

    def http_get(self, url: str, *, timeout: float, headers: Mapping[str, str] | None = None,
                 allow_remote: bool = False) -> HttpResult:
        if not allow_remote and not is_loopback_url(url):
            raise ProbeRefused(url)
        self.http_calls.append({"url": url, "headers": dict(headers or {}), "allow_remote": allow_remote})
        r = self.http.get(url)
        if isinstance(r, Exception):
            raise r
        return r if r is not None else HttpResult(None, error="UNREACHABLE")

    def environ(self) -> Mapping[str, str]:
        return dict(self._env)

    def stat(self, path: str) -> dict[str, Any] | None:
        if str(path) in self.stats:
            return self.stats[str(path)]
        if str(path) in self.binaries.values():
            return {"size": 100, "mtime_ns": 1}
        p = Path(path)
        if p.exists():
            st = p.stat()
            return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
        return None


def git_handler(*, remote: str | None = "https://github.com/fadhlillah-dli/dly-aeos.git", head: str = "a" * 40,
                branch: str = "main", porcelain: str = "") -> Callable[[list[str]], RunResult]:
    def handle(argv: list[str]) -> RunResult:
        args = argv[3:] if len(argv) > 2 and argv[1] == "-C" else argv[1:]
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return ok("/repo\n")
        if args == ["rev-parse", "HEAD"]:
            return ok(head + "\n")
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return ok(branch + "\n")
        if args[:2] == ["rev-parse", "--git-path"]:
            return ok(".git/hooks\n")
        if args[:2] == ["status", "--porcelain"]:
            return ok(porcelain)
        if args[:3] == ["remote", "get-url", "origin"]:
            return ok(remote + "\n") if remote else fail(2, "No such remote")
        if args[:2] == ["config", "--get"]:
            return fail(1, "")
        if args == ["--version"]:
            return ok("git version 2.45.1\n")
        return fail(1, "unsupported")

    return handle


def base_runner(**kwargs: Any) -> FakeRunner:
    """A host with git + python only (nothing optional installed)."""
    r = FakeRunner(**kwargs)
    r.add_binary("git")
    r.outputs["git"] = git_handler()
    return r


@pytest.fixture
def fake_runner():
    r = base_runner()
    set_runner_override(r)
    yield r
    set_runner_override(None)


def test_fake_runner_refuses_remote_without_opt_in():
    r = FakeRunner()
    with pytest.raises(ProbeRefused):
        r.http_get("https://example.invalid/", timeout=1)
    assert r.http_get("http://127.0.0.1:1/x", timeout=1).error == "UNREACHABLE"


def test_git_handler_answers_read_only_queries():
    r = base_runner()
    assert r.run([r.binaries["git"], "-C", "/x", "rev-parse", "HEAD"], timeout=1).stdout.strip() == "a" * 40
    assert "dly-aeos" in r.run([r.binaries["git"], "-C", "/x", "remote", "get-url", "origin"], timeout=1).stdout
