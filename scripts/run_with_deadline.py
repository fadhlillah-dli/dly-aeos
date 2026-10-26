"""Run a command with a deadline, portably (macOS has no `timeout`; scripts/run_tests.sh uses this).

    python scripts/run_with_deadline.py <minutes> -- <command> [args...]

The command's output goes straight through. Exit code: the command's own, or 124 (as timeout(1)) when the deadline
passed and the command and its children were stopped. A command that cannot start exits 127. Stdlib only."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
from collections.abc import Sequence

TIMED_OUT = 124
NOT_FOUND = 127
USAGE = 2
GRACE_SECONDS = 5.0


def _popen(argv: Sequence[str]) -> subprocess.Popen[bytes]:
    if sys.platform == "win32":
        return subprocess.Popen(list(argv), creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return subprocess.Popen(list(argv), start_new_session=True)  # own process group: the whole tree can be stopped


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Stop the command and its children: polite first, forceful after a grace period."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        proc.wait(GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        if sys.platform != "win32":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        proc.kill()
        proc.wait()


def run(minutes: float, argv: Sequence[str]) -> int:
    try:
        proc = _popen(argv)
    except OSError as exc:
        print(f"run_with_deadline: cannot start {argv[0]!r}: {exc}".encode("ascii", "replace").decode("ascii"),
              file=sys.stderr)
        return NOT_FOUND
    try:
        return proc.wait(timeout=minutes * 60)
    except subprocess.TimeoutExpired:
        _stop(proc)
        print(f"run_with_deadline: deadline of {minutes:g} minute(s) passed; the command was stopped", file=sys.stderr)
        return TIMED_OUT
    except KeyboardInterrupt:
        _stop(proc)
        return 130


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--":
        print("usage: run_with_deadline.py <minutes> -- <command> [args...]", file=sys.stderr)
        return USAGE
    try:
        minutes = float(args[0])
    except ValueError:
        minutes = -1.0
    if not (math.isfinite(minutes) and minutes > 0):
        print(f"run_with_deadline: minutes must be a positive number, got {args[0]!r}".encode("ascii", "replace")
              .decode("ascii"), file=sys.stderr)
        return USAGE
    return run(minutes, args[2:])


if __name__ == "__main__":
    sys.exit(main())
