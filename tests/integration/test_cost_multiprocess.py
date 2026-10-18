"""Budget admission across OS processes (master §14 "Budget/reservation diperbarui atomik", §50
"Budget DB/gateway tidak sehat -> admission ditolak bila enforcement tidak dapat dipercaya").

The thread test in test_cost_service.py shares one engine; here separate Python processes (each
with its own SQLite connection pool, like a second ``aeos`` process) race for one $1.00 budget.
The admission lock must serialise them so no more than three $0.30 holds are ever admitted.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from aeos.core.db import session_factory
from aeos.core.paths import repo_root
from aeos.core.timeutil import utcnow
from aeos.cost import service
from aeos.models.cost import BudgetReservation
from aeos.models.routing import ModelRoute
from tests.unit.test_cost_fakes import install_fake_policy

CHILD = r"""
import sys, time
from pathlib import Path

go, idx = Path(sys.argv[1]), sys.argv[2]
from aeos.core import config
from aeos.core.db import init_engine, session_factory
from aeos.core.errors import Blocked
from aeos.cost import facade

init_engine(config.get_settings())
deadline = time.monotonic() + 60
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.005)
s = session_factory()()
try:
    facade.reserve(s, facade.ReservationRequest(run_id=f"proc-{idx}", project_id=f"pp{idx}", domain="PERSONAL",
                                                route_key="paid-pro", est_usd=0.3, est_tokens=10))
    s.commit()
    print("RESULT ok")
except Blocked as exc:
    s.rollback()
    print("RESULT " + exc.code)
finally:
    s.close()
"""

PROCESSES = 6


def test_separate_processes_never_exceed_the_budget(settings, monkeypatch, tmp_path: Path):
    install_fake_policy(monkeypatch)
    s = session_factory()()
    s.add(ModelRoute(route_key="paid-pro", pool="WORKER", provider="openrouter", model_id="pro", is_free="NO",
                     pricing={"input_per_mtok": 1.0}, pricing_checked_at=utcnow()))
    s.commit()
    service.set_budget(s, actor_user_id="owner", scope_type="GLOBAL", scope_id="*", period="MONTH", limit_usd=1.0,
                       rationale="owner raises worker budget for the race test")
    s.commit()
    s.close()

    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    go = tmp_path / "go"
    env = dict(os.environ)  # carries AEOS_DATA_DIR/AEOS_CONFIG of this test's isolated data dir
    env["PYTHONPATH"] = str(repo_root() / "backend") + os.pathsep + env.get("PYTHONPATH", "")
    procs = [subprocess.Popen([sys.executable, str(script), str(go), str(i)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path))
             for i in range(PROCESSES)]
    time.sleep(2.0)  # let every child import and open its engine before the start signal
    go.write_text("go", encoding="utf-8")
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        line = next((ln for ln in out.splitlines() if ln.startswith("RESULT ")), None)
        assert line is not None, err[-2000:]
        results.append(line.split(" ", 1)[1])

    assert results.count("ok") <= 3, results  # 3 x $0.30 fit under $1.00; a 4th never does
    assert set(results) <= {"ok", "BUDGET_EXCEEDED", "BUDGET_LOCK_UNAVAILABLE"}, results
    if "BUDGET_LOCK_UNAVAILABLE" not in results:  # busy_timeout waited instead of failing closed
        assert results.count("ok") == 3, results
    s = session_factory()()
    held = [Decimal(r.reserved_usd) for r in s.scalars(select(BudgetReservation)
                                                         .where(BudgetReservation.status == "RESERVED"))]
    s.close()
    assert len(held) == results.count("ok") and sum(held, Decimal(0)) <= Decimal("1.0")
