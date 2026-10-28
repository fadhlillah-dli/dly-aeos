"""A BUDGET decision's ``auth_scope.routes`` covers exact route keys only (review finding 2026-10-28, LESSONS L27).

``aeos policy decide --scope routes=a,b`` stored the scope as text, and ``route_key in routes`` on text is a substring
test: a BUDGET naming ``opencode-go/kimi-k3.1-pro`` also covered ``opencode-go/kimi-k3``, and one naming an OpenRouter
``:free`` key covered its paid twin. The Go pool-move guard read only list scopes, so a text-scoped BUDGET did not
block a move. ``scope_route_keys`` is the one reader: a list stays a list, text is split on ',', exact keys only.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aeos.core.errors import ValidationFailed
from aeos.models.decisions import Decision
from aeos.models.routing import ModelRoute
from aeos.policy.cli import _scope
from aeos.routing import seed, service
from aeos.routing.facade import scope_route_keys
from tests.conftest import ADMIN_PW, create_admin
from tests.unit.test_routing_fakes import human_decision

K31 = "opencode-go/kimi-k3.1-pro"
K3 = "opencode-go/kimi-k3"
FREE = "openrouter/acme/model-x:free"
PAID = "openrouter/acme/model-x"
runner = CliRunner()


def _route(key: str) -> ModelRoute:
    return ModelRoute(route_key=key, pool="WORKER", provider=key.split("/", 1)[0], model_id=key)


def _budget(db, routes: object) -> Decision:
    return human_decision(db, kind="BUDGET", target_type="model_budget", target_id="plan", auth_scope={"routes": routes})


# ---------------------------------------------------------------------------------------------- the one reader


@pytest.mark.parametrize("scope,keys", [
    ({"routes": [K31, " " + K3 + " "]}, [K31, K3]),
    ({"routes": [K31, 7, None, "", "  "]}, [K31]),
    ({"routes": f"{K31}, {K3} ,,"}, [K31, K3]),
    ({"routes": K31}, [K31]),
    ({"routes": ""}, []),
    ({"routes": {"a": 1}}, []),
    ({"routes": 3}, []),
    ({}, []),
    (None, []),
    ("routes", []),
])
def test_scope_route_keys_normalises_every_shape(scope, keys):
    assert scope_route_keys(scope) == keys == service.scope_route_keys(scope)


# ---------------------------------------------------------------------------------------------- budget coverage


def test_bite_a_text_scope_does_not_cover_a_route_whose_key_is_a_substring(db):
    d = _budget(db, K31)  # what 'aeos policy decide --scope routes=opencode-go/kimi-k3.1-pro' stored on main
    assert service.budget_decision_valid(db, d.id, _route(K3)) == (False, "budget decision does not cover this route")
    assert service.budget_decision_valid(db, d.id, _route(K31)) == (True, "")  # near miss: the exact key


def test_bite_a_free_route_in_the_scope_does_not_cover_its_paid_twin(db):
    for scope in (FREE, [FREE], f"{FREE},{K31}"):
        d = _budget(db, scope)
        assert service.budget_decision_valid(db, d.id, _route(PAID)) == (
            False, "budget decision does not cover this route"), scope
        assert service.budget_decision_valid(db, d.id, _route(FREE)) == (True, ""), scope


def test_a_list_scope_still_covers_its_exact_keys_and_a_comma_text_covers_each(db):
    listed = _budget(db, [K31, PAID])
    text = _budget(db, f"{K31} , {PAID}")
    for d in (listed, text):
        assert service.budget_decision_valid(db, d.id, _route(K31)) == (True, "")
        assert service.budget_decision_valid(db, d.id, _route(PAID)) == (True, "")
        assert service.budget_decision_valid(db, d.id, _route(K3))[0] is False


def test_a_targeted_budget_still_covers_its_route(db):
    d = human_decision(db, kind="BUDGET", target_type="model_route", target_id=K3)
    assert service.budget_decision_valid(db, d.id, _route(K3)) == (True, "")
    assert service.budget_decision_valid(db, d.id, _route(K31))[0] is False


# ---------------------------------------------------------------------------------------------- Go pool-move guard


def test_bite_the_pool_move_guard_sees_a_text_scoped_budget_naming_the_route(db):
    seed.run(db)
    db.commit()
    assert service.get_route(db, K31).pool == "WATCHLIST"
    d = _budget(db, f"opencode-go/glm-5.3, {K31}")
    db.commit()
    assert service.route_decision_ids(db, service.get_route(db, K31)) == [d.id]
    with pytest.raises(ValidationFailed) as ei:
        service.set_route_pool(db, K31, pool="WORKER", rationale="owner decision R31: best first")
    assert ei.value.code == "ROUTE_POOL_MOVE_HAS_DECISIONS" and "already has 1 decision(s)" in ei.value.message


def test_the_pool_move_guard_ignores_a_text_budget_naming_a_longer_key(db):
    seed.run(db)
    db.commit()
    _budget(db, K31 + "-next")  # near miss: contains the route key, names another route
    db.commit()
    assert service.route_decision_ids(db, service.get_route(db, K31)) == []
    route, _ = service.set_route_pool(db, K31, pool="WORKER", rationale="owner decision R31: best first")
    assert route.pool == "WORKER"


# ---------------------------------------------------------------------------------------------- CLI stores a list


def test_cli_scope_stores_routes_as_a_list_and_other_keys_as_text():
    assert _scope([f"routes= {K31} , {K3} ,", "note=a,b"]) == {"routes": [K31, K3], "note": "a,b"}
    assert _scope([f"routes={K31}"]) == {"routes": [K31]}


def test_cli_records_a_budget_whose_scope_is_a_list(settings, db):
    from aeos.cli import app

    create_admin(db)
    r = runner.invoke(app, ["policy", "decide", "--kind", "BUDGET", "--as", "owner", "--domain", "PERSONAL",
                            "--target-type", "model_budget", "--target-id", "opencode-go", "--days", "30",
                            "--scope", f"routes={K31},{PAID}",
                            "--rationale", "plan fee for two named routes"], input=ADMIN_PW + "\n")
    assert r.exit_code == 0, r.output
    db.expire_all()
    d = db.query(Decision).filter_by(kind="BUDGET").one()
    assert d.auth_scope == {"routes": [K31, PAID]}
    assert service.budget_decision_valid(db, d.id, _route(K3))[0] is False
    assert service.budget_decision_valid(db, d.id, _route(K31)) == (True, "")
