"""``smart-router`` as a selectable model — the pure half.

WHAT THIS IS FOR. An operator picks one model for a chat and wants the router to choose
per prompt instead. Hermes has no hook that can change the model of a chat turn — the
closed ``VALID_HOOKS`` set has nothing for it, and ``pre_llm_call`` fires on every turn
but its return value is read for ``{"context": str}`` only, so a returned ``{"model": …}``
is dropped on the floor. What DOES exist is ``llm_request`` MIDDLEWARE
(``hermes_cli/middleware.py:77``, registered through ``ctx.register_middleware``), whose
return value replaces the outgoing provider kwargs wholesale. That is the seam, and it
needs no change to hermes-agent.

THIS MODULE IS PURE. It takes the request, the facts the middleware was handed, and an
injected ``route`` callable, and returns either a replacement request or None. No Hermes
import, no IO, no clock — the adapter half lives in the plugin's ``__init__``, which is
the only place allowed to know about Hermes.

THREE DECISIONS WORTH ARGUING WITH, all of them measured rather than assumed:

1. ONCE PER TURN, NOT PER API CALL. ``llm_request`` fires on every provider call, and one
   turn makes many — each tool-call iteration is another. Routing on each would pay the
   classifier repeatedly for one prompt and could change the answering model mid-turn,
   with the earlier half of the conversation produced by a different one. Keyed on
   ``turn_id``, so the operator's "route each prompt" is honoured exactly once per prompt.

2. NEVER ACROSS PROVIDERS. This seam rewrites kwargs; it does not rebuild the provider
   client, whose credentials and base_url were bound before we were called. So a decision
   naming another rail is REFUSED rather than sent to the wrong endpoint — the failure
   would otherwise be an authentication error against a provider the operator never chose.
   Crossing rails needs ``llm_execution`` middleware, which replaces the call entirely and
   takes ownership of retries, fallback and credential rotation; that is a different
   feature with a different risk, not a flag on this one.

3. A SENTINEL THAT NEVER REACHES THE WIRE. If routing declines, the fake id must not be
   sent — a provider would answer with a confusing model-not-found. The fallback is the
   policy's own ``fail_safe``, which is precisely the block that exists for "everything
   else failed". With no fail_safe to fall back on, this refuses to rewrite and says so,
   because inventing a model here would be this module choosing routing policy.

THE ID IS COLON-FREE ON PURPOSE. hermes-webui's frontend splits ``@<plugin>:<model>`` at
the LAST colon (``static/ui.js:2994``), so an id containing one is mis-parsed into the
wrong provider. ``smart-router`` survives that; ``smart:router`` or ``auto:cheap`` would
not.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

#: The model id an operator selects to mean "let the router choose".
SENTINEL = "smart-router"

#: Reasons a rewrite did not happen, for the caller to log. Closed set: a diagnostic
#: string invented at the call site is a diagnostic nobody can grep for.
NOT_SENTINEL = "not-sentinel"
NO_TASK_TEXT = "no-task-text"
ROUTER_DECLINED = "router-declined"
CROSS_PROVIDER = "cross-provider"
NO_FALLBACK = "no-fallback"
REWRITTEN = "rewritten"
REUSED = "reused-turn-decision"


def is_sentinel(model: Any) -> bool:
    """True when ``model`` selects the router rather than a real model.

    Case- and whitespace-insensitive: an operator types this into a picker or a slash
    command, and ``/model Smart-Router`` meaning nothing would be a trap. Compared
    against the bare id only — a provider-qualified form arrives with the provider
    already split off by the caller.
    """
    return isinstance(model, str) and model.strip().lower() == SENTINEL


def task_text_from_messages(messages: Any) -> str:
    """The text to route on: the last user message, flattened.

    The LAST user message rather than the whole transcript, because that is the prompt
    being answered; earlier turns are context the router's signals would double-count
    (``est_input_tokens`` grows with every reply and would ratchet a chat toward the
    biggest tier for no reason).

    Content may be a string or the list-of-parts shape, so both are handled; anything
    else contributes nothing rather than raising, because a middleware that throws takes
    the turn down with it.
    """
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: List[str] = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part, str):
                    parts.append(part)
            return "\n".join(parts).strip()
        return ""
    return ""


def _same_rail(chosen: Any, current: Any) -> bool:
    """Whether a decision can be applied by rewriting kwargs alone.

    An absent chosen provider means "wherever the request was already going", which is
    applicable by construction. Compared case-folded because policy and runtime spell
    provider names by hand.
    """
    if chosen is None or chosen == "":
        return True
    return str(chosen).strip().lower() == str(current or "").strip().lower()


def plan_rewrite(
    request: Dict[str, Any],
    *,
    provider: Any,
    turn_id: Any,
    route: Callable[[str], Optional[Dict[str, Any]]],
    decided: Dict[Any, Dict[str, Any]],
    fail_safe: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Decide what this provider call's ``model`` should be.

    Returns ``{"outcome": <one of the reasons above>, "request": <dict or None>,
    "model": <str or None>}``. ``request`` is None whenever nothing should change, so the
    caller can return None to the middleware chain and leave the payload untouched.

    ``decided`` is the caller's per-process cache, mutated here: one entry per turn.
    Passed in rather than owned, so the plugin can bound it and the tests can inspect it.
    """
    if not isinstance(request, dict):
        return {"outcome": NOT_SENTINEL, "request": None, "model": None}
    if not is_sentinel(request.get("model")):
        return {"outcome": NOT_SENTINEL, "request": None, "model": None}

    # Already decided for this turn: reuse it, so every call inside one prompt is
    # answered by the same model.
    cached = decided.get(turn_id) if turn_id is not None else None
    if isinstance(cached, dict) and cached.get("model"):
        return _applied(request, cached["model"], REUSED)

    text = task_text_from_messages(request.get("messages"))
    if not text:
        return _fallback(request, fail_safe, provider, NO_TASK_TEXT)

    try:
        decision = route(text)
    except Exception:  # noqa: BLE001 - a routing failure must not take the turn down
        decision = None
    if not isinstance(decision, dict) or not decision.get("model"):
        return _fallback(request, fail_safe, provider, ROUTER_DECLINED)

    if not _same_rail(decision.get("provider"), provider):
        # The decision is real but unusable from here. Not a fallback: falling back would
        # silently ignore a correct routing answer. The caller logs it and the request
        # goes out on the model the operator's own config named.
        return _fallback(request, fail_safe, provider, CROSS_PROVIDER)

    chosen = str(decision["model"])
    if turn_id is not None:
        decided[turn_id] = {"model": chosen, "provider": decision.get("provider")}
    return _applied(request, chosen, REWRITTEN)


def _applied(request: Dict[str, Any], model: str, outcome: str) -> Dict[str, Any]:
    replacement = dict(request)
    replacement["model"] = model
    return {"outcome": outcome, "request": replacement, "model": model}


def _fallback(
    request: Dict[str, Any],
    fail_safe: Optional[Dict[str, Any]],
    provider: Any,
    outcome: str,
) -> Dict[str, Any]:
    """The sentinel must never go on the wire; fall back to the policy's last resort.

    Only when that last resort is on the rail this request is already going to — the
    same constraint the decision itself is held to, for the same reason.
    """
    model = (fail_safe or {}).get("model") if isinstance(fail_safe, dict) else None
    if model and _same_rail((fail_safe or {}).get("provider"), provider):
        # Applied: the outcome stays the reason we DEVIATED, which is what an operator
        # needs to read — "the router declined, so the last resort answered".
        return _applied(request, str(model), outcome)
    # Could not apply: one outcome for both shapes of that (no fail_safe at all, or one
    # on another rail), because the caller's remedy is the same either way and keeping
    # the deviation reason here would report a fallback that did not happen.
    return {"outcome": NO_FALLBACK, "request": None, "model": None}
