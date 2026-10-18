"""Parser isolation (§05 step 5 "mengisolasi parser", §25 parser isolation): timeout, caps, clean env."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import psutil
import pytest

from aeos.intake import parse_worker, parser
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import create_project


def _cmd(code: str):
    def build(src: Path, dst: Path, kind: str, max_chars: int) -> list[str]:
        return [sys.executable, "-I", "-c", code, str(src), str(dst)]

    return build


def test_parser_timeout_kills_process(tmp_path: Path, monkeypatch):
    src = tmp_path / "in.md"
    src.write_text("# x", encoding="utf-8")
    pid_file = tmp_path / "pid"
    monkeypatch.setattr(parser, "worker_command", _cmd(
        f"import os,time; open(r'{pid_file}','w').write(str(os.getpid())); time.sleep(60)"))
    started = time.monotonic()
    out = parser.run_parser(src, "markdown", timeout=2)
    assert time.monotonic() - started < 20
    assert out.status == "FAILED" and out.error_code == "PARSE_TIMEOUT"
    deadline = time.monotonic() + 10
    pid = int(pid_file.read_text()) if pid_file.exists() else None
    while pid and psutil.pid_exists(pid) and time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.1)
    if pid:
        alive = psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        assert not alive, "parser process tree must be killed on timeout"


def test_upload_survives_parser_timeout(personal_client: AuthedClient, monkeypatch):
    monkeypatch.setattr(parser, "worker_command", _cmd("import time; time.sleep(60)"))
    monkeypatch.setattr(parser, "PARSE_TIMEOUT_SECONDS", 1.5)
    p = create_project(personal_client)
    r = personal_client.post(f"/api/projects/{p['id']}/attachments", files={"file": ("slow.md", b"# slow", "text/markdown")})
    assert r.status_code == 200, r.text
    att = r.json()["attachment"]
    assert att["parse_status"] == "FAILED" and "PARSE_TIMEOUT" in att["parse_error"]
    assert att["scan_status"] == "CLEAN"  # text was still scanned in-process before parsing
    t = personal_client.get(f"/api/attachments/{att['id']}/text")
    assert t.status_code == 409


def test_output_size_cap(tmp_path: Path, monkeypatch):
    src = tmp_path / "in.txt"
    src.write_text("x", encoding="utf-8")
    monkeypatch.setattr(parser, "worker_command", _cmd(
        "import sys,json; json.dump({'status':'PARSED','text':'y'*200000,'meta':{}}, open(sys.argv[2],'w'))"))
    out = parser.run_parser(src, "text", max_output_bytes=10_000)
    assert out.status == "FAILED" and out.error_code == "PARSE_OUTPUT_TOO_LARGE"


@pytest.mark.parametrize("code,error_code", [
    ("import sys; open(sys.argv[2],'w').write('not json')", "PARSE_BAD_OUTPUT"),
    ("import sys,json; json.dump({'status':'OWNED'}, open(sys.argv[2],'w'))", "PARSE_BAD_OUTPUT"),
    ("import sys; sys.exit(3)", "PARSE_NO_OUTPUT"),
])
def test_bad_worker_output(tmp_path: Path, monkeypatch, code: str, error_code: str):
    src = tmp_path / "in.txt"
    src.write_text("x", encoding="utf-8")
    monkeypatch.setattr(parser, "worker_command", _cmd(code))
    out = parser.run_parser(src, "text")
    assert out.status == "FAILED" and out.error_code == error_code


def test_worker_env_has_no_secrets_or_aeos_vars(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AEOS_SUPER_SECRET", "should-not-leak")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-shouldnotleakshouldnotleak")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    src = tmp_path / "in.txt"
    src.write_text("x", encoding="utf-8")
    monkeypatch.setattr(parser, "worker_command", _cmd(
        "import sys,os,json; json.dump({'status':'PARSED','text':json.dumps({'env':sorted(os.environ),"
        "'cwd':os.getcwd(),'path':sys.path}),'meta':{}}, open(sys.argv[2],'w'))"))
    out = parser.run_parser(src, "text")
    info = json.loads(out.text)
    assert "AEOS_SUPER_SECRET" not in info["env"] and "OPENROUTER_API_KEY" not in info["env"]
    assert "PYTHONPATH" not in info["env"]
    assert str(tmp_path) not in info["path"]
    assert "aeos-parse-" in info["cwd"]


def test_real_worker_runs_isolated_without_aeos(tmp_path: Path):
    """The worker must not import aeos (it runs with -I, outside the app's sys.path)."""
    src = tmp_path / "a.json"
    src.write_text('{"a": 1}', encoding="utf-8")
    out = parser.run_parser(src, "json")
    assert out.status == "PARSED" and out.meta["valid_json"] is True
    assert "import aeos" not in Path(parse_worker.__file__).read_text(encoding="utf-8")
    assert "from aeos" not in Path(parse_worker.__file__).read_text(encoding="utf-8")


def test_worker_reports_invalid_structured_text(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert parse_worker.parse(bad, "json", 1000)["meta"]["valid_json"] is False
    yml = tmp_path / "x.yaml"
    yml.write_text("a: [1, 2\n", encoding="utf-8")
    assert parse_worker.parse(yml, "yaml", 1000)["meta"]["valid_yaml"] is False
    tagged = tmp_path / "evil.yaml"
    tagged.write_text("!!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8")
    res = parse_worker.parse(tagged, "yaml", 1000)  # safe_load: python tags are never constructed
    assert res["meta"]["valid_yaml"] is False
    csvf = tmp_path / "x.csv"
    csvf.write_text("a,b\n1,2\n", encoding="utf-8")
    assert parse_worker.parse(csvf, "csv", 1000)["meta"]["rows"] == 2
    assert parse_worker.parse(csvf, "csv", 3)["meta"]["truncated"] is True


def test_worker_main_usage(tmp_path: Path):
    """In-process call with limits=False (rlimits are only for the dedicated worker process)."""
    assert parse_worker.main([], limits=False) == 2
    src = tmp_path / "a.md"
    src.write_text("# a", encoding="utf-8")
    dst = tmp_path / "out.json"
    assert parse_worker.main([str(src), str(dst), "markdown", "100"], limits=False) == 0
    assert json.loads(dst.read_text(encoding="utf-8"))["status"] == "PARSED"
    assert parse_worker.main([str(tmp_path / "missing"), str(dst), "markdown", "100"], limits=False) == 0
    assert json.loads(dst.read_text(encoding="utf-8"))["status"] == "FAILED"
