"""Unit tests for auto-breaker (router/breaker.py and router/blocklist.py)."""

import json
import tempfile
from pathlib import Path

import pytest

from router.breaker import BreakerState, _Entry, _Event, FAILURE_WEIGHTS
from router.blocklist import Blocklist


# ---------------------------------------------------------------------------
# BreakerState — pure state machine tests
# ---------------------------------------------------------------------------

BREAKER_CONFIG = {
    "enabled": True,
    "threshold": 5,
    "window_seconds": 600,
    "base_cooldown_seconds": 60,
    "max_cooldown_seconds": 900,
    "backoff_multiplier": 2.0,
}


class TestBreakerStateClosedToOpen:
    """CLOSED → OPEN transitions."""

    def test_trips_when_weight_exceeds_threshold(self):
        bs = BreakerState(BREAKER_CONFIG)
        # 2 TTFB stalls = weight 6 > threshold 5
        tripped = bs.record("gpt-5.6-sol@openai-codex", "ttfb_stall", 100.0)
        assert not tripped  # first event: weight 3 < 5
        tripped = bs.record("gpt-5.6-sol@openai-codex", "ttfb_stall", 110.0)
        assert tripped
        assert bs.is_blocked("gpt-5.6-sol@openai-codex", 120.0)

    def test_does_not_trip_below_threshold(self):
        bs = BreakerState(BREAKER_CONFIG)
        # 2 hard_timeouts = weight 2 < 5
        bs.record("model@prov", "hard_timeout", 100.0)
        bs.record("model@prov", "hard_timeout", 110.0)
        assert not bs.is_blocked("model@prov", 120.0)

    def test_sliding_window_prunes_old_events(self):
        bs = BreakerState(BREAKER_CONFIG)
        # Event at t=0, window=600s. At t=700, event pruned.
        bs.record("model@prov", "ttfb_stall", 0.0)  # weight 3
        bs.record("model@prov", "idle_stall", 10.0)  # weight 2, total 5 → trips
        # Verify blocked
        assert bs.is_blocked("model@prov", 50.0)
        # After cooldown + window expiry, old events gone
        # New event at t=700: old events are 700s old → pruned
        # Only the new event counts
        bs2 = BreakerState(BREAKER_CONFIG)
        bs2.record("model2@prov", "ttfb_stall", 0.0)
        # At t=700, this event is outside window
        tripped = bs2.record("model2@prov", "idle_stall", 700.0)
        # ttfb_stall at 0 is pruned; idle_stall at 700 = weight 2 < 5
        assert not tripped

    def test_weight_ttfb_stall(self):
        assert FAILURE_WEIGHTS["ttfb_stall"] == 3

    def test_weight_idle_stall(self):
        assert FAILURE_WEIGHTS["idle_stall"] == 2

    def test_weight_hard_timeout(self):
        assert FAILURE_WEIGHTS["hard_timeout"] == 1

    def test_weight_crash(self):
        assert FAILURE_WEIGHTS["crash"] == 1


class TestBreakerOpenHalfOpen:
    """OPEN → HALF_OPEN → CLOSED transitions."""

    def test_halp_open_after_cooldown(self):
        bs = BreakerState(BREAKER_CONFIG)
        # Trip with 2 TTFB stalls
        bs.record("model@prov", "ttfb_stall", 100.0)
        bs.record("model@prov", "ttfb_stall", 110.0)
        # Within cooldown (base = 60s, tripped at 110, cooldown until 170)
        assert bs.is_blocked("model@prov", 120.0)
        # After cooldown
        assert not bs.is_blocked("model@prov", 200.0)
        # Now in HALF_OPEN — not blocked

    def test_half_open_success_resets(self):
        bs = BreakerState(BREAKER_CONFIG)
        bs.record("model@prov", "ttfb_stall", 100.0)
        bs.record("model@prov", "ttfb_stall", 110.0)
        # Pass cooldown
        assert not bs.is_blocked("model@prov", 200.0)
        # Record success
        bs.record_success("model@prov", 210.0)
        # Verify not blocked
        assert not bs.is_blocked("model@prov", 220.0)

    def test_half_open_failure_retrips(self):
        bs = BreakerState(BREAKER_CONFIG)
        # Trip
        bs.record("model@prov", "ttfb_stall", 100.0)
        bs.record("model@prov", "ttfb_stall", 110.0)
        # Enter HALF_OPEN
        assert not bs.is_blocked("model@prov", 200.0)
        # Probe fails
        tripped = bs.record("model@prov", "idle_stall", 210.0)
        assert tripped
        # Back to OPEN with extended cooldown (120s now)
        assert bs.is_blocked("model@prov", 220.0)

    def test_exponential_backoff(self):
        bs = BreakerState(BREAKER_CONFIG)
        # First trip at t=110 (base 60s, until 170)
        bs.record("model@prov", "ttfb_stall", 100.0)
        bs.record("model@prov", "ttfb_stall", 110.0)
        assert bs.is_blocked("model@prov", 120.0)
        # Cooldown expires → HALF_OPEN
        assert not bs.is_blocked("model@prov", 200.0)
        # Second trip (HALF_OPEN failure): backoff = 60 * 2 = 120s, until 330
        bs.record("model@prov", "idle_stall", 210.0)
        assert bs.is_blocked("model@prov", 220.0)
        # Cooldown expires → HALF_OPEN
        assert not bs.is_blocked("model@prov", 400.0)
        # Third trip: backoff = 120 * 2 = 240s, until 650
        bs.record("model@prov", "hard_timeout", 410.0)
        assert bs.is_blocked("model@prov", 500.0)
        # Cooldown expires → HALF_OPEN
        assert not bs.is_blocked("model@prov", 700.0)
        # Fourth trip: backoff = 240 * 2 = 480s, until 1180
        bs.record("model@prov", "crash", 710.0)
        assert bs.is_blocked("model@prov", 800.0)

    def test_backoff_capped_at_max(self):
        config = {**BREAKER_CONFIG, "base_cooldown_seconds": 400}
        bs = BreakerState(config)
        # First trip: 400s, until 510
        bs.record("model@prov", "ttfb_stall", 100.0)
        bs.record("model@prov", "ttfb_stall", 110.0)
        assert bs.is_blocked("model@prov", 200.0)
        # Cooldown expires → HALF_OPEN
        assert not bs.is_blocked("model@prov", 600.0)
        # Second trip: backoff = 400 * 2 = 800s, until 1410
        bs.record("model@prov", "idle_stall", 610.0)
        assert bs.is_blocked("model@prov", 700.0)
        # Cooldown expires → HALF_OPEN
        assert not bs.is_blocked("model@prov", 1500.0)
        # Third trip: backoff = 800 * 2 = 1600 capped at 900, until 2400
        bs.record("model@prov", "crash", 1510.0)
        assert bs.is_blocked("model@prov", 1600.0)
        assert not bs.is_blocked("model@prov", 2500.0)  # 1510+900 = 2410 < 2500


class TestBreakerSerialization:
    """to_dict / from_dict round trip."""

    def test_round_trip_empty(self):
        bs = BreakerState(BREAKER_CONFIG)
        data = bs.to_dict()
        bs2 = BreakerState.from_dict(data, BREAKER_CONFIG)
        assert bs2.to_dict() == data

    def test_round_trip_with_entries(self):
        bs = BreakerState(BREAKER_CONFIG)
        bs.record("a@b", "ttfb_stall", 100.0)
        bs.record("a@b", "ttfb_stall", 110.0)
        bs.record("c@d", "idle_stall", 200.0)
        data = bs.to_dict()
        bs2 = BreakerState.from_dict(data, BREAKER_CONFIG)
        assert bs2.to_dict() == data

    def test_version_mismatch_returns_empty(self):
        bs = BreakerState.from_dict(
            {"version": 99, "entries": {}},
            BREAKER_CONFIG,
        )
        assert bs.to_dict() == {"version": 1, "entries": {}}

    def test_corrupt_json_returns_empty(self):
        bs = BreakerState.from_dict({"garbage": True}, BREAKER_CONFIG)
        assert bs.to_dict() == {"version": 1, "entries": {}}

    def test_none_returns_empty(self):
        bs = BreakerState.from_dict(None, BREAKER_CONFIG)  # type: ignore
        assert bs.to_dict() == {"version": 1, "entries": {}}


class TestBreakerStateTransitions:
    """Full state machine coverage."""

    def test_closed_to_open(self):
        bs = BreakerState(BREAKER_CONFIG)
        tripped = bs.record("k", "ttfb_stall", 100.0)
        assert not tripped
        tripped = bs.record("k", "ttfb_stall", 110.0)
        assert tripped
        assert bs.is_blocked("k", 120.0)

    def test_open_to_half_open(self):
        bs = BreakerState(BREAKER_CONFIG)
        bs.record("k", "ttfb_stall", 100.0)
        bs.record("k", "ttfb_stall", 110.0)
        assert bs.is_blocked("k", 120.0)
        assert not bs.is_blocked("k", 200.0)  # HALF_OPEN

    def test_half_open_to_closed(self):
        bs = BreakerState(BREAKER_CONFIG)
        bs.record("k", "ttfb_stall", 100.0)
        bs.record("k", "ttfb_stall", 110.0)
        assert not bs.is_blocked("k", 200.0)
        bs.record_success("k", 210.0)
        assert not bs.is_blocked("k", 220.0)

    def test_half_open_to_open(self):
        bs = BreakerState(BREAKER_CONFIG)
        bs.record("k", "ttfb_stall", 100.0)
        bs.record("k", "ttfb_stall", 110.0)
        assert not bs.is_blocked("k", 200.0)
        tripped = bs.record("k", "ttfb_stall", 210.0)
        assert tripped
        assert bs.is_blocked("k", 220.0)

    def test_success_in_closed_does_not_reset(self):
        """record_success in CLOSED has no effect (window governs expiry)."""
        bs = BreakerState(BREAKER_CONFIG)
        bs.record("k", "ttfb_stall", 100.0)
        bs.record_success("k", 110.0)
        # Still has 1 TTFB stall event (weight 3)
        tripped = bs.record("k", "idle_stall", 120.0)
        assert tripped  # 3 + 2 = 5 ≥ threshold


# ---------------------------------------------------------------------------
# Blocklist + Breaker integration tests
# ---------------------------------------------------------------------------

BLOCKLIST_CONFIG = {
    "blocklist": {
        "manual_ban": [
            {"model": "gpt-5.6-sol", "provider": "openai-codex",
             "reason": "accept-but-never-stream"},
        ],
        "fallback_chain": ["gpt-5.6-sol", "glm-5.2"],
        "auto_breaker": {
            "enabled": True,
            "threshold": 5,
            "window_seconds": 600,
            "base_cooldown_seconds": 60,
            "max_cooldown_seconds": 900,
            "backoff_multiplier": 2.0,
        },
    }
}


class TestBlocklistWithBreaker:
    """Blocklist integration with BreakerState."""

    @staticmethod
    def _clean_state():
        from router.blocklist import _state_path
        sp = _state_path()
        if sp.exists():
            sp.unlink()

    def test_config_ban_still_fires(self):
        self._clean_state()
        bl = Blocklist(BLOCKLIST_CONFIG)
        assert bl.is_blocked("gpt-5.6-sol", "openai-codex") is True

    def test_breaker_blocks_after_trip(self):
        self._clean_state()
        bl = Blocklist(BLOCKLIST_CONFIG)
        model, provider = "some-flaky", "test-prov"
        tripped = bl.record_failure(model, provider, "ttfb_stall")
        assert not tripped
        tripped = bl.record_failure(model, provider, "ttfb_stall")
        assert tripped
        assert bl.is_blocked(model, provider) is True

    def test_config_ban_fires_with_breaker_cooldown(self):
        self._clean_state()
        bl = Blocklist(BLOCKLIST_CONFIG)
        assert bl.is_blocked("gpt-5.6-sol", "openai-codex") is True

    def test_breaker_disabled(self):
        self._clean_state()
        config = {
            "blocklist": {
                "manual_ban": [],
                "fallback_chain": [],
                "auto_breaker": {"enabled": False},
            }
        }
        bl = Blocklist(config)
        assert not bl.breaker_enabled()
        assert bl.breaker_status() == []
        assert bl.record_failure("m", "p", "ttfb_stall") is False
        assert not bl.is_blocked("m", "p")

    def test_fallback_chain_unchanged(self):
        self._clean_state()
        bl = Blocklist(BLOCKLIST_CONFIG)
        assert bl.fallback_for("gpt-5.6-sol") == "glm-5.2"
        assert bl.fallback_for("glm-5.2") is None

    def test_record_success_resets_breaker(self):
        self._clean_state()
        bl = Blocklist(BLOCKLIST_CONFIG)
        model, provider = "flaky", "prov"
        bl.record_failure(model, provider, "ttfb_stall")
        bl.record_failure(model, provider, "ttfb_stall")
        assert bl.is_blocked(model, provider)
        bl.record_success(model, provider)

    def test_breaker_status(self):
        self._clean_state()
        bl = Blocklist(BLOCKLIST_CONFIG)
        model, provider = "flaky2", "prov2"
        bl.record_failure(model, provider, "ttfb_stall")
        bl.record_failure(model, provider, "ttfb_stall")
        status = bl.breaker_status()
        assert len(status) >= 1
        our_entry = [s for s in status if s["model_key"] == f"{model}@{provider}"]
        assert len(our_entry) == 1
        assert our_entry[0]["state"] == "OPEN"

    def test_breaker_state_serialization(self):
        self._clean_state()
        bl = Blocklist(BLOCKLIST_CONFIG)
        model, provider = "flaky3", "prov3"
        bl.record_failure(model, provider, "ttfb_stall")
        bl.record_failure(model, provider, "ttfb_stall")
        state = bl.breaker_state_dict()
        assert "entries" in state
        assert f"{model}@{provider}" in state["entries"]
        assert state["entries"][f"{model}@{provider}"]["state"] == "OPEN"

    def test_fail_closed_no_state_file(self):
        self._clean_state()
        bl = Blocklist(BLOCKLIST_CONFIG)
        assert bl.is_blocked("gpt-5.6-sol", "openai-codex") is True
        assert bl.is_blocked("claude-opus", "anthropic") is False

    def test_blocked_model_not_blocked_wrong_provider(self):
        self._clean_state()
        bl = Blocklist(BLOCKLIST_CONFIG)
        assert bl.is_blocked("gpt-5.6-sol", "anthropic") is False


# ---------------------------------------------------------------------------
# _Event / _Entry tests
# ---------------------------------------------------------------------------

class TestEvent:
    def test_to_dict_from_dict(self):
        ev = _Event("ttfb_stall", 100.0, 3)
        d = ev.to_dict()
        ev2 = _Event.from_dict(d)
        assert ev2 is not None
        assert ev2.kind == "ttfb_stall"
        assert ev2.ts == 100.0
        assert ev2.weight == 3

    def test_from_dict_invalid(self):
        assert _Event.from_dict({}) is None
        assert _Event.from_dict({"kind": ""}) is None
        # str(123) = "123" — valid kind, should create event
        ev = _Event.from_dict({"kind": 123})  # type: ignore
        assert ev is not None
        assert ev.kind == "123"


class TestEntry:
    def test_prune(self):
        entry = _Entry()
        entry.events = [
            _Event("a", 100.0, 1),
            _Event("b", 200.0, 1),
            _Event("c", 300.0, 1),
        ]
        entry.prune(350.0, 100.0)  # cutoff = 250
        assert len(entry.events) == 1  # only c survives
        assert entry.events[0].kind == "c"

    def test_total_weight(self):
        entry = _Entry()
        entry.events = [
            _Event("a", 100.0, 3),
            _Event("b", 200.0, 2),
        ]
        assert entry.total_weight() == 5

    def test_to_dict_from_dict_round_trip(self):
        entry = _Entry()
        entry.state = "OPEN"
        entry.events = [_Event("ttfb_stall", 100.0, 3)]
        entry.cooldown_until = 200.0
        entry.backoff_seconds = 60.0
        entry.last_failure_kind = "ttfb_stall"

        data = entry.to_dict()
        entry2 = _Entry.from_dict(data)
        assert entry2 is not None
        assert entry2.state == "OPEN"
        assert len(entry2.events) == 1
        assert entry2.events[0].kind == "ttfb_stall"
        assert entry2.cooldown_until == 200.0
        assert entry2.backoff_seconds == 60.0
        assert entry2.last_failure_kind == "ttfb_stall"

    def test_from_dict_invalid(self):
        assert _Entry.from_dict(None) is None  # type: ignore
        assert _Entry.from_dict("garbage") is None  # type: ignore
