"""Unit tests for the model capability registry (router/capabilities.py)."""

import ast
import inspect
import random
from datetime import datetime, timedelta, timezone

import pytest

import router.capabilities as caps_module
from router.capabilities import (
    BILLING_MODES,
    CAPABILITY_ASSERTION_KEYS,
    FALLBACK_STRATEGIES,
    MAX_REGISTERED_CONTEXT,
    MODEL_CAPABILITIES,
    REQUIREMENT_KEYS,
    apply_time_cap,
    apply_time_policy,
    capabilities_for,
    derive_requirements,
    effective_price,
    filter_chain,
    in_expensive_window,
    independent_rails,
    next_window_change,
    order_chain,
    price_multiplier,
    price_window_diagnostics,
    registry_diagnostics,
    satisfies,
    upstream_group,
)

# The clock is INJECTED, never read: every time-dependent assertion below names
# the instant it is asserting about. 2026-08-17 is a Monday, so the weekday of
# each date is unambiguous and asserted once in
# test_the_reference_clocks_are_the_weekdays_they_claim.
UTC = timezone.utc


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC datetime in the reference week: day 17 = Monday .. 23 = Sunday."""
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


MON = 17
WED = 19
FRI = 21
SAT = 22
SUN = 23


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------

def test_registry_has_no_diagnostics():
    assert registry_diagnostics() == []


def test_every_billing_mode_is_in_the_closed_set():
    modes = {entry["billing_mode"] for entry in MODEL_CAPABILITIES.values()}
    assert modes <= BILLING_MODES


def test_requirement_keys_is_the_documented_closed_set():
    assert REQUIREMENT_KEYS == frozenset(
        {"min_context", "vision", "tool_calling", "structured_output"}
    )


# ---------------------------------------------------------------------------
# capabilities_for
# ---------------------------------------------------------------------------

def test_capabilities_for_known_model_returns_registry_entry():
    caps = capabilities_for("glm-4.7")
    assert caps["provider"] == "zai"
    assert caps["context_window"] == 200_000
    assert caps["billing_mode"] == "plan"


def test_capabilities_for_unknown_model_without_declared_is_none():
    assert capabilities_for("no-such-model-v9") is None


def test_capabilities_for_unknown_model_with_declared_returns_declared():
    caps = capabilities_for(
        "no-such-model-v9", {"context_window": 32_000, "vision": True}
    )
    assert caps == {"context_window": 32_000, "vision": True}


def test_capabilities_for_never_mutates_the_registry():
    caps = capabilities_for("glm-4.7", {"context_window": 1})
    assert caps["context_window"] == 1
    assert MODEL_CAPABILITIES["glm-4.7"]["context_window"] == 200_000


def test_declared_overrides_beat_the_registry_entry():
    caps = capabilities_for("glm-4.7", {"vision": True, "context_window": 999})
    assert caps["vision"] is True
    assert caps["context_window"] == 999
    # untouched fields still come from the registry
    assert caps["provider"] == "zai"


def test_capabilities_for_ignores_non_capability_keys_in_declared():
    caps = capabilities_for("glm-4.7", {"model": "glm-4.7", "weight": 3})
    assert "model" not in caps
    assert "weight" not in caps


# ---------------------------------------------------------------------------
# satisfies — the four requirement kinds
# ---------------------------------------------------------------------------

def test_satisfies_min_context_passes_for_a_big_window():
    assert satisfies("glm-5.3", {"min_context": 500_000}) == (True, "")


def test_satisfies_min_context_rejects_context_too_small():
    assert satisfies("glm-4.5v", {"min_context": 500_000}) == (
        False,
        "context_too_small",
    )


def test_satisfies_vision_passes_for_a_vision_model():
    assert satisfies("glm-4.6v", {"vision": True}) == (True, "")


def test_satisfies_vision_rejects_no_vision():
    assert satisfies("glm-4.7", {"vision": True}) == (False, "no_vision")


def test_satisfies_tool_calling_passes_for_a_tool_model():
    assert satisfies("glm-4.7", {"tool_calling": True}) == (True, "")


def test_satisfies_tool_calling_rejects_no_tool_calling():
    assert satisfies("z-ai/glm-5.2:free", {"tool_calling": True}) == (
        False,
        "no_tool_calling",
    )


def test_satisfies_structured_output_passes_for_a_structured_model():
    assert satisfies("glm-4.7", {"structured_output": True}) == (True, "")


def test_satisfies_structured_output_rejects_no_structured_output():
    assert satisfies("MiniMax-M3", {"structured_output": True}) == (
        False,
        "no_structured_output",
    )


def test_satisfies_with_no_requirements_passes():
    assert satisfies("glm-4.7", {}) == (True, "")


def test_satisfies_false_requirement_does_not_constrain():
    # asking for vision=False must not reject a text-only model
    assert satisfies("glm-4.7", {"vision": False}) == (True, "")


def test_satisfies_ignores_keys_outside_the_requirement_set():
    assert satisfies("glm-4.7", {"needs_telepathy": True}) == (True, "")


def test_satisfies_reports_the_contradiction_over_the_unknown():
    # context is known-too-small; vision is unknown for this fake elo
    ok, reason = satisfies(
        "mystery-elo",
        {"min_context": 100_000, "vision": True},
        {"context_window": 8_000},
    )
    assert (ok, reason) == (False, "context_too_small")


# ---------------------------------------------------------------------------
# satisfies — context boundary
# ---------------------------------------------------------------------------

def test_min_context_exactly_equal_to_context_window_passes():
    assert satisfies("glm-4.7", {"min_context": 200_000}) == (True, "")


def test_min_context_one_token_over_context_window_fails():
    assert satisfies("glm-4.7", {"min_context": 200_001}) == (
        False,
        "context_too_small",
    )


# ---------------------------------------------------------------------------
# satisfies — unknown capabilities never reject
# ---------------------------------------------------------------------------

def test_unknown_model_returns_capability_unknown_and_passes():
    assert satisfies("no-such-model-v9", {"vision": True}) == (
        True,
        "capability_unknown",
    )


def test_unpublished_capability_on_a_known_model_is_unknown_not_a_rejection():
    ok, reason = satisfies("glm-4.7", {"vision": True}, {"vision": None})
    assert (ok, reason) == (True, "capability_unknown")


def test_declared_value_flips_a_rejection_into_a_pass():
    assert satisfies("glm-4.7", {"vision": True}) == (False, "no_vision")
    assert satisfies("glm-4.7", {"vision": True}, {"vision": True}) == (True, "")


def test_declared_context_window_flips_context_too_small_into_a_pass():
    assert satisfies("glm-4.5v", {"min_context": 200_000})[0] is False
    ok, reason = satisfies(
        "glm-4.5v", {"min_context": 200_000}, {"context_window": 262_144}
    )
    assert (ok, reason) == (True, "")


# ---------------------------------------------------------------------------
# filter_chain
# ---------------------------------------------------------------------------

def _chain():
    return [
        {"model": "glm-4.7", "provider": "zai"},
        {"model": "glm-4.6v", "provider": "zai"},
        {"model": "kimi-k3", "provider": "moonshot"},
    ]


def test_filter_chain_preserves_order_of_eligible_entries():
    result = filter_chain(_chain(), {"tool_calling": True})
    assert [entry["model"] for entry in result["eligible"]] == [
        "glm-4.7",
        "glm-4.6v",
        "kimi-k3",
    ]
    assert result["rejected"] == []
    assert result["bypassed"] is False


def test_filter_chain_leaves_eligible_entries_unchanged():
    chain = _chain()
    result = filter_chain(chain, {"tool_calling": True})
    assert result["eligible"][0] is chain[0]
    assert "reject_reason" not in result["eligible"][0]


def test_filter_chain_rejected_entries_carry_reject_reason():
    result = filter_chain(_chain(), {"vision": True})
    assert [entry["model"] for entry in result["eligible"]] == [
        "glm-4.6v",
        "kimi-k3",
    ]
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["model"] == "glm-4.7"
    assert result["rejected"][0]["reject_reason"] == "no_vision"


def test_filter_chain_does_not_mutate_the_rejected_source_entry():
    chain = _chain()
    filter_chain(chain, {"vision": True})
    assert "reject_reason" not in chain[0]


def test_filter_chain_lists_unknown_models_but_keeps_them_eligible():
    chain = [
        {"model": "glm-4.6v", "provider": "zai"},
        {"model": "mystery-elo", "provider": "somewhere"},
    ]
    result = filter_chain(chain, {"vision": True})
    assert result["unknown"] == ["mystery-elo"]
    assert [entry["model"] for entry in result["eligible"]] == [
        "glm-4.6v",
        "mystery-elo",
    ]
    assert result["bypassed"] is False


def test_filter_chain_bypasses_when_no_elo_can_meet_the_requirement():
    chain = _chain()
    result = filter_chain(chain, {"min_context": 99_000_000})
    assert result["bypassed"] is True
    assert result["eligible"] == chain


def test_filter_chain_on_an_empty_chain_is_not_a_bypass():
    result = filter_chain([], {"vision": True})
    assert result == {
        "eligible": [],
        "rejected": [],
        "unknown": [],
        "bypassed": False,
        "unsatisfiable": [],
    }


def test_glm_52_free_is_rejected_when_tool_calling_is_required():
    # Production trap: a strong free model that hard-fails any loop which
    # sends tool definitions.
    chain = [
        {"model": "z-ai/glm-5.2:free", "provider": "openrouter"},
        {"model": "glm-4.7", "provider": "zai"},
    ]
    result = filter_chain(chain, {"tool_calling": True})
    assert [entry["model"] for entry in result["eligible"]] == ["glm-4.7"]
    assert result["rejected"][0]["model"] == "z-ai/glm-5.2:free"
    assert result["rejected"][0]["reject_reason"] == "no_tool_calling"
    assert result["bypassed"] is False


def test_glm_52_free_stays_eligible_when_tools_are_not_required():
    chain = [{"model": "z-ai/glm-5.2:free", "provider": "openrouter"}]
    result = filter_chain(chain, {"min_context": 100_000})
    assert [entry["model"] for entry in result["eligible"]] == [
        "z-ai/glm-5.2:free"
    ]
    assert result["rejected"] == []


# ---------------------------------------------------------------------------
# order_chain
# ---------------------------------------------------------------------------

def test_order_chain_sequential_is_identity():
    chain = _chain()
    assert order_chain(chain, "sequential") == chain


def test_order_chain_sequential_returns_a_new_list():
    chain = _chain()
    assert order_chain(chain, "sequential") is not chain


def test_order_chain_never_mutates_its_input_list():
    chain = _chain()
    snapshot = list(chain)
    order_chain(chain, "random", pin_primary=False, rng=random.Random(7))
    order_chain(chain, "random", pin_primary=True, rng=random.Random(7))
    assert chain == snapshot


def test_order_chain_random_with_pin_primary_fixes_index_zero():
    chain = _chain()
    for seed in range(30):
        ordered = order_chain(
            chain, "random", pin_primary=True, rng=random.Random(seed)
        )
        assert ordered[0] is chain[0]
        assert sorted(e["model"] for e in ordered) == sorted(
            e["model"] for e in chain
        )


def test_order_chain_random_with_pin_primary_still_shuffles_the_tail():
    chain = _chain()
    tails = {
        tuple(
            e["model"]
            for e in order_chain(
                chain, "random", pin_primary=True, rng=random.Random(seed)
            )[1:]
        )
        for seed in range(30)
    }
    assert len(tails) > 1


def test_order_chain_random_without_pin_primary_can_move_index_zero():
    chain = _chain()
    moved = [
        seed
        for seed in range(30)
        if order_chain(
            chain, "random", pin_primary=False, rng=random.Random(seed)
        )[0]["model"]
        != chain[0]["model"]
    ]
    assert moved


def test_order_chain_random_with_rng_none_degrades_to_sequential():
    chain = _chain()
    assert order_chain(chain, "random", pin_primary=False, rng=None) == chain


def test_order_chain_unknown_strategy_degrades_to_sequential():
    chain = _chain()
    assert order_chain(chain, "round-robin", rng=random.Random(1)) == chain


def test_order_chain_is_deterministic_for_equally_seeded_rngs():
    chain = _chain()
    first = order_chain(chain, "random", pin_primary=False, rng=random.Random(1234))
    second = order_chain(chain, "random", pin_primary=False, rng=random.Random(1234))
    assert [e["model"] for e in first] == [e["model"] for e in second]


def test_order_chain_random_on_a_single_entry_chain_is_identity():
    chain = [{"model": "glm-4.7", "provider": "zai"}]
    assert order_chain(chain, "random", pin_primary=False, rng=random.Random(3)) == chain


# ---------------------------------------------------------------------------
# derive_requirements
# ---------------------------------------------------------------------------

def test_derive_requirements_applies_the_125_percent_safety_factor():
    reqs = derive_requirements({"est_input_tokens": 200_000})
    assert reqs == {"min_context": 250_000}


def test_derive_requirements_rounds_the_safety_factor_up():
    assert derive_requirements({"est_input_tokens": 3})["min_context"] == 4
    assert derive_requirements({"est_input_tokens": 1})["min_context"] == 2
    assert derive_requirements({"est_input_tokens": 4})["min_context"] == 5


def test_derive_requirements_ignores_zero_est_input_tokens():
    assert derive_requirements({"est_input_tokens": 0}) == {}


def test_derive_requirements_maps_the_boolean_signals():
    reqs = derive_requirements(
        {
            "needs_vision": True,
            "needs_tools": True,
            "needs_structured_output": True,
        }
    )
    assert reqs == {
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
    }


def test_derive_requirements_omits_false_boolean_signals():
    assert derive_requirements({"needs_vision": False, "needs_tools": False}) == {}


def test_derive_requirements_tier_floor_takes_the_max_min_context():
    reqs = derive_requirements(
        {"est_input_tokens": 100_000}, {"min_context": 400_000}
    )
    assert reqs["min_context"] == 400_000


def test_derive_requirements_keeps_the_derived_min_context_when_it_is_higher():
    reqs = derive_requirements(
        {"est_input_tokens": 800_000}, {"min_context": 400_000}
    )
    assert reqs["min_context"] == 1_000_000


def test_derive_requirements_tier_floor_wins_on_boolean_conflict():
    reqs = derive_requirements({"needs_vision": False}, {"vision": True})
    assert reqs["vision"] is True


def test_derive_requirements_only_emits_requirement_keys():
    reqs = derive_requirements(
        {"est_input_tokens": 10, "verb_class": "hard", "needs_vision": True},
        {"min_context": 1, "flavour": "spicy"},
    )
    assert set(reqs) <= REQUIREMENT_KEYS
    assert "flavour" not in reqs


def test_derive_requirements_on_an_empty_feature_vector_is_empty():
    assert derive_requirements({}) == {}


def test_derived_requirements_feed_straight_into_satisfies():
    reqs = derive_requirements({"est_input_tokens": 200_000})
    assert satisfies("glm-4.7", reqs) == (False, "context_too_small")
    assert satisfies("glm-5.3", reqs) == (True, "")


# ---------------------------------------------------------------------------
# upstream_group / independent_rails
# ---------------------------------------------------------------------------

def test_upstream_group_collapses_nous_into_openrouter():
    assert upstream_group("nous") == "openrouter"
    assert upstream_group("openrouter") == "openrouter"


def test_upstream_group_returns_other_providers_unchanged():
    assert upstream_group("zai") == "zai"
    assert upstream_group("deepseek") == "deepseek"
    assert upstream_group("moonshot") == "moonshot"


def test_upstream_group_of_a_missing_provider_is_empty():
    assert upstream_group("") == ""
    assert upstream_group(None) == ""


def test_independent_rails_counts_groups_not_providers():
    chain = [
        {"model": "meituan/longcat-2.0:free", "provider": "nous"},
        {"model": "openrouter/free", "provider": "openrouter"},
    ]
    assert independent_rails(chain) == 1


def test_independent_rails_counts_distinct_upstreams():
    chain = [
        {"model": "meituan/longcat-2.0:free", "provider": "nous"},
        {"model": "openrouter/free", "provider": "openrouter"},
        {"model": "glm-4.7", "provider": "zai"},
    ]
    assert independent_rails(chain) == 2


def test_independent_rails_of_an_empty_chain_is_zero():
    assert independent_rails([]) == 0


def test_independent_rails_ignores_hops_without_a_provider():
    chain = [{"model": "glm-4.7", "provider": "zai"}, {"model": "mystery-elo"}]
    assert independent_rails(chain) == 1


# ---------------------------------------------------------------------------
# F4 — commercial metadata must not make a model "known"
# ---------------------------------------------------------------------------

def test_capability_assertion_keys_is_the_documented_closed_set():
    assert CAPABILITY_ASSERTION_KEYS == frozenset(
        {
            "context_window",
            "max_input_tokens",
            "max_output",
            "vision",
            "tool_calling",
            "structured_output",
        }
    )


def test_billing_mode_alone_does_not_make_an_unknown_model_known():
    """F4: router.yaml mandates billing_mode on EVERY elo.

    Counting it as a capability declaration made the unknown-model warning and
    liveness's capabilities_known flag permanently dead.
    """
    assert (
        capabilities_for(
            "gpt-9-does-not-exist",
            {"model": "gpt-9-does-not-exist", "provider": "openai-codex",
             "billing_mode": "metered"},
        )
        is None
    )


def test_commercial_and_identity_metadata_alone_leaves_a_model_unknown():
    for declared in (
        {"provider": "openai-codex"},
        {"notes": "the one we always mean"},
        {"price_in": 1.0, "price_out": 2.0},
        {"price_windows": [{"hours_utc": [1, 4], "multiplier": 2.0}]},
        {"billing_mode": "plan", "notes": "x", "price_out": 3.0},
    ):
        assert capabilities_for("gpt-9-does-not-exist", declared) is None, declared


def test_one_real_capability_key_is_enough_to_make_a_model_known():
    caps = capabilities_for(
        "house-model", {"billing_mode": "metered", "context_window": 500_000}
    )
    # Known now — and the commercial field still merges, it just is not evidence.
    assert caps == {"billing_mode": "metered", "context_window": 500_000}


def test_billing_mode_only_hop_reports_capability_unknown():
    """The whole point of F4: the hop must still be flagged as unverifiable."""
    assert satisfies(
        "gpt-9-does-not-exist", {"vision": True}, {"billing_mode": "metered"}
    ) == (True, "capability_unknown")


def test_a_registry_model_is_still_known_with_only_commercial_overrides():
    caps = capabilities_for("glm-4.7", {"billing_mode": "metered"})
    assert caps["billing_mode"] == "metered"
    assert caps["context_window"] == 200_000


def test_filter_chain_lists_a_billing_mode_only_hop_as_unknown():
    chain = [
        {"model": "glm-4.6v", "provider": "zai", "billing_mode": "plan"},
        {"model": "gpt-9-does-not-exist", "provider": "openai-codex",
         "billing_mode": "metered"},
    ]
    result = filter_chain(chain, {"vision": True})
    assert result["unknown"] == ["gpt-9-does-not-exist"]


# ---------------------------------------------------------------------------
# F5 — the bypass must keep its diagnostics
# ---------------------------------------------------------------------------

def test_bypass_retains_the_per_elo_reject_reasons():
    """F5: 'nothing can meet this' is only actionable next to WHICH requirement."""
    chain = _chain()
    result = filter_chain(chain, {"min_context": 99_000_000})
    assert result["bypassed"] is True
    assert result["eligible"] == chain
    assert [(hop["model"], hop["reject_reason"]) for hop in result["rejected"]] == [
        ("glm-4.7", "context_too_small"),
        ("glm-4.6v", "context_too_small"),
        ("kimi-k3", "context_too_small"),
    ]


def test_bypass_diagnostics_are_not_excluded_from_eligible():
    """On the bypass path `rejected` is informational, NOT a removal list."""
    chain = _chain()
    result = filter_chain(chain, {"min_context": 99_000_000})
    rejected_models = {hop["model"] for hop in result["rejected"]}
    eligible_models = {hop["model"] for hop in result["eligible"]}
    assert rejected_models <= eligible_models


def test_bypass_rejected_copies_do_not_mutate_the_source_entries():
    chain = _chain()
    filter_chain(chain, {"min_context": 99_000_000})
    assert all("reject_reason" not in hop for hop in chain)


def test_the_500_file_refactor_reports_every_reason():
    """The reproduction from the review: 'refactor the auth module across 500
    files' -> est_input_tokens 2_000_012 -> min_context 2_500_015 -> every hop
    rejected. The console must be able to name the requirement nothing met.
    """
    requirements = derive_requirements({"est_input_tokens": 2_000_012})
    assert requirements["min_context"] == 2_500_015
    chain = [
        {"model": "glm-5.3", "provider": "zai"},
        {"model": "gpt-5.6-luna", "provider": "openai-codex"},
        {"model": "deepseek-v4-flash", "provider": "deepseek"},
    ]
    result = filter_chain(chain, requirements)
    assert result["bypassed"] is True
    assert len(result["rejected"]) == 3
    assert {hop["reject_reason"] for hop in result["rejected"]} == {
        "context_too_small"
    }
    assert result["unsatisfiable"] == ["min_context"]


def test_max_registered_context_is_the_biggest_window_in_the_registry():
    assert MAX_REGISTERED_CONTEXT == max(
        entry["context_window"] for entry in MODEL_CAPABILITIES.values()
    )
    assert MAX_REGISTERED_CONTEXT == 1_050_000


def test_an_ordinary_rejection_is_not_reported_as_unsatisfiable():
    result = filter_chain(_chain(), {"min_context": 500_000})
    assert result["unsatisfiable"] == []
    assert [hop["model"] for hop in result["eligible"]] == ["kimi-k3"]


def test_unsatisfiable_is_named_even_when_a_fail_open_hop_stays_eligible():
    """The requirement is pathological whether or not an unknown elo passes."""
    result = filter_chain(
        [{"model": "mystery-elo", "provider": "somewhere"}],
        {"min_context": 3_000_000},
    )
    assert result["bypassed"] is False
    assert result["unsatisfiable"] == ["min_context"]


def test_a_declared_bigger_window_makes_the_requirement_satisfiable_again():
    result = filter_chain(
        [{"model": "house-model", "provider": "local-rail",
          "context_window": 4_000_000}],
        {"min_context": 3_000_000},
    )
    assert result["unsatisfiable"] == []
    assert result["bypassed"] is False


# ---------------------------------------------------------------------------
# The clock is injected, never read
# ---------------------------------------------------------------------------

def test_capabilities_module_never_reads_the_wall_clock():
    """Load-bearing: reading the clock here would make every routing test flaky.

    Asserted over the AST rather than the text, so the module can still DISCUSS
    ``now()`` in its docstring while never calling it.
    """
    tree = ast.parse(inspect.getsource(caps_module))
    called = set()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                called.add(node.func.id)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])

    assert not called & {
        "now", "utcnow", "today", "monotonic", "time", "fromtimestamp", "open",
    }
    # No IO, no state, no network: the import list is the proof.
    assert imported <= {"__future__", "datetime", "random", "typing"}


def test_the_reference_clocks_are_the_weekdays_they_claim():
    assert _at(MON, 0).weekday() == 0
    assert _at(WED, 0).weekday() == 2
    assert _at(FRI, 0).weekday() == 4
    assert _at(SAT, 0).weekday() == 5
    assert _at(SUN, 0).weekday() == 6


# ---------------------------------------------------------------------------
# price_multiplier — half-open [start, end) boundaries
# ---------------------------------------------------------------------------

def test_deepseek_peak_boundaries_are_half_open():
    # 01:00-04:00 and 06:00-10:00 UTC, every day.
    expected = {
        0: 1.0, 1: 2.0, 2: 2.0, 3: 2.0, 4: 1.0, 5: 1.0,
        6: 2.0, 7: 2.0, 9: 2.0, 10: 1.0, 23: 1.0,
    }
    for hour, multiplier in expected.items():
        assert price_multiplier(
            "deepseek-v4-pro", _at(WED, hour)
        ) == multiplier, hour
        assert price_multiplier(
            "deepseek-v4-flash", _at(WED, hour)
        ) == multiplier, hour


def test_the_start_hour_is_inside_and_the_end_hour_is_outside():
    assert price_multiplier("deepseek-v4-pro", _at(WED, 6)) == 2.0
    assert price_multiplier("deepseek-v4-pro", _at(WED, 9, 59)) == 2.0
    assert price_multiplier("deepseek-v4-pro", _at(WED, 10)) == 1.0


def test_deepseek_peak_applies_on_the_weekend_too():
    assert price_multiplier("deepseek-v4-pro", _at(SAT, 7)) == 2.0
    assert price_multiplier("deepseek-v4-pro", _at(SUN, 2)) == 2.0


def test_xiaomi_night_window_is_a_discount_not_a_peak():
    assert price_multiplier("mimo-v2.5", _at(WED, 15)) == 1.0
    assert price_multiplier("mimo-v2.5", _at(WED, 16)) == 0.8
    assert price_multiplier("mimo-v2.5", _at(WED, 23)) == 0.8
    # end == 24 is midnight-exclusive: the next day's 00:00 is outside.
    assert price_multiplier("mimo-v2.5", _at(WED, 0)) == 1.0
    assert price_multiplier("mimo-v2.5-pro", _at(WED, 20)) == 0.8


def test_zai_peak_is_gated_to_weekdays():
    for day in (MON, WED, FRI):
        assert price_multiplier("glm-5.3", _at(day, 7)) == 2.0, day
    # A weekend hour that would otherwise match bills off-peak.
    assert price_multiplier("glm-5.3", _at(SAT, 7)) == 1.0
    assert price_multiplier("glm-5.3", _at(SUN, 9)) == 1.0


def test_every_plan_covered_zai_model_carries_the_weekday_peak():
    for model in ("glm-5.3", "glm-5-turbo", "glm-4.7", "glm-4.6v"):
        assert price_multiplier(model, _at(WED, 7)) == 2.0, model
        assert price_multiplier(model, _at(SAT, 7)) == 1.0, model


def test_a_metered_zai_model_has_no_window():
    assert price_multiplier("glm-4.6", _at(WED, 7)) == 1.0
    assert price_multiplier("glm-5.2", _at(WED, 7)) == 1.0


def test_the_two_primary_rails_peak_at_the_same_hour():
    """06:00-10:00 UTC is double price on deepseek AND zai simultaneously."""
    when = _at(WED, 7)
    assert price_multiplier("deepseek-v4-pro", when) == 2.0
    assert price_multiplier("glm-5.3", when) == 2.0


def test_when_none_is_one_point_zero_everywhere():
    for model in MODEL_CAPABILITIES:
        assert price_multiplier(model) == 1.0, model
        assert price_multiplier(model, None) == 1.0, model


def test_price_multiplier_of_an_unknown_model_is_one():
    assert price_multiplier("gpt-9-does-not-exist", _at(WED, 7)) == 1.0
    assert price_multiplier("", _at(WED, 7)) == 1.0


def test_a_flat_priced_model_is_one_at_every_hour():
    for hour in range(24):
        assert price_multiplier("kimi-k3", _at(WED, hour)) == 1.0


def test_an_aware_non_utc_clock_is_converted_to_utc():
    # 04:00 in UTC-03 is 07:00 UTC, inside both primary peaks.
    local = datetime(2026, 8, 19, 4, 0, tzinfo=timezone(timedelta(hours=-3)))
    assert price_multiplier("deepseek-v4-pro", local) == 2.0
    assert price_multiplier("glm-5.3", local) == 2.0


def test_a_naive_clock_is_assumed_to_be_utc():
    assert price_multiplier("deepseek-v4-pro", datetime(2026, 8, 19, 7, 0)) == 2.0
    assert price_multiplier("deepseek-v4-pro", datetime(2026, 8, 19, 12, 0)) == 1.0


def test_an_unusable_clock_is_treated_as_no_clock():
    for junk in ("07:00", 7, [], {}, object()):
        assert price_multiplier("deepseek-v4-pro", junk) == 1.0, junk


def test_a_declared_window_overrides_the_registry():
    # An operator correcting a stale window in YAML, no code change.
    declared = {"price_windows": [{"hours_utc": [12, 13], "multiplier": 3.0}]}
    assert price_multiplier("deepseek-v4-pro", _at(WED, 12), declared) == 3.0
    assert price_multiplier("deepseek-v4-pro", _at(WED, 7), declared) == 1.0


def test_a_malformed_declared_window_falls_back_to_the_base_rate():
    # start > end would need wrap-around arithmetic; it is a lint error instead.
    declared = {"price_windows": [{"hours_utc": [22, 3], "multiplier": 2.0}]}
    assert price_multiplier("kimi-k3", _at(WED, 23), declared) == 1.0
    assert price_window_diagnostics("kimi-k3", declared["price_windows"])


def test_price_multiplier_never_mutates_the_registry():
    declared = {"price_windows": [{"hours_utc": [0, 1], "multiplier": 5.0}]}
    price_multiplier("deepseek-v4-pro", _at(WED, 0), declared)
    assert MODEL_CAPABILITIES["deepseek-v4-pro"]["price_windows"] == [
        {"hours_utc": [1, 4], "multiplier": 2.0},
        {"hours_utc": [6, 10], "multiplier": 2.0},
    ]


def test_a_merged_view_cannot_mutate_the_registry_windows():
    caps = capabilities_for("mimo-v2.5")
    caps["price_windows"][0]["multiplier"] = 99.0
    assert MODEL_CAPABILITIES["mimo-v2.5"]["price_windows"][0]["multiplier"] == 0.8


# ---------------------------------------------------------------------------
# effective_price — a None price is never 0.0
# ---------------------------------------------------------------------------

def test_effective_price_scales_the_base_rate_inside_a_peak():
    assert effective_price("deepseek-v4-pro", _at(WED, 7)) == pytest.approx(
        (1.32, 3.96)
    )
    assert effective_price("deepseek-v4-pro", _at(WED, 12)) == pytest.approx(
        (0.66, 1.98)
    )


def test_effective_price_scales_the_base_rate_inside_a_discount():
    assert effective_price("mimo-v2.5", _at(WED, 20)) == pytest.approx(
        (0.112, 0.224)
    )
    assert effective_price("mimo-v2.5", _at(WED, 12)) == pytest.approx(
        (0.14, 0.28)
    )


def test_effective_price_of_a_plan_model_is_none_not_zero():
    """glm-5.3 publishes no dollar price. A plan model is NOT free."""
    for when in (None, _at(WED, 7), _at(SAT, 7)):
        assert effective_price("glm-5.3", when) is None
    assert effective_price("glm-5.3", _at(WED, 7)) != (0.0, 0.0)


def test_effective_price_of_an_unpriced_metered_model_is_none():
    assert effective_price("glm-4.5-flash", _at(WED, 7)) is None


def test_effective_price_of_an_unknown_model_is_none():
    assert effective_price("gpt-9-does-not-exist", _at(WED, 7)) is None


def test_a_published_zero_price_is_a_price():
    assert effective_price("glm-4.7-flash", _at(WED, 7)) == (0.0, 0.0)


def test_half_a_published_price_pair_is_no_price():
    """The missing half would have to be invented — and inventing it as 0.0 is
    exactly the coercion the design forbids."""
    assert effective_price(
        "glm-4.5-flash", _at(WED, 7), {"price_in": 1.0}
    ) is None


def test_effective_price_with_no_clock_is_the_base_rate():
    assert effective_price("deepseek-v4-pro") == pytest.approx((0.66, 1.98))
    assert effective_price("mimo-v2.5", None) == pytest.approx((0.14, 0.28))


def test_zai_peak_scales_a_plan_model_that_does_publish_dollars():
    assert effective_price("glm-5-turbo", _at(WED, 7)) == pytest.approx(
        (2.40, 8.00)
    )
    assert effective_price("glm-5-turbo", _at(SAT, 7)) == pytest.approx(
        (1.20, 4.00)
    )


# ---------------------------------------------------------------------------
# in_expensive_window
# ---------------------------------------------------------------------------

def test_in_expensive_window_only_for_a_multiplier_above_one():
    assert in_expensive_window("deepseek-v4-pro", _at(WED, 7)) is True
    assert in_expensive_window("deepseek-v4-pro", _at(WED, 12)) is False
    assert in_expensive_window("glm-5.3", _at(WED, 7)) is True
    assert in_expensive_window("glm-5.3", _at(SAT, 7)) is False


def test_a_cheap_window_is_not_an_expensive_one():
    assert price_multiplier("mimo-v2.5", _at(WED, 20)) == 0.8
    assert in_expensive_window("mimo-v2.5", _at(WED, 20)) is False


def test_in_expensive_window_without_a_clock_is_false():
    assert in_expensive_window("deepseek-v4-pro") is False
    assert in_expensive_window("gpt-9-does-not-exist", _at(WED, 7)) is False


# ---------------------------------------------------------------------------
# next_window_change
# ---------------------------------------------------------------------------

def test_next_window_change_reports_the_end_of_a_peak():
    assert next_window_change("deepseek-v4-pro", _at(WED, 3)) == 4
    assert next_window_change("deepseek-v4-pro", _at(WED, 7)) == 10


def test_next_window_change_reports_the_start_of_the_next_peak():
    assert next_window_change("deepseek-v4-pro", _at(WED, 5)) == 6


def test_next_window_change_crosses_the_day_boundary():
    # Off-peak from 10:00; the next change is 01:00 TOMORROW.
    assert next_window_change("deepseek-v4-pro", _at(WED, 10)) == 1
    assert next_window_change("deepseek-v4-pro", _at(WED, 23)) == 1
    # Inside the 16:00-00:00 discount, the change is midnight.
    assert next_window_change("mimo-v2.5", _at(WED, 20)) == 0
    assert next_window_change("mimo-v2.5", _at(WED, 23, 59)) == 0


def test_next_window_change_crosses_the_weekend():
    """Friday evening: the next change is MONDAY 06:00, three days out."""
    assert next_window_change("glm-5.3", _at(FRI, 20)) == 6
    assert next_window_change("glm-5.3", _at(SAT, 7)) == 6
    assert next_window_change("glm-5.3", _at(SUN, 23)) == 6


def test_next_window_change_of_a_flat_model_is_none():
    assert next_window_change("kimi-k3", _at(WED, 7)) is None
    assert next_window_change("gpt-5.6-luna", _at(WED, 7)) is None


def test_next_window_change_without_a_clock_or_model_is_none():
    assert next_window_change("deepseek-v4-pro") is None
    assert next_window_change("gpt-9-does-not-exist", _at(WED, 7)) is None


def test_next_window_change_of_an_all_hours_window_is_none():
    """A window covering every hour at one multiplier never changes."""
    declared = {"price_windows": [{"hours_utc": [0, 24], "multiplier": 2.0}]}
    assert price_multiplier("kimi-k3", _at(WED, 13), declared) == 2.0
    assert next_window_change("kimi-k3", _at(WED, 13), declared) is None


# ---------------------------------------------------------------------------
# order_chain — cheapest_now
# ---------------------------------------------------------------------------

def _priced_chain():
    return [
        {"model": "kimi-k3", "provider": "moonshot"},          # out 15.00
        {"model": "mimo-v2.5", "provider": "xiaomi"},          # out 0.28
        {"model": "gpt-5.6-luna", "provider": "openai-codex"},  # out 1.20
    ]


def test_cheapest_now_orders_by_effective_output_price():
    ordered = order_chain(
        _priced_chain(), "cheapest_now", pin_primary=False, when=_at(WED, 12)
    )
    assert [hop["model"] for hop in ordered] == [
        "mimo-v2.5", "gpt-5.6-luna", "kimi-k3",
    ]


def test_cheapest_now_reorders_when_a_peak_moves_a_price():
    chain = [
        {"model": "deepseek-v4-flash", "provider": "deepseek"},  # 0.66 -> 1.32
        {"model": "gpt-5.6-luna", "provider": "openai-codex"},   # 1.20 flat
    ]
    off_peak = order_chain(chain, "cheapest_now", pin_primary=False,
                           when=_at(WED, 12))
    assert [hop["model"] for hop in off_peak] == [
        "deepseek-v4-flash", "gpt-5.6-luna",
    ]
    peak = order_chain(chain, "cheapest_now", pin_primary=False,
                       when=_at(WED, 7))
    assert [hop["model"] for hop in peak] == [
        "gpt-5.6-luna", "deepseek-v4-flash",
    ]


def test_cheapest_now_ties_keep_declared_order():
    # glm-4.7 and glm-4.6 both bill 2.20 out, and neither is in a peak on a
    # Saturday — so the operator's declared order is what survives.
    when = _at(SAT, 7)
    first = [{"model": "glm-4.7", "provider": "zai"},
             {"model": "glm-4.6", "provider": "zai"}]
    second = [{"model": "glm-4.6", "provider": "zai"},
              {"model": "glm-4.7", "provider": "zai"}]
    assert effective_price("glm-4.7", when)[1] == effective_price(
        "glm-4.6", when
    )[1]
    assert [hop["model"] for hop in order_chain(
        first, "cheapest_now", pin_primary=False, when=when)] == [
        "glm-4.7", "glm-4.6"]
    assert [hop["model"] for hop in order_chain(
        second, "cheapest_now", pin_primary=False, when=when)] == [
        "glm-4.6", "glm-4.7"]


def test_cheapest_now_places_an_unpriced_plan_model_by_billing_rank():
    """glm-5.3 has no dollar price. Treated as 0.0 it would merely TIE with the
    free rail and keep declared order; by billing rank it sorts ahead of it.
    """
    chain = [
        {"model": "glm-4.7-flash", "provider": "zai"},  # free, published 0.00
        {"model": "glm-5.3", "provider": "zai"},        # plan, price None
    ]
    ordered = order_chain(chain, "cheapest_now", pin_primary=False,
                          when=_at(SAT, 7))
    assert [hop["model"] for hop in ordered] == ["glm-5.3", "glm-4.7-flash"]


def test_cheapest_now_sorts_an_unpriced_metered_model_last():
    """An unpublished METERED price is a cost risk, not a freebie."""
    chain = [
        {"model": "glm-4.5-flash", "provider": "zai"},   # metered, price None
        {"model": "kimi-k3", "provider": "moonshot"},    # out 15.00
        {"model": "mimo-v2.5", "provider": "xiaomi"},    # out 0.28
    ]
    ordered = order_chain(chain, "cheapest_now", pin_primary=False,
                          when=_at(WED, 12))
    assert [hop["model"] for hop in ordered] == [
        "mimo-v2.5", "kimi-k3", "glm-4.5-flash",
    ]


def test_cheapest_now_ranks_plan_ahead_of_priced_ahead_of_unpriced_metered():
    chain = [
        {"model": "glm-4.5-flash", "provider": "zai"},  # unpriced metered
        {"model": "kimi-k3", "provider": "moonshot"},   # priced
        {"model": "glm-5.3", "provider": "zai"},        # unpriced plan
    ]
    ordered = order_chain(chain, "cheapest_now", pin_primary=False,
                          when=_at(SAT, 7))
    assert [hop["model"] for hop in ordered] == [
        "glm-5.3", "kimi-k3", "glm-4.5-flash",
    ]


def test_cheapest_now_with_no_clock_degrades_to_sequential():
    chain = _priced_chain()
    assert order_chain(chain, "cheapest_now", pin_primary=False) == chain
    assert order_chain(chain, "cheapest_now", pin_primary=False, when=None) == chain


def test_cheapest_now_with_pin_primary_reorders_the_tail_only():
    chain = _priced_chain()
    ordered = order_chain(
        chain, "cheapest_now", pin_primary=True, when=_at(WED, 12)
    )
    assert ordered[0] is chain[0]
    assert [hop["model"] for hop in ordered] == [
        "kimi-k3", "mimo-v2.5", "gpt-5.6-luna",
    ]


def test_cheapest_now_returns_a_new_list_and_never_mutates_the_input():
    chain = _priced_chain()
    snapshot = list(chain)
    ordered = order_chain(chain, "cheapest_now", pin_primary=False,
                          when=_at(WED, 12))
    assert ordered is not chain
    assert chain == snapshot


def test_cheapest_now_keeps_the_original_entry_objects():
    chain = _priced_chain()
    ordered = order_chain(chain, "cheapest_now", pin_primary=False,
                          when=_at(WED, 12))
    assert {id(hop) for hop in ordered} == {id(hop) for hop in chain}


def test_cheapest_now_on_a_single_entry_chain_is_identity():
    chain = [{"model": "kimi-k3", "provider": "moonshot"}]
    assert order_chain(chain, "cheapest_now", pin_primary=False,
                       when=_at(WED, 12)) == chain


def test_cheapest_now_tolerates_junk_entries():
    chain = [{"model": "kimi-k3", "provider": "moonshot"}, "not-a-hop",
             {"model": "mimo-v2.5", "provider": "xiaomi"}]
    ordered = order_chain(chain, "cheapest_now", pin_primary=False,
                          when=_at(WED, 12))
    assert len(ordered) == 3
    assert ordered[0]["model"] == "mimo-v2.5"


def test_a_clock_does_not_change_the_other_strategies():
    chain = _priced_chain()
    assert order_chain(chain, "sequential", when=_at(WED, 7)) == chain
    assert order_chain(chain, "round-robin", when=_at(WED, 7)) == chain
    seeded = order_chain(chain, "random", pin_primary=False,
                         rng=random.Random(5), when=_at(WED, 7))
    unclocked = order_chain(chain, "random", pin_primary=False,
                            rng=random.Random(5))
    assert [hop["model"] for hop in seeded] == [
        hop["model"] for hop in unclocked
    ]


def test_fallback_strategies_is_the_documented_closed_set():
    assert FALLBACK_STRATEGIES == frozenset(
        {"sequential", "random", "cheapest_now"}
    )


# ---------------------------------------------------------------------------
# apply_time_policy
# ---------------------------------------------------------------------------

def _mixed_chain():
    return [
        {"model": "deepseek-v4-pro", "provider": "deepseek"},
        {"model": "glm-5.3", "provider": "zai"},
        {"model": "gpt-5.6-luna", "provider": "openai-codex"},
        {"model": "mimo-v2.5", "provider": "xiaomi"},
    ]


def test_avoid_peak_demotes_without_removing():
    chain = _mixed_chain()
    result = apply_time_policy(chain, {"avoid_peak": ["deepseek"]}, _at(WED, 7))
    assert [hop["model"] for hop in result["chain"]] == [
        "glm-5.3", "gpt-5.6-luna", "mimo-v2.5", "deepseek-v4-pro",
    ]
    assert result["demoted"] == ["deepseek-v4-pro"]
    assert result["promoted"] == []
    assert len(result["chain"]) == len(chain)


def test_avoid_peak_preserves_relative_order_among_the_demoted():
    chain = [
        {"model": "deepseek-v4-pro", "provider": "deepseek"},
        {"model": "gpt-5.6-luna", "provider": "openai-codex"},
        {"model": "deepseek-v4-flash", "provider": "deepseek"},
    ]
    result = apply_time_policy(chain, {"avoid_peak": ["deepseek"]}, _at(WED, 7))
    assert [hop["model"] for hop in result["chain"]] == [
        "gpt-5.6-luna", "deepseek-v4-pro", "deepseek-v4-flash",
    ]


def test_avoid_peak_does_nothing_outside_the_window():
    chain = _mixed_chain()
    result = apply_time_policy(
        chain, {"avoid_peak": ["deepseek", "zai"]}, _at(WED, 12)
    )
    assert [hop["model"] for hop in result["chain"]] == [
        hop["model"] for hop in chain
    ]
    assert result["demoted"] == []


def test_avoid_peak_matches_provider_names_case_insensitively():
    result = apply_time_policy(
        _mixed_chain(), {"avoid_peak": ["  DeepSeek "]}, _at(WED, 7)
    )
    assert result["demoted"] == ["deepseek-v4-pro"]


def test_avoid_peak_ignores_a_provider_that_is_not_in_the_chain():
    result = apply_time_policy(
        _mixed_chain(), {"avoid_peak": ["anthropic"]}, _at(WED, 7)
    )
    assert result["demoted"] == []


def test_prefer_promotes_a_model_that_is_not_in_an_expensive_window():
    result = apply_time_policy(
        _mixed_chain(), {"prefer": ["mimo-v2.5"]}, _at(WED, 7)
    )
    assert [hop["model"] for hop in result["chain"]][0] == "mimo-v2.5"
    assert result["promoted"] == ["mimo-v2.5"]


def test_prefer_does_not_promote_a_model_inside_its_own_expensive_window():
    """Promoting an elo into its own peak would invert the intent."""
    chain = _mixed_chain()
    result = apply_time_policy(chain, {"prefer": ["glm-5.3"]}, _at(WED, 7))
    assert result["promoted"] == []
    assert [hop["model"] for hop in result["chain"]] == [
        hop["model"] for hop in chain
    ]
    # ...and the same policy DOES promote it once the peak is over.
    weekend = apply_time_policy(chain, {"prefer": ["glm-5.3"]}, _at(SAT, 7))
    assert weekend["promoted"] == ["glm-5.3"]
    assert weekend["chain"][0]["model"] == "glm-5.3"


def test_prefer_matches_model_ids_exactly():
    chain = [{"model": "MiniMax-M3", "provider": "minimax"},
             {"model": "kimi-k3", "provider": "moonshot"}]
    assert apply_time_policy(
        chain, {"prefer": ["minimax-m3"]}, _at(WED, 7)
    )["promoted"] == []
    assert apply_time_policy(
        chain, {"prefer": ["MiniMax-M3"]}, _at(WED, 7)
    )["promoted"] == ["MiniMax-M3"]


def test_time_policy_without_a_clock_is_a_no_op():
    chain = _mixed_chain()
    result = apply_time_policy(
        chain, {"avoid_peak": ["deepseek", "zai"], "prefer": ["mimo-v2.5"]}
    )
    assert [hop["model"] for hop in result["chain"]] == [
        hop["model"] for hop in chain
    ]
    assert result["demoted"] == []
    assert result["promoted"] == []


def test_time_policy_tolerates_junk():
    chain = _mixed_chain()
    for policy in (None, [], "avoid everything", {"avoid_peak": "deepseek"},
                   {"prefer": 3}, {}):
        result = apply_time_policy(chain, policy, _at(WED, 7))
        assert [hop["model"] for hop in result["chain"]] == [
            hop["model"] for hop in chain
        ], policy


def test_time_policy_never_mutates_its_input_list():
    chain = _mixed_chain()
    snapshot = [hop["model"] for hop in chain]
    apply_time_policy(
        chain, {"avoid_peak": ["deepseek", "zai"], "prefer": ["mimo-v2.5"]},
        _at(WED, 7),
    )
    assert [hop["model"] for hop in chain] == snapshot


def test_time_policy_on_an_empty_chain_is_empty():
    assert apply_time_policy([], {"avoid_peak": ["zai"]}, _at(WED, 7)) == {
        "chain": [], "demoted": [], "promoted": [],
    }


def test_both_primary_rails_demoted_at_once_still_leaves_a_chain():
    """The real case the feature exists for: 07:00 UTC on a Wednesday is peak on
    deepseek AND zai simultaneously, so avoid_peak names both — and the chain
    must not empty out. A non-peak elo rises to the front instead.
    """
    chain = _mixed_chain()
    result = apply_time_policy(
        chain, {"avoid_peak": ["deepseek", "zai"]}, _at(WED, 7)
    )
    assert result["chain"], "a cost policy must never empty a chain"
    assert len(result["chain"]) == len(chain)
    assert result["chain"][0]["model"] == "gpt-5.6-luna"
    assert not in_expensive_window(result["chain"][0]["model"], _at(WED, 7))
    assert result["demoted"] == ["deepseek-v4-pro", "glm-5.3"]
    assert [hop["model"] for hop in result["chain"]] == [
        "gpt-5.6-luna", "mimo-v2.5", "deepseek-v4-pro", "glm-5.3",
    ]


def test_a_chain_of_nothing_but_peaking_rails_keeps_every_hop():
    chain = [
        {"model": "deepseek-v4-pro", "provider": "deepseek"},
        {"model": "glm-5.3", "provider": "zai"},
    ]
    result = apply_time_policy(
        chain, {"avoid_peak": ["deepseek", "zai"]}, _at(WED, 7)
    )
    assert [hop["model"] for hop in result["chain"]] == [
        "deepseek-v4-pro", "glm-5.3",
    ]
    assert result["demoted"] == ["deepseek-v4-pro", "glm-5.3"]


# ---------------------------------------------------------------------------
# apply_time_cap
# ---------------------------------------------------------------------------

def test_time_cap_excludes_an_over_cap_elo():
    chain = _mixed_chain()
    result = apply_time_cap(chain, 1.5, _at(WED, 7))
    assert [hop["model"] for hop in result["chain"]] == [
        "gpt-5.6-luna", "mimo-v2.5",
    ]
    assert result["capped"] == [
        {"model": "deepseek-v4-pro", "multiplier": 2.0},
        {"model": "glm-5.3", "multiplier": 2.0},
    ]
    assert result["bypassed"] is False


def test_time_cap_is_a_ceiling_not_a_strict_bound():
    result = apply_time_cap(_mixed_chain(), 2.0, _at(WED, 7))
    assert [hop["model"] for hop in result["chain"]] == [
        "deepseek-v4-pro", "glm-5.3", "gpt-5.6-luna", "mimo-v2.5",
    ]
    assert result["capped"] == []


def test_time_cap_bypasses_rather_than_emptying_the_chain():
    """A cost control must never be able to cause an outage."""
    chain = [
        {"model": "deepseek-v4-pro", "provider": "deepseek"},
        {"model": "glm-5.3", "provider": "zai"},
    ]
    result = apply_time_cap(chain, 1.5, _at(WED, 7))
    assert result["bypassed"] is True
    assert [hop["model"] for hop in result["chain"]] == [
        "deepseek-v4-pro", "glm-5.3",
    ]
    # Diagnostics are RETAINED on the bypass path, same as the capability filter.
    assert result["capped"] == [
        {"model": "deepseek-v4-pro", "multiplier": 2.0},
        {"model": "glm-5.3", "multiplier": 2.0},
    ]


def test_time_cap_without_a_clock_is_no_cap():
    chain = _mixed_chain()
    result = apply_time_cap(chain, 1.0)
    assert [hop["model"] for hop in result["chain"]] == [
        hop["model"] for hop in chain
    ]
    assert result["capped"] == []
    assert result["bypassed"] is False


def test_an_absent_or_junk_max_multiplier_is_no_cap():
    chain = _mixed_chain()
    for cap in (None, "loads", True, [2.0]):
        result = apply_time_cap(chain, cap, _at(WED, 7))
        assert [hop["model"] for hop in result["chain"]] == [
            hop["model"] for hop in chain
        ], cap
        assert result["capped"] == [], cap


def test_time_cap_never_touches_a_cheap_window():
    result = apply_time_cap(
        [{"model": "mimo-v2.5", "provider": "xiaomi"},
         {"model": "kimi-k3", "provider": "moonshot"}],
        1.0, _at(WED, 20),
    )
    assert [hop["model"] for hop in result["chain"]] == [
        "mimo-v2.5", "kimi-k3",
    ]
    assert result["capped"] == []


def test_time_cap_never_mutates_its_input_list():
    chain = _mixed_chain()
    snapshot = [hop["model"] for hop in chain]
    apply_time_cap(chain, 1.5, _at(WED, 7))
    assert [hop["model"] for hop in chain] == snapshot


def test_time_cap_on_an_empty_chain_is_empty():
    assert apply_time_cap([], 1.5, _at(WED, 7)) == {
        "chain": [], "capped": [], "bypassed": False,
    }


def test_time_cap_keeps_a_hop_it_cannot_price():
    result = apply_time_cap(
        [{"model": "mystery-elo", "provider": "somewhere"}, "junk"],
        1.0, _at(WED, 7),
    )
    assert len(result["chain"]) == 2
    assert result["capped"] == []


# ---------------------------------------------------------------------------
# registry / window diagnostics  (F14: this must be callable from lint)
# ---------------------------------------------------------------------------

def test_every_diagnostic_is_shaped_for_lint_to_append_verbatim(monkeypatch):
    monkeypatch.setitem(
        caps_module.MODEL_CAPABILITIES, "broken-elo", {"provider": "nowhere"}
    )
    problems = registry_diagnostics()
    assert problems
    assert all(problem.startswith("model '") for problem in problems)
    assert any(
        problem.startswith("model 'broken-elo': ") for problem in problems
    )


def test_registry_diagnostics_reports_a_bad_window_without_raising(monkeypatch):
    monkeypatch.setitem(
        caps_module.MODEL_CAPABILITIES,
        "wrapping-elo",
        {
            "provider": "nowhere", "context_window": 1000,
            "billing_mode": "metered", "vision": False,
            "tool_calling": True, "structured_output": True,
            "price_windows": [{"hours_utc": [22, 3], "multiplier": 2.0}],
        },
    )
    problems = registry_diagnostics()
    assert any("'hours_utc'" in problem for problem in problems)


def test_price_window_diagnostics_accepts_absent_windows():
    assert price_window_diagnostics("kimi-k3", None) == []


def test_price_window_diagnostics_accepts_every_shipped_window():
    for model, entry in MODEL_CAPABILITIES.items():
        assert price_window_diagnostics(
            model, entry.get("price_windows")
        ) == [], model


def test_price_window_diagnostics_rejects_a_midnight_crossing_window():
    problems = price_window_diagnostics(
        "x", [{"hours_utc": [22, 3], "multiplier": 2.0}]
    )
    assert len(problems) == 1
    assert "two entries" in problems[0]


def test_price_window_diagnostics_rejects_out_of_range_hours():
    assert price_window_diagnostics(
        "x", [{"hours_utc": [6, 25], "multiplier": 2.0}]
    )
    assert price_window_diagnostics(
        "x", [{"hours_utc": [-1, 6], "multiplier": 2.0}]
    )
    # 24 is the legal midnight-exclusive end.
    assert price_window_diagnostics(
        "x", [{"hours_utc": [16, 24], "multiplier": 0.8}]
    ) == []


def test_price_window_diagnostics_reports_overlapping_windows():
    problems = price_window_diagnostics(
        "x",
        [{"hours_utc": [6, 10], "multiplier": 2.0},
         {"hours_utc": [9, 12], "multiplier": 3.0}],
    )
    assert problems == ["model 'x': price_windows entries overlap"]


def test_adjacent_half_open_windows_do_not_overlap():
    assert price_window_diagnostics(
        "x",
        [{"hours_utc": [1, 4], "multiplier": 2.0},
         {"hours_utc": [4, 6], "multiplier": 3.0}],
    ) == []


def test_windows_on_disjoint_weekdays_do_not_overlap():
    assert price_window_diagnostics(
        "x",
        [{"hours_utc": [6, 10], "weekdays": [0, 1, 2, 3, 4], "multiplier": 2.0},
         {"hours_utc": [6, 10], "weekdays": [5, 6], "multiplier": 0.5}],
    ) == []


def test_price_window_diagnostics_rejects_bad_weekdays_and_multipliers():
    assert price_window_diagnostics(
        "x", [{"hours_utc": [6, 10], "weekdays": [7], "multiplier": 2.0}]
    )
    assert price_window_diagnostics(
        "x", [{"hours_utc": [6, 10], "weekdays": "monday", "multiplier": 2.0}]
    )
    assert price_window_diagnostics(
        "x", [{"hours_utc": [6, 10], "multiplier": 0}]
    )
    assert price_window_diagnostics(
        "x", [{"hours_utc": [6, 10], "multiplier": "double"}]
    )


def test_price_window_diagnostics_rejects_a_malformed_container():
    assert price_window_diagnostics("x", {"hours_utc": [6, 10]}) == [
        "model 'x': price_windows must be a list"
    ]
    assert price_window_diagnostics("x", ["06:00-10:00"])


def test_a_weekday_gated_window_is_inert_when_the_gate_is_malformed():
    declared = {
        "price_windows": [
            {"hours_utc": [6, 10], "weekdays": ["monday"], "multiplier": 2.0}
        ]
    }
    assert price_multiplier("kimi-k3", _at(MON, 7), declared) == 1.0


def test_a_price_declared_without_any_capability_stays_unknown():
    """One gate, not two: F4's rule decides pricing visibility as well."""
    declared = {"price_in": 1.0, "price_out": 2.0, "billing_mode": "metered"}
    assert effective_price("house-model", _at(WED, 7), declared) is None
    # Declaring one real capability makes the declared price usable.
    with_capability = dict(declared, context_window=500_000)
    assert effective_price(
        "house-model", _at(WED, 7), with_capability
    ) == (1.0, 2.0)


def test_avoid_peak_is_per_elo_not_per_provider():
    """A same-provider elo with FLAT pricing costs no more, so it is not demoted.

    glm-5.3 bills plan credits at 2x during the weekday peak; metered glm-4.6 is
    flat at every hour. Demoting glm-4.6 would degrade the route and save nothing.
    """
    chain = [
        {"model": "glm-5.3", "provider": "zai"},
        {"model": "glm-4.6", "provider": "zai"},
        {"model": "gpt-5.6-luna", "provider": "openai-codex"},
    ]
    result = apply_time_policy(chain, {"avoid_peak": ["zai"]}, _at(WED, 7))
    assert result["demoted"] == ["glm-5.3"]
    assert [hop["model"] for hop in result["chain"]] == [
        "glm-4.6", "gpt-5.6-luna", "glm-5.3",
    ]


def test_cheapest_now_sorts_an_undescribable_elo_last_without_dropping_it():
    """Ordering is not eligibility: an elo we cannot price still gets a slot."""
    chain = [
        {"model": "ghost-model", "provider": "other-rail",
         "billing_mode": "plan"},
        {"model": "kimi-k3", "provider": "moonshot"},
    ]
    ordered = order_chain(chain, "cheapest_now", pin_primary=False,
                          when=_at(WED, 12))
    assert [hop["model"] for hop in ordered] == ["kimi-k3", "ghost-model"]


def test_time_cap_bypasses_when_only_unnamed_hops_would_survive():
    """A chain of hops nothing can name is not a route, so the cap gives way."""
    result = apply_time_cap(
        [{"model": "glm-5.3", "provider": "zai"}, {"provider": "zai"}],
        1.0, _at(WED, 7),
    )
    assert result["bypassed"] is True
    assert [hop.get("model") for hop in result["chain"]] == ["glm-5.3", None]
    assert result["capped"] == [{"model": "glm-5.3", "multiplier": 2.0}]
