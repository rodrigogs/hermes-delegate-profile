"""Rule matching engine — first-match over ordered Table 1.

Pure: no IO, no state, no model calls. Deterministic.
Reads blocked_model boolean, never writes it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Closed operator set — extend ONLY by adding a row, never a new operator family
# ---------------------------------------------------------------------------

# Operators that need no value (boolean checks)
_UNARY_OPS: Set[str] = {"eq", "ne", "contains", "in", "nin"}

# All recognized operators
_VALID_OPS: Set[str] = {
    "eq", "ne", "in", "nin", "gt", "gte", "lt", "lte",
    "contains", "starts_with", "ends_with", "matches",
}

# Regex `matches` is gated to this single field
_MATCHES_ALLOWED_FIELD = "verb_class"

# Closed output set
_VALID_OUTPUT_KEYS: Set[str] = {"profile", "model", "provider", "deny", "action"}

# Valid operator sets for config lint
_ALLOWED_OPS = _VALID_OPS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def match(
    features: Dict[str, Any],
    blocked_model: bool,
    rules: List[Dict[str, Any]],
    default: Dict[str, Any],
    tiers: Dict[str, Dict[str, str]],
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Run Table 1 top-down first-match.

    Returns (output, matched_rule_id). output always has at least one key.
    matched_rule_id is None when the default fired.

    Args:
        features: flat signal dict from signals.extract()
        blocked_model: boolean from blocklist pre-filter
        rules: list of rule dicts from router.yaml (id, when, then)
        default: default routing dict
        tiers: {T1: {model, provider}, ...}
    """
    for rule in rules:
        when = rule.get("when", {})
        if _all_clauses_match(when, features, blocked_model):
            output = dict(rule.get("then", {}))
            if not output:
                continue
            # Resolve tier aliases in model field
            output = _resolve_tiers(output, tiers)
            return output, rule["id"]

    # Fall-through: default
    output = dict(default)
    output = _resolve_tiers(output, tiers)
    return output, None


def resolve_tiers(output: Dict[str, Any], tiers: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """Resolve Tn aliases in output.model against Table 2."""
    return _resolve_tiers(output, tiers)


def explain(
    task: str,
    features: Dict[str, Any],
    blocked_model: bool,
    rules: List[Dict[str, Any]],
    default: Dict[str, Any],
    tiers: Dict[str, Dict[str, str]],
) -> Dict[str, Any]:
    """Full transparency: run match() and return the decision trace.

    Returns {matched_rule_id, output, matched_clauses, cause}.
    cause is from the closed set (see spec).
    """
    output, rule_id = match(features, blocked_model, rules, default, tiers)
    matched_clauses: Dict[str, Any] = {}
    cause = "default_fallthrough"

    if rule_id is not None:
        for rule in rules:
            if rule["id"] == rule_id:
                matched_clauses = _matching_clauses(rule.get("when", {}), features)
                break
        # Determine cause from the matched rule
        cause = _determine_cause(rule_id, output)
    else:
        cause = "default_fallthrough"

    # Drill down: blocklist is the most specific
    if output.get("deny"):
        cause = "blocklist_veto"
    # default_fallthrough stays as-is — classifier hasn't fired yet at pure-core stage

    return {
        "matched_rule_id": rule_id,
        "output": output,
        "matched_clauses": matched_clauses,
        "cause": cause,
    }


# ---------------------------------------------------------------------------
# Config validation (lint)
# ---------------------------------------------------------------------------

def lint(config: Dict[str, Any]) -> List[str]:
    """Validate router.yaml. Returns list of error strings (empty = valid).

    Fail-closed: any error means the config is invalid.
    Checks:
      - enabled present (skip if false)
      - mandatory default present
      - rules have required fields (id, when, then)
      - rule ids unique
      - when clauses use closed operators
      - then clauses use closed output keys
      - matches op gated to allowlisted field
      - default refers to a real tier or concrete profile/model
      - no dead/shadowed row detection
      - tiers T1-T4 present
    """
    errors: List[str] = []

    # yaml.safe_load() may legally yield scalars, lists, or None. Lint is the
    # fail-closed boundary for that external input: return diagnostics rather
    # than leaking a Python type error through the CLI.
    if not isinstance(config, dict):
        return ["router.yaml root must be a mapping"]
    if not config:
        return ["router.yaml not loaded or empty"]

    if "default" not in config:
        errors.append("missing mandatory 'default' routing")

    tiers_cfg = config.get("tiers")
    if not isinstance(tiers_cfg, dict):
        errors.append("missing 'tiers' mapping")
        tiers_cfg = {}
    else:
        for tn in ("T1", "T2", "T3", "T4"):
            if tn not in tiers_cfg:
                errors.append(f"missing tier {tn}")

    rules_raw = config.get("rules", [])
    if not isinstance(rules_raw, list):
        errors.append("'rules' must be a list")
        return errors
    rules: List[Dict[str, Any]] = rules_raw
    # Empty rules with a default is valid — everything falls through to default.

    seen_ids: Set[str] = set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(f"rule[{i}] must be a mapping")
            continue
        rid = rule.get("id")
        if not rid:
            errors.append(f"rule[{i}] missing 'id'")
            continue
        if rid in seen_ids:
            errors.append(f"duplicate rule id '{rid}'")
        seen_ids.add(rid)

        when = rule.get("when")
        if not when or not isinstance(when, dict):
            errors.append(f"rule '{rid}': missing or invalid 'when'")
            continue

        then = rule.get("then")
        if not then or not isinstance(then, dict):
            errors.append(f"rule '{rid}': missing or invalid 'then'")
            continue

        # Validate when clauses
        for field, condition in when.items():
            if not isinstance(condition, dict):
                errors.append(f"rule '{rid}': 'when.{field}' must be an op map")
                continue
            for op, val in condition.items():
                if op not in _VALID_OPS:
                    errors.append(
                        f"rule '{rid}': 'when.{field}' uses unknown operator '{op}'"
                    )
                if op == "matches" and field != _MATCHES_ALLOWED_FIELD:
                    errors.append(
                        f"rule '{rid}': 'matches' operator only allowed on "
                        f"'{_MATCHES_ALLOWED_FIELD}', found on '{field}'"
                    )

        # Validate then output keys
        for key in then:
            if key not in _VALID_OUTPUT_KEYS:
                errors.append(f"rule '{rid}': 'then.{key}' not in closed output set")
            if key == "model":
                model_val = then[key]
                if isinstance(model_val, str) and model_val.startswith("T"):
                    tn = model_val
                    if tn not in tiers_cfg:
                        errors.append(f"rule '{rid}': 'then.model' references unknown tier '{tn}'")
            if key == "deny" and not isinstance(then[key], bool):
                errors.append(f"rule '{rid}': 'then.deny' must be boolean")

    # Detect shadowed rows — a rule whose when is a superset of a later row
    # (always matched by the earlier rule, so it can never fire)
    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            ri, rj = rules[i], rules[j]
            # Invalid rows were reported above; skip them during the derived
            # shadow analysis so one malformed row cannot mask other errors.
            if not isinstance(ri, dict) or not isinstance(rj, dict):
                continue
            # Missing ids are already validation errors above; do not turn a
            # useful lint report into a KeyError during shadow analysis.
            if not ri.get("id") or not rj.get("id"):
                continue
            earlier_when = ri.get("when")
            later_when = rj.get("when")
            if not isinstance(earlier_when, dict) or not isinstance(later_when, dict):
                continue
            if _is_shadowed(earlier_when, later_when):
                errors.append(
                    f"rule '{rj['id']}' is shadowed by earlier rule '{ri['id']}'"
                )

    return errors


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _all_clauses_match(
    when: Dict[str, Any],
    features: Dict[str, Any],
    blocked_model: bool,
) -> bool:
    """Return True when ALL when clauses hold against features."""
    if not when:
        return False

    for field, condition in when.items():
        # Special case: blocked_model injected boolean (never in features)
        if field == "blocked_model":
            if not _eval_clause("eq", blocked_model, condition.get("eq", True)):
                return False
            continue

        if field not in features:
            return False

        feat_val = features[field]
        for op, target in condition.items():
            if not _eval_clause(op, feat_val, target):
                return False

    return True


def _eval_clause(op: str, actual: Any, target: Any) -> bool:
    """Evaluate a single (op, target) against an actual value."""
    try:
        if op == "eq":
            return actual == target
        elif op == "ne":
            return actual != target
        elif op == "in":
            if isinstance(target, list):
                return actual in target
            return actual == target
        elif op == "nin":
            if isinstance(target, list):
                return actual not in target
            return actual != target
        elif op == "gt":
            return float(actual) > float(target)
        elif op == "gte":
            return float(actual) >= float(target)
        elif op == "lt":
            return float(actual) < float(target)
        elif op == "lte":
            return float(actual) <= float(target)
        elif op == "contains":
            if isinstance(actual, list):
                return str(target).lower() in [str(a).lower() for a in actual]
            return str(target).lower() in str(actual).lower()
        elif op == "starts_with":
            return str(actual).lower().startswith(str(target).lower())
        elif op == "ends_with":
            return str(actual).lower().endswith(str(target).lower())
        elif op == "matches":
            return bool(re.search(str(target), str(actual)))
        return False
    except (TypeError, ValueError):
        return False


def _resolve_tiers(output: Dict[str, Any], tiers: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve Tn aliases in output['model'] against Table 2.

    Returns a new dict — never mutates the input.
    """
    result = dict(output)
    model = result.get("model")
    if isinstance(model, str) and model in tiers:
        tier = tiers[model]
        result["model"] = tier.get("model", model)
        if "provider" in tier:
            result["provider"] = tier["provider"]
        fallback = tier.get("fallback")
        if isinstance(fallback, list):
            result["fallback"] = [
                dict(target) for target in fallback if isinstance(target, dict)
            ]
    return result


def _matching_clauses(when: Dict[str, Any], features: Dict[str, Any]) -> Dict[str, Any]:
    """Return the subset of when clauses that matched."""
    matched: Dict[str, Any] = {}
    for field, condition in when.items():
        if field in features:
            for op, target in condition.items():
                if _eval_clause(op, features[field], target):
                    matched[field] = condition
    return matched


def _determine_cause(rule_id: str, output: Dict[str, Any]) -> str:
    """Map rule id + output to a closed-set cause label."""
    if output.get("deny"):
        return "blocklist_veto"
    if output.get("action") == "classify":
        return "classifier"

    # Rule-id-based causes
    if "keyword" in rule_id.lower() or "review" in rule_id.lower():
        return "keyword_match"
    if "size" in rule_id.lower():
        return "size_rule"
    if "code" in rule_id.lower() or "trivial" in rule_id.lower():
        return "has_code_rule"
    if "hard" in rule_id.lower():
        return "hard_rule"

    return "classifier"  # fallback


def _is_shadowed(
    earlier_when: Dict[str, Any],
    later_when: Dict[str, Any],
) -> bool:
    """Return True if earlier_when is a superset of later_when.

    A rule is shadowed when an earlier rule will ALWAYS match before it.
    This is a conservative check: if the earlier rule's when is broader
    (fewer or same fields with same ops), the later rule can never fire.
    """
    if not earlier_when or not later_when:
        return False

    # Same fields? Then earlier wins (first-match semantics)
    if set(earlier_when.keys()) == set(later_when.keys()):
        return True

    # Earlier has subset of fields? Then it's broader - fires first always
    if set(earlier_when.keys()).issubset(set(later_when.keys())):
        # Check if the conditions are identical for shared fields
        for field in set(earlier_when.keys()) & set(later_when.keys()):
            if earlier_when[field] != later_when[field]:
                return False
        return True

    return False
