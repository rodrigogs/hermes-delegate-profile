"""`smart-router` as a selectable model — the pure decision, without Hermes.

The seam is `llm_request` middleware, because no HOOK in hermes-agent can change the
model of a chat turn: `VALID_HOOKS` is a closed set with nothing for it, and
`pre_llm_call` fires every turn but its return value is read for `{"context": str}`
only, so a returned `{"model": ...}` is discarded.
"""
from __future__ import annotations

import pytest

from router.pseudo_model import (
    CROSS_PROVIDER,
    NO_FALLBACK,
    NOT_SENTINEL,
    NO_TASK_TEXT,
    REUSED,
    REWRITTEN,
    ROUTER_DECLINED,
    SENTINEL,
    is_sentinel,
    plan_rewrite,
    task_text_from_messages,
)

_FAIL_SAFE = {"model": "last-resort", "provider": "bedrock"}


def _req(model=SENTINEL, text="fix a typo in one file"):
    return {"model": model, "messages": [{"role": "user", "content": text}]}


def _route_to(model, provider="bedrock"):
    return lambda _text: {"model": model, "provider": provider}


@pytest.mark.parametrize(
    "value,expected",
    [
        (SENTINEL, True),
        ("Smart-Router", True),      # an operator types this by hand
        ("  smart-router  ", True),
        ("smart_router", False),     # a different id, not a spelling of this one
        ("smart:router", False),
        ("us.anthropic.claude-opus-5", False),
        (None, False),
        (123, False),
    ],
)
def test_the_sentinel_is_recognised_case_and_space_insensitively(value, expected):
    assert is_sentinel(value) is expected


def test_a_real_model_is_left_completely_alone():
    """The common case, and the one that must cost nothing."""
    plan = plan_rewrite(_req(model="us.anthropic.claude-opus-5"), provider="bedrock",
                        turn_id="t1", route=_route_to("never-called"), decided={})
    assert plan == {"outcome": NOT_SENTINEL, "request": None, "model": None}


def test_the_sentinel_is_replaced_by_what_the_router_chose():
    decided = {}
    plan = plan_rewrite(_req(), provider="bedrock",
                        turn_id="t1", route=_route_to("haiku"), decided=decided)
    assert plan["outcome"] == REWRITTEN
    assert plan["request"]["model"] == "haiku"
    assert plan["model"] == "haiku"
    # The rest of the payload rides through untouched — this replaces one key.
    assert plan["request"]["messages"] == _req()["messages"]
    assert decided["t1"]["model"] == "haiku", "and the turn's decision is remembered"


def test_one_decision_per_turn_no_matter_how_many_provider_calls_it_makes():
    """A turn makes many API calls — every tool-call iteration is another.

    Routing on each would pay the classifier repeatedly for one prompt and could change
    the answering model MID-TURN, leaving the first half of the exchange produced by a
    different one.
    """
    calls = []

    def route(text):
        calls.append(text)
        return {"model": f"model-{len(calls)}", "provider": "bedrock"}

    decided = {}
    first = plan_rewrite(_req(), provider="bedrock", turn_id="t1", route=route, decided=decided)
    second = plan_rewrite(_req(), provider="bedrock", turn_id="t1", route=route, decided=decided)
    third = plan_rewrite(_req(), provider="bedrock", turn_id="t1", route=route, decided=decided)

    assert len(calls) == 1, "the router was asked exactly once for this prompt"
    assert first["request"]["model"] == "model-1"
    assert second["request"]["model"] == "model-1" and second["outcome"] == REUSED
    assert third["request"]["model"] == "model-1"

    # A NEW turn asks again — "route each prompt" is the whole point.
    plan_rewrite(_req(), provider="bedrock", turn_id="t2", route=route, decided=decided)
    assert len(calls) == 2


def test_a_decision_on_another_rail_is_refused_rather_than_misrouted():
    """This seam rewrites kwargs; it does not rebuild the provider client.

    The credentials and base_url were bound before the middleware was called, so sending
    a bedrock id to a zai client would fail as an authentication error against a provider
    the operator never chose. Crossing rails needs `llm_execution`, which replaces the
    call and takes ownership of retries — a different feature with a different risk.
    """
    plan = plan_rewrite(_req(), provider="bedrock", turn_id="t1",
                        route=_route_to("glm-5.3-flash", provider="zai"),
                        decided={}, fail_safe=_FAIL_SAFE)
    assert plan["outcome"] == CROSS_PROVIDER
    assert plan["request"]["model"] == "last-resort", (
        "the fake id must not reach the wire, so the last resort answers"
    )


def test_a_chosen_model_with_no_provider_is_applicable_by_construction():
    """No provider named means "wherever this request was already going"."""
    plan = plan_rewrite(_req(), provider="bedrock", turn_id="t1",
                        route=lambda _t: {"model": "same-rail"}, decided={})
    assert plan["outcome"] == REWRITTEN and plan["request"]["model"] == "same-rail"


@pytest.mark.parametrize(
    "route,expected_outcome",
    [
        (lambda _t: None, ROUTER_DECLINED),
        (lambda _t: {}, ROUTER_DECLINED),
        (lambda _t: {"provider": "bedrock"}, ROUTER_DECLINED),   # no model
        (lambda _t: (_ for _ in ()).throw(RuntimeError("boom")), ROUTER_DECLINED),
    ],
)
def test_when_routing_declines_the_last_resort_answers(route, expected_outcome):
    """The sentinel must never go on the wire — a provider would answer model-not-found.

    `fail_safe` is precisely the block that exists for "everything else failed", so it is
    what this falls back to rather than a model this module picked.
    """
    plan = plan_rewrite(_req(), provider="bedrock", turn_id="t1",
                        route=route, decided={}, fail_safe=_FAIL_SAFE)
    assert plan["outcome"] == expected_outcome
    assert plan["request"]["model"] == "last-resort"


def test_with_no_usable_last_resort_it_refuses_to_rewrite_and_says_so():
    """Inventing a model here would be this module choosing routing policy."""
    for fail_safe in (None, {}, {"provider": "bedrock"}, {"model": "x", "provider": "zai"}):
        plan = plan_rewrite(_req(), provider="bedrock", turn_id="t1",
                            route=lambda _t: None, decided={}, fail_safe=fail_safe)
        assert plan == {"outcome": NO_FALLBACK, "request": None, "model": None}, fail_safe


def test_a_prompt_with_no_user_text_falls_back_instead_of_routing_on_nothing():
    request = {"model": SENTINEL, "messages": [{"role": "system", "content": "be nice"}]}
    plan = plan_rewrite(request, provider="bedrock", turn_id="t1",
                        route=_route_to("never"), decided={}, fail_safe=_FAIL_SAFE)
    assert plan["outcome"] == NO_TASK_TEXT
    assert plan["request"]["model"] == "last-resort"


def test_a_non_mapping_request_is_not_a_crash():
    for bad in (None, [], "model", 7):
        assert plan_rewrite(bad, provider="bedrock", turn_id="t1",
                            route=_route_to("x"), decided={})["request"] is None


def test_the_routed_text_is_the_LAST_user_message():
    """Earlier turns are context the signals would double-count.

    `est_input_tokens` grows with every reply, so routing on the whole transcript would
    ratchet a long chat toward the biggest tier for no reason.
    """
    seen = []
    plan_rewrite(
        {"model": SENTINEL, "messages": [
            {"role": "user", "content": "first thing"},
            {"role": "assistant", "content": "a long reply " * 50},
            {"role": "user", "content": "the actual prompt"},
        ]},
        provider="bedrock", turn_id="t1",
        route=lambda t: seen.append(t) or {"model": "m", "provider": "bedrock"},
        decided={},
    )
    assert seen == ["the actual prompt"]


@pytest.mark.parametrize(
    "content,expected",
    [
        ("plain string", "plain string"),
        ([{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}],
         "part one\npart two"),
        (["bare", "strings"], "bare\nstrings"),
        ([{"type": "image_url", "image_url": {"url": "x"}}], ""),
        (None, ""),
        (12, ""),
    ],
)
def test_task_text_handles_every_content_shape_without_raising(content, expected):
    """A middleware that throws takes the turn down with it, so nothing here may raise."""
    assert task_text_from_messages([{"role": "user", "content": content}]) == expected


@pytest.mark.parametrize("messages", [None, "not a list", 5, [], [{"role": "user"}], ["x"]])
def test_task_text_of_a_malformed_message_list_is_empty(messages):
    assert task_text_from_messages(messages) == ""


def test_a_turnless_call_still_routes_but_remembers_nothing():
    """turn_id None (a caller that does not supply one) must not key a cache on None."""
    decided = {}
    calls = []
    route = lambda t: calls.append(t) or {"model": "m", "provider": "bedrock"}
    plan_rewrite(_req(), provider="bedrock", turn_id=None, route=route, decided=decided)
    plan_rewrite(_req(), provider="bedrock", turn_id=None, route=route, decided=decided)
    assert decided == {}, "nothing cached under a null turn"
    assert len(calls) == 2, "so each call decides for itself"
