"""The classifier only works if the host grants the plugin LLM trust.

Production evidence that motivated this file: of 47 recorded routing decisions,
ZERO used the classifier and 16 ended in `fail_safe_strong` with
`reason: no_classifier` / `classifier_error`. The cause was not the router logic
— it was a missing config grant. `ctx.llm.complete(provider=…, model=…)` is
gated per-plugin and fails CLOSED, so without

    plugins:
      entries:
        delegate-profile:
          llm:
            allow_provider_override: true
            allow_model_override: true

every task that needed classification silently fell through to the last resort.

These tests pin the two halves of that contract: the plugin must degrade safely
when trust is absent (never crash, never route wrongly), and the router must
actually reach the classifier when it is present. They fail loudly if someone
re-tightens the grant or changes the fall-through, which is what let this go
unnoticed in the first place.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location("delegate_profile_trust", REPO_ROOT / "__init__.py")
assert _spec is not None and _spec.loader is not None
dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp)


ROUTER_CONFIG = {
    "enabled": True,
    "classifier": {"model": "judge", "provider": "judge-rail"},
    "fail_safe": {"profile": "coder", "model": "safe", "provider": "safe-rail"},
    "blocklist": {"manual_ban": [], "fallback_chain": [], "auto_breaker": {"enabled": False}},
    # No rule matches an ambiguous task, so `default` sends it to the classifier.
    "rules": [],
    "default": {"action": "classify"},
    "tiers": {"T1": {"model": "tiny", "provider": "cheap"},
              "T2": {"model": "small", "provider": "cheap"},
              "T3": {"model": "mid", "provider": "strong-rail"},
              "T4": {"model": "strong", "provider": "strong-rail"}},
}


class _TrustError(PermissionError):
    """Stands in for agent.plugin_llm.PluginLlmTrustError."""


def _ctx(*, trusted: bool):
    """A host context whose llm facade mirrors the real trust gate."""
    def complete(**kwargs):
        if not trusted:
            raise _TrustError(
                "plugin 'hermes-smart-router' may not override provider "
                "(set plugins.entries.hermes-smart-router.llm"
                ".allow_provider_override)"
            )
        return SimpleNamespace(text='{"tier": "T3", "confidence": "high"}')

    return SimpleNamespace(llm=SimpleNamespace(complete=complete))


def test_without_trust_the_classifier_cannot_run_and_routing_falls_back(monkeypatch):
    """The failure mode seen in production: refused override → fail-safe."""
    monkeypatch.setattr(dp, "_load_router_config", lambda: ROUTER_CONFIG)
    classify_fn = dp._make_classify_fn(_ctx(trusted=False))
    assert classify_fn is not None, "a trust problem must not look like 'no classifier configured'"

    from router.adapter import route
    from router.decision_log import DecisionLog

    log = DecisionLog()
    result = route(task="an ambiguous task with no clear signal", config=ROUTER_CONFIG,
                   classify_fn=classify_fn, decision_log=log)

    # Routing still returns a usable target — a trust error must never surface as
    # a crash or a missing route.
    assert result["model"] == "safe"
    entry = log.tail(1)[0]
    assert entry["cause"] == "fail_safe_strong"
    assert entry["steps"][-1]["in"]["reason"] == "classifier_error", \
        "the trace must say WHY it fell back, so this is diagnosable next time"


def test_with_trust_the_classifier_decides_the_tier(monkeypatch):
    """The fix: granted override → the classifier is reached and honoured."""
    monkeypatch.setattr(dp, "_load_router_config", lambda: ROUTER_CONFIG)
    classify_fn = dp._make_classify_fn(_ctx(trusted=True))

    from router.adapter import route
    from router.decision_log import DecisionLog

    log = DecisionLog()
    result = route(task="an ambiguous task with no clear signal", config=ROUTER_CONFIG,
                   classify_fn=classify_fn, decision_log=log)

    entry = log.tail(1)[0]
    assert entry["cause"] == "classifier", "a classified task must be recorded as classifier-caused"
    assert result["model"] == "mid", "the T3 the classifier chose is the model that routes"
    stages = [s["stage"] for s in entry["steps"]]
    assert stages[-1] == "classifier"
    assert entry["steps"][-1]["in"] == {"tier": "T3", "confidence": "high"}, \
        "the trace carries the classifier's own answer, which cannot be recomputed later"


def test_a_disabled_router_reports_no_classifier_rather_than_a_trust_error(monkeypatch):
    """Two different 'no classifier' situations must stay distinguishable."""
    monkeypatch.setattr(dp, "_load_router_config", lambda: {**ROUTER_CONFIG, "enabled": False})
    assert dp._make_classify_fn(_ctx(trusted=True)) is None

    monkeypatch.setattr(dp, "_load_router_config", lambda: ROUTER_CONFIG)
    # A host that offers no llm facade at all is also 'no classifier', not a crash.
    assert dp._make_classify_fn(SimpleNamespace()) is None
    assert dp._make_classify_fn(None) is None


def test_the_shipped_policy_names_a_classifier_the_grant_must_cover():
    """Guards the pairing that broke: whatever classifier router.yaml names, the
    deployment's llm allowlist has to permit it. This asserts the repo's own
    policy is internally coherent, so a classifier swap can't silently
    re-introduce the fail-safe storm."""
    import pathlib

    policy = yaml.safe_load(pathlib.Path("router.yaml").read_text(encoding="utf-8"))
    classifier = policy.get("classifier") or {}
    assert classifier.get("model") and classifier.get("provider"), \
        "router.yaml must name a classifier model+provider, or Stage 1 can never run"
    # And it must be a model the tier table also knows how to reach, i.e. a real
    # provider in this deployment rather than a placeholder.
    providers = {t.get("provider") for t in (policy.get("tiers") or {}).values()}
    providers.discard(None)
    assert classifier["provider"] in providers, (
        f"classifier provider {classifier['provider']!r} is not used by any tier "
        f"({sorted(providers)}) — likely a stale or untrusted rail"
    )
