"""Service over the hermes-one-capability-router policy.

The Dashboard, CLI and Hermes One sidecar must observe the same ``router.yaml``
and core routing functions.  Read paths are fail-safe: they reload the YAML on
every request, expose only non-secret operational state, and perform
deterministic Stage-0 simulations only — they never call the LLM classifier or
mutate breaker state.

Capability-router material rides the same read paths: :meth:`explain` previews
the effective attempt chain (``chain_plan``) with a FIXED-seed rng so a polled
preview does not reshuffle, :meth:`policy` carries the per-tier fallback/
capability/time knobs through to the console, :meth:`status` reports advisory
``warnings`` strictly separately from blocking ``validation_errors``, and
:meth:`liveness` marks elos the capability registry cannot describe and reports
each elo's price-window state.

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

from router.blocklist import Blocklist
from router.rules import explain as rules_explain
from router.rules import lint as rules_lint
from router.signals import extract

# Both are imported defensively: this file is deployed by copy, so it can land
# next to a rules.py/capabilities.py that predates capability routing. Every use
# below degrades to pre-capability behaviour when the symbol is missing — a read
# path must never 500 because a sibling module is older.
try:
    from router.rules import lint_warnings as rules_lint_warnings
except ImportError:  # pragma: no cover - rules.py without advisory warnings
    rules_lint_warnings = None  # type: ignore[assignment]

try:
    from router import capabilities as _caps
except ImportError:  # pragma: no cover - registry absent on an older install
    _caps = None  # type: ignore[assignment]

_DEFAULT_MAX_TASK_CHARS = 8_192

# Bound on the composed prompt (``prompt_text``) a preview may be sized from.
# Deliberately far larger than the task bound and NOT the same knob: the task is
# a goal line, the prompt is goal + context and is exactly the thing that has to
# be big for this parameter to be worth having. 1 MiB is ~291k tokens at the
# module's 3.6-chars-per-token ratio, so it covers every delegated turn a
# real context window can hold while still bounding the substring scans
# ``signals.extract`` runs over it — an unbounded preview is a read path that can
# be made to cost arbitrary CPU. Constructor-overridable for the same reason
# ``max_task_chars`` is.
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

# The only top-level ``router.yaml`` keys an operator may edit through the write
# path. ``fail_safe`` is included (last-resort routing must be editable); every
# other top-level key in a change set is ignored. ``router.yaml`` is HOT
# (re-read per request); ``config.yaml``/compaction is RESTART-class and is not
# routed through here.
#
# Capability routing and the time layer added NO member here, deliberately: the
# new knobs (``fallback_strategy``, ``pin_primary``, ``billing_mode``,
# ``requirements``, ``time_cap``, ``time_policy``, per-elo declared capability and
# ``price_windows`` overrides) all live INSIDE ``tiers``, which is already hot,
# and ``classifier.chain`` lives inside ``classifier``.
# ``_deep_merge_value`` recurses through dicts, so a tier edit carrying them
# round-trips untouched, while a tier's ``fallback`` LIST still replaces
# wholesale — which is exactly what makes reordering hops or deleting an elo
# expressible. Adding a key here would only widen the write surface.
_HOT_KEYS = frozenset(
    {"rules", "default", "tiers", "classifier", "fail_safe", "blocklist", "enabled"}
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
        "multipliers": {},
    }


def _deep_merge_value(old: Any, new: Any) -> Any:
    """Merge ``new`` over ``old``: dicts recurse, everything else REPLACES.

    Lists (rules, manual_ban, fallback_chain, tier.fallback) and scalars replace
    wholesale so an operator can delete or reorder an entry by sending the full
    new list. An index/union merge would make deletion impossible and could
    corrupt rule order, which ``rules.lint`` shadow-detection depends on.
    """
    if isinstance(old, dict) and isinstance(new, dict):
        result = dict(old)
        for key, value in new.items():
            result[key] = _deep_merge_value(result.get(key), value)
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
        what the write gate refuses on. ``warnings`` only informs (a tier whose
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
        """
        config, errors = self._load()
        classifier = _as_mapping(config.get("classifier"))
        breaker = _as_mapping(_as_mapping(config.get("blocklist")).get("auto_breaker"))
        tiers = config.get("tiers")
        return {
            "valid": not errors,
            "validation_errors": errors,
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
            declared_caps = self._declared_capability_index(config)
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
                    self._time_state(model, declared_caps.get(model), when)
                )
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
    ) -> Dict[str, Any]:
        """Price-window state for one elo at ``when``.

        Three EXTRA FIELDS on a liveness entry, never a new ``state`` value:

          ``in_expensive_window``  True only inside a ``multiplier > 1.0`` window,
              so xiaomi's 0.8x night discount — also a window — never reads as
              "avoid this now".
          ``price_multiplier``     what this elo costs right now relative to its
              stored base rate.
          ``next_window_change``   WHEN that multiplier next changes and to what
              (``{hour, weekday, hours_ahead, multiplier}``), or None when it never
              does (a flat-priced or unknown elo).

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

        Degrades to the neutral 1.0 / not-expensive / no-change answer when the
        registry is absent, raises, or answers with something that is not a number.
        That is the registry's OWN neutral for "no window matched or nothing is
        known", not an invented number, and
        ``capabilities_known`` sits beside it to tell an operator which of the two
        they are looking at. Degrading per elo rather than per response is
        deliberate: one unpriceable elo must not blank the price state of every
        other rail.
        """
        neutral: Dict[str, Any] = {
            "in_expensive_window": False,
            "price_multiplier": _FLAT_MULTIPLIER,
            "next_window_change": None,
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
        }

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
        """
        if not isinstance(prompt_text, str):
            raise ValueError("prompt_text must be a string")
        if not prompt_text:
            return task, _SIZED_FROM_TASK
        if len(prompt_text) > self._max_prompt_chars:
            raise ValueError(
                f"prompt_text exceeds {self._max_prompt_chars} characters"
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
        """Expose the same validation data shown by :meth:`status`."""
        _config, errors = self._load()
        return {"valid": not errors, "errors": errors}

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
        """
        # plan()/apply() validate that changes is a mapping before calling here.
        current = self._read_config_dict()
        merged = dict(current)
        for key, value in changes.items():
            if key not in _HOT_KEYS:
                continue
            merged[key] = _deep_merge_value(current.get(key), value)
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
        """
        if not isinstance(changes, dict):
            raise ValueError("changes must be a mapping")
        try:
            current_raw = self._read_config_bytes()
            current, merged = self._merge_hot(changes)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            return {"valid": False, "errors": [f"could not read router config: {exc}"],
                    "diff": "", "preview": {}, "policy": {}, "base_hash": ""}
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
                _current, merged = self._merge_hot(changes)
            except (yaml.YAMLError, ValueError) as exc:
                return {"ok": False, "errors": [f"could not parse router config: {exc}"]}
            errors = self._lint_merged(merged)
            if errors:
                return {"ok": False, "errors": errors}
            # Snapshot the exact prior bytes, then write the merged config.
            self._atomic_write_bytes(self._backup_path(), current_raw)
            new_raw = yaml.safe_dump(merged, sort_keys=False).encode("utf-8")
            self._atomic_write_bytes(self._config_path, new_raw)
            # Hash the exact bytes we wrote — not a re-read, which could fail
            # transiently (returning ok with an empty hash) and, worse, differ
            # from the file and cause the next plan()'s base_hash to false-409.
            return {"ok": True, "base_hash": self._hash_bytes(new_raw)}

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
        """
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
        """
        collected: List[Dict[str, Any]] = []
        for path in self._trace_files():
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            file_entries: List[Dict[str, Any]] = []
            for line in raw.splitlines():
                line = line.strip()
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

        Each item: ``{id, ts, cause, task, model}``. ``id`` is the entry's
        timestamp-plus-ordinal so a specific trace can be fetched by :meth:`route`.
        The response also carries the resolved ``trace_path`` and total ``count``
        so an empty list is diagnosable as 'no traces yet' vs 'wrong path'.
        """
        from router.durable_decision_log import routes_path

        try:
            safe_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            safe_limit = 50
        entries = self._read_trace_entries()
        items: List[Dict[str, Any]] = []
        for ordinal, entry in enumerate(entries):
            out = entry.get("output", {}) if isinstance(entry.get("output"), dict) else {}
            items.append({
                "id": self._trace_id(entry, ordinal),
                "ts": entry.get("ts"),
                "cause": entry.get("cause"),
                # Which rule fired, so a surface can count hits per rule — a rule
                # that never fires is an operator finding, and `cause` alone
                # cannot identify it.
                "rule_id": entry.get("rule_id"),
                "task": entry.get("task", ""),
                "model": out.get("model", ""),
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
    def _trace_id(entry: Dict[str, Any], ordinal: int) -> str:
        """Stable id for a trace entry: timestamp + ordinal (unique within a read)."""
        return f"{entry.get('ts', 0)}-{ordinal}"
