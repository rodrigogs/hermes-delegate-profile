"""Contract tests for provider-quota exhaustion handling."""

import importlib.util
from pathlib import Path

from router.breaker import FAILURE_WEIGHTS

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("delegate_profile_exhaustion", REPO_ROOT / "__init__.py")
assert _spec is not None and _spec.loader is not None
_dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dp)


def test_is_exhaustion_recognizes_provider_quota_and_balance_errors():
    samples = [
        "Error code: 429 ... usage_limit_reached",
        "HTTP 402: Insufficient credits",
        "Insufficient Balance",
        "insufficient account balance",
        "Weekly/Monthly Limit Exhausted",
        "code':'1113'",
        "429 Too Many Requests",
    ]

    assert all(_dp._is_exhaustion(sample) for sample in samples)


def test_is_exhaustion_does_not_match_generic_or_unrelated_output():
    samples = [
        "API call failed after 3 retries:",
        "some normal output",
        "",
        "rate the limit of detection",
    ]

    assert not any(_dp._is_exhaustion(sample) for sample in samples)


def test_quota_exhaustion_has_an_immediate_breaker_weight():
    assert FAILURE_WEIGHTS["quota_exhausted"] >= 5
