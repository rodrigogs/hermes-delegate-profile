"""End-to-end proof of the replay chain: a real routing decision must survive
all the way to what the console renders.

Every other test in this suite checks one link (route() builds steps, the durable
log writes a line, the service reads it back, the sidecar exposes it). This file
is the only one that runs the WHOLE chain with the production wiring, which is
what the replay feature actually promises: the trace you scrub in the UI is the
decision the router really made.

It cannot pass by accident: the assertions pin the cause, the stage sequence and
the chosen model back to a route() call whose inputs are fixed here, and the
final read goes through the sidecar's authenticated dispatcher rather than the
file.
"""

from __future__ import annotations

import json

import pytest
import yaml

from router.adapter import route
from router.durable_decision_log import DurableDecisionLog, routes_path
from router.one_sidecar import SidecarApp
from router.service import RouterService

_TOKEN = "replay-e2e-token"


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Production-shaped wiring: a policy on disk, a temp state dir, a sidecar."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_ROUTE_TRACE_FILE", raising=False)

    policy = {
        "enabled": True,
        "classifier": {"model": "judge", "provider": "judge-rail"},
        "fail_safe": {"profile": "coder", "model": "safe", "provider": "safe-rail"},
        "blocklist": {"manual_ban": [{"model": "banned", "provider": "bad-rail"}],
                      "fallback_chain": [], "auto_breaker": {"enabled": False}},
        "rules": [{
            "id": "hard-verbs",
            "when": {"verb_class": {"eq": "hard"}},
            "then": {"profile": "coder", "model": "T4"},
        }],
        "default": {"action": "classify"},
        "tiers": {
            "T1": {"model": "tiny", "provider": "cheap"},
            "T2": {"model": "small", "provider": "cheap"},
            "T3": {"model": "mid", "provider": "strong-rail"},
            "T4": {"model": "strong", "provider": "strong-rail"},
        },
    }
    config_path = tmp_path / "router.yaml"
    config_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    token_path = tmp_path / "token"
    token_path.write_text(_TOKEN, encoding="utf-8")
    app = SidecarApp(RouterService(config_path), token_path=lambda: token_path)
    return policy, app


def _auth():
    return {"X-Hermes-Sidecar-Token": _TOKEN}


def test_a_real_decision_becomes_a_replayable_trace(wired):
    """route() → routes.jsonl → GET /routes → GET /routes?id= with steps."""
    policy, app = wired

    # 1. A real routing decision, through the production log.
    decision = route(task="Debug a race condition in the cache", config=policy,
                     decision_log=DurableDecisionLog())
    assert decision["model"] == "strong", "the hard-verb rule should pick T4"

    # 2. It persisted exactly one trace line.
    lines = [l for l in routes_path().read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["cause"] == "hard_rule"

    # 3. The sidecar lists it — through the token gate, not the file.
    assert app.dispatch("GET", "/routes", {})[0] == 401, "traces must stay token-gated"
    status, listing = app.dispatch("GET", "/routes", _auth())
    assert status == 200
    assert listing["count"] == 1
    row = listing["routes"][0]
    assert row["cause"] == "hard_rule"
    assert row["model"] == "strong", "the listing shows the model that was actually chosen"

    # 4. The full trace is fetchable by the id the listing advertised, and its
    #    steps are the pipeline the decision really walked.
    status, trace = app.dispatch("GET", "/routes", _auth(), {"id": [row["id"]]})
    assert status == 200
    assert [s["stage"] for s in trace["steps"]] == ["blocklist", "signals", "rules"]
    assert trace["steps"][0]["out"] == {"blocked": False}
    assert trace["steps"][-1]["cause"] == "hard_rule"
    assert trace["steps"][-1]["out"]["rule_id"] == "hard-verbs", \
        "replay must name the rule that fired, so the UI highlights the right node"


def test_a_refused_decision_is_replayable_too(wired):
    """A blocklist veto is the shortest path — it must still be a full trace."""
    policy, app = wired

    decision = route(task="anything", config=policy, requested_model="banned",
                     requested_provider="bad-rail", decision_log=DurableDecisionLog())
    assert decision["deny"] is True

    row = app.dispatch("GET", "/routes", _auth())[1]["routes"][0]
    assert row["cause"] == "blocklist_veto"
    trace = app.dispatch("GET", "/routes", _auth(), {"id": [row["id"]]})[1]
    stages = [s["stage"] for s in trace["steps"]]
    assert stages == ["blocklist", "veto"], "a veto stops at the blocklist, and says so"
    assert trace["steps"][-1]["out"]["deny"] is True


def test_traces_accumulate_newest_first(wired):
    """The console shows recent routes — ordering is the whole point."""
    policy, app = wired
    log = DurableDecisionLog()
    route(task="Debug a race condition", config=policy, decision_log=log)
    route(task="Rename a variable in 2 files", config=policy, decision_log=log)

    listing = app.dispatch("GET", "/routes", _auth())[1]
    assert listing["count"] == 2
    tasks = [r["task"] for r in listing["routes"]]
    assert tasks[0].startswith("Rename"), "most recent decision leads the list"


def test_replay_is_unaffected_by_a_corrupt_trace_line(wired):
    """One bad line must not blind the operator to the good ones."""
    policy, app = wired
    route(task="Debug a race condition", config=policy, decision_log=DurableDecisionLog())
    with open(routes_path(), "a", encoding="utf-8") as handle:
        handle.write("{ this is not json\n")
    route(task="Debug another race condition", config=policy, decision_log=DurableDecisionLog())

    listing = app.dispatch("GET", "/routes", _auth())[1]
    assert listing["count"] == 2, "the corrupt line is skipped, both real traces survive"


def test_the_trace_file_is_the_same_path_for_writer_and_reader(tmp_path, monkeypatch):
    """The writer runs per-profile, the reader is pinned to one profile. If those
    resolve differently the console shows an empty replay while traces pile up
    elsewhere — the failure mode this project actually hit."""
    monkeypatch.delenv("HERMES_ROUTE_TRACE_FILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "coder"))
    writer_path = routes_path()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "rodrigo"))
    reader_path = routes_path()
    assert writer_path == reader_path
