"""End-to-end proof of the guarded write path, as an operator drives it.

The unit tests cover plan()/apply() in isolation. This file walks the exact
sequence the console performs — plan → inspect → apply → (confirm) → revert —
through the sidecar's authenticated dispatcher, and pins the three promises the
UI makes to the operator:

  1. nothing is written until apply (plan is a dry run),
  2. a stale plan is refused rather than silently clobbering someone else's edit,
  3. revert really restores the exact previous bytes.

None of these can pass by accident: each asserts on the router.yaml bytes on
disk before and after the HTTP-shaped call that is supposed to change them.
"""

from __future__ import annotations

import hashlib

import pytest
import yaml

from router.one_sidecar import SidecarApp
from router.service import RouterService

_TOKEN = "write-path-e2e-token"


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def console(tmp_path):
    """A sidecar over a real policy file, as the console talks to it."""
    policy = {
        "enabled": True,
        "classifier": {"model": "judge", "provider": "judge-rail"},
        "fail_safe": {"profile": "coder", "model": "safe", "provider": "safe-rail"},
        "blocklist": {"manual_ban": [], "fallback_chain": [], "auto_breaker": {"enabled": False}},
        "rules": [{"id": "hard-verbs", "when": {"verb_class": {"eq": "hard"}},
                   "then": {"profile": "coder", "model": "T4"}}],
        "default": {"action": "classify"},
        "tiers": {"T1": {"model": "tiny", "provider": "cheap"},
                  "T2": {"model": "small", "provider": "cheap"},
                  "T3": {"model": "mid", "provider": "strong-rail"},
                  "T4": {"model": "strong", "provider": "strong-rail"}},
        # A field no UI form knows about: it must survive every round trip.
        "operator_note": "do not lose me",
    }
    config = tmp_path / "router.yaml"
    config.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    token = tmp_path / "token"
    token.write_text(_TOKEN, encoding="utf-8")
    app = SidecarApp(RouterService(config), token_path=lambda: token)
    return app, config


def _auth():
    return {"X-Hermes-Sidecar-Token": _TOKEN}


def _plan(app, changes):
    status, body = app.dispatch("POST", "/plan", _auth(), body={"policy": changes})
    assert status == 200
    return body


def test_plan_is_a_dry_run_and_apply_is_the_only_writer(console):
    app, config = console
    before = _digest(config)

    plan = _plan(app, {"tiers": {"T4": {"model": "stronger", "provider": "strong-rail"}}})
    assert plan["valid"] is True
    assert plan["diff"], "the operator is shown a real diff before committing"
    assert _digest(config) == before, "planning must not touch the file"

    status, applied = app.dispatch("POST", "/apply", _auth(),
                                   body={"plan": plan, "policy": plan["policy"]})
    assert status == 200 and applied["ok"] is True
    assert _digest(config) != before, "apply is what writes"
    assert yaml.safe_load(config.read_text())["tiers"]["T4"]["model"] == "stronger"


def test_a_stale_plan_is_refused_instead_of_clobbering(console):
    app, config = console
    stale = _plan(app, {"tiers": {"T4": {"model": "from-operator-a", "provider": "strong-rail"}}})

    # Someone else commits first.
    fresh = _plan(app, {"tiers": {"T4": {"model": "from-operator-b", "provider": "strong-rail"}}})
    assert app.dispatch("POST", "/apply", _auth(),
                        body={"plan": fresh, "policy": fresh["policy"]})[0] == 200
    after_b = _digest(config)

    # Operator A now applies a plan computed against the old bytes.
    status, conflict = app.dispatch("POST", "/apply", _auth(),
                                    body={"plan": stale, "policy": stale["policy"]})
    assert status == 409, "a drifted base_hash must be a conflict, not a write"
    assert conflict["conflict"] is True
    assert _digest(config) == after_b, "operator B's edit survives untouched"
    assert yaml.safe_load(config.read_text())["tiers"]["T4"]["model"] == "from-operator-b"


def test_revert_restores_the_exact_previous_bytes(console):
    app, config = console
    original = config.read_bytes()

    plan = _plan(app, {"default": {"action": "T1"}})
    app.dispatch("POST", "/apply", _auth(), body={"plan": plan, "policy": plan["policy"]})
    assert config.read_bytes() != original

    status, reverted = app.dispatch("POST", "/apply/revert", _auth(), body={})
    assert status == 200 and reverted["reverted"] is True
    assert config.read_bytes() == original, "revert is byte-exact, not a re-serialisation"


def test_an_invalid_policy_is_refused_before_it_reaches_disk(console):
    app, config = console
    before = _digest(config)

    # A rule pointing at a tier that does not exist would break routing.
    plan = _plan(app, {"rules": [{"id": "bad", "when": {"verb_class": {"eq": "hard"}},
                                  "then": {"model": "T9"}}]})
    assert plan["valid"] is False and plan["errors"]

    status, refused = app.dispatch("POST", "/apply", _auth(),
                                   body={"plan": plan, "policy": plan["policy"]})
    assert status == 400 and refused["ok"] is False
    assert _digest(config) == before, "a lint failure never reaches the file"


def test_editing_one_field_preserves_everything_the_ui_does_not_know(console):
    app, config = console

    plan = _plan(app, {"tiers": {"T4": {"model": "stronger", "provider": "strong-rail"}}})
    app.dispatch("POST", "/apply", _auth(), body={"plan": plan, "policy": plan["policy"]})

    written = yaml.safe_load(config.read_text())
    assert written["operator_note"] == "do not lose me", \
        "an unknown top-level field must not be dropped by a node edit"
    assert written["classifier"]["model"] == "judge", "untouched sections stay intact"
    assert written["rules"][0]["id"] == "hard-verbs"


def test_writes_require_the_token(console):
    app, _config = console
    assert app.dispatch("POST", "/plan", {}, body={"policy": {}})[0] == 401
    assert app.dispatch("POST", "/apply", {}, body={})[0] == 401
    assert app.dispatch("POST", "/apply/revert", {}, body={})[0] == 401
