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

    def test_anchors_accessor(self):
        c = Classifier(SAMPLE_CONFIG, anchors=ANCHORS)
        assert len(c.anchors()) == 2

    def test_build_prompt_from_config(self):
        prompt = build_prompt_from_config(SAMPLE_CONFIG, "test task",
                                         {"verb_class": "trivial"})
        assert "test task" in prompt
        assert "trivial" in prompt
