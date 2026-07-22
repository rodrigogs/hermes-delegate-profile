"""Unit tests for blocklist pre-filter (router/blocklist.py)."""

import pytest
from router.blocklist import Blocklist


CONFIG = {
    "blocklist": {
        "manual_ban": [
            {"model": "gpt-5.6-sol", "provider": "openai-codex",
             "reason": "accept-but-never-stream"},
        ],
        "fallback_chain": ["gpt-5.6-sol", "glm-5.2"],
        "auto_breaker": {"enabled": False},
    }
}


class TestBlocklist:
    def test_known_banned_model(self):
        bl = Blocklist(CONFIG)
        assert bl.is_blocked("gpt-5.6-sol", "openai-codex") is True

    def test_banned_model_wrong_provider(self):
        """Model banned on specific provider only."""
        bl = Blocklist(CONFIG)
        # The ban says provider=openai-codex — so anthropic should pass
        assert bl.is_blocked("gpt-5.6-sol", "anthropic") is False

    def test_unrelated_model(self):
        bl = Blocklist(CONFIG)
        assert bl.is_blocked("claude-opus", "anthropic") is False

    def test_none_model(self):
        bl = Blocklist(CONFIG)
        assert bl.is_blocked(None, None) is False

    def test_ban_without_provider(self):
        """Ban entry with empty provider bans all providers for that model."""
        config = {
            "blocklist": {
                "manual_ban": [{"model": "broken-model", "reason": "bad"}],
                "fallback_chain": [],
                "auto_breaker": {"enabled": False},
            }
        }
        bl = Blocklist(config)
        assert bl.is_blocked("broken-model", "any-provider") is True

    def test_fallback_chain_next(self):
        bl = Blocklist(CONFIG)
        assert bl.fallback_for("gpt-5.6-sol") == "glm-5.2"

    def test_fallback_chain_last(self):
        bl = Blocklist(CONFIG)
        assert bl.fallback_for("glm-5.2") is None

    def test_fallback_chain_unknown(self):
        bl = Blocklist(CONFIG)
        assert bl.fallback_for("unknown-model") is None

    def test_manual_bans_list(self):
        bl = Blocklist(CONFIG)
        bans = bl.manual_bans()
        assert len(bans) == 1
        assert bans[0]["model"] == "gpt-5.6-sol"

    def test_fallback_chain_list(self):
        bl = Blocklist(CONFIG)
        chain = bl.fallback_chain()
        assert chain == ["gpt-5.6-sol", "glm-5.2"]

    def test_auto_breaker_disabled(self):
        """v1: auto-breaker is deferred, engine off."""
        bl = Blocklist(CONFIG)
        # Even if cooldowns were present (v1: empty), breaker is off
        assert bl.is_blocked("some-model", "some-provider") is False

    def test_empty_config(self):
        bl = Blocklist({})
        assert bl.is_blocked("anything", "anywhere") is False
        assert bl.fallback_for("x") is None

    def test_case_insensitive_match(self):
        bl = Blocklist(CONFIG)
        assert bl.is_blocked("GPT-5.6-SOL", "OPENAI-CODEX") is True


class TestBlocklistCache:
    """Tests for decision cache (router/cache.py)."""

    def test_hash_stability(self):
        from router.cache import hash_task, normalize
        t1 = "  Rename   getCwd to getCurrentWorkingDirectory  "
        t2 = "rename getcwd to getcurrentworkingdirectory"
        assert normalize(t1) == normalize(t2)
        assert hash_task(t1) == hash_task(t2)

    def test_cache_hit(self):
        from router.cache import Cache
        c = Cache()
        c.set("hello", {"tier": "T1"})
        assert c.get("hello") == {"tier": "T1"}

    def test_cache_miss(self):
        from router.cache import Cache
        c = Cache()
        assert c.get("never-cached") is None

    def test_cache_size(self):
        from router.cache import Cache
        c = Cache()
        assert c.size() == 0
        c.set("a", {})
        c.set("b", {})
        assert c.size() == 2

    def test_cache_whitespace_normalization(self):
        from router.cache import Cache
        c = Cache()
        c.set("  hello  world  ", {"tier": "T2"})
        assert c.get("hello world") == {"tier": "T2"}


class TestSessionPin:
    """Tests for session model-floor pin (router/cache.py)."""

    def test_initial_unset(self):
        from router.cache import SessionPin
        sp = SessionPin()
        assert sp.is_set() is False
        assert sp.tier is None

    def test_set_tier(self):
        from router.cache import SessionPin
        sp = SessionPin()
        sp.set("T3")
        assert sp.is_set() is True
        assert sp.tier == "T3"

    def test_upward_only(self):
        from router.cache import SessionPin
        sp = SessionPin()
        sp.set("T3")
        sp.set("T1")  # should not downgrade
        assert sp.tier == "T3"

    def test_upgrade_allowed(self):
        from router.cache import SessionPin
        sp = SessionPin()
        sp.set("T2")
        sp.set("T4")  # upgrade ok
        assert sp.tier == "T4"

    def test_reset(self):
        from router.cache import SessionPin
        sp = SessionPin()
        sp.set("T3")
        sp.reset()
        assert sp.is_set() is False

    def test_invalid_tier_ignored(self):
        from router.cache import SessionPin
        sp = SessionPin()
        sp.set("T99")
        assert sp.is_set() is False


class TestDecisionLog:
    """Tests for decision log (router/decision_log.py)."""

    def test_record_and_tail(self):
        from router.decision_log import DecisionLog
        dl = DecisionLog()
        dl.record("classifier", {"profile": "coder", "model": "claude-opus"},
                  matched_rule_id="review-request", task_preview="review this PR")
        entries = dl.tail(1)
        assert len(entries) == 1
        assert entries[0]["cause"] == "classifier"
        assert entries[0]["rule_id"] == "review-request"

    def test_invalid_cause_rejected(self):
        from router.decision_log import DecisionLog
        dl = DecisionLog()
        dl.record("not-a-valid-cause", {"profile": "coder"})
        assert dl.tail(1)[0]["cause"] == "fail_safe_strong"

    def test_format_line(self):
        from router.decision_log import DecisionLog
        dl = DecisionLog()
        dl.record("keyword_match", {"profile": "reviewer", "model": "T3"},
                  matched_rule_id="review-request", task_preview="review this")
        line = dl.format_line(dl.tail(1)[0])
        assert "cause=keyword_match" in line
        assert "rule=review-request" in line
        assert "profile=reviewer" in line

    def test_entries_returns_copy(self):
        from router.decision_log import DecisionLog
        dl = DecisionLog()
        dl.record("default_fallthrough", {"action": "classify"})
        entries = dl.entries()
        entries.pop()
        assert len(dl.entries()) == 1  # original untouched

    def test_multiple_entries(self):
        from router.decision_log import DecisionLog
        dl = DecisionLog()
        for i in range(5):
            dl.record("classifier", {"model": f"m{i}"})
        assert len(dl.tail(10)) == 5
        assert len(dl.tail(3)) == 3
