"""Service over the Smart Router policy.

The Dashboard, CLI and Hermes One sidecar must observe the same ``router.yaml``
and core routing functions.  Read paths are fail-safe: they reload the YAML on
every request, expose only non-secret operational state, and perform
deterministic Stage-0 simulations only — they never call the LLM classifier or
mutate breaker state.

Capability-router material rides the same read paths: :meth:`explain` previews
the effective attempt chain (``chain_plan``) with a FIXED-seed rng so a polled
preview does not reshuffle, :meth:`policy` carries the per-tier fallback/
capability/time knobs through to the console, :meth:`status` reports advisory
``warnings`` strictly separately from blocking ``validation_errors``,
:meth:`liveness` marks elos the capability registry cannot describe and reports
each elo's price-window state, and :meth:`capabilities` serves the model
catalogue an operator audits a decision against — the capability facts, the
billing mode, the published prices and the price windows, per model.

THIS MODULE IS THE EDGE THAT READS THE CLOCK. ``signals``, ``rules`` and
``capabilities`` are pure, deterministic and IO-free: the clock is a PARAMETER
they receive, exactly like the ``random.Random`` threaded for the ``random``
fallback strategy. So :func:`_utc_now` is the one wall-clock read here, its value
is passed down as data, and nothing below is asked to look at a clock.
:meth:`explain` accepts an explicit ``at`` for the question the 4am-cron case
asks — "what would this route to at 07:00 UTC?" — and every response says which
clock produced it, because a time-relative answer that does not name its hour is
indistinguishable from a wrong one. The evaluation clock is truncated to the top
of the hour: price windows are declared in whole UTC hours, so the hour is the
resolution that can change an answer, and quantising it is what makes two
previews in the same hour byte-identical while a preview in the next hour is
legitimately allowed to differ.

THE OTHER INPUT A PREVIEW MUST NOT INVENT IS SIZE. Production sizes a turn from
the text the child actually receives — context PLUS goal — and routes on the
``est_input_tokens`` that text produces. A preview that measured the goal line
alone would report 6 tokens for a 33k-token turn, derive no ``min_context``
requirement from it, and show a chain filtered against nothing: a plan that never
existed, on the one surface an operator opens to find out why a turn landed where
it did. So :meth:`explain` accepts ``prompt_text`` — the same parameter, with the
same meaning, as ``adapter.route`` — and reports the ``features`` it measured so
the two surfaces can be compared number by number. The COMPOSITION of context and
goal stays in its single definition (the plugin's ``_compose_prompt``); this
module takes its output as data, because a second copy of that envelope is how
the signal went blind the first time.

The write paths (:meth:`plan`, :meth:`apply`, :meth:`apply_revert`) edit only
``router.yaml`` — the HOT config the router re-reads per request, so a
successful apply is visible with no restart.  Every write is lint-gated,
optimistic-concurrency guarded (a ``base_hash`` mismatch is a 409-style
conflict, never a silent clobber), serialized behind an instance lock (so two
concurrent applies in one process cannot interleave), written atomically via a
temp-file + ``os.replace`` rename, and revertable from a ``.bak`` snapshot.
``config.yaml`` (Hermes core / compaction) is RESTART-class and is deliberately
NOT reachable here.

TWO THINGS THE WRITE PATH HAS TO BE ABLE TO SAY. A mapping knob is REMOVED by
sending an explicit null (``{'tiers': {'T1': {'time_cap': None}}}``): a deep merge
can otherwise only add or overwrite, and two of the four mapping knobs are cost
controls an operator may need to lift in a hurry. And an edit that changes nothing
is reported as ``no_op`` by both halves, because ``valid: True`` / ``ok: True``
about a file that did not change is indistinguishable from a committed edit — that
is exactly how ``{'time_cap': {}}`` came to look like a successful removal.

THE ROUTE-TRACE READERS NAME THE ELO THAT RAN. :meth:`routes` labels each recent
decision with ``decision_log.attempted_head_of`` — the head of the planned chain,
which is what the executor dispatches — and keeps the declared tier primary beside
it as ``declared_model``. Labelling the list with ``output.model`` made the primary
operator surface report a model that was filtered out and never attempted, which is
the whole reason ``output.attempted_model`` is persisted.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import inspect
import json
import os
import random
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml

# Relative first, absolute second — the same two shapes ``rules.py`` resolves in.
# Hermes loads this plugin as ``hermes_plugins.<slug>.router.service``, where
# ``router`` is not a top-level package; the direct source-loading test harnesses
# put it on ``sys.path`` and need the absolute name. There is no third fallback
# here on purpose: these four are hard requirements, so an install genuinely
# missing them must fail loudly at import rather than degrade.
try:
    from .blocklist import Blocklist
    from .rules import explain as rules_explain
    from .rules import lint as rules_lint
    from .signals import extract
except ImportError:  # pragma: no cover - flat layout used by the test harness
    from router.blocklist import Blocklist
    from router.rules import explain as rules_explain
    from router.rules import lint as rules_lint
    from router.signals import extract

# Both are imported defensively: this file is deployed by copy, so it can land
# next to a rules.py/capabilities.py that predates capability routing. Every use
# below degrades to pre-capability behaviour when the symbol is missing — a read
# path must never 500 because a sibling module is older.
#
# Each one tries BOTH shapes before concluding the symbol is absent. Trying only
# the absolute name meant the package shape raised ImportError for the wrong
# reason — module not found, not symbol not found — so both degraded to None on
# every production load and the degrade was indistinguishable from an old
# sibling.
try:
    from .rules import lint_warnings as rules_lint_warnings
except ImportError:  # pragma: no cover - flat layout, or rules.py without warnings
    try:
        from router.rules import lint_warnings as rules_lint_warnings
    except ImportError:  # pragma: no cover - rules.py without advisory warnings
        rules_lint_warnings = None  # type: ignore[assignment]

# Structured jump targets for the errors lint() reports (shadowed pairs today).
# Defensive for the same reason as lint_warnings: an older rules.py without
# lint_findings degrades to all-None targets, which every consumer already
# treats as "no rule to jump to".
try:
    from .rules import lint_findings as rules_lint_findings
except ImportError:  # pragma: no cover - flat layout, or rules.py without findings
    try:
        from router.rules import lint_findings as rules_lint_findings
    except ImportError:  # pragma: no cover - rules.py without structured findings
        rules_lint_findings = None  # type: ignore[assignment]

# The top-level ``price_windows`` overlay merge (spec t_c90c5336). Defensive for
# the same reason as lint_warnings: this file is deployed by copy and can land
# beside a rules.py that predates the overlay, in which case the merge degrades
# to the identity — no overlay, no change — instead of a read path 500.
try:
    from .rules import with_global_price_windows
except ImportError:  # pragma: no cover - flat layout, or rules.py without the overlay
    try:
        from router.rules import with_global_price_windows
    except ImportError:  # pragma: no cover - rules.py predates the overlay
        def with_global_price_windows(config: Dict[str, Any]) -> Dict[str, Any]:
            return config

try:
    from . import capabilities as _caps
except ImportError:  # pragma: no cover - flat layout, or registry absent
    try:
        from router import capabilities as _caps
    except ImportError:  # pragma: no cover - registry absent on an older install
        _caps = None  # type: ignore[assignment]

# The ONE accessor for "which elo did production attempt first" (see
# decision_log's own docstring). Imported rather than re-derived here: this
# surface exists to agree with the executor, and a second reading of "head of the
# chain" is precisely how it came to disagree. Defensive for the reason above —
# a decision_log that predates the key degrades to the declared tier primary,
# which is the honest answer for the entries such a build wrote.
try:
    from .decision_log import attempted_head_of as _attempted_head_of
except ImportError:  # pragma: no cover - flat layout, or an older decision_log
    try:
        from router.decision_log import attempted_head_of as _attempted_head_of
    except ImportError:  # pragma: no cover - decision_log without the accessor
        _attempted_head_of = None  # type: ignore[assignment]

_DEFAULT_MAX_TASK_CHARS = 8_192

# Bound on the composed prompt (``prompt_text``) a preview may be sized from.
# Deliberately far larger than the task bound and NOT the same knob: the task is
# a goal line, the prompt is goal + context and is exactly the thing that has to
# be big for this parameter to be worth having.
#
# WHAT THE BOUND PROTECTS: the substring and regex scans ``signals.extract`` runs
# over the text, which are linear and measured at ~0.35 ms per 1000 chars. 1 MiB
# is therefore ~0.4 s of CPU for one request, on a path a caller reaches over
# HTTP and whose body size the sidecar does not itself limit — so this is the only
# thing between one request and arbitrary CPU, and an unbounded preview is not an
# option.
#
# WHAT IT THEREFORE DOES NOT REACH, stated because the previous version of this
# comment claimed the opposite. 1 MiB is ~291k tokens at the module's
# 3.6-chars-per-token ratio, while the shipped ``huge-context-read`` rule fires
# above 400k ``est_input_tokens`` (~1.44M composed chars) and the capability
# registry holds context windows up to 1,050,000 tokens (~3.8M chars). A turn big
# enough to trigger that rule routes fine in production and is refused HERE. That
# is a real limitation of this surface rather than of the router, so it is named
# in the refusal itself (:meth:`_resolve_prompt`) instead of being left for an
# operator to infer from a bare number — and it is liftable per instance through
# ``max_prompt_chars``, which is also why the knob exists. ``router chain
# --prompt-text``, whose caller is the operator's own shell rather than an HTTP
# client, deliberately has no bound at all and can reproduce such a turn today.
_DEFAULT_MAX_PROMPT_CHARS = 1_048_576

# How the text the preview was sized from was obtained. Reported so an operator
# can tell "this preview measured the goal line" from "this preview measured the
# real composed turn" — the two are the same response shape and wildly different
# answers, which is precisely why the distinction has to be in the payload.
_SIZED_FROM_TASK = "task"
_SIZED_FROM_PROMPT = "prompt_text"

# Fixed seed for :meth:`explain`'s chain preview. explain() is a READ path the
# operator console polls; under ``fallback_strategy: random`` a fresh rng per
# call would reorder the previewed chain on every poll, so the operator could
# never tell a policy change from shuffle noise. A fixed seed makes the preview
# stable across reloads. Production routing injects a request-derived rng
# instead, so real traffic still spreads across the tail.
_EXPLAIN_PREVIEW_SEED = 0

# Whether the installed rules.explain accepts an rng / an injected clock.
# Resolved once each, by signature rather than by catching TypeError, so a
# genuine TypeError raised INSIDE explain is never masked by a silent second
# call. The two are INDEPENDENT: a rules.py may predate either, and losing one
# injected parameter must not cost the other.
try:
    _EXPLAIN_PARAMETERS = frozenset(inspect.signature(rules_explain).parameters)
except (TypeError, ValueError):  # pragma: no cover - unintrospectable callable
    _EXPLAIN_PARAMETERS = frozenset()
_EXPLAIN_ACCEPTS_RNG = "rng" in _EXPLAIN_PARAMETERS
_EXPLAIN_ACCEPTS_WHEN = "when" in _EXPLAIN_PARAMETERS

# What :meth:`RouterService.explain` promises about preview reproducibility,
# reported in the response so "the preview changed" is diagnosable. The fixed
# seed pins the ORDERING; the hour is the one input a later call may legitimately
# differ on, because price windows are declared in whole UTC hours.
_PREVIEW_REPRODUCIBLE_WITHIN = "utc_hour"

# Neutral multiplier — the value ``capabilities.price_multiplier`` returns when no
# window matches or nothing is known about an elo, mirrored here so the degraded
# answer is the registry's own neutral rather than an invented one. Windows are
# declared data, so "differs from the base rate" is asked with a tolerance rather
# than by float equality (mirrors ``capabilities._MULTIPLIER_EPSILON``).
_FLAT_MULTIPLIER = 1.0
_MULTIPLIER_EPSILON = 1e-9

# Keys on a tier / fallback hop / classifier hop that are routing or identity,
# never a capability declaration. Mirrors ``rules._NON_CAPABILITY_KEYS`` so
# liveness's ``capabilities_known`` flag and ``rules.lint_warnings()`` agree on
# which elos are unverifiable. ``provider`` matters most: it IS a registry field,
# so handing it to ``capabilities_for`` as a declared override would make every
# elo in policy look known. ``time_cap``/``time_policy`` are routing knobs of the
# same kind as ``fallback_strategy`` — they say WHEN to use an elo, never what it
# can do — so they are excluded here too, which keeps the mirror exact.
_NON_CAPABILITY_KEYS = frozenset(
    {"model", "provider", "fallback", "fallback_strategy", "pin_primary",
     "requirements", "time_cap", "time_policy"}
)

# ------------------------------------------------------------------
# The capability catalogue view — an ALLOWLIST, never a passthrough
# ------------------------------------------------------------------
# :meth:`RouterService.capabilities` publishes registry material to a browser, so
# WHICH fields it serves is a security decision and is written down here rather
# than inherited. The commercial/identity half is an explicit tuple: handing out
# ``capabilities._REGISTRY_FIELDS`` (or the merged entry whole) would publish
# whatever field the registry grows next, and the day one of those carries a
# credential this read path leaks it with no edit and no review. ``notes`` is
# excluded for the same reason — it is free text, which is exactly where a pasted
# key lands.
#
# The CAPABILITY half is taken from the registry's own
# :data:`capabilities.CAPABILITY_ASSERTION_KEYS`, because that set is closed by
# definition to claims about what a model can do: a new capability the registry
# learns should reach the audit panel without a second edit here, and no
# credential can ever be a capability assertion.
_CATALOGUE_COMMERCIAL_FIELDS: Tuple[str, ...] = (
    "provider",
    "billing_mode",
    "price_in",
    "price_out",
    "price_windows",
    # The HUMAN confirmation date for a model's windows (see
    # capabilities.verified_date_diagnostics). A date string, not free text and
    # not a secret, so it is safe to serve — the console shows "confirmado por
    # uma pessoa em YYYY-MM-DD" beside the window.
    "price_windows_verified",
)

# Used only when the installed capabilities.py predates
# CAPABILITY_ASSERTION_KEYS: this file is deployed by copy (see the import guard
# above), and a catalogue that served no capability facts at all would render
# every elo unverified — the false panel this endpoint exists to remove.
_CATALOGUE_CAPABILITY_FALLBACK: Tuple[str, ...] = (
    "context_window",
    "max_input_tokens",
    "max_output",
    "vision",
    "tool_calling",
    "structured_output",
)


def _catalogue_fields() -> frozenset:
    """The exact field allowlist :meth:`RouterService.capabilities` may serve.

    Fail-safe: an unintrospectable or older registry degrades to the local
    capability tuple instead of raising inside a read path.
    """
    capability_keys: Any = getattr(_caps, "CAPABILITY_ASSERTION_KEYS", None)
    if not isinstance(capability_keys, (set, frozenset)):
        capability_keys = set(_CATALOGUE_CAPABILITY_FALLBACK)
    return frozenset(
        {str(key) for key in capability_keys} | set(_CATALOGUE_COMMERCIAL_FIELDS)
    )


def _is_number(value: Any) -> bool:
    """Whether ``value`` is a real number — ``bool`` is NOT one.

    ``True`` is an ``int`` in Python, and a price of ``True`` would otherwise read
    as a published 1.0. Used to answer "is a price published?" locally when the
    registry cannot be asked.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)

# The only top-level ``router.yaml`` keys an operator may edit through the write
# path. ``fail_safe`` is included (last-resort routing must be editable); every
# other top-level key in a change set is ignored. ``router.yaml`` is HOT
# (re-read per request); ``config.yaml``/compaction is RESTART-class and is not
# routed through here.
#
# Capability routing and the time layer added NO member here, deliberately: the
# per-elo knobs (``fallback_strategy``, ``pin_primary``, ``billing_mode``,
# ``requirements``, ``time_cap``, ``time_policy``, per-elo declared capability and
# ``price_windows`` overrides) all live INSIDE ``tiers``, which is already hot,
# and ``classifier.chain`` lives inside ``classifier``.
# ``_deep_merge_value`` recurses through dicts, so a tier edit carrying them
# round-trips untouched, while a tier's ``fallback`` LIST still replaces
# wholesale — which is exactly what makes reordering hops or deleting an elo
# expressible.
#
# ``price_windows`` IS the one deliberate addition, and it is top-level because
# it has to be: the model-keyed overlay (spec t_c90c5336) is a table the operator
# edits whole, keyed by exact model id, and it is merged OVER the registry and
# UNDER a per-elo ``tier[].price_windows`` declaration. It could not live inside
# ``tiers`` without inventing a tier to host it, and a per-tier spelling would
# invite the divergence the spec forbids (a window edited in one tier and not
# another). Adding the key here widens the write surface by exactly one table,
# gated by the same lint that refuses a malformed window anywhere else.
_HOT_KEYS = frozenset(
    {
        "rules", "default", "tiers", "classifier", "fail_safe", "blocklist",
        "enabled", "price_windows",
    }
)


def _as_list(value: Any) -> List[Any]:
    """Return ``value`` when it is a list, else []. Defensive YAML reader."""
    return value if isinstance(value, list) else []


def _as_mapping(value: Any) -> Dict[str, Any]:
    """Return ``value`` when it is a mapping, else {}. Defensive YAML reader.

    ``yaml.safe_load`` will hand back whatever the file says, so a block an
    operator typo'd into a list or a scalar must not become an ``AttributeError``
    inside a read path.
    """
    return value if isinstance(value, dict) else {}


def _next_window_change(change: Any) -> Optional[Dict[str, Any]]:
    """Normalise ``capabilities.next_window_change``'s answer for a liveness entry.

    The registry answers with ``{hour, weekday, hours_ahead, multiplier}``, and all
    four are carried: a COPY, so a caller mutating the liveness payload cannot edit
    the registry's cached answer, but otherwise verbatim — this module does not get
    to decide which of the registry's fields a console is allowed to see.

    A bare ``int`` is the pre-``weekday`` spelling, kept working because service.py
    is deployed by copy and can land beside an older capabilities.py. The hour is
    preserved and ``weekday``/``hours_ahead``/``multiplier`` report None: an older
    registry genuinely cannot answer the countdown, and defaulting the weekday to
    "today" would manufacture the off-by-two-days error the richer shape was
    introduced to remove. One key, one shape, either way.

    Anything else — including a bool, which is an ``int`` in Python and would
    otherwise render as hour 0 or 1 — is None.
    """
    if isinstance(change, dict):
        return dict(change)
    if isinstance(change, int) and not isinstance(change, bool):
        return {
            "hour": change,
            "weekday": None,
            "hours_ahead": None,
            "multiplier": None,
        }
    return None


def _empty_chain_plan() -> Dict[str, Any]:
    """The chain-plan shape with nothing in it.

    A MIRROR of ``rules._empty_chain_plan()``, phase-2 keys included. A read path
    must be shape-stable, so a rules.py that produced no plan yields this rather
    than a missing key — and the shape it yields has to be the PLANNER's degraded
    shape, not a narrower one, because the console branches on these keys and
    cannot see which module produced the plan it was handed.

    ``time_agnostic: True`` is the load-bearing member. Without it the console's
    ``planWhen`` finds no ``utc_hour`` and no ``time_agnostic``, falls through
    both branches, and prices the plan against the BROWSER's hour — inventing an
    hour for a plan that never saw one, which is the exact failure the
    ``time_agnostic`` flag exists to prevent. Reporting it True says "no clock",
    which is the truth and is a state the console renders.

    ``utc_hour``/``utc_weekday``/``time_cap`` stay ABSENT rather than null, for
    the reason ``rules.plan_chain`` documents: a JSON consumer reads a null hour
    as 0, i.e. midnight, which is a specific and wrong answer where absence is a
    correct one. Absence plus ``time_agnostic: True`` is unambiguous; a null hour
    is not.

    ``decision_log.empty_chain_plan()`` is still the narrow pre-phase-2 shape
    (plus its ``rejected_truncated`` counter, which is that module's own and does
    not belong here). That divergence is deliberate on this side and reported;
    it is not a licence to shrink this one back.
    """
    return {
        "chain": [],
        "requirements": {},
        "rejected": [],
        "unknown": [],
        "bypassed": False,
        "unsatisfiable": [],
        "strategy": "sequential",
        "strategy_declared": "sequential",
        "strategy_degraded": False,
        "strategy_degraded_reason": "",
        "pin_primary": True,
        "independent_rails": 0,
        "time_agnostic": True,
        "time_cap_bypassed": False,
        "capped": [],
        "demoted": [],
        "promoted": [],
        "peak_priced": [],
        "multipliers": {},
    }


# "Delete this key" — the merge result for an explicitly null change. A unique
# object rather than None or a string, so it can never collide with a value an
# operator could legitimately send in a policy edit.
_REMOVE = object()


def _deep_merge_value(old: Any, new: Any) -> Any:
    """Merge ``new`` over ``old``: dicts recurse, everything else REPLACES.

    Lists (rules, manual_ban, fallback_chain, tier.fallback) and scalars replace
    wholesale so an operator can delete or reorder an entry by sending the full
    new list. An index/union merge would make deletion impossible and could
    corrupt rule order, which ``rules.lint`` shadow-detection depends on.

    REMOVAL IS AN EXPLICIT NULL, and it has to be expressible: a recursive dict
    merge can only add or overwrite keys, so ``{'time_cap': {}}`` merges nothing
    over the existing mapping and the edit is a silent no-op — measured on a tier
    carrying ``time_cap``/``time_policy``, which are the two COST controls an
    operator may need to lift in a hurry. ``{'time_cap': None}`` therefore means
    "delete this key", signalled to the caller as :data:`_REMOVE` (a sentinel, not
    ``None``, because ``None`` is also the value being asked about). ``{}`` keeps
    its literal meaning — merge no sub-keys, i.e. change nothing — and
    :meth:`RouterService.plan`/:meth:`~RouterService.apply` now report that as
    ``no_op`` rather than as a successful edit.

    A null can never SET a key here. That is deliberate and matches what the rest
    of this file argues about the time knobs: a JSON consumer reads
    ``Number(null)`` as 0, so a null ``time_cap`` written to the hot file would be
    a ceiling of $0 rather than an absent one. Absence is the only honest way to
    spell "no cap", so a null request produces absence. Nothing is displaced by
    taking the meaning, either: ``rules.lint`` already refuses a null where a
    mapping or a list belongs, so a null change could previously only be rejected.
    """
    if new is None:
        return _REMOVE
    if isinstance(old, dict) and isinstance(new, dict):
        result = dict(old)
        for key, value in new.items():
            merged = _deep_merge_value(result.get(key), value)
            if merged is _REMOVE:
                result.pop(key, None)  # already absent -> still absent
            else:
                result[key] = merged
        return result
    return copy.deepcopy(new)


# ------------------------------------------------------------------
# The injected clock — read HERE (this is the edge), never below
# ------------------------------------------------------------------

def _utc_now() -> datetime:
    """The real current UTC time — the ONLY wall-clock read in this module.

    A module-level function, not an inline call, so the whole time surface has a
    single seam: a test pins the hour by replacing this, exactly as production
    pins it by passing ``at``.
    """
    return datetime.now(timezone.utc)


def _to_utc_hour(when: datetime) -> datetime:
    """``when`` as an aware UTC datetime truncated to the top of the hour.

    An aware datetime is CONVERTED to UTC; a naive one is taken to already be UTC,
    the same reading ``capabilities`` and ``rules`` use, so the reported hour and
    the multipliers applied to it can never come from two different clocks.

    The truncation is the load-bearing part. ``price_windows`` are declared in
    whole UTC hours, so minutes and seconds cannot change any answer — but they
    would change the response bytes, and a preview whose payload churns every
    second is indistinguishable from a nondeterministic one. Quantising makes
    "identical within the hour, free to differ in the next" a property of the
    code rather than a hope.
    """
    stamp = (
        when.astimezone(timezone.utc)
        if when.tzinfo is not None
        else when.replace(tzinfo=timezone.utc)
    )
    return stamp.replace(minute=0, second=0, microsecond=0)


def _resolve_at(at: Optional[Union[datetime, str]]) -> Tuple[datetime, str]:
    """Return ``(when, at_source)`` — the clock to inject and where it came from.

    ``at_source`` is ``now`` or ``explicit``, so a rendered plan says which clock
    produced it.

    ``at`` may be a ``datetime`` (aware or naive-UTC) or an ISO-8601 string, since
    an HTTP query parameter arrives as text and every caller parsing it separately
    is how two surfaces end up disagreeing about what ``07:00`` meant. The CLI's
    bare-hour and ``HH:MM`` sugar stays in the CLI: choosing which DATE a bare hour
    belongs to is an interface decision, and the weekday it picks decides whether
    the weekday-gated zai peak applies.

    Fail-CLOSED on an unusable value: a ``ValueError``, which :meth:`explain`
    already raises for a bad task and the sidecar renders as a 400. Silently
    falling back to "now" would answer a different question than the one asked,
    which for an audit surface is worse than refusing.
    """
    if at is None:
        return _to_utc_hour(_utc_now()), "now"
    if isinstance(at, datetime):
        return _to_utc_hour(at), "explicit"
    if isinstance(at, str):
        return _to_utc_hour(_parse_iso_utc(at)), "explicit"
    raise ValueError("at must be a datetime or an ISO-8601 timestamp")


def _parse_iso_utc(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``.

    ``fromisoformat`` rejects ``Z`` on older interpreters and it is the spelling
    every JSON producer emits, so it is translated rather than refused.
    """
    text = raw.strip()
    iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(
            "at must be an ISO-8601 timestamp, e.g. 2026-08-17T07:00:00Z"
        ) from None


def _clock_features(when: datetime) -> Dict[str, Any]:
    """The two INJECTED time features for the feature vector.

    ``signals.extract()`` is pure and must never read a clock, so the caller adds
    these — the same thing ``adapter.route`` does on the production path. Names and
    ranges are the addendum's: ``utc_hour`` 0-23, ``utc_weekday`` 0=Monday.
    ``when`` is already normalised by :func:`_to_utc_hour`.
    """
    return {"utc_hour": when.hour, "utc_weekday": when.weekday()}


class RouterService:
    """A fail-safe view over one ``router.yaml`` path, with a guarded write path."""

    def __init__(
        self,
        config_path: Path,
        max_task_chars: int = _DEFAULT_MAX_TASK_CHARS,
        max_prompt_chars: int = _DEFAULT_MAX_PROMPT_CHARS,
    ):
        self._config_path = Path(config_path)
        self._max_task_chars = max_task_chars
        # Separate bound for the composed prompt — see
        # :data:`_DEFAULT_MAX_PROMPT_CHARS`. Reusing the task bound would refuse
        # every prompt worth passing.
        self._max_prompt_chars = max_prompt_chars
        # Serializes the read-hash -> lint -> snapshot -> write critical section.
        # ThreadingHTTPServer runs one thread per request over a single shared
        # RouterService; without this, two concurrent applies carrying the same
        # base_hash would both pass the drift check and both write.
        self._write_lock = threading.Lock()
        # Boot provenance, stamped by the sidecar (one_sidecar) so /status can
        # report three ages: process start, code mtime, config mtime. None here
        # means "not served by the sidecar" — status() then omits the two fields
        # rather than inventing ages for a process that never reported them.
        self._process_started_at: Optional[str] = None
        self._code_mtime: Optional[str] = None

    def _load(self) -> Tuple[Dict[str, Any], List[str]]:
        """Return policy plus parse/topology errors instead of raising them."""
        try:
            raw = self._config_path.read_text(encoding="utf-8")
            config = yaml.safe_load(raw) or {}
        except (OSError, yaml.YAMLError) as exc:
            return {}, [f"could not load router config: {exc}"]
        if not isinstance(config, dict):
            return {}, ["router config root must be a mapping"]
        errors = rules_lint(config)
        return config, errors

    def status(self) -> Dict[str, Any]:
        """Compact health snapshot suitable for an operator UI.

        ``validation_errors`` blocks — it is what ``valid`` is computed from and
        what the write gate refuses on. ``error_targets`` rides beside it,
        aligned by index: for every error that NAMES a rule (a shadowed pair,
        today), the structured coordinates a console jumps to; None where the
        error names no rule. ``warnings`` only informs (a tier whose
        two first hops share an upstream still routes; a model missing from the
        capability registry is unverifiable, not wrong; a ``time_cap`` that will
        bypass at some hour is a cost control an operator may knowingly ship), so
        it is kept strictly separate and NEVER flips ``valid``.

        ``warnings`` is whatever ``rules.lint_warnings`` returns, PASSED THROUGH
        UNFILTERED — stringified and reordered by nothing. That pass-through is
        this method's entire contribution to the warning surface, and it is
        deliberately dumb: ``rules.lint_warnings`` owns which findings exist,
        including the ``capabilities.registry_diagnostics()`` self-check it folds
        in, so a finding this method dropped or rewrote would be a finding nobody
        can see. ``status`` is the only caller of ``lint_warnings`` an operator
        ever looks at, which is what makes filtering here unrecoverable rather
        than merely lossy.

        This surface asserts nothing about WHICH findings ``lint_warnings``
        returns — an older rules.py that does not fold the registry check in, or
        does not export ``lint_warnings`` at all, degrades to fewer warnings and a
        still-valid response (see :meth:`_warnings`). The claim here is only that
        whatever arrives, arrives intact.

        Every field is read through a type guard. ``status`` is the endpoint an
        operator hits *because* the config is broken, so it has to survive a
        loadable-but-wrong file (``rules: 5``, ``tiers: nope``): the diagnosis
        belongs in ``validation_errors``, never in a traceback where the one
        surface that could have explained the problem is the one that died of it.

        Provenance fields:
        - ``process_started_at`` — wall-clock start of the sidecar process, captured at boot (ISO 8601 UTC)
        - ``code_mtime`` — mtime of the router/ package directory (ISO 8601 UTC)
        - ``config_mtime`` — mtime of the loaded router.yaml (ISO 8601 UTC)

        These three ages make staleness visible: if ``code_mtime > process_started_at``
        the sidecar is running code older than what is on disk.
        """
        config, errors = self._load()
        classifier = _as_mapping(config.get("classifier"))
        breaker = _as_mapping(_as_mapping(config.get("blocklist")).get("auto_breaker"))
        tiers = config.get("tiers")
        # Provenance: config mtime is read on every request via _load; process start
        # and code mtime are stamped by the sidecar at boot and stay as captured.
        code_mtime = self._code_mtime
        process_started_at = self._process_started_at
        config_mtime = None
        try:
            config_mtime = datetime.fromtimestamp(
                self._config_path.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except OSError:
            pass
        result = {
            "valid": not errors,
            "validation_errors": errors,
            "error_targets": self._error_targets(errors, config),
            "warnings": self._warnings(config),
            "enabled": config.get("enabled", False),
            "rules_count": len(_as_list(config.get("rules"))),
            "tiers": list(tiers.keys()) if isinstance(tiers, dict) else [],
            "classifier": {
                "model": classifier.get("model", ""),
                "provider": classifier.get("provider", ""),
            },
            "breaker_enabled": bool(breaker.get("enabled", False)),
        }
        if process_started_at is not None:
            result["process_started_at"] = process_started_at
        if code_mtime is not None:
            result["code_mtime"] = code_mtime
        if config_mtime is not None:
            result["config_mtime"] = config_mtime
        return result

    def policy(self) -> Dict[str, Any]:
        """Return only the declarative, non-secret policy material.

        Rules are hand-projected onto the closed (id, status, when, then) shape.
        Tiers are NOT projected field-by-field — see :meth:`_policy_tiers` — so
        the per-tier fallback/capability/time knobs (``time_policy``, ``time_cap``)
        reach the console unabridged.

        A ``rules`` value that is not a list degrades to no rules rather than
        raising, for the reason :meth:`status` gives: the console reads this
        endpoint alongside a broken config, and ``status`` is where the breakage is
        described.
        """
        config, _errors = self._load()
        rules = _as_list(config.get("rules"))
        return {
            "rules": [
                {
                    "id": rule.get("id"),
                    "status": rule.get("status", "stable"),
                    "when": rule.get("when", {}),
                    "then": rule.get("then", {}),
                }
                for rule in rules
                if isinstance(rule, dict)
            ],
            "default": config.get("default", {}),
            "tiers": self._policy_tiers(config.get("tiers")),
            "fail_safe": config.get("fail_safe", {}),
            # The model-keyed price-window overlay, served verbatim so the console
            # can render the current table and post edits back through plan/apply.
            "price_windows": config.get("price_windows", {}),
        }

    @staticmethod
    def _warnings(config: Dict[str, Any]) -> List[str]:
        """Advisory lint findings — informational, never blocking.

        Deliberately NOT merged into ``validation_errors``: ``rules.lint`` is the
        fail-closed write gate, so anything reported there stops an operator's
        apply. These findings describe redundancy quality, not validity.

        Fail-safe like every other read: an older rules.py without
        ``lint_warnings``, or one that raises on a malformed config, yields no
        warnings rather than breaking :meth:`status`.
        """
        if rules_lint_warnings is None:
            return []
        try:
            found = rules_lint_warnings(config)
        except Exception:  # noqa: BLE001 - a read path must not raise
            return []
        if not isinstance(found, list):
            return []
        return [str(warning) for warning in found]

    @staticmethod
    def _error_targets(errors: List[str], config: Any) -> List[Any]:
        """One jump target per error, aligned by index; None where there is none.

        ``error_targets[i]`` corresponds to ``errors[i]`` (on /status,
        ``validation_errors[i]``): a dict like ``{code: 'shadowed',
        later_index, later_id, earlier_index, earlier_id, message}`` the
        console can navigate to, or None when the error names no rule. The
        alignment is by the ``message`` lint_findings carries — the exact
        string lint() already produced — so the two lists stay paired without
        lint() itself changing: the write gate is untouched.

        Fail-safe like every other read: an older rules.py without
        lint_findings, or one that raises on a malformed config, yields all
        None rather than breaking :meth:`status` / :meth:`lint`.
        """
        if rules_lint_findings is None:
            return [None] * len(errors)
        try:
            findings = rules_lint_findings(config)
        except Exception:  # noqa: BLE001 - a read path must not raise
            return [None] * len(errors)
        if not isinstance(findings, list):
            return [None] * len(errors)
        by_message = {
            finding["message"]: finding
            for finding in findings
            if isinstance(finding, dict)
            and isinstance(finding.get("message"), str)
        }
        return [by_message.get(error) for error in errors]

    @staticmethod
    def _policy_tiers(tiers: Any) -> Dict[str, Any]:
        """Copy each tier so every declared field reaches the console.

        The capability-routing knobs (``fallback_strategy``, ``pin_primary``,
        ``billing_mode``, ``requirements``), the time knobs (``time_policy`` with
        its ``avoid_peak``/``prefer`` lists, ``time_cap.max_multiplier``) and any
        per-elo declared capability or ``price_windows`` override live INSIDE the
        tier mapping, so copying the mapping whole is what exposes them. An
        explicit field projection was rejected: it would silently drop the next
        field added to a tier, exactly the failure this method exists to prevent.

        The copy is DEEP. The time knobs are the first tier fields that are
        themselves mappings and lists, and the console posts this material back
        through :meth:`plan`/:meth:`apply` — a shallow copy would hand out live
        references into the parsed config, so a console that normalised
        ``time_policy.avoid_peak`` in place would be editing the policy it is
        merely displaying.

        Absent knobs are NOT materialised with their documented defaults
        (``fallback_strategy`` "sequential", ``pin_primary`` True). The console
        posts this same material back through :meth:`plan`/:meth:`apply`, so
        defaulting here would write knobs the operator never chose; the defaults
        stay where they belong, in ``rules.plan_chain``.

        A non-mapping ``tiers`` (bad YAML) degrades to {} instead of handing a
        scalar to a caller that expects a mapping.
        """
        if not isinstance(tiers, dict):
            return {}
        return {name: copy.deepcopy(tier) for name, tier in tiers.items()}

    def blocklist(self) -> Dict[str, Any]:
        """Return manual bans and the real persisted breaker state."""
        config, _errors = self._load()
        blocklist = Blocklist(config)
        return {
            "manual_bans": blocklist.manual_bans(),
            "fallback_chain": blocklist.fallback_chain(),
            "breaker_enabled": blocklist.breaker_enabled(),
            "breaker_cooldowns": blocklist.breaker_status(),
        }

    def liveness(self) -> Dict[str, Any]:
        """Compose policy references with manual-ban and breaker health.

        This is deliberately observational: it reloads policy and persisted
        breaker state but never records, probes, or otherwise mutates either.
        Every returned target has one of four operator-facing states:
        ``alive``, ``degraded``, ``quota_exhausted``, or ``dead``.

        Capability coverage and price-window state ride along as separate FIELDS
        rather than as new states: the four states are a closed set the console
        renders and callers branch on, and a fifth value would break every
        consumer. "The registry has never heard of this elo"
        (``capabilities_known``) and "this elo costs double right now"
        (``in_expensive_window``, ``price_multiplier``, ``next_window_change``) are
        both orthogonal to whether it answers at all — a peak-priced rail is
        perfectly ``alive``, and pricing it as anything else would tell the console
        to route around a rail that is working.

        The window fields are evaluated at the real current UTC hour, reported in
        ``evaluated_at`` so the numbers are attributable to an hour rather than
        floating free. ``next_window_change`` is the UTC hour the multiplier next
        changes, or None when it never does; it is what powers a "peak ends in 2h"
        affordance without this surface having to own scheduling.
        """
        try:
            when = _to_utc_hour(_utc_now())
            config, errors = self._load()
            blocklist = Blocklist(config)
            references = self._policy_references(config, blocklist.fallback_chain())
            # Per-elo YAML declarations only, read off the RAW config: the
            # "declared" origin is decided against THIS index, before the overlay
            # is injected, so an overlay window is never mistaken for a hand-made
            # declaration (or the reverse).
            declared_raw = self._declared_capability_index(config)
            # The effective declared view: the top-level ``price_windows`` overlay
            # merged into the tiers (a copy), so pricing reads the overlay through
            # the same ``declared`` channel a per-elo declaration uses.
            declared_caps = self._declared_capability_index(
                with_global_price_windows(config)
            )
            manual_bans = blocklist.manual_bans()
            breaker_status = {
                entry.get("model_key"): entry
                for entry in blocklist.breaker_status()
                if isinstance(entry, dict) and isinstance(entry.get("model_key"), str)
            }

            models: List[Dict[str, Any]] = []
            for model, provider in references:
                key = f"{model}@{provider}"
                breaker = breaker_status.get(key, {})
                if self._is_manually_banned(manual_bans, model, provider):
                    state = "dead"
                elif breaker.get("state") == "OPEN" and breaker.get(
                    "last_failure_kind"
                ) == "quota_exhausted":
                    state = "quota_exhausted"
                elif breaker.get("state") in ("OPEN", "HALF_OPEN"):
                    state = "degraded"
                else:
                    state = "alive"
                record: Dict[str, Any] = {
                    "model_key": key,
                    "model": model,
                    "provider": provider,
                    "state": state,
                    # Extra fields, NOT a fifth state — see the docstring.
                    "capabilities_known": self._capabilities_known(
                        model, declared_caps.get(model)
                    ),
                    "breaker": breaker,
                }
                record.update(
                    self._time_state(
                        model,
                        declared_caps.get(model),
                        when,
                        origin=self._price_windows_origin(
                            config, declared_raw, model
                        ),
                    )
                )
                windows = self._served_windows(model, declared_caps.get(model))
                if windows:
                    record["price_windows"] = windows
                models.append(record)

            worst = max((entry["state"] for entry in models), key=self._liveness_rank, default="alive")
            result: Dict[str, Any] = {
                "models": models,
                "worst": worst,
                "evaluated_at": {
                    "at": when.isoformat(),
                    "at_source": "now",
                    "utc_hour": when.hour,
                    "utc_weekday": when.weekday(),
                },
            }
            if errors:
                result["validation_errors"] = errors
            return result
        except Exception as exc:
            return {
                "models": [],
                "worst": "degraded",
                "error": f"could not compose liveness: {exc}",
            }

    @staticmethod
    def _policy_references(
        config: Dict[str, Any], fallback_chain: List[str]
    ) -> List[Tuple[str, str]]:
        """Return unique ``(model, provider)`` pairs declared by policy.

        The classifier is now a CHAIN (``classifier.chain``) with the top-level
        ``model``/``provider`` kept as a compatibility mirror of ``chain[0]``.
        Both are walked so liveness covers every classifier hop, and ``add()``
        de-duplicates, so the mirrored entry is never counted twice.
        """
        references: List[Tuple[str, str]] = []

        def add(item: Any) -> None:
            if not isinstance(item, dict):
                return
            model = item.get("model")
            provider = item.get("provider")
            if not isinstance(model, str) or not model or not isinstance(provider, str) or not provider:
                return
            pair = (model, provider)
            if pair not in references:
                references.append(pair)

        classifier = config.get("classifier", {})
        add(classifier)  # compatibility mirror of chain[0]
        if isinstance(classifier, dict):
            for hop in _as_list(classifier.get("chain")):
                add(hop)
        tiers = config.get("tiers", {})
        if isinstance(tiers, dict):
            for tier in tiers.values():
                add(tier)
                if isinstance(tier, dict):
                    for fallback in tier.get("fallback", []) or []:
                        add(fallback)
        fail_safe = config.get("fail_safe", {})
        add(fail_safe)
        if isinstance(fail_safe, dict):
            for fallback in fail_safe.get("fallback", []) or []:
                add(fallback)

        # The historical fallback chain stores model names. Map each one to
        # every provider already declared elsewhere in policy; no provider is
        # invented for an unknown chain entry.
        known_models = {model for model, _provider in references}
        for fallback in fallback_chain:
            if isinstance(fallback, dict):
                add(fallback)
            elif isinstance(fallback, str) and fallback in known_models:
                continue
        return sorted(references)

    @staticmethod
    def _declared_capability_index(
        config: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        """Map model id -> capability keys policy declares for it.

        Walked so that ``capabilities_known`` honours the registry contract:
        ``declared`` WINS over the registry, so an elo the registry has never
        heard of is still described — and must not be flagged — when the YAML
        describes it. Identity/routing keys are excluded (see
        :data:`_NON_CAPABILITY_KEYS`); anything else is handed to
        ``capabilities_for``, which drops the fields it does not recognize, so
        stray tuning keys (``temperature``, ``timeout_seconds``) cannot fake
        knowledge.
        """
        index: Dict[str, Dict[str, Any]] = {}

        def collect(entry: Any) -> None:
            if not isinstance(entry, dict):
                return
            model = entry.get("model")
            if not isinstance(model, str) or not model:
                return
            declared = {
                key: value
                for key, value in entry.items()
                if key not in _NON_CAPABILITY_KEYS
            }
            if declared:
                index.setdefault(model, {}).update(declared)

        for block in (config.get("classifier"), config.get("fail_safe")):
            collect(block)
            if isinstance(block, dict):
                for hop in _as_list(block.get("chain")) + _as_list(
                    block.get("fallback")
                ):
                    collect(hop)

        tiers = config.get("tiers")
        if isinstance(tiers, dict):
            for tier in tiers.values():
                collect(tier)
                if isinstance(tier, dict):
                    for hop in _as_list(tier.get("fallback")):
                        collect(hop)
        return index

    @staticmethod
    def _capabilities_known(
        model: str, declared: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Whether the capability registry (or policy) can describe ``model``.

        Fail-OPEN, matching the registry's own posture: with no registry
        importable, or a registry that raises, nothing is provably unknown, so
        this reports True instead of nagging the operator about every elo.
        """
        if _caps is None:
            return True
        try:
            return _caps.capabilities_for(model, declared or None) is not None
        except (AttributeError, TypeError, ValueError):
            return True

    @staticmethod
    def _time_state(
        model: str,
        declared: Optional[Dict[str, Any]],
        when: datetime,
        origin: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Price-window state for one elo at ``when``.

        Four EXTRA FIELDS on a liveness entry, never a new ``state`` value:

          ``in_expensive_window``  True only inside a ``multiplier > 1.0`` window,
              so xiaomi's 0.8x night discount — also a window — never reads as
              "avoid this now".
          ``price_multiplier``     what this elo costs right now relative to its
              stored base rate.
          ``next_window_change``   WHEN that multiplier next changes and to what
              (``{hour, weekday, hours_ahead, multiplier}``), or None when it never
              does (a flat-priced or unknown elo).
          ``price_windows_origin`` WHICH source produced the windows the numbers
              above came from — ``declared`` (a per-elo YAML override), ``overlay``
              (the top-level ``price_windows`` table) or ``registry`` (the code
              registry). This function cannot decide it (the source is a property
              of the CONFIG, not of the merged ``declared`` it is handed), so it is
              INJECTED as ``origin`` by the caller; the neutral degrade reports
              None, "no source attributable".

        ``next_window_change`` is carried through as the registry's own MAPPING,
        not reduced to its hour. The day and ``hours_ahead`` are load-bearing: a
        weekday-gated window makes a bare hour ambiguous by up to two days, so a
        console rendering "peak ends in 2h" from an hour alone would be wrong by 45
        hours every weekend. A registry old enough to answer with a bare int still
        works — the hour is kept and the rest reported as unknown rather than
        guessed as "today", which is the exact guess the richer shape exists to
        stop.

        ``declared`` carries the per-elo overrides policy declares, so an operator
        who corrected a stale window in YAML sees the corrected window here.

        Degrades to the neutral 1.0 / not-expensive / no-change / origin-None answer
        when the registry is absent, raises, or answers with something that is not a
        number. That is the registry's OWN neutral for "no window matched or nothing
        is known", not an invented number, and
        ``capabilities_known`` sits beside it to tell an operator which of the two
        they are looking at. Degrading per elo rather than per response is
        deliberate: one unpriceable elo must not blank the price state of every
        other rail.
        """
        neutral: Dict[str, Any] = {
            "in_expensive_window": False,
            "price_multiplier": _FLAT_MULTIPLIER,
            "next_window_change": None,
            "price_windows_origin": origin,
        }
        if _caps is None:
            return neutral
        overrides = declared or None
        try:
            multiplier = _caps.price_multiplier(model, when, overrides)
            expensive = _caps.in_expensive_window(model, when, overrides)
            change = _caps.next_window_change(model, when, overrides)
        except (AttributeError, TypeError, ValueError):
            return neutral
        if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
            return neutral
        return {
            "in_expensive_window": bool(expensive),
            "price_multiplier": float(multiplier),
            "next_window_change": _next_window_change(change),
            "price_windows_origin": origin,
        }

    @staticmethod
    def _price_windows_origin(
        config: Any,
        declared_raw: Any,
        model: str,
    ) -> Optional[str]:
        """Which source produced ``model``'s price windows — a fact, not a deduction.

        The three sources, in precedence order (spec t_c90c5336):

          * ``declared``  a per-elo ``price_windows`` the operator wrote on a
              tier or fallback hop in router.yaml — the deliberate local exception;
          * ``overlay``   the top-level ``price_windows`` table;
          * ``registry``  the code registry (or "no windows anywhere": flat
              pricing is still the registry's own answer).

        ``declared_raw`` must be the index built from the RAW config, BEFORE the
        overlay is injected — the whole point of this function is to tell the two
        YAML sources apart, and a merged index has already blurred them. Returns
        None only when ``declared_raw`` cannot be trusted (a non-mapping), which
        the caller reports as "no source attributable".
        """
        if not isinstance(declared_raw, dict):
            return None
        declared = declared_raw.get(model)
        if isinstance(declared, dict) and "price_windows" in declared:
            return "declared"
        overlay = config.get("price_windows") if isinstance(config, dict) else None
        if isinstance(overlay, dict) and model in overlay:
            return "overlay"
        return "registry"

    @staticmethod
    def _served_windows(
        model: str, declared: Optional[Dict[str, Any]]
    ) -> Optional[List[Dict[str, Any]]]:
        """The windows list in force for ``model``, or None when it has none.

        Read through ``capabilities_for`` — the same merge, with the same
        precedence, the running path prices with — so a liveness entry can never
        describe a window the router is not applying. None means "no time-varying
        price" (flat, or the model is unknown to the registry), and an empty list
        is treated the same way: ``[]`` is the operator's explicit "flatten this
        model", so serving it as a window list would contradict what it means.
        """
        if _caps is None:
            return None
        try:
            caps = _caps.capabilities_for(model, declared or None)
        except (AttributeError, TypeError, ValueError):
            return None
        if not isinstance(caps, dict):
            return None
        windows = caps.get("price_windows")
        if not isinstance(windows, list) or not windows:
            return None
        return [
            dict(window) if isinstance(window, dict) else window
            for window in windows
        ]

    @staticmethod
    def _is_manually_banned(
        bans: List[Dict[str, str]], model: str, provider: str
    ) -> bool:
        """Match manual bans with the same model/provider semantics as Blocklist."""
        for ban in bans:
            if not isinstance(ban, dict):
                continue
            ban_model = str(ban.get("model", ""))
            ban_provider = str(ban.get("provider", ""))
            if ban_model and ban_model.lower() != model.lower():
                continue
            if not ban_provider or ban_provider.lower() == provider.lower():
                return True
        return False

    @staticmethod
    def _liveness_rank(state: str) -> int:
        return {"alive": 0, "degraded": 1, "quota_exhausted": 2, "dead": 3}.get(
            state, 1
        )

    # ------------------------------------------------------------------
    # The capability catalogue — what an operator audits a decision against
    # ------------------------------------------------------------------

    def capabilities(self) -> Dict[str, Any]:
        """Return the model catalogue: capability facts, billing, prices, windows.

        This is the read path behind the console's price audit. Without it the
        panel does not merely render empty, it renders FALSE: with no catalogue
        every elo shows as capability-unverified and every rail shows as
        publishing no per-token price, including the metered ones that publish
        one. A panel that states the opposite of the truth is worse than an absent
        panel, which is why the shape below is fixed by what the audit has to
        answer rather than by what was convenient to serialize.

        Shape::

            {"models": {"<model>": {<capability facts>, <commercial fields>,
                                    "price_published": bool,
                                    "in_registry": bool,
                                    "declared_overrides": ["billing_mode", ...]}},
             "unknown_models": ["<model>", ...],
             "warnings": ["model '<id>' is declared in policy but ..."],
             "registry_available": bool,
             "time_agnostic": True}

        NO PRICE PUBLISHED IS NOT A PRICE OF ZERO, and keeping those two apart is
        the whole point of the panel. ``price_in``/``price_out`` are served
        VERBATIM — ``None`` when the vendor publishes no per-token rate, ``0.0``
        when the rail is genuinely free — and ``price_published`` states which of
        the two an operator is looking at. A plan rail bills in credits off an
        allowance already bought; rendering that as ``$0`` would make it look like
        the cheapest thing on the screen when it is merely the least priced.

        ``price_published`` IS ASKED OF THE RUNNING PATH
        (``capabilities.effective_price``), not recomputed from the served fields.
        ``effective_price`` is what ``cheapest_now`` ranks on, so asking it is what
        makes this surface incapable of disagreeing with the ordering it is
        auditing — the failure mode where a decision is verified through the
        surface that displays it rather than the path that runs it. Only when the
        registry cannot be asked at all does the local ``price_in``/``price_out``
        test stand in.

        TIME-AGNOSTIC BY CONSTRUCTION. No clock is read here and none is applied:
        the prices are the BASE rates, i.e. what the model costs OUTSIDE every
        declared window, and ``price_windows`` is served as declared so the
        consumer can price any hour it likes. ``liveness`` is the surface that
        reports what an elo costs right NOW, evaluated at an hour it names.
        ``time_agnostic: True`` says so in the payload rather than leaving a
        consumer to assume which of the two it received.

        NOTHING IS INVENTED. Every fact is whatever ``capabilities.capabilities_for``
        returns for that model merged with the per-elo ``declared`` overrides
        policy declares — the same merge, with the same precedence, that the filter
        runs on — so a stale rate an operator corrected in router.yaml shows up as
        THEIR number, named in ``declared_overrides``. A field the registry does
        not hold is absent, never defaulted.

        AN UNKNOWN MODEL IS FLAGGED, NEVER FABRICATED. A model policy names that
        the registry cannot describe is listed in ``unknown_models`` with a loud
        ``warnings`` string and is deliberately ABSENT from ``models``: a console
        reads the presence of an entry as "this elo's capabilities are verified",
        so serving a hollow one would silence the unknown-model flag on exactly the
        elos that route unchecked. Dropping such an elo from a chain could empty
        it, so it still routes — it just routes visibly unverified.

        SERVES NO SECRET. Fields are ALLOWLISTED (see
        :func:`_catalogue_fields`), not passed through, so a registry that later
        grows a field carrying a credential does not publish it here by default.

        Fail-safe like every other read path: never raises. A missing registry, a
        corrupt config or a registry that raises degrades to an empty catalogue
        with the reason attached, which a console renders as "unverified" — the
        honest answer for "the catalogue could not be read".
        """
        result: Dict[str, Any] = {
            "models": {},
            "unknown_models": [],
            "warnings": [],
            "registry_available": _caps is not None,
            "time_agnostic": True,
        }
        try:
            config, _errors = self._load()
            # Two indexes: the raw one names what the operator WROTE (origin
            # "declared", ``declared_overrides``), and the injected one carries the
            # top-level ``price_windows`` overlay merged in, so the catalogue
            # describes the same windows the running path prices with.
            declared_raw = self._declared_capability_index(config)
            declared_index = self._declared_capability_index(
                with_global_price_windows(config)
            )
            providers = self._policy_provider_index(config)
            fields = _catalogue_fields()

            registry = getattr(_caps, "MODEL_CAPABILITIES", None) if _caps else None
            if not isinstance(registry, dict):
                registry = {}

            models: Dict[str, Any] = {}
            unknown: List[str] = []
            warnings: List[str] = []
            # Every model the registry holds, every model policy DESCRIBES, and
            # every model policy merely NAMES. The third set is what makes the
            # unknown-model flag complete: an elo that declares no capability at
            # all appears in neither of the first two, and it is exactly the elo
            # that routes unchecked and has to be reported for it.
            #
            # Sorted so two reads in a row are byte-identical: this endpoint is
            # polled, and a response whose key order churns is indistinguishable
            # from one whose content changed.
            candidates = set(registry) | set(declared_index) | set(providers)
            for model in sorted(candidates):
                declared = declared_index.get(model) or None
                caps = self._merged_capabilities(model, declared)
                if caps is None:
                    unknown.append(model)
                    warnings.append(
                        f"model '{model}' is declared in policy but unknown to the "
                        f"capability registry; it routes UNCHECKED"
                    )
                    continue
                entry = {
                    key: value for key, value in caps.items() if key in fields
                }
                # Identity, not capability: the registry's own provider wins, and
                # policy's fills the gap only for an elo the registry has never
                # heard of but the YAML describes. It is never passed to
                # ``capabilities_for`` (see :data:`_NON_CAPABILITY_KEYS`), so it
                # cannot make a model look known.
                if not entry.get("provider") and model in providers:
                    entry["provider"] = providers[model]
                entry["price_published"] = self._price_published(
                    model, declared, caps
                )
                entry["in_registry"] = model in registry
                entry["price_windows_origin"] = self._price_windows_origin(
                    config, declared_raw, model
                )
                # ``declared_overrides`` names what the OPERATOR wrote, never the
                # injected overlay, so it reads off the RAW index.
                entry["declared_overrides"] = sorted(
                    key for key in (declared_raw.get(model) or {}) if key in fields
                )
                # The explicit empty overlay list means flat pricing: no windows to
                # serve, so the key is dropped rather than served as [] — a list
                # that reads as "windowed" when the operator asked for the opposite.
                if entry.get("price_windows") == []:
                    entry.pop("price_windows")
                models[model] = entry

            result["models"] = models
            result["unknown_models"] = unknown
            result["warnings"] = warnings
            return result
        except Exception as exc:  # noqa: BLE001 - a read path must not raise
            result["error"] = f"could not compose capabilities: {exc}"
            return result

    @staticmethod
    def _merged_capabilities(
        model: str, declared: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """The registry entry for ``model`` merged with policy's ``declared``.

        Exactly ``capabilities.capabilities_for`` — the same call, the same
        precedence, the same "declared wins" rule the capability filter runs on —
        so the catalogue cannot describe an elo differently from the path that
        decides with it. None means the model is unknown to both.

        Degrades to None on an absent or raising registry rather than propagating,
        which the caller reports as unknown-and-flagged instead of as an entry
        nobody can vouch for.
        """
        if _caps is None:
            return None
        try:
            caps = _caps.capabilities_for(model, declared or None)
        except (AttributeError, TypeError, ValueError):
            return None
        return caps if isinstance(caps, dict) else None

    @staticmethod
    def _price_published(
        model: str, declared: Optional[Dict[str, Any]], caps: Dict[str, Any]
    ) -> bool:
        """Whether ``model`` publishes a per-token price — asked of the RUNNING path.

        ``capabilities.effective_price`` is what ``cheapest_now`` ranks on, and it
        already owns every edge of this question: a plan rail's ``None``, a
        genuinely free rail's published ``0.0``, and a half-published pair whose
        missing half would have to be invented. Asking it with ``when=None``
        (time-agnostic: no clock is read on this path) answers precisely "is there
        a base rate to scale?", so the audit panel and the cost comparison can
        never disagree about which elos are priced.

        The local test is a FALLBACK for an install whose capabilities.py has no
        ``effective_price``, and it mirrors that function's rule: both halves must
        be real numbers, and a ``bool`` is not one.
        """
        if _caps is not None:
            try:
                return _caps.effective_price(model, None, declared or None) is not None
            except (AttributeError, TypeError, ValueError):
                pass
        return _is_number(caps.get("price_in")) and _is_number(caps.get("price_out"))

    @classmethod
    def _policy_provider_index(cls, config: Dict[str, Any]) -> Dict[str, str]:
        """Map model id -> the provider policy declares for it (first wins).

        Read off :meth:`_policy_references`, which already walks every tier,
        fallback hop, classifier hop and ``fail_safe`` and returns sorted unique
        pairs — so "first wins" is deterministic rather than YAML-order-dependent.
        Used only to fill a provider the registry does not hold; see
        :meth:`capabilities`.
        """
        index: Dict[str, str] = {}
        try:
            references = cls._policy_references(config, [])
        except Exception:  # noqa: BLE001 - a read path must not raise
            return index
        for model, provider in references:
            index.setdefault(model, provider)
        return index

    def explain(
        self,
        task: str,
        at: Optional[Union[datetime, str]] = None,
        prompt_text: str = "",
    ) -> Dict[str, Any]:
        """Run a deterministic Stage-0 dry-run without invoking a classifier.

        The response carries the decision trace plus ``chain_plan`` — the
        effective attempt chain: derived capability requirements, the elos that
        survived the filter in the order they would be tried, the elos that were
        rejected with their reason, the fallback strategy, the per-elo price
        multipliers that were in force, the time-layer flags that fired
        (``capped``, ``demoted``, ``promoted``, ``time_cap_bypassed``) and the
        count of independent upstream rails.

        ``chain_plan`` is lifted to the TOP LEVEL as well as staying inside
        ``decision``, so a console reads one key and gets a stable shape even
        from a rules.py that produces no plan.

        THE CLOCK. The plan is evaluated at a real time, defaulting to the current
        UTC hour, because a console showing a time-agnostic plan would show
        multipliers of 1.0, an inert ``time_cap`` and a declared rather than a
        ``cheapest_now`` order — i.e. a plan production would never produce. ``at``
        overrides it (a ``datetime`` or an ISO-8601 string) so the console and CLI
        can ask "what would this route to at 07:00 UTC?", which is exactly the
        question the 4am cron raises, and ``utc_hour``/``utc_weekday`` are injected
        into the feature vector, so a time-keyed rule is as live here as it is in
        production. ``evaluated_at`` reports which clock was used, where it came
        from, and whether it actually reached the planner.

        THE SIZE. ``task`` is the GOAL. ``prompt_text`` is the full text the model
        would actually receive (context + goal) and defaults to ``task`` — the same
        parameter, spelled the same way and meaning the same thing, as
        ``adapter.route``. Signals are read from it, because ``est_input_tokens``
        has to measure the real input: a 120k-char context routes on 33344
        estimated tokens and a ``min_context`` floor derived from them, while the
        goal line alone measures 6 and derives no floor at all. Without this
        parameter the router's own diagnostic answers a different question than the
        path that runs, and an operator investigating a context-heavy turn is shown
        a plan that never existed.

        The composition of context and goal is NOT re-implemented here: the caller
        passes the already-composed text, so the plugin's ``_compose_prompt`` stays
        the single definition of that envelope. The measured vector is returned as
        ``features`` and ``preview.sized_from``/``preview.prompt_chars`` name which
        text produced it, so "this preview measured the goal line" is visible
        rather than inferred.

        REPRODUCIBILITY, and why it is not the same thing as time-independence:

        * The ORDERING is reproducible. A FIXED-seed ``random.Random`` is injected
          (see :data:`_EXPLAIN_PREVIEW_SEED`), so under ``fallback_strategy:
          random`` a polled preview does not reshuffle and an operator can tell a
          policy change from shuffle noise. Production routing injects a
          request-derived rng instead, so real traffic still spreads.
        * The HOUR is not held still, and must not be. Under ``cheapest_now``, a
          ``time_cap`` or a ``time_policy``, a call in the next hour may
          legitimately order differently — that is the feature working. So the
          evaluation clock is truncated to the hour (see :func:`_to_utc_hour`):
          two calls seconds apart return byte-identical responses, while a call in
          another hour is free to differ, and ``preview`` says which of the two an
          operator is looking at (``reproducible_within: utc_hour`` plus
          ``time_relative`` and the reasons it is). "The preview changed" is
          therefore diagnosable as "the hour changed" instead of reading as
          nondeterminism.

        Raises ValueError for an empty or oversized task, an oversized or
        non-string ``prompt_text``, an unusable ``at``, or an invalid policy — the
        sidecar renders each as a 400.
        """
        task = task.strip()
        if not task:
            raise ValueError("task is required")
        if len(task) > self._max_task_chars:
            raise ValueError(f"task exceeds {self._max_task_chars} characters")
        # Both resolved before the config is read: an unusable clock or an
        # unusable prompt is the CALLER's error, and reporting either as "policy is
        # invalid" would send an operator to the wrong file.
        prompt, sized_from = self._resolve_prompt(task, prompt_text)
        when, at_source = _resolve_at(at)

        config, errors = self._load()
        if errors:
            raise ValueError("router policy is invalid")
        # The top-level ``price_windows`` overlay is merged into the tiers so the
        # dry-run prices the same windows the production path (adapter.route) does
        # — a preview that ignored the overlay would answer a different question
        # than the path it exists to reproduce.
        config = with_global_price_windows(config)
        features = self._explain_features(prompt, when)
        decision = self._explain_decision(task, features, config, when)
        plan = self._chain_plan_of(decision)
        requires_classifier = decision.get("output", {}).get("action") == "classify"
        return {
            "mode": "deterministic_dry_run",
            "requires_classifier": requires_classifier,
            "decision": decision,
            "chain_plan": plan,
            # The vector the plan was derived from. Reported because the numbers
            # that decide a route (est_input_tokens, needs_vision) are otherwise
            # only visible where a rule happened to match on them, and "why did
            # this turn route here" is exactly a question about those numbers.
            "features": features,
            "evaluated_at": self._evaluated_at(when, at_source, plan),
            "preview": self._preview_note(decision, plan, sized_from, len(prompt)),
        }

    def _resolve_prompt(self, task: str, prompt_text: str) -> Tuple[str, str]:
        """Return ``(text_to_size_from, sized_from)``.

        ``prompt_text or task`` — the SAME falsy test ``adapter.route`` uses, so a
        caller that supplies no context is measured exactly as production measures
        it, and one that supplies whitespace is measured exactly as production
        would measure that too. Being cleverer here (stripping, or treating
        whitespace as absent) would make the preview disagree with the path it
        exists to reproduce, in the one direction that matters: char_len.

        Fail-CLOSED on an unusable value, like every other explain input. A
        non-string is a caller bug reported as a 400 rather than an
        ``AttributeError`` out of a read path; an over-bound prompt is refused
        rather than silently truncated, because a truncated prompt would produce a
        smaller ``est_input_tokens`` and therefore a confidently wrong plan — the
        precise failure this parameter exists to fix.

        THE REFUSAL NAMES THE LIMITATION, because this is where an operator meets
        it. The default bound is ~291k estimated tokens and the shipped
        ``huge-context-read`` rule fires above 400k, so the one rule whose whole
        subject is a giant read cannot be previewed at the default (see
        :data:`_DEFAULT_MAX_PROMPT_CHARS` for why the bound stays). A bare
        "exceeds N characters" reads as "the router cannot route a turn this big",
        which is false and is the same defect as any other surface that answers a
        different question than the path it displays: so the message says the bound
        is this preview's CPU budget and names both ways past it.
        """
        if not isinstance(prompt_text, str):
            raise ValueError("prompt_text must be a string")
        if not prompt_text:
            return task, _SIZED_FROM_TASK
        if len(prompt_text) > self._max_prompt_chars:
            raise ValueError(
                f"prompt_text exceeds {self._max_prompt_chars} characters: this "
                "preview's CPU bound, not a routing limit — production routes "
                "turns larger than this. Raise max_prompt_chars on the service, "
                "or use `router chain --prompt-text`, which has no bound"
            )
        return prompt_text, _SIZED_FROM_PROMPT

    @staticmethod
    def _explain_features(prompt: str, when: datetime) -> Dict[str, Any]:
        """The feature vector for a preview: extracted signals plus the clock.

        The two clock features are added HERE, exactly as ``adapter.route`` does on
        the production path: ``signals.extract()`` is pure and must never read a
        clock, and a rule keyed on ``utc_hour`` has to fire in the console or the
        operator's preview would answer a question production does not ask.

        ``prompt`` is the text production would size the turn from, not the goal
        line — see :meth:`_resolve_prompt`.
        """
        features = extract(prompt)
        features.update(_clock_features(when))
        return features

    @staticmethod
    def _explain_decision(
        task: str,
        features: Dict[str, Any],
        config: Dict[str, Any],
        when: datetime,
    ) -> Dict[str, Any]:
        """Call ``rules.explain`` with the fixed-seed preview rng and the clock.

        ``task`` stays the goal line while ``features`` were measured from the
        composed prompt — the same split production makes, where the classifier and
        the response cache key on the goal alone and only the signals see the
        context.

        Each injected parameter is passed only when the installed ``rules.explain``
        declares it. service.py is deployed by file copy, so it can land beside a
        rules.py that predates either injection; losing one must not cost the
        other, and neither may become a TypeError inside a read path.
        """
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
            # A rules.py predating fallback strategies orders chains
            # sequentially, so the preview is stable without an rng anyway.
            kwargs["rng"] = random.Random(_EXPLAIN_PREVIEW_SEED)
        if _EXPLAIN_ACCEPTS_WHEN:
            # A rules.py predating the time layer plans time-agnostically; the
            # response then reports time_aware False rather than claiming an hour.
            kwargs["when"] = when
        return rules_explain(*args, **kwargs)

    @staticmethod
    def _chain_plan_of(decision: Dict[str, Any]) -> Dict[str, Any]:
        """Lift ``rules.explain``'s ``chain_plan`` out of the decision trace."""
        plan = decision.get("chain_plan") if isinstance(decision, dict) else None
        return plan if isinstance(plan, dict) else _empty_chain_plan()

    @staticmethod
    def _evaluated_at(
        when: datetime, at_source: str, plan: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Which clock the preview was evaluated at, and whether it landed.

        Key names AND VALUES mirror the CLI's ``--at`` payload (``at``,
        ``at_source``, ``utc_hour``, ``utc_weekday``) so one console can render
        either surface without a translation table. The values matter as much as
        the names: ``at_source`` is ``now`` or ``explicit`` here and the CLI reports
        the same two words for the same two cases rather than the flag spelling
        (``--at``), because a field whose vocabulary depends on which surface
        produced it is a translation table wearing the same key name. The CLI has
        one value this surface never emits, ``time-agnostic``: it can be asked to
        plan with no clock at all, whereas ``explain`` always injects one (there is
        no HTTP way to ask for a clockless preview, and a console that rendered
        multipliers of 1.0 as fact would be worse than one that names an hour).

        ``time_aware`` is read back OFF THE PLAN rather than asserted from the fact
        that a clock was passed: the planner is the only thing that knows whether
        the clock reached it, and an older rules.py that cannot accept one plans
        time-agnostically. Claiming an hour a plan never saw is the exact failure
        ``rules``' ``time_agnostic`` flag exists to prevent, so the honest report is
        "here is the hour I asked about, and no, it was not used".
        """
        return {
            "at": when.isoformat(),
            "at_source": at_source,
            "utc_hour": when.hour,
            "utc_weekday": when.weekday(),
            "time_aware": plan.get("time_agnostic") is False,
        }

    @staticmethod
    def _preview_note(
        decision: Dict[str, Any],
        plan: Dict[str, Any],
        sized_from: str = _SIZED_FROM_TASK,
        prompt_chars: int = 0,
    ) -> Dict[str, Any]:
        """What is reproducible about this preview, and what it was measured from.

        ``seed`` and ``reproducible_within`` are the guarantee: same task, same
        prompt, same policy, same UTC hour => the same response, shuffle included.
        ``time_relative`` and ``time_relative_reasons`` are the disclaimer: this
        plan's content came partly from the hour, so the same call in the next hour
        may legitimately differ. Without the pair, an operator watching a
        ``cheapest_now`` tier reorder itself at 06:00 has no way to tell the
        feature working from the preview being unstable.

        ``sized_from`` and ``prompt_chars`` are the other disclaimer, and the more
        easily missed one: a preview sized from the goal line looks exactly like a
        preview sized from the real turn, and answers a different question. Naming
        the text is what lets an operator see that a chain "filtered against
        nothing" was filtered against six tokens of goal, not against the 120k
        chars production actually sent.
        """
        reasons = RouterService._time_relative_reasons(decision, plan)
        return {
            "seed": _EXPLAIN_PREVIEW_SEED,
            "reproducible_within": _PREVIEW_REPRODUCIBLE_WITHIN,
            "time_relative": bool(reasons),
            "time_relative_reasons": reasons,
            "sized_from": sized_from,
            "prompt_chars": prompt_chars,
        }

    @staticmethod
    def _time_relative_reasons(
        decision: Dict[str, Any], plan: Dict[str, Any]
    ) -> List[str]:
        """Why this preview depends on the hour it was evaluated at.

        A closed set, reported in a fixed order so two responses can be diffed:

          ``cheapest_now``  the ORDER came from effective prices at this hour.
          ``time_cap``      a price ceiling was evaluated against this hour.
          ``time_policy``   the tier declares ``avoid_peak``/``prefer``, which are
              decided per hour — reported from the DECLARED knob rather than from
              whether it moved anything, because a policy that moves nothing at
              this hour is precisely the one that will move something at another.
          ``price_window``  at least one elo is priced off its base rate now, so
              the reported multipliers are hour-specific even if the order is not.

        Empty when no clock reached the planner: nothing that was not evaluated
        against an hour can depend on one.
        """
        if plan.get("time_agnostic") is not False:
            return []

        reasons: List[str] = []
        if plan.get("strategy") == "cheapest_now":
            reasons.append("cheapest_now")
        if isinstance(plan.get("time_cap"), dict):
            reasons.append("time_cap")
        output = decision.get("output") if isinstance(decision, dict) else None
        if isinstance(output, dict) and isinstance(output.get("time_policy"), dict):
            reasons.append("time_policy")
        multipliers = plan.get("multipliers")
        if isinstance(multipliers, dict) and any(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and abs(float(value) - _FLAT_MULTIPLIER) > _MULTIPLIER_EPSILON
            for value in multipliers.values()
        ):
            reasons.append("price_window")
        return reasons

    def lint(self) -> Dict[str, Any]:
        """Expose the same validation data shown by :meth:`status`.

        ``error_targets`` rides alongside ``errors``, aligned by index: the
        structured jump target for the error at the same position, or None when
        that error names no rule.
        """
        _config, errors = self._load()
        return {
            "valid": not errors,
            "errors": errors,
            "error_targets": self._error_targets(errors, _config),
        }

    # ------------------------------------------------------------------
    # Write path (router.yaml HOT edits only; lint-gated, atomic, revertable)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_fail_safe(config: Dict[str, Any]) -> List[str]:
        """Minimal structural check for ``fail_safe``.

        ``rules.lint`` never inspects ``fail_safe`` (it validates default/tiers/
        rules only), so a malformed last-resort target — the route every
        fall-through request lands on — would otherwise be written unchecked.
        Guard the shape here before it can reach the hot file.
        """
        if "fail_safe" not in config:
            return []
        fail_safe = config.get("fail_safe")
        if not isinstance(fail_safe, dict):
            return ["fail_safe must be a mapping"]
        errors: List[str] = []
        for field in ("model", "provider"):
            value = fail_safe.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"fail_safe.{field} must be a non-empty string")
        fallback = fail_safe.get("fallback", [])
        if fallback and not isinstance(fallback, list):
            errors.append("fail_safe.fallback must be a list")
        return errors

    def _merge_hot(self, changes: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Read current config and merge only the allowlisted top-level keys.

        Returns ``(current, merged)``. Any key outside :data:`_HOT_KEYS` in
        ``changes`` is ignored — never written.

        A null value REMOVES the key, at this level exactly as it does inside a
        nested mapping (see :func:`_deep_merge_value`): ``{'tiers': None}`` drops
        the whole block, and ``{'tiers': {'T1': {'time_cap': None}}}`` drops one
        knob off one tier. Removing something the policy cannot live without is
        not special-cased here — :meth:`_lint_merged` is the fail-closed gate for
        that, and it refuses the merged result before any write.
        """
        # plan()/apply() validate that changes is a mapping before calling here.
        current = self._read_config_dict()
        merged = dict(current)
        for key, value in changes.items():
            if key not in _HOT_KEYS:
                continue
            merged_value = _deep_merge_value(current.get(key), value)
            if merged_value is _REMOVE:
                merged.pop(key, None)
            else:
                merged[key] = merged_value
        return current, merged

    def _read_config_bytes(self) -> bytes:
        """Return the exact on-disk bytes (hash basis for optimistic concurrency)."""
        return self._config_path.read_bytes()

    def _read_config_dict(self) -> Dict[str, Any]:
        """Parse the current config, raising a clear error on malformed YAML."""
        raw = self._read_config_bytes()
        config = yaml.safe_load(raw) or {}
        if not isinstance(config, dict):
            raise ValueError("router config root must be a mapping")
        return config

    @staticmethod
    def _hash_bytes(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    def _lint_merged(self, merged: Dict[str, Any]) -> List[str]:
        """Full pre-write validation: rule/tier lint plus the fail_safe guard."""
        return list(rules_lint(merged)) + self._validate_fail_safe(merged)

    def plan(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Preview an edit: merge, lint, diff, and hash — WITHOUT writing.

        The returned ``base_hash`` pins the on-disk state this plan was computed
        against; :meth:`apply` refuses to write if the file has drifted since.

        ``no_op`` says the merge changed nothing — an empty diff reported as a
        fact instead of left to be noticed. ``valid: True`` on a plan that would
        write the identical file reads as "your edit is fine" when the truth is
        "your edit does not exist", which is how an attempted REMOVAL spelled as
        ``{'time_cap': {}}`` came to look like a success; a removal is spelled with
        a null (see :func:`_deep_merge_value`).
        """
        if not isinstance(changes, dict):
            raise ValueError("changes must be a mapping")
        try:
            current_raw = self._read_config_bytes()
            current, merged = self._merge_hot(changes)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            return {"valid": False, "errors": [f"could not read router config: {exc}"],
                    "diff": "", "no_op": False, "preview": {}, "policy": {},
                    "base_hash": ""}
        errors = self._lint_merged(merged)
        before = yaml.safe_dump(current, sort_keys=False)
        after = yaml.safe_dump(merged, sort_keys=False)
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="router.yaml (current)",
                tofile="router.yaml (proposed)",
            )
        )
        return {
            "valid": not errors,
            "errors": errors,
            "diff": diff,
            # Read off the diff, not off a second comparison: the diff is what the
            # operator is shown, so "nothing changed" has to be the same answer.
            "no_op": not diff,
            "preview": merged,
            "policy": merged,
            "base_hash": self._hash_bytes(current_raw),
        }

    def apply(self, base_hash: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Commit an edit to ``router.yaml`` under optimistic concurrency.

        Serialized behind :attr:`_write_lock`. Refuses (409-style ``conflict``)
        if the file changed since ``base_hash`` was computed, refuses if the
        merged result fails lint, snapshots the prior bytes to ``.bak``, and
        writes atomically. Because the router re-reads per request, the change
        is live immediately with no restart.

        AN EDIT THAT CHANGES NOTHING REPORTS ``no_op: True`` and does not write.
        ``ok: True`` on its own cannot be told apart from a committed change, which
        is how a mis-spelled removal (``{'time_cap': {}}`` rather than
        ``{'time_cap': None}``) was answered with success while the knob stayed on
        disk. Not writing is the other half: rewriting the file with identical
        content would overwrite the ``.bak`` snapshot with the state it already
        holds and quietly spend the operator's one revert.

        The no-op test is the MERGED POLICY against the parsed current one, not the
        serialized bytes: re-serializing a hand-formatted file is a change to the
        bytes and no change to the routing, and the knob an operator asked about is
        the routing.

        A REMOVAL MUST BE SENT AS THE CHANGE, not as ``plan()['policy']``. This
        takes ``changes`` and merges them over the file, and ``policy`` is the
        already-MERGED result: a key deleted out of it is simply a key the merge
        does not mention, so the on-disk one survives. Feeding a null-removal plan's
        ``policy`` back in therefore restores the knob and — because that leaves the
        file exactly as it was — is answered ``no_op: True`` rather than as a
        successful deletion. Treating a submitted ``policy`` as a REPLACE instead
        would fix the round trip by making every partial submit delete the rest of
        router.yaml, which is a much worse trade on a hot config.
        """
        if not isinstance(changes, dict):
            raise ValueError("changes must be a mapping")
        with self._write_lock:
            try:
                current_raw = self._read_config_bytes()
            except OSError as exc:
                return {"ok": False, "errors": [f"could not read router config: {exc}"]}
            current_hash = self._hash_bytes(current_raw)
            if base_hash != current_hash:
                return {"ok": False, "conflict": True, "base_hash": current_hash}
            try:
                current, merged = self._merge_hot(changes)
            except (yaml.YAMLError, ValueError) as exc:
                return {"ok": False, "errors": [f"could not parse router config: {exc}"]}
            errors = self._lint_merged(merged)
            if errors:
                return {"ok": False, "errors": errors}
            if merged == current:
                # Nothing to commit. Reported as ok because nothing failed, and as
                # no_op because "committed" would be a lie; the returned hash is
                # still the on-disk one, so a follow-up plan cannot false-409.
                return {"ok": True, "no_op": True, "base_hash": current_hash}
            # Snapshot the exact prior bytes, then write the merged config.
            self._atomic_write_bytes(self._backup_path(), current_raw)
            new_raw = yaml.safe_dump(merged, sort_keys=False).encode("utf-8")
            self._atomic_write_bytes(self._config_path, new_raw)
            # Hash the exact bytes we wrote — not a re-read, which could fail
            # transiently (returning ok with an empty hash) and, worse, differ
            # from the file and cause the next plan()'s base_hash to false-409.
            return {"ok": True, "no_op": False,
                    "base_hash": self._hash_bytes(new_raw)}

    def apply_revert(self) -> Dict[str, Any]:
        """Restore the last ``.bak`` snapshot atomically. No snapshot -> no-op."""
        with self._write_lock:
            backup = self._backup_path()
            try:
                snapshot = backup.read_bytes()
            except OSError:
                return {"ok": False, "error": "no snapshot"}
            self._atomic_write_bytes(self._config_path, snapshot)
            # Hash the exact restored bytes, not a re-read (same rationale as apply).
            return {"ok": True, "reverted": True,
                    "base_hash": self._hash_bytes(snapshot)}

    def _backup_path(self) -> Path:
        return self._config_path.with_suffix(self._config_path.suffix + ".bak")

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        """Write ``data`` to ``path`` atomically (temp file + ``os.replace``).

        Mirrors ``Blocklist._save_state``: a partial file can never be observed
        because the rename is atomic. The caller serializes concurrent writers.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".yaml", prefix="router-", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_path, str(path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Route-trace readers (for visual replay). This process is a READER only —
    # the delegate_profile plugin is the single writer of routes.jsonl. Reads
    # are fail-safe (missing file -> empty, corrupt line -> skipped), mirroring
    # Blocklist._load_state, so a bad trace file never breaks the endpoint.
    # ------------------------------------------------------------------

    @staticmethod
    def _trace_files() -> List[Path]:
        """Current routes.jsonl plus its rotated backups, newest content first.

        Back-filling from routes.jsonl.1 keeps the recent-routes list non-empty
        immediately after a rotation (when the current file is fresh).

        RELATIVE FIRST, ABSOLUTE SECOND — the module-scope idiom of this file,
        applied at function scope for exactly the same reason. Hermes loads the
        plugin as ``hermes_plugins.<slug>.router.service``, where ``router`` is
        NOT a top-level package, so the absolute name alone raised
        ``ModuleNotFoundError`` out of every :meth:`routes` and :meth:`route`
        call on the shape production runs: the Decisions surface dead in
        production with the whole suite green, because every test imports the
        flat shape. Both bounds come from the durable log rather than from a
        second copy here, so this file set and ``durable_decision_log``'s can
        never name different files.
        """
        try:
            from .durable_decision_log import routes_path, _TRACE_BACKUPS
        except ImportError:  # pragma: no cover - flat layout used by the test harness
            from router.durable_decision_log import routes_path, _TRACE_BACKUPS

        base = routes_path()
        files = [base]
        for n in range(1, _TRACE_BACKUPS + 1):
            files.append(base.with_suffix(base.suffix + f".{n}"))
        return files

    def _read_trace_entries(self) -> List[Dict[str, Any]]:
        """Return parsed trace entries oldest→newest across rotated files.

        Reads the current file plus its rotated backups (so the list stays
        non-empty right after a rotation). Each line is parsed defensively; a
        corrupt line is skipped, never raised. The trace file is size-bounded,
        so reading it whole keeps the id scheme (ordinal) consistent between
        :meth:`routes` and :meth:`route`.

        DECODED PER LINE, because an undecodable byte is a corrupt LINE and gets
        the same treatment as unparseable JSON: skipped. Whole-file
        ``read_text(encoding='utf-8')`` raises ``UnicodeDecodeError`` — a
        ``ValueError``, not an ``OSError`` — so one bad byte anywhere in
        routes.jsonl escaped this reader and aborted both :meth:`routes` and
        :meth:`route`, which the sidecar answers with a dropped connection and a
        Decisions tab that stays dead until someone finds the file. The writer
        appends from another process, so a torn multi-byte write is a thing that
        happens, not a hypothetical. Skipping the line keeps every readable entry —
        including the ones written AFTER the damage, which is what an operator
        opening the tab after an incident came for.
        """
        collected: List[Dict[str, Any]] = []
        for path in self._trace_files():
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            file_entries: List[Dict[str, Any]] = []
            for raw_line in raw.splitlines():
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue  # torn/foreign bytes — this line only
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(obj, dict):
                    file_entries.append(obj)
            # Prepend older files so the combined list stays oldest→newest.
            collected = file_entries + collected
        return collected

    def routes(self, limit: int = 50) -> Dict[str, Any]:
        """Return a compact list of recent routes, most recent first.

        Each item: ``{id, ts, cause, rule_id, task, model, provider,
        declared_model}``. ``id`` is the entry's timestamp-plus-ordinal so a
        specific trace can be fetched by :meth:`route`. The response also carries
        the resolved ``trace_path`` and total ``count`` so an empty list is
        diagnosable as 'no traces yet' vs 'wrong path'.

        ``model`` IS THE ELO THAT RAN, read through
        :func:`decision_log.attempted_head_of` — the head of the planned chain,
        which is what the executor dispatches first. It used to be
        ``output.model``, the DECLARED TIER PRIMARY, and after a capability
        filter, a time cap, a shuffle or a blocklist veto those are different
        elos: this list showed ``glm-5.3`` for a vision turn ``gpt-5.6-luna``
        served. That is the whole reason ``attempted_model`` is persisted, and
        this is the operator's primary decision surface, so it is the one place
        the distinction may not be dropped.

        The tier identity is NOT dropped, it moves to ``declared_model``: it is
        how a decision is tied back to the rule and tier that made it, and
        ``rule_id`` alone does not name the tier. When nothing was filtered the
        two are equal, which is the honest report of that case rather than a
        missing key.

        The import is relative-first for the reason :meth:`_trace_files`
        documents: under Hermes's ``hermes_plugins.<slug>`` shape the absolute
        name does not resolve, and this method is the Decisions surface.
        """
        try:
            from .durable_decision_log import routes_path
        except ImportError:  # pragma: no cover - flat layout used by the test harness
            from router.durable_decision_log import routes_path

        try:
            safe_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            safe_limit = 50
        entries = self._read_trace_entries()
        items: List[Dict[str, Any]] = []
        for ordinal, entry in enumerate(entries):
            out = entry.get("output", {}) if isinstance(entry.get("output"), dict) else {}
            attempted_model, attempted_provider = self._attempted_head(entry)
            items.append({
                "id": self._trace_id(entry, ordinal),
                "ts": entry.get("ts"),
                "cause": entry.get("cause"),
                # Which rule fired, so a surface can count hits per rule — a rule
                # that never fires is an operator finding, and `cause` alone
                # cannot identify it.
                "rule_id": entry.get("rule_id"),
                "task": entry.get("task", ""),
                # What RAN, and the rail it ran on.
                "model": attempted_model,
                "provider": attempted_provider,
                # The tier primary the rule/classifier settled on — equal to
                # `model` unless the chain was filtered, capped, shuffled or vetoed.
                "declared_model": out.get("model", ""),
            })
        items.reverse()  # most recent first
        return {
            "trace_path": str(routes_path()),
            "count": len(items),
            "routes": items[:safe_limit],
        }

    def route(self, route_id: str) -> Optional[Dict[str, Any]]:
        """Return the full trace entry (including ``steps``) for ``route_id``."""
        if not route_id:
            return None
        entries = self._read_trace_entries()
        for ordinal, entry in enumerate(entries):
            if self._trace_id(entry, ordinal) == route_id:
                return entry
        return None

    @staticmethod
    def _attempted_head(entry: Dict[str, Any]) -> Tuple[str, str]:
        """``(model, provider)`` production attempted FIRST for a trace entry.

        A thin pass-through to :func:`decision_log.attempted_head_of` — deliberately
        thin, because the point of that function is that there is exactly one
        definition of "the head" and this surface is a reader of it, not a second
        author. The only local logic is the degrade for a decision_log that predates
        the accessor: the declared pair, which is all such a build ever wrote.
        """
        if _attempted_head_of is not None:
            return _attempted_head_of(entry)
        out = entry.get("output") if isinstance(entry, dict) else None
        if not isinstance(out, dict):
            return "", ""
        return str(out.get("model") or ""), str(out.get("provider") or "")

    @staticmethod
    def _trace_id(entry: Dict[str, Any], ordinal: int) -> str:
        """Stable id for a trace entry: timestamp + ordinal (unique within a read)."""
        return f"{entry.get('ts', 0)}-{ordinal}"
