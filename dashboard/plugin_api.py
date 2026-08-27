"""Smart Router dashboard plugin — backend API routes.

Mounted at /api/plugins/hermes-smart-router/ by the dashboard plugin
system. Imports the pure-core router from the delegate-profile plugin.

Contract: every route here is a READ path — including the two POSTs, where the
method is transport (a body is the only pipe wide enough for a composed prompt, and
/lint takes none), never a write. It reloads ``router.yaml`` per request (the file
is HOT), exposes only non-secret operational state, runs deterministic Stage-0
simulations only — never the LLM classifier — and never mutates breaker state. No
route may raise over the ROUTER's state: a missing or corrupt config degrades to an
empty default carried alongside a diagnostic, because a dashboard panel that 500s
tells the operator nothing about *why*. The only refusals on this surface are over
the CALLER's own input — an unusable ``at`` or ``prompt_text`` on /explain, or a
body that is not a JSON object — and each is a 400, not a 500 (see
:func:`api_explain`).

Shape parity with :class:`router.service.RouterService` is load-bearing, not
cosmetic. The dashboard plugin and the Hermes One console are two views of ONE
router, and an operator compares them. So the shared material —
``validation_errors`` vs advisory ``warnings``, the per-tier fallback/capability/
time knobs, ``chain_plan``, and the composition of the /explain trace itself —
is DELEGATED to ``RouterService`` rather than re-derived here. Re-deriving is how
the two surfaces drifted in the first place: this file used to report neither
warnings nor the chain plan, so the console rendered empty panels and the feature
looked broken. Legacy keys this plugin's bundled UI already reads
(``classifier_model``, ``banned_models``) are kept ADDITIVELY on top; nothing
that shipped is removed.

THE CLOCK IS INJECTED HERE, and that is the whole reason /explain reads a wall
clock at all. ``signals``, ``rules`` and ``capabilities`` are pure and IO-free:
the hour is a PARAMETER they receive, exactly like the ``random.Random`` threaded
for the ``random`` fallback strategy. This module previously omitted it — it built
the feature vector from ``signals.extract()`` alone and called ``rules.explain``
with no ``when=`` — so ``utc_hour``/``utc_weekday`` were absent, a time-keyed rule
was permanently inert on this endpoint while firing in production, every price
multiplier read 1.0, ``time_cap`` was inert, and a ``cheapest_now`` tier reported
``strategy_degraded`` with the reason "no clock was injected" while production had
in fact compared prices. That is a plan production would never produce, rendered
on the second operator surface. So the clock is resolved per request (defaulting
to the current UTC hour), ``at`` may override it for the "what would this route to
at 07:00 UTC?" question the 4am cron raises, and ``evaluated_at`` reports which
clock was used and whether it actually reached the planner — a time-relative
answer that does not name its hour is indistinguishable from a wrong one.

THE SIZE IS THE SAME CLASS OF OMISSION, and it was the second one on this
endpoint. ``task`` is the goal line; ``prompt_text`` is the text the child would
really receive, and ``est_input_tokens`` has to measure THAT. /explain used to
accept only ``task``, so a context-heavy turn — one that in production reaches
``huge-context-read`` and a tier with a ``min_context`` floor — previewed as about
six estimated tokens, matched no rule and derived no requirement. Both surfaces
returned a valid-looking plan for a turn nobody was going to send. So the
parameter is accepted here, spelled and validated exactly as ``RouterService`` and
the sidecar spell and validate it, and ``preview.sized_from`` /
``preview.prompt_chars`` say which text the answer came from — a preview sized
from the goal is byte-shaped like one sized from the real turn and answers a
different question.
"""

from __future__ import annotations

import inspect
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Dict, Optional, Tuple

# The router modules live in the parent plugin directory. This stays for the flat
# shape only — where ``dashboard`` is itself top-level, ``..router`` is beyond the
# top-level package, and the absolute name below is the only one that can resolve.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent  # dashboard/ -> delegate-profile/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import yaml
from fastapi import APIRouter, Body, HTTPException, Query

# Relative first, absolute second. ``router`` is a SIBLING of this package, so the
# relative form climbs one level: under Hermes's ``hermes_plugins.<slug>`` shape
# this binds the same ``hermes_plugins.<slug>.router`` modules the rest of the
# plugin uses. Resolving the absolute name first did technically work there —
# ``_PLUGIN_DIR`` above puts the plugin root on ``sys.path`` — but it imported a
# SECOND, independent copy of the router package under the top-level name, so this
# read path saw different module-level state (its own ``rules._caps``,
# ``_signals``, caches) than the write path did. No None fallback: hard requirements.
#
# EXACTLY ONE ARM IS REACHABLE PER LAYOUT, which is why the whole statement is
# excluded rather than one half of it: measured in this repo ``dashboard`` is
# top-level, so the relative form cannot resolve and only the fallback runs, while
# under ``hermes_plugins.<slug>`` only the relative form does. The arm coverage
# cannot see here is asserted as BEHAVIOUR instead — see
# ``test_plugin_binds_the_sibling_router_under_a_package_layout``, which imports
# this file under a package and pins that it binds the SIBLING router package.
try:  # pragma: no cover - one arm per layout; see above
    from ..router import service as _service_mod
    from ..router.decision_log import DecisionLog, empty_chain_plan
    from ..router.rules import explain as rules_explain
    from ..router.service import RouterService
    from ..router.signals import extract
except ImportError:
    from router import service as _service_mod
    from router.decision_log import DecisionLog, empty_chain_plan
    from router.rules import explain as rules_explain
    from router.service import RouterService
    from router.signals import extract

router = APIRouter()

_CONFIG_PATH = _PLUGIN_DIR / "router.yaml"
_log = DecisionLog()

# Fixed seed for the chain preview /explain returns, matching
# ``RouterService._EXPLAIN_PREVIEW_SEED``: the dashboard polls this endpoint, and
# under ``fallback_strategy: random`` (or ``cheapest_now`` with tied prices) a
# fresh rng per call would reshuffle the previewed chain on every poll, so an
# operator could not tell a policy change from shuffle noise. Production routing
# injects a request-derived rng, so real traffic still spreads across the tail.
_EXPLAIN_PREVIEW_SEED = 0

# Whether the installed rules.explain accepts an rng / an injected clock.
# Resolved once each, by signature rather than by catching TypeError, so a
# genuine TypeError raised INSIDE explain is never masked by a silent second
# call. The two are INDEPENDENT: a rules.py may predate either, and losing one
# injected parameter must not cost the other. These govern only the local mirror
# in :func:`_explain_decision`; the delegated path is the service's business.
try:
    _EXPLAIN_PARAMETERS = frozenset(inspect.signature(rules_explain).parameters)
except (TypeError, ValueError):  # pragma: no cover - unintrospectable callable
    _EXPLAIN_PARAMETERS = frozenset()
_EXPLAIN_ACCEPTS_RNG = "rng" in _EXPLAIN_PARAMETERS
_EXPLAIN_ACCEPTS_WHEN = "when" in _EXPLAIN_PARAMETERS


def _service() -> RouterService:
    """A fresh service over the CURRENT config path.

    Built per request rather than pinned at import: ``_CONFIG_PATH`` is a module
    attribute an installer (or a test) may repoint, and every RouterService read
    reloads the YAML anyway, so there is nothing to cache. No write path is
    exposed here, so the instance's write lock is never contended.
    """
    return RouterService(_CONFIG_PATH)


def _load_config() -> Dict[str, Any]:
    """Parse ``router.yaml`` defensively — {} for missing, unreadable or corrupt.

    Used for the legacy compatibility keys below and for the /explain dry run,
    which deliberately does not refuse on a broken policy. Errors are NOT
    swallowed from the operator: they surface as ``validation_errors`` on
    /status, which reads them through :class:`RouterService`.
    """
    try:
        raw = _CONFIG_PATH.read_text(encoding="utf-8")
        config = yaml.safe_load(raw) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return config if isinstance(config, dict) else {}


# ── The clock (the ONLY wall-clock read on this surface) ──────────────

def _utc_now() -> datetime:
    """The real current UTC time — the one wall-clock read in this module.

    A module-level function, not an inline call, so the whole time surface has a
    single seam: a test pins the hour by replacing this, exactly as an operator
    pins it by passing ``at``.
    """
    return datetime.now(timezone.utc)


def _to_utc_hour(when: datetime) -> datetime:
    """``when`` as an aware UTC datetime truncated to the top of the hour.

    Mirrors ``router.service._to_utc_hour``, because the reported hour and the
    multipliers applied to it must never come from two different readings: an
    aware datetime is CONVERTED to UTC, a naive one is taken to already BE UTC.

    The truncation is load-bearing. ``price_windows`` are declared in whole UTC
    hours, so minutes cannot change any answer — but they would change the
    response bytes, and a polled preview whose payload churns every second is
    indistinguishable from a nondeterministic one.
    """
    stamp = (
        when.astimezone(timezone.utc)
        if when.tzinfo is not None
        else when.replace(tzinfo=timezone.utc)
    )
    return stamp.replace(minute=0, second=0, microsecond=0)


def _resolve_at(at: Optional[str]) -> Tuple[datetime, str]:
    """Return ``(when, at_source)`` — the clock to inject and where it came from.

    ``at_source`` is ``now`` or ``explicit``, so a rendered plan says which clock
    produced it. A query parameter arrives as text, and a trailing ``Z`` is the
    spelling every JSON producer emits, so it is translated rather than refused.

    Fail-CLOSED on an unusable value, with a ``ValueError`` the route renders as a
    400: silently falling back to "now" would answer a different question than the
    one asked, which on an audit surface is worse than refusing. Parsed BEFORE the
    config is read, because an unusable clock is the caller's error and reporting
    it as a policy problem would send an operator to the wrong file.
    """
    if at is None or (isinstance(at, str) and not at.strip()):
        return _to_utc_hour(_utc_now()), "now"
    if isinstance(at, datetime):
        return _to_utc_hour(at), "explicit"
    text = str(at).strip()
    iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(
            "at must be an ISO-8601 timestamp, e.g. 2026-08-17T07:00:00Z"
        ) from None
    return _to_utc_hour(parsed), "explicit"


def _clock_features(when: datetime) -> Dict[str, Any]:
    """The two INJECTED time features for the feature vector.

    Delegated to ``router.service._clock_features`` when that module supplies it,
    so the two surfaces cannot disagree about the names or the ranges
    (``utc_hour`` 0-23, ``utc_weekday`` 0=Monday). The local fallback is for a
    router/ deployed before the time layer.
    """
    delegate = getattr(_service_mod, "_clock_features", None)
    if callable(delegate):
        features = delegate(when)
        if isinstance(features, dict):
            return dict(features)
    return {"utc_hour": when.hour, "utc_weekday": when.weekday()}


# ── The size (the other half of the question production asks) ────────

def _resolve_prompt(task: str, prompt_text: Optional[str]) -> Tuple[str, str]:
    """Return ``(text_to_size_from, sized_from)`` for this preview.

    Delegated to ``RouterService._resolve_prompt`` — the one validator both
    surfaces share, holding the one prompt bound and the one ``sized_from``
    vocabulary. A second copy of that rule is how two surfaces end up disagreeing
    about what "no prompt" or "too big" means, which on this endpoint is the
    difference between measuring 120k chars of context and measuring six tokens
    of goal.

    ``None`` — an in-process caller omitting the parameter — is normalised to ""
    ("same as task") before delegating, exactly as the sidecar's query-string form
    and its JSON-null form both are. Anything else non-string, and any prompt over
    the bound, is refused by the service with a ``ValueError`` the route renders as
    a 400: a truncated or coerced prompt would produce a smaller
    ``est_input_tokens`` and therefore a confidently wrong plan — the precise
    failure this parameter exists to fix.
    """
    supplied = "" if prompt_text is None else prompt_text
    delegate = getattr(_service(), "_resolve_prompt", None)
    if callable(delegate):
        resolved = delegate(task, supplied)
        if (
            isinstance(resolved, tuple)
            and len(resolved) == 2
            and isinstance(resolved[0], str)
        ):
            return resolved[0], str(resolved[1])
    # Local mirror for a router/ deployed before the parameter: the SAME falsy
    # test ``adapter.route`` uses, so a caller supplying no context is measured
    # exactly as production measures it.
    if not isinstance(supplied, str):
        raise ValueError("prompt_text must be a string")
    if not supplied:
        return task, "task"
    return supplied, "prompt_text"


def _explain_features(prompt: str, when: datetime) -> Dict[str, Any]:
    """The feature vector for a preview: extracted signals plus the clock.

    Delegated to ``RouterService._explain_features`` so both surfaces measure one
    vector. The clock features are added at the EDGE — here — because
    ``signals.extract()`` is pure and must never read a wall clock, and because a
    rule keyed on ``utc_hour`` has to fire on this endpoint or the operator's
    preview answers a question production does not ask.

    ``prompt`` is the text production would SIZE THE TURN FROM — context + goal,
    not the goal line (see :func:`_resolve_prompt`). Measuring the goal alone is
    what made a context-heavy turn preview as ``est_input_tokens`` 6, matching no
    ``min_context`` rule and deriving no requirement, while production routed it
    to a long-context tier.
    """
    delegate = getattr(RouterService, "_explain_features", None)
    if callable(delegate):
        features = delegate(prompt, when)
        if isinstance(features, dict):
            return dict(features)
    features = extract(prompt)
    features.update(_clock_features(when))
    return features


def _explain_decision(
    task: str, config: Dict[str, Any], when: datetime,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Compose the Stage-0 decision trace at ``when``, via the service when it can.

    Full delegation to ``RouterService.explain`` is not possible here, and all
    three reasons are real rather than stylistic:

      1. SHAPE — the service nests the trace under ``decision`` inside a
         ``mode``/``requires_classifier`` envelope, while this plugin's bundled UI
         reads the trace keys (``output``, ``cause``, ``matched_clauses``,
         ``matched_rule_id``) at the TOP level. Re-nesting would break a shipped
         response contract.
      2. REFUSAL — the service raises on an invalid policy. A broken config is
         exactly when an operator needs to see where a task would land, and
         /status already reports the errors.
      3. RECORDING — this route appends to the plugin's own ``DecisionLog``,
         which the service correctly knows nothing about.

    What CAN be delegated — and is, because two implementations of one
    composition is how the missing clock happened — is the composition itself:
    clock features into the feature vector, fixed-seed preview rng, and ``when=``
    through to the planner. ``RouterService._explain_decision`` is that exact
    step, resolved per call so a repointed or patched service is honoured, and
    called BY KEYWORD against its declared parameters: the helper has already
    grown a ``features`` argument once, and a positional call would turn the next
    such addition into a TypeError inside a read path. The local mirror below is
    behaviourally equivalent and exists only for a router/ deployed before that
    helper.

    ``task`` stays the GOAL while the features are measured from ``prompt`` (the
    composed context + goal, defaulting to the task) — the same split production
    makes, where the classifier keys on the goal alone and only the signals see the
    context.
    """
    features = _explain_features(task if prompt is None else prompt, when)
    delegate = getattr(RouterService, "_explain_decision", None)
    if callable(delegate):
        try:
            declared = frozenset(inspect.signature(delegate).parameters)
        except (TypeError, ValueError):  # pragma: no cover - unintrospectable
            declared = frozenset()
        if {"task", "config", "when"} <= declared:
            delegated: Dict[str, Any] = {"task": task, "config": config,
                                         "when": when}
            if "features" in declared:
                delegated["features"] = features
            return delegate(**delegated)

    args = (
        task,
        features,
        False,
        config.get("rules", []),
        config.get("default", {}),
        config.get("tiers", {}),
    )
    kwargs: Dict[str, Any] = {}
    if _EXPLAIN_ACCEPTS_RNG:
        # A rules.py predating fallback strategies orders chains sequentially,
        # so the preview is stable without an rng anyway.
        kwargs["rng"] = random.Random(_EXPLAIN_PREVIEW_SEED)
    if _EXPLAIN_ACCEPTS_WHEN:
        # A rules.py predating the time layer plans time-agnostically; the
        # response then reports time_aware False rather than claiming an hour.
        kwargs["when"] = when
    return rules_explain(*args, **kwargs)


def _chain_plan_of(decision: Dict[str, Any]) -> Dict[str, Any]:
    """Lift ``chain_plan`` out of the trace, degrading to the SERVICE's shape.

    Delegated to ``RouterService._chain_plan_of`` because the DEGRADED shape is
    load-bearing and deliberately wider than ``decision_log.empty_chain_plan()``:
    the console branches on ``time_agnostic`` and cannot see which module produced
    the plan it was handed, so a narrower default here would leave it pricing a
    plan that saw no clock against the BROWSER's hour. decision_log's shape is the
    fallback only where the service has no opinion.
    """
    delegate = getattr(RouterService, "_chain_plan_of", None)
    if callable(delegate):
        plan = delegate(decision)
        if isinstance(plan, dict):
            return plan
    plan = decision.get("chain_plan") if isinstance(decision, dict) else None
    return plan if isinstance(plan, dict) else empty_chain_plan()


def _evaluated_at(
    when: datetime, at_source: str, plan: Dict[str, Any]
) -> Dict[str, Any]:
    """Which clock the preview was evaluated at, and whether it landed.

    Delegated to ``RouterService._evaluated_at`` when present so the key names
    (``at``, ``at_source``, ``utc_hour``, ``utc_weekday``, ``time_aware``) match
    the service and the CLI and one console can render either surface.

    ``time_aware`` is read back OFF THE PLAN rather than asserted from the fact
    that a clock was passed: the planner is the only thing that knows whether the
    clock reached it. Claiming an hour a plan never saw is precisely the failure
    ``chain_plan.time_agnostic`` exists to prevent.
    """
    delegate = getattr(RouterService, "_evaluated_at", None)
    if callable(delegate):
        reported = delegate(when, at_source, plan)
        if isinstance(reported, dict):
            return dict(reported)
    return {
        "at": when.isoformat(),
        "at_source": at_source,
        "utc_hour": when.hour,
        "utc_weekday": when.weekday(),
        "time_aware": plan.get("time_agnostic") is False,
    }


def _preview_note(
    decision: Dict[str, Any],
    plan: Dict[str, Any],
    sized_from: str,
    prompt_chars: int,
) -> Dict[str, Any]:
    """What this preview is reproducible within, and what text it measured.

    Delegated to ``RouterService._preview_note`` for the same reason
    ``evaluated_at`` is delegated: the console renders one shape from either
    surface, and the disclaimers are the load-bearing part. ``sized_from`` and
    ``prompt_chars`` are the ones this endpoint was missing entirely — a preview
    sized from the goal line looks EXACTLY like a preview sized from the real turn
    and answers a different question, so naming the text is what lets an operator
    see that a chain "requiring nothing" was filtered against six tokens of goal
    rather than against the context production sent.

    The fallback is deliberately narrow: on a router/ predating the note, the two
    keys this route knows for certain are still reported rather than an empty
    object, because an absent ``sized_from`` renders as "undefined" and reads as a
    preview that measured nothing at all.
    """
    delegate = getattr(RouterService, "_preview_note", None)
    if callable(delegate):
        try:
            declared = frozenset(inspect.signature(delegate).parameters)
        except (TypeError, ValueError):  # pragma: no cover - unintrospectable
            declared = frozenset()
        args: Tuple[Any, ...] = (decision, plan)
        if {"sized_from", "prompt_chars"} <= declared:
            args = (decision, plan, sized_from, prompt_chars)
        note = delegate(*args)
        if isinstance(note, dict):
            note = dict(note)
            # A note that predates the two size keys still owes them: this route
            # resolved both, and the operator's question is which text was measured.
            note.setdefault("sized_from", sized_from)
            note.setdefault("prompt_chars", prompt_chars)
            return note
    return {"sized_from": sized_from, "prompt_chars": prompt_chars}


def _explain_payload(
    task: str, at: Optional[str], prompt_text: Optional[str]
) -> Dict[str, Any]:
    """The one /explain body both forms return — see :func:`api_explain`.

    Shared rather than duplicated for the reason this whole module keeps
    delegating: two implementations of one preview is how a surface ends up
    answering a different question than the one production asks, and a GET and a
    POST of one route that disagreed would be that failure inside a single file.
    """
    try:
        when, at_source = _resolve_at(at)
        # Both caller-input resolutions happen BEFORE the config is read, so an
        # unusable clock or an over-bound prompt is never reported as a policy
        # problem and never sends an operator to the wrong file.
        prompt, sized_from = _resolve_prompt(task, prompt_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    c = _load_config()
    result = _explain_decision(task, c, when, prompt)
    plan = _chain_plan_of(result)
    # The recorded task_preview stays the GOAL, matching what production records:
    # the prompt is measured, not logged, and a context dump would blow the
    # DecisionLog's per-entry bound.
    _log.record(str(result.get("cause", "")), result.get("output", {}) or {},
                matched_rule_id=result.get("matched_rule_id"),
                task_preview=task[:120],
                chain_plan=plan)
    return {
        **result,
        "chain_plan": plan,
        "evaluated_at": _evaluated_at(when, at_source, plan),
        "preview": _preview_note(result, plan, sized_from, len(prompt)),
    }


# ── API ──────────────────────────────────────────────────────────────

@router.get("/status")
async def api_status():
    """Health snapshot, shape-compatible with ``RouterService.status()``.

    ``validation_errors`` blocks and is the only input to ``valid``; ``warnings``
    is advisory (a tier whose first two hops share an upstream still routes; an
    elo the capability registry cannot describe is unverifiable, not wrong) and
    NEVER flips ``valid``. Keeping the two strictly separate is the whole point:
    merged, every advisory finding would read as a broken router.
    """
    status = dict(_service().status())
    # The console reads ``warnings`` unconditionally, and a missing key renders as
    # "undefined" rather than "none". A service that computes no advisory findings
    # still owes the operator the empty list.
    status.setdefault("warnings", [])
    c = _load_config()
    bl = c.get("blocklist", {}) or {}
    manual_ban = bl.get("manual_ban", [])
    # Legacy keys this plugin's bundled UI reads. Additive — never a substitute
    # for the parity keys above.
    status["banned_models"] = [
        b.get("model")
        for b in (manual_ban if isinstance(manual_ban, list) else [])
        if isinstance(b, dict) and b.get("model")
    ]
    status["classifier_model"] = (status.get("classifier") or {}).get("model", "")
    return status


@router.get("/explain")
async def api_explain(
    task: Annotated[str, Query(description="Task to classify")],
    # Annotated rather than ``at: Optional[str] = Query(None)`` so the PYTHON
    # default is really None. With the parameter spelled as a default value, an
    # in-process caller that omits it receives fastapi's ``Query`` sentinel — only
    # the HTTP layer substitutes the real default — and this route would then
    # refuse its own default as an unparseable clock.
    at: Annotated[
        Optional[str],
        Query(
            description=(
                "ISO-8601 instant to evaluate the plan at, e.g. "
                "2026-08-17T07:00:00Z. Defaults to the current UTC hour."
            ),
        ),
    ] = None,
    # Same spelling, same meaning and same Annotated-with-a-real-None reasoning as
    # ``at``: ``RouterService.explain`` and the sidecar's GET and POST /explain all
    # call this ``prompt_text``, and a per-surface translation table is how one
    # router grows two vocabularies.
    prompt_text: Annotated[
        Optional[str],
        Query(
            description=(
                "The composed prompt (context + goal) the turn would really "
                "send, so est_input_tokens and the capability filter reproduce "
                "the production decision. Defaults to the task."
            ),
        ),
    ] = None,
):
    """Deterministic Stage-0 dry run: the decision trace plus its ``chain_plan``.

    Evaluated at a real UTC hour — see the module docstring — so a time-keyed
    rule fires here exactly as it fires in production, the reported multipliers
    are the ones in force, ``time_cap`` is live, and a ``cheapest_now`` tier
    really does compare prices. ``evaluated_at`` names the hour that produced the
    answer.

    Sized from the real turn. ``task`` is the GOAL; ``prompt_text`` is the full
    text the model would receive and is what the signals are read from, exactly as
    ``adapter.route`` reads them on the production path. Sizing the turn from the
    goal line alone previewed a context-heavy turn — one that in production hits a
    ``min_context`` rule and routes to a long-context tier — as
    ``est_input_tokens`` of about 6, matching no rule and deriving no requirement:
    a plan that never existed, rendered on the second operator surface, which is
    the same defect the missing clock was. ``preview.sized_from`` and
    ``preview.prompt_chars`` name which text produced the answer.

    ``chain_plan`` is lifted to the TOP LEVEL as well as staying inside the
    trace, so a console reads one key and gets a stable shape even from a
    rules.py that produced no plan.

    Unlike ``RouterService.explain``, this route deliberately does NOT refuse on
    an invalid policy: a broken config is exactly when an operator needs to see
    where a task would land, and /status already reports the errors. An unusable
    ``at`` or ``prompt_text`` IS refused (400), because those are the caller's own
    input and answering a different hour — or a different SIZE — than the one asked
    would be a silently wrong audit record.

    A POST form of this route exists for one reason only: a query string cannot
    carry the prompt this parameter exists for. See :func:`api_explain_post`.
    """
    return _explain_payload(task, at, prompt_text)


@router.post("/explain")
async def api_explain_post(
    # ``Any``, not ``Dict[str, Any]``: with a mapping annotation fastapi refuses a
    # non-object body ITSELF, as a 422 with its own error shape, and this surface's
    # refusals are 400s naming the offending field — the same wording the sidecar's
    # /explain returns. One router must not answer the same bad body two ways.
    payload: Annotated[Any, Body()] = None,
):
    """The SAME answer as the GET, through a wider pipe.

    Identical handler, identical parameter names, identical validation — the body
    is only a wider pipe for ``prompt_text``, exactly as it is on the sidecar. It
    is not a convenience: the composed prompt this endpoint has to be sized from is
    routinely 100k+ characters, which no query string survives (clients and proxies
    cap a URL long before that), so without a body the parameter could not carry
    the payload it exists for and the dashboard would be stuck previewing the goal
    line — the very defect it was added to fix.

    Still a READ. Nothing here mutates policy or breaker state; POST is the
    transport, not a write.

    An explicit JSON ``null`` reads as "not supplied", which is the one thing the
    query-string form cannot express. Anything else non-string is REFUSED rather
    than coerced (the 0 that ``or ""`` would have quietly turned into "now"), and a
    body that is not a JSON object is a 400 — the same fail-closed treatment the
    caller's own input gets everywhere else on this surface.
    """
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail="request body must be a JSON object"
        )
    values: Dict[str, Optional[str]] = {}
    for name in ("task", "at", "prompt_text"):
        value = payload.get(name)
        if value is None:  # absent, or an explicit null
            values[name] = None
            continue
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail=f"{name} must be a string")
        values[name] = value
    if values["task"] is None:
        # ``task`` is REQUIRED on both forms — the GET's own required-parameter
        # machinery refuses it too. Only its ABSENCE is refused here; an explicitly
        # empty string is passed through exactly as the query-string form passes it,
        # so the two forms cannot answer one input two ways.
        raise HTTPException(status_code=400, detail="task is required")
    return _explain_payload(values["task"], values["at"], values["prompt_text"])


@router.post("/lint")
async def api_lint():
    """Blocking validation, with the advisory findings alongside it.

    ``warnings`` rides here too so the operator sees both in one place, but
    ``valid`` is computed from ``errors`` only.
    """
    service = _service()
    result = dict(service.lint())
    result["warnings"] = service.status().get("warnings", [])
    return result


@router.get("/blocklist")
async def api_blocklist():
    return _service().blocklist()


@router.get("/log")
async def api_log(
    # Annotated, with a real Python default, for exactly the reason spelled out on
    # ``api_explain``'s ``at``: written as ``tail: int = Query(50, ...)`` the
    # default an in-process caller receives is fastapi's ``Query`` sentinel rather
    # than 50, and ``DecisionLog.tail`` then died on ``-Query(...)`` — a TypeError
    # out of a read path, on the surface whose whole contract is that it never
    # raises over the router's state. The bound stays declared for the HTTP layer.
    tail: Annotated[int, Query(ge=1, le=500)] = 50,
):
    """The recorded decisions this plugin's own /explain appended, newest last."""
    return {"entries": _log.tail(tail)}


@router.get("/rules")
async def api_rules():
    """Declarative policy — the policy endpoint, shaped by ``RouterService``.

    Tiers are copied WHOLE rather than projected field-by-field, which is what
    carries the per-tier knobs (``fallback_strategy``, ``pin_primary``,
    ``billing_mode``, ``requirements``, ``time_policy``, ``time_cap``) and any
    per-elo declared capability override through to the console. An explicit
    projection would silently drop the next field added to a tier — the exact
    failure this endpoint has already suffered once.
    """
    return _service().policy()
