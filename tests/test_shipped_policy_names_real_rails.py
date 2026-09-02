"""The shipped policy must name rails the vendor actually runs, and say so once.

WHY THIS FILE EXISTS. On 2026-08-26 z.ai dropped glm-4.7 and glm-5-turbo from the
GLM Coding Plan's credit table and started auto-routing both ids to
glm-5.3-flash. Nothing broke: every request succeeded, glm-5.3-flash answered, and
the trace, the decision log and the console all reported glm-4.7 — the id nobody
ran. `router/price_watch.py` did not catch it either, because its zai anchor is the
peak-hours clause and that clause did not change; the supported-models section did.

A silent substitution is the worst shape a policy defect can take: there is no
error to notice, and every downstream surface confidently states the wrong model.
So the registry's alias notes are read here as a contract, and the shipped policy
is checked against them.

The three coherence assertions below are the ones router.example.yaml asks for in
prose ("Keep it in step with T1 by hand", "Regenerate it whenever `tiers` changes",
"Keep these two keys byte-equal to chain[0]"). A comment cannot fail; these can.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from router.capabilities import MODEL_CAPABILITIES

#: The phrase the registry uses to mark an id the vendor silently substitutes.
#: Read as a contract rather than restated: an entry that rewords it stops being
#: matched here, so the assertion that the phrase EXISTS comes first.
_ALIAS_NOTE = re.compile(r"plan auto-routes this id to (\S+)")

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _policy() -> dict:
    """The shipped example, not the live file.

    router.yaml is gitignored and seeded from this on first load, so the example
    is what every install starts from and the only one a repo test can pin.
    """
    return yaml.safe_load(
        (_ROOT / "router.example.yaml").read_text(encoding="utf-8")
    )


def _chain(entry: dict) -> list[tuple[str, str]]:
    """(model, provider) for a primary plus its declared fallback hops."""
    hops = [(entry.get("model"), entry.get("provider"))]
    for hop in entry.get("fallback") or []:
        hops.append((hop.get("model"), hop.get("provider")))
    return hops


def _every_named_elo(policy: dict) -> list[tuple[str, str]]:
    """Every (model, provider) the policy dispatches on, from every surface.

    Deliberately includes the classifier and fail_safe: the classifier sits on the
    critical path of every delegation and fail_safe is the last resort, so an alias
    hiding in either is worse than one in a tier, not better.
    """
    named: list[tuple[str, str]] = []
    for tier in (policy.get("tiers") or {}).values():
        named.extend(_chain(tier))
    named.extend(_chain(policy.get("fail_safe") or {}))
    classifier = policy.get("classifier") or {}
    named.append((classifier.get("model"), classifier.get("provider")))
    for hop in classifier.get("chain") or []:
        named.append((hop.get("model"), hop.get("provider")))
    compaction = policy.get("compaction") or {}
    named.append((compaction.get("model"), compaction.get("provider")))
    return [pair for pair in named if pair[0]]


def test_the_registry_still_marks_its_aliases_the_way_this_file_reads_them():
    """The contract first: if the phrase is gone, every test below goes quiet."""
    aliases = {
        model: match.group(1)
        for model, entry in MODEL_CAPABILITIES.items()
        if (match := _ALIAS_NOTE.search(str(entry.get("notes") or "")))
    }
    assert aliases, (
        "no registry entry marks itself as an id the plan auto-routes. Either the "
        "vendor stopped substituting ids — check before believing that — or the "
        "note was reworded and the alias guard below now passes vacuously."
    )
    # The four the vendor substitutes today, and what each becomes.
    assert aliases == {
        "glm-5.2": "glm-5.3",
        "glm-5.1": "glm-5.3",
        "glm-4.7": "glm-5.3-flash",
        "glm-5-turbo": "glm-5.3-flash",
    }
    # And every target is itself a real entry: an alias pointing at nothing would
    # leave an operator with no id to move to.
    for target in aliases.values():
        assert target in MODEL_CAPABILITIES, target


def test_the_shipped_policy_names_no_id_the_vendor_silently_substitutes():
    """The defect this file was written for, in one assertion.

    glm-4.7 sat in FOUR places here — T1's primary, the classifier pair, fail_safe
    and blocklist.fallback_chain — after the plan stopped serving it.
    """
    policy = _policy()
    aliased = {
        model
        for model, entry in MODEL_CAPABILITIES.items()
        if _ALIAS_NOTE.search(str(entry.get("notes") or ""))
    }
    offenders = sorted(
        {model for model, _ in _every_named_elo(policy) if model in aliased}
    )
    assert offenders == [], (
        f"the policy names {offenders}, which the plan auto-routes to another "
        f"model: the request will succeed and every trace will report the id that "
        f"did not run"
    )
    # blocklist.fallback_chain is model-only, so it is checked separately rather
    # than left out — Blocklist.fallback_for() walks it positionally to pick a
    # replacement rail, which is exactly where a phantom id does the most damage.
    banned = set(policy["blocklist"]["fallback_chain"]) & aliased
    assert banned == set(), banned


def test_every_elo_the_policy_names_is_known_to_the_registry():
    """An unknown id asserts no capability, so the filter cannot protect it.

    `filter_chain` keeps unknown models eligible on purpose (a missing entry must
    not empty a chain), which means a typo in a model name routes traffic instead
    of failing. The shipped policy is the one place that can be pinned.
    """
    unknown = sorted(
        {
            model
            for model, provider in _every_named_elo(_policy())
            if model not in MODEL_CAPABILITIES
        }
    )
    assert unknown == [], unknown


def test_every_elo_the_policy_names_sits_on_the_provider_the_registry_gives_it():
    """A right id on the wrong rail is a 404 at best and someone else's bill at worst."""
    mismatched = [
        (model, provider, MODEL_CAPABILITIES[model].get("provider"))
        for model, provider in _every_named_elo(_policy())
        if model in MODEL_CAPABILITIES
        and MODEL_CAPABILITIES[model].get("provider") != provider
    ]
    assert mismatched == [], mismatched


def test_fail_safe_is_still_the_t1_chain_the_comment_promises():
    """"This IS the T1 chain, duplicated on purpose ... Keep it in step by hand."

    Hand-kept is exactly what drifted: the last-resort route is the one nobody
    exercises until everything else is already down.
    """
    policy = _policy()
    assert _chain(policy["fail_safe"]) == _chain(policy["tiers"]["T1"])


def test_the_classifier_pair_mirrors_its_own_chain_head():
    """"Keep these two keys byte-equal to chain[0]" — the flat pair is what runs."""
    classifier = _policy()["classifier"]
    head = classifier["chain"][0]
    assert (classifier["model"], classifier["provider"]) == (
        head["model"], head["provider"]
    ), (
        "the runner dispatches on the flat pair and the console reads the chain: "
        "a divergence runs the classifier on a rail the policy does not declare"
    )


def test_the_blocklist_fallback_chain_is_the_tier_union_in_tier_order():
    """"Regenerate it whenever `tiers` changes" — so regenerate it here and compare.

    Blocklist.fallback_for() walks this list POSITIONALLY, so a stale entry does
    not just look wrong: it decides which rail a banned model is replaced by.
    """
    policy = _policy()
    derived: list[str] = []
    for name in sorted(policy["tiers"]):
        for model, _ in _chain(policy["tiers"][name]):
            if model not in derived:
                derived.append(model)
    assert policy["blocklist"]["fallback_chain"] == derived


def test_the_general_purpose_tier_is_cheaper_at_the_margin_than_the_flagship():
    """T2 carries the most traffic, so its primary is the money decision here.

    The tier's own comment argues the trade on numbers; this pins the half a test
    can check. Both units, because the install pays in credits and a plan-less one
    would pay in dollars, and a change that improved one while quietly wrecking the
    other should fail.
    """
    policy = _policy()
    primary = policy["tiers"]["T2"]["model"]
    assert primary == "glm-5.3-flash"

    flagship = MODEL_CAPABILITIES["glm-5.3"]
    chosen = MODEL_CAPABILITIES[primary]
    assert chosen["billing_mode"] == "plan" == flagship["billing_mode"], (
        "both are plan rails, so the comparison is like for like"
    )
    # Dollars, for whoever is not on the plan: 0.50 against 4.40 out.
    assert chosen["price_out"] < flagship["price_out"]
    assert chosen["price_in"] < flagship["price_in"]
    # Credits are the unit that actually bills here, and they are not a registry
    # field — the plan's table gives 8 output against 24 — so what is pinned is the
    # note that records them, next to the price that must not contradict it.
    assert "2.3/0.56/8 credits" in str(chosen.get("notes") or "")
    assert "6.9/1.7/24" in str(flagship.get("notes") or "")


def test_the_busiest_tier_still_pins_its_plan_primary():
    """A LITERAL pin, and deliberately so — nothing else can catch its removal.

    `pin_primary: true` on T2 is idle on today's roster: `cheapest_now` buckets by
    billing_mode, so a plan-covered primary already leads every dollar-priced hop
    without it (proved over all 168 hours in
    tests/router/test_capabilities.py::test_the_shipped_t2_pin_is_redundant_today_and_says_what_it_protects).
    A mutation flipping this line therefore breaks NO behavioural test, which makes
    it exactly the kind of "does nothing, delete it" line that gets deleted — and
    the day the roster puts a dollar-priced model in front, the tier that carries
    the most traffic starts ordering itself by a price the operator does not pay,
    silently. The declaration is pinned so the deletion has to be deliberate.
    """
    tier = _policy()["tiers"]["T2"]
    assert tier.get("fallback_strategy") == "cheapest_now"
    assert tier.get("pin_primary") is True


def test_an_image_turn_can_stay_on_the_plan_rail():
    """The `vision-required` row routes to T2, and T2's primary must be able to see.

    While it could not, the capability filter dropped it on every image request and
    the turn billed dollars on a subscription seat — routing correctly and paying
    for it. This is the assertion that keeps the row and the roster in agreement:
    either the primary sees, or the row's destination has to change.
    """
    policy = _policy()
    rows = [rule for rule in policy["rules"] if rule.get("id") == "vision-required"]
    assert len(rows) == 1, "the row this test is about must still exist"
    tier_name = rows[0]["then"]["model"]
    tier = policy["tiers"][tier_name]
    assert MODEL_CAPABILITIES[tier["model"]]["vision"] is True, (
        f"{tier_name}'s primary cannot see, so every image turn skips it and "
        f"bills a paid rail"
    )
    assert MODEL_CAPABILITIES[tier["model"]]["billing_mode"] == "plan", (
        "and it has to be the plan rail, or the row costs money by design"
    )


@pytest.mark.parametrize("surface", ["tiers", "fail_safe", "classifier"])
def test_the_traversal_reaches_each_surface_it_claims_to_cover(surface):
    """A traversal that silently read nothing would pass every test above."""
    policy = _policy()
    assert policy.get(surface), f"the shipped policy declares no {surface}"
    named = _every_named_elo(policy)
    if surface == "classifier":
        expected = policy["classifier"]["model"]
    elif surface == "fail_safe":
        expected = policy["fail_safe"]["model"]
    else:
        expected = policy["tiers"]["T4"]["model"]
    assert any(model == expected for model, _ in named), (
        f"{surface}'s own model never appeared in the traversal"
    )


def test_the_168_hour_arithmetic_in_the_shipped_prose_is_the_swept_truth():
    """The operator tunes the time knobs from those comments, so they are a contract.

    They were wrong for four weeks. deepseek added a weekday restriction to both
    its windows in a silent page edit (absent from its changelog; Wayback brackets
    it 21/08 vs 24/08); the registry gained the `weekdays` gate and the prose did
    not. `router.example.yaml` claimed T3/T4 split 119h/29h/20h and T2's tail
    flipped 49/168 (29.17%) "EVERY day", while the suite already asserted 15/20/133
    and 35 — the two halves of the repo disagreed about the same week, and the
    operator-facing half was the wrong one.

    So the numbers are re-derived here by SWEEPING all 168 hours and compared to the
    text, rather than pinned as literals in a second place. A vendor window change
    now fails this test instead of quietly outdating the page.
    """
    from datetime import datetime, timedelta, timezone

    from router.capabilities import price_multiplier
    from router.rules import plan_chain

    monday = datetime(2026, 8, 17, tzinfo=timezone.utc)
    tiers = _policy()["tiers"]

    # T3's three cases, swept.
    tier = tiers["T3"]
    out = {"model": tier["model"], "provider": tier["provider"],
           "fallback": tier["fallback"]}
    for key in ("fallback_strategy", "time_cap", "time_policy", "billing_mode"):
        if key in tier:
            out[key] = tier[key]
    quiet = reordered = both = 0
    for hour in range(168):
        plan = plan_chain(out, {}, when=monday + timedelta(hours=hour))
        demoted, priced = plan.get("demoted") or [], plan.get("peak_priced") or []
        if demoted and priced:
            reordered += 1
        elif priced:
            both += 1
        elif not demoted:
            quiet += 1
    assert (quiet, reordered, both) == (133, 15, 20), (quiet, reordered, both)

    # T2's tail is a function of deepseek-v4-flash's multiplier alone.
    flips = sum(
        price_multiplier("deepseek-v4-flash", when=monday + timedelta(hours=h)) > 1.0
        for h in range(168)
    )
    assert flips == 35, flips

    prose = (_ROOT / "router.example.yaml").read_text(encoding="utf-8")
    # The figures the prose states, read back out of it.
    assert re.search(rf"{quiet}h \({100 * quiet / 168:.1f}%\)", prose), (
        "the T3/T4 quiet-hour count in router.example.yaml is not the swept one"
    )
    assert re.search(rf"{reordered}h\s+\({100 * reordered / 168:.1f}%\)", prose)
    assert re.search(rf"{both}h \({100 * both / 168:.1f}%\)", prose)
    assert f"flipped for {flips} of" in prose, (
        "the T2 tail flip count in router.example.yaml is not the swept one"
    )
    assert f"{quiet}h/{reordered}h/{both}h split" in prose, (
        "T4's back-reference to T3's split is stale"
    )

    # And the vendor fact that produced all of it: no surviving "EVERY day" claim
    # about a window the registry gates.
    assert "EVERY day" not in prose, (
        "router.example.yaml still claims a window applies every day; the registry "
        "gates both deepseek windows Mon-Fri"
    )
