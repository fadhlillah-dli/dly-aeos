"""§33 ops scenario "broker outage", routing side (the worker side is in test_worker_runtime_executor.py):
while the capability broker is unavailable, a route whose upstream credential only the broker can check is
never eligible (fail closed, no fallback to an unchecked credential path)."""

from __future__ import annotations

from aeos.models.routing import ModelRoute
from aeos.routing.service import credential_rejections
from tests.unit.test_operations_fakes import block_module


def test_route_needing_a_broker_credential_is_ineligible_while_the_broker_is_unavailable(db, monkeypatch,
                                                                                      worker_keys):
    zen = ModelRoute(route_key="z", pool="WORKER", provider="opencode-zen",
                     endpoint="https://opencode.ai/inference/openai/v1",  # S07 (ADR-0041 amendment 2026-10-25)
                     model_id="m", mode_params={"credential_ref": "workers/opencode-zen"})
    assert credential_rejections(zen, domain="PERSONAL") == []
    block_module(monkeypatch, "aeos.broker.facade")
    reasons = credential_rejections(zen, domain="PERSONAL")
    assert reasons and "broker unavailable" in reasons[0] and "fail closed" in reasons[0]
    # a loopback test endpoint without a credential needs no broker check (it is not a paid fallback path)
    loop = ModelRoute(route_key="l", pool="WORKER", provider="local-test", endpoint="http://127.0.0.1:9/v1",
                      model_id="m", mode_params={})
    assert credential_rejections(loop, domain="PERSONAL") == []
