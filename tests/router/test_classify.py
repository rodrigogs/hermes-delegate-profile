"""Unit tests for classifier (router/classify.py)."""

import pytest
from router.classify import Classifier, build_prompt_from_config


SAMPLE_CONFIG = {
    "classifier": {
        "model": "glm-5.2",
        "provider": "zai",
        "temperature": 0,
        "max_tokens": 128,
        "timeout_seconds": 8,
    },
    "tiers": {
        "T1": {"model": "glm-5.2-fast", "provider": "zai"},
        "T2": {"model": "glm-5.2", "provider": "zai"},
        "T3": {"model": "claude-sonnet", "provider": "anthropic"},
        "T4": {"model": "claude-opus", "provider": "anthropic"},
    },
}

ANCHORS = [
    {
        "tier": "T1",
        "description": "Rename getCwd to getCurrentWorkingDirectory",
        "expected": {"tier": "T1", "confidence": "high",
                     "needs_capability": "mechanical rename"},
    },
    {
        "tier": "T4",
        "description": "Debug a race condition in the user cache",
        "expected": {"tier": "T4", "confidence": "high",
                     "needs_capability": "concurrency bug"},
    },
]


class TestClassifier:
    def test_init_from_config(self):
        c = Classifier(SAMPLE_CONFIG)
        assert c.model == "glm-5.2"
        assert c.provider == "zai"
        assert c.temperature == 0
        assert c.max_tokens == 128
        assert c.timeout_seconds == 8

    def test_build_prompt_includes_tiers(self):
        c = Classifier(SAMPLE_CONFIG)
        prompt = c.build_prompt("hello", {"verb_class": "unknown"})
        assert "T1" in prompt
        assert "T2" in prompt
        assert "T3" in prompt
        assert "T4" in prompt
        assert "TRIVIAL" in prompt
        assert "HARD" in prompt

    def test_build_prompt_includes_features(self):
        c = Classifier(SAMPLE_CONFIG)
        fv = {"verb_class": "hard", "has_code": True, "size_lines": 200,
              "has_stacktrace": True, "num_requirements": 3}
        prompt = c.build_prompt("debug race condition", fv)
        assert "hard" in prompt
        assert "has_code: True" in prompt
        assert "200" in prompt
        assert "debug race condition" in prompt

    def test_build_prompt_includes_anchors(self):
        c = Classifier(SAMPLE_CONFIG, anchors=ANCHORS)
        prompt = c.build_prompt("test", {"verb_class": "unknown"})
        assert "Rename getCwd" in prompt
        assert "Debug a race condition" in prompt

    def test_safety_ratchet_high_confidence(self):
        c = Classifier(SAMPLE_CONFIG)
        tier, cfg = c.safety_ratchet("T1", "high")
        assert tier == "T1"
        assert cfg["model"] == "glm-5.2-fast"

    def test_safety_ratchet_low_confidence_bumps_up(self):
        c = Classifier(SAMPLE_CONFIG)
        tier, cfg = c.safety_ratchet("T1", "low")
        assert tier == "T2"  # T1+low → T2
        assert cfg["model"] == "glm-5.2"

    def test_safety_ratchet_t4_low_stays_t4(self):
        c = Classifier(SAMPLE_CONFIG)
        tier, cfg = c.safety_ratchet("T4", "low")
        assert tier == "T4"  # ceiling

    def test_safety_ratchet_unknown_tier(self):
        c = Classifier(SAMPLE_CONFIG)
        tier, cfg = c.safety_ratchet("T99", "high")
        assert tier == "T4"  # unknown → safest

    def test_safety_ratchet_med_confidence(self):
        c = Classifier(SAMPLE_CONFIG)
        tier, cfg = c.safety_ratchet("T2", "med")
        assert tier == "T2"  # med doesn't bump (only low bumps)
        assert cfg["model"] == "glm-5.2"

    def test_tiers_accessor(self):
        c = Classifier(SAMPLE_CONFIG)
        tiers = c.tiers()
        assert "T1" in tiers
        assert tiers["T4"]["model"] == "claude-opus"

    def test_no_tiers_table_degrades_to_empty_not_stale_hardcode(self):
        """A config without ``tiers`` must not invent rails (DEFAULT_TIERS removed)."""
        c = Classifier({"classifier": {}})
        assert c.tiers() == {}
        # safety_ratchet still answers a tier, but materialises NO model —
        # the adapter then routes under the caller's default instead of a
        # stale elo that is not among the live links.
        tier, cfg = c.safety_ratchet("T4", "high")
        assert tier == "T4"
        assert cfg == {}

    def test_anchors_accessor(self):
        c = Classifier(SAMPLE_CONFIG, anchors=ANCHORS)
        assert len(c.anchors()) == 2

    def test_build_prompt_from_config(self):
        prompt = build_prompt_from_config(SAMPLE_CONFIG, "test task",
                                         {"verb_class": "trivial"})
        assert "test task" in prompt
        assert "trivial" in prompt


@pytest.mark.parametrize("confidence", ["low", "Low", "LOW", "low ", " low", "LOW ", "very low"])
def test_ratchet_fires_for_any_casing_or_hedge_of_low(confidence):
    """The classifier is an LLM, not a typed API.

    It is prompted for "high|med|low" and returns "Low", "LOW", "very low". An
    exact == "low" comparison missed all of those, and a missed ratchet is the
    expensive direction: work the classifier was NOT confident about goes to the
    CHEAPEST tier. Measured before the fix, only exact lowercase "low" ratcheted.
    """
    c = Classifier(SAMPLE_CONFIG)
    tier, _ = c.safety_ratchet("T1", confidence)
    assert tier == "T2", f"{confidence!r} should have ratcheted T1 -> T2"


@pytest.mark.parametrize("confidence", ["high", "med", "", None, 0.1])
def test_ratchet_stays_put_when_confidence_is_not_low(confidence):
    """Normalising must not make the ratchet fire on everything."""
    c = Classifier(SAMPLE_CONFIG)
    tier, _ = c.safety_ratchet("T1", confidence)
    assert tier == "T1"


@pytest.mark.parametrize("tier,expected", [("t1", "T1"), ("T1 ", "T1"), ("bogus", "T4"), ("", "T4"), (None, "T4")])
def test_tier_is_normalised_and_an_unknown_tier_fails_upward(tier, expected):
    """An unrecognised tier must resolve to the strongest, never the cheapest."""
    c = Classifier(SAMPLE_CONFIG)
    assert c.safety_ratchet(tier, "high")[0] == expected
