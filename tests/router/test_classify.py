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


# ── The prompt is bounded by the classifier's own window ──────────────────────
# Raised while designing "Smart Router as a selectable model": if the classifier
# runs before every prompt, what is sent to it must not exceed that model's
# context size. It never did — `build_prompt` ended with f'Task: "{task}"' and
# nothing bounded `task`. Short card bodies hid it; a pasted stacktrace or a long
# chat turn would not.
#
# Two bounds, both floors of the same intent (err toward less room): the purpose
# cap, because a T1..T4 verdict does not improve past a few thousand characters
# when the SCALE of the job already travels as features; and the model's window
# when anyone knows it.

_UNCATALOGUED = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_a_task_bigger_than_the_budget_is_cut_and_the_prompt_says_so():
    """Silently truncating would be the worse bug: the classifier would answer
    confidently about work it never saw."""
    c = Classifier({"classifier": {"model": _UNCATALOGUED, "max_tokens": 128}, "tiers": {}})
    budget = c.task_char_budget()
    assert budget > 0, "non-vacuity: there is a budget to exceed"
    prompt = c.build_prompt("x" * (budget * 3), {"num_files": 7})

    assert f"first {budget} of {budget * 3} characters" in prompt, (
        "the prompt states both numbers, so a hedged answer is not read as a confident one"
    )
    assert "the rest was not sent" in prompt
    # The quoted excerpt itself, not a character count — "excerpt" in the disclosure
    # prose contains an x and made the naive count off by two.
    assert '"' + "x" * budget + '"' in prompt, "exactly the budget, quoted"
    assert '"' + "x" * (budget + 1) + '"' not in prompt, "and not one character more"
    # The scale of the job still reaches the classifier — via the features, which is
    # the whole reason a small text budget costs nothing.
    assert "num_files: 7" in prompt


def test_a_task_inside_the_budget_is_passed_through_untouched():
    c = Classifier({"classifier": {"model": _UNCATALOGUED}, "tiers": {}})
    prompt = c.build_prompt("rename a variable", {})
    assert 'Task: "rename a variable"' in prompt
    assert "was not sent" not in prompt, "nothing was cut, so nothing is disclosed"


def test_an_uncatalogued_classifier_still_gets_a_bound():
    """The docker install's case, and the one that matters most.

    Its classifier is us.anthropic.*, which the registry deliberately does not carry
    (same weights, different commercial rail), so the window lookup is honestly None.
    An unknown window must not mean an unbounded prompt.
    """
    c = Classifier({"classifier": {"model": _UNCATALOGUED}, "tiers": {}})
    assert c._context_window is None, "non-vacuity: this id really is uncatalogued"
    assert c.task_char_budget() > 0


def test_a_declared_window_smaller_than_the_cap_is_what_binds():
    """The clamp is reachable configuration, not decoration.

    500-token window, 128 reserved for the reply, the recorded 5/4 headroom, at 3.6
    chars per token, less a 675-char preamble.
    """
    c = Classifier({
        "classifier": {"model": "made-up-tiny", "context_window": 500, "max_tokens": 128},
        "tiers": {},
    })
    assert c._context_window == 500, "a declared window wins over an absent registry entry"
    clamped = c.task_char_budget(675)
    assert clamped == 394, clamped
    unclamped = Classifier({"classifier": {"model": _UNCATALOGUED}, "tiers": {}})
    assert clamped < unclamped.task_char_budget(675), "the window, not the cap, bound it"


def test_a_window_too_small_for_its_own_reply_yields_no_text_rather_than_all_of_it():
    """0 means "features only", never "send everything" — the direction a bound must fail."""
    c = Classifier({
        "classifier": {"model": "absurd", "context_window": 10, "max_tokens": 128},
        "tiers": {},
    })
    assert c.task_char_budget(675) == 0
    prompt = c.build_prompt("anything at all", {})
    assert '""' in prompt, "an empty excerpt, and the disclosure says how much was dropped"
    assert "first 0 of 15 characters" in prompt


@pytest.mark.parametrize("window", [None, 0, -1, "200000", 12.5, {}])
def test_a_window_that_is_not_a_positive_int_is_treated_as_unknown(window):
    """Never a guess: a malformed declared window degrades to the absolute cap."""
    c = Classifier({
        "classifier": {"model": "junk-window", "context_window": window, "vision": True},
        "tiers": {},
    })
    assert c._context_window is None
    assert c.task_char_budget() > 0


def test_a_registry_that_raises_degrades_to_unknown_instead_of_propagating(monkeypatch):
    """A classifier must still build a prompt when the catalogue is unusable."""
    from router import classify as mod

    def boom(*_a, **_k):
        raise TypeError("registry unusable")

    monkeypatch.setattr(mod._caps, "capabilities_for", boom)
    c = Classifier({"classifier": {"model": "whatever"}, "tiers": {}})
    assert c._context_window is None
    assert c.build_prompt("hi", {})


def test_a_none_task_is_a_prompt_not_a_crash():
    c = Classifier({"classifier": {"model": _UNCATALOGUED}, "tiers": {}})
    assert 'Task: ""' in c.build_prompt(None, {})


def test_the_budget_is_far_below_every_catalogued_window():
    """The measurement the purpose cap rests on, kept as a test so it cannot rot.

    If a future registry entry declares a window tighter than the cap, this fails and
    the cap has to be revisited rather than silently becoming the binding constraint.
    """
    from router import capabilities as caps
    from router.signals import chars_per_token

    windows = [
        v["context_window"] for v in caps.MODEL_CAPABILITIES.values()
        if isinstance(v, dict) and isinstance(v.get("context_window"), int)
    ]
    assert windows, "non-vacuity: the registry declares windows"
    cap_tokens = Classifier({"classifier": {"model": _UNCATALOGUED}, "tiers": {}}) \
        .task_char_budget() / chars_per_token()
    assert cap_tokens < min(windows) / 10, (
        f"the cap is {cap_tokens:.0f} tokens against a tightest catalogued window of "
        f"{min(windows)}; it must stay a small fraction of it"
    )
