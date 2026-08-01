"""Contract tests for provider-quota exhaustion handling."""

import importlib.util

import pytest
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

# --- the retry count in the terminal banner is not fixed -------------------


@pytest.mark.parametrize(
    "transcript",
    [
        "API call failed after 1 retry: rate limited",
        "API call failed after 3 retries: 429",
        "API call failed after 5 retries: 429",
        "API call failed after 10 retries: upstream 503",
    ],
)
def test_reported_failure_tolerates_any_retry_count(transcript):
    """The banner renders whatever retry budget the provider had.

    Pinning the literal "after 3 retries:" meant a provider that gave up after 1
    or 5 attempts read as SUCCESS: its error transcript was returned to the caller
    as the agent's answer, and the cross-rail fallback never ran.
    """
    assert _dp._reported_agent_failure(transcript, "") is True


def test_reported_failure_needs_a_cause_not_just_the_word_error():
    """A terminal banner with no retry preamble counts only when it names a cause.

    An answer that merely discusses a 429 must not be mistaken for one, or every
    delegation about rate limiting would be retried on another rail.
    """
    assert _dp._reported_agent_failure("Provider error: 429 Too Many Requests", "") is True
    assert _dp._reported_agent_failure("You should handle HTTP 429 with backoff.", "") is False
    assert _dp._reported_agent_failure("Done. Renamed getCwd in 3 files.", "") is False


@pytest.mark.parametrize(
    "transcript",
    [
        "HTTP 401 Unauthorized. Non-retryable client error (HTTP 401). Aborting.",
        "HTTP 403 Forbidden. Aborting.",
        "TLS certificate verification failed",
        "Connection refused",
        "Name or service not known",
    ],
)
def test_terminal_failures_without_a_retry_preamble_are_failures(transcript):
    """Not every terminal provider failure retries first.

    An expired key, a revoked entitlement, a TLS failure and a refused connection
    all abort immediately, so they never carry "after N retries". Measured before
    this branch: every one read as SUCCESS, meaning the error transcript was
    returned to the caller as the agent's answer and no rail-switch was tried.
    """
    assert _dp._reported_agent_failure(transcript, "") is True


@pytest.mark.parametrize(
    "transcript",
    [
        "You should return 401 when the token is absent.",
        "Use TLS 1.3 and verify the certificate chain.",
        "Handle connection errors with exponential backoff.",
        "Done. Renamed getCwd in 3 files.",
    ],
)
def test_prose_about_failures_is_not_a_failure(transcript):
    """A correct answer that discusses these codes must not be retried elsewhere.

    This is the cost of widening the net: every pattern requires an abort marker
    or an unambiguous transport error, never a bare status code in a sentence.
    """
    assert _dp._reported_agent_failure(transcript, "") is False
