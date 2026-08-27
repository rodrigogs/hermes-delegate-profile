"""Offline contract tests for the pricing-page change detector."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from router.price_watch import ProviderAdapter, run_watch

UTC = timezone.utc
NOW = datetime(2026, 8, 26, 15, 30, tzinfo=UTC)
DEEPSEEK_PAGE = """
<html><main><p>Off-peak rates are half of the peak rates.</p>
<footer>Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through Friday (all other hours are off-peak).</footer>
</main></html>
"""
DEEPSEEK_CHANGED_PAGE = DEEPSEEK_PAGE.replace("Monday through Friday", "every day")


@pytest.fixture
def deepseek() -> ProviderAdapter:
    return ProviderAdapter(
        provider="deepseek",
        url="https://api-docs.deepseek.com/quick_start/pricing",
        anchor="Peak hours",
    )


def test_equal_text_records_provenance_and_never_creates_a_card(
    tmp_path: Path, deepseek: ProviderAdapter
) -> None:
    state = tmp_path / "pricing-state.json"
    cards: list[dict[str, str]] = []
    args = dict(
        state_path=state,
        adapters=[deepseek],
        registered_providers={"deepseek"},
        policy_providers={"deepseek"},
        registry={"deepseek-v4-pro": {"price_windows_verified": "2026-08-20"}},
        fetch=lambda url: DEEPSEEK_PAGE,
        create_card=cards.append,
        now=lambda: NOW,
    )
    first = run_watch(**args)
    second = run_watch(**args)

    assert first.checked == ["deepseek"]
    assert second.confirmed == ["deepseek"]
    assert cards == []
    saved = state.read_text(encoding="utf-8")
    assert '"url": "https://api-docs.deepseek.com/quick_start/pricing"' in saved
    assert "Peak hours are 01:00 - 04:00" in saved
    assert "2026-08-26T15:30:00+00:00" in saved


def test_changed_text_creates_review_card_but_does_not_change_registry(
    tmp_path: Path, deepseek: ProviderAdapter
) -> None:
    state = tmp_path / "pricing-state.json"
    registry = {"deepseek-v4-pro": {"price_windows_verified": "2026-08-20"}}
    cards: list[dict[str, str]] = []
    common = dict(
        state_path=state,
        adapters=[deepseek],
        registered_providers={"deepseek"},
        policy_providers={"deepseek"},
        registry=registry,
        create_card=cards.append,
        now=lambda: NOW,
    )
    run_watch(**common, fetch=lambda url: DEEPSEEK_PAGE)
    result = run_watch(**common, fetch=lambda url: DEEPSEEK_CHANGED_PAGE)

    assert result.changed == ["deepseek"]
    assert len(cards) == 1
    assert "Monday through Friday" in cards[0]["body"]
    assert "every day" in cards[0]["body"]
    assert "deepseek-v4-pro" in cards[0]["body"]
    assert registry == {"deepseek-v4-pro": {"price_windows_verified": "2026-08-20"}}


def test_fetch_failure_preserves_prior_provenance_and_records_failure(
    tmp_path: Path, deepseek: ProviderAdapter
) -> None:
    state = tmp_path / "pricing-state.json"
    cards: list[dict[str, str]] = []
    run_watch(
        state_path=state, adapters=[deepseek], registered_providers={"deepseek"},
        policy_providers={"deepseek"}, registry={}, fetch=lambda url: DEEPSEEK_PAGE,
        create_card=cards.append, now=lambda: NOW,
    )
    def unavailable(url: str) -> str:
        raise OSError("network down")
    result = run_watch(
        state_path=state, adapters=[deepseek], registered_providers={"deepseek"},
        policy_providers={"deepseek"}, registry={}, fetch=unavailable,
        create_card=cards.append, now=lambda: NOW,
    )

    assert result.failed == ["deepseek"]
    assert cards == []
    saved = state.read_text(encoding="utf-8")
    assert "network down" in saved
    assert "Peak hours are 01:00 - 04:00" in saved


def test_only_registered_and_policy_named_providers_are_fetched(
    tmp_path: Path, deepseek: ProviderAdapter
) -> None:
    state = tmp_path / "pricing-state.json"
    zai = ProviderAdapter("zai", "https://docs.z.ai/devpack/overview.md", "Singapore Standard Time")
    fetched: list[str] = []
    result = run_watch(
        state_path=state, adapters=[deepseek, zai], registered_providers={"deepseek", "zai"},
        policy_providers={"deepseek"}, registry={},
        fetch=lambda url: fetched.append(url) or DEEPSEEK_PAGE,
        create_card=lambda card: pytest.fail(f"unexpected card: {card}"), now=lambda: NOW,
    )

    assert result.checked == ["deepseek"]
    assert fetched == [deepseek.url]


def test_malformed_state_and_malformed_page_fail_closed_without_erasing_history(
    tmp_path: Path, deepseek: ProviderAdapter
) -> None:
    state = tmp_path / "nested" / "pricing-state.json"
    state.parent.mkdir()
    state.write_text('{"providers": []}', encoding="utf-8")
    result = run_watch(
        state_path=state, adapters=[deepseek], registered_providers={"deepseek"},
        policy_providers={"deepseek"}, registry={}, fetch=lambda url: "<p>no clause</p>",
        create_card=lambda card: pytest.fail(f"unexpected card: {card}"), now=lambda: NOW,
    )
    assert result.failed == ["deepseek"]
    assert "pricing anchor not found" in state.read_text(encoding="utf-8")


def test_incomplete_prior_state_becomes_a_baseline_without_a_review_card(
    tmp_path: Path, deepseek: ProviderAdapter
) -> None:
    state = tmp_path / "pricing-state.json"
    state.write_text('{"providers": {"deepseek": {"sha256": "old"}}}', encoding="utf-8")
    result = run_watch(
        state_path=state, adapters=[deepseek], registered_providers={"deepseek"},
        policy_providers={"deepseek"}, registry={}, fetch=lambda url: DEEPSEEK_PAGE,
        create_card=lambda card: pytest.fail(f"unexpected card: {card}"), now=lambda: NOW,
    )
    assert result.changed == []
    assert "Peak hours" in state.read_text(encoding="utf-8")
