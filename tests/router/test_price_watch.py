"""Offline contract tests for the pricing-page change detector."""
from __future__ import annotations

from datetime import datetime, timezone
import json
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
# The historical trap: the og:title meta tag carried the anchor's words while the
# body clause held the rule. The detector must refuse, not confirm, the title.
XIAOMI_TRAP_PAGE = (
    '<html><head>\n'
    '<meta property="og:title" content="Xiaomi MiMo Api Open Platform - Token Plan Global Launch" />\n'
    '<meta name="description" content="Token Plan pricing and FAQ" />\n'
    "<title>Xiaomi MiMo Home</title>\n"
    "</head><body>\n"
    "<p>夜间优惠速率:非高峰期（北京时间 0:00-8:00，即 UTC 16:00-24:00） 0.8x 消耗系数。</p>\n"
    "</body></html>"
)


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
    # Same anchor but no literal: a provider that failed before recovering. The
    # missing literal means there is nothing to diff, so the first good read is
    # a baseline, never a "change".
    state.write_text(
        json.dumps(
            {"providers": {"deepseek": {"anchor": "Peak hours", "sha256": "old"}}}
        ),
        encoding="utf-8",
    )
    result = run_watch(
        state_path=state, adapters=[deepseek], registered_providers={"deepseek"},
        policy_providers={"deepseek"}, registry={}, fetch=lambda url: DEEPSEEK_PAGE,
        create_card=lambda card: pytest.fail(f"unexpected card: {card}"), now=lambda: NOW,
    )
    assert result.changed == []
    assert "Peak hours" in state.read_text(encoding="utf-8")


def test_anchor_that_hits_the_og_meta_is_refused_not_confirmed() -> None:
    # The exact historical defect: "Token Plan" resolved to the og:title meta tag
    # and the detector reported confirmed while watching the title. Title is not
    # a rule clause, so the anchor must be refused loudly.
    adapter = ProviderAdapter("xiaomi", "https://mimo.mi.com/docs/zh-CN/quick-start/faq/token-plan", "Token Plan")
    with pytest.raises(ValueError, match="resolved to page metadata"):
        adapter.extract(XIAOMI_TRAP_PAGE)


def test_anchor_that_hits_a_plain_meta_tag_is_refused() -> None:
    adapter = ProviderAdapter("xiaomi", "https://supplier.example/plan", "Token Plan pricing")
    with pytest.raises(ValueError, match="resolved to page metadata"):
        adapter.extract(XIAOMI_TRAP_PAGE)


def test_anchor_that_hits_the_title_element_is_refused() -> None:
    adapter = ProviderAdapter("xiaomi", "https://supplier.example/plan", "MiMo Home")
    with pytest.raises(ValueError, match="resolved to page metadata"):
        adapter.extract(XIAOMI_TRAP_PAGE)


def test_anchor_that_hits_an_og_string_outside_meta_is_refused() -> None:
    adapter = ProviderAdapter("supplier", "https://supplier.example/plan", "og:title in head")
    with pytest.raises(ValueError, match="resolved to page metadata"):
        adapter.extract('<html><body><p>the og:title in head says nothing</p></body></html>')


def test_body_anchor_still_extracts_when_the_meta_trap_precedes() -> None:
    adapter = ProviderAdapter("xiaomi", "https://mimo.mi.com/docs/zh-CN/quick-start/faq/token-plan", "0.8x 消耗系数")
    assert adapter.extract(XIAOMI_TRAP_PAGE) == (
        "<p>夜间优惠速率:非高峰期（北京时间 0:00-8:00，即 UTC 16:00-24:00） 0.8x 消耗系数。</p>"
    )


def test_unwatchable_adapter_is_a_permanent_condition_not_a_daily_failure(
    tmp_path: Path,
) -> None:
    state = tmp_path / "pricing-state.json"
    adapter = ProviderAdapter(
        "supplier",
        "https://supplier.example/pricing",
        "clause",
        watchable=False,
        reason="client-rendered; simple fetch returns only the shell",
    )
    result = run_watch(
        state_path=state, adapters=[adapter], registered_providers={"supplier"},
        policy_providers={"supplier"}, registry={},
        fetch=lambda url: pytest.fail(f"unwatchable must not be fetched: {url}"),
        create_card=lambda card: pytest.fail(f"unexpected card: {card}"), now=lambda: NOW,
    )

    assert result.unwatchable == ["supplier"]
    assert result.checked == []
    assert result.failed == []
    entry = json.loads(state.read_text(encoding="utf-8"))["providers"]["supplier"]
    assert entry["permanent"] == {"status": "unwatchable", "since": "2026-08-26T15:30:00+00:00"}
    assert entry["reason"] == "client-rendered; simple fetch returns only the shell"


def test_unwatchable_condition_keeps_its_since_and_default_reason_across_runs(
    tmp_path: Path,
) -> None:
    state = tmp_path / "pricing-state.json"
    adapter = ProviderAdapter("supplier", "https://supplier.example/pricing", "clause", watchable=False)
    later = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)
    args = dict(
        state_path=state, adapters=[adapter], registered_providers={"supplier"},
        policy_providers={"supplier"}, registry={},
        fetch=lambda url: pytest.fail(f"unwatchable must not be fetched: {url}"),
        create_card=lambda card: pytest.fail(f"unexpected card: {card}"),
    )
    first = run_watch(**args, now=lambda: NOW)
    second = run_watch(**args, now=lambda: later)

    assert first.unwatchable == second.unwatchable == ["supplier"]
    assert second.failed == []
    entry = json.loads(state.read_text(encoding="utf-8"))["providers"]["supplier"]
    assert entry["permanent"]["since"] == "2026-08-26T15:30:00+00:00"
    assert entry["reason"] == "not watchable by simple fetch"


def test_recovered_adapter_clears_the_permanent_condition_on_confirmation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "pricing-state.json"
    adapter = ProviderAdapter("supplier", "https://supplier.example/pricing", "clause")
    declared = ProviderAdapter(
        "supplier", "https://supplier.example/pricing", "clause", watchable=False,
        reason="shell only",
    )
    common = dict(
        state_path=state, adapters=[adapter], registered_providers={"supplier"},
        policy_providers={"supplier"}, registry={},
        create_card=lambda card: pytest.fail(f"unexpected card: {card}"), now=lambda: NOW,
    )
    run_watch(**common, fetch=lambda url: "<p>the clause is 0.8x off-peak</p>")
    run_watch(
        **{**common, "adapters": [declared]},
        fetch=lambda url: pytest.fail(f"unwatchable must not be fetched: {url}"),
    )
    result = run_watch(**common, fetch=lambda url: "<p>the clause is 0.8x off-peak</p>")

    assert result.confirmed == ["supplier"]
    entry = json.loads(state.read_text(encoding="utf-8"))["providers"]["supplier"]
    assert "permanent" not in entry
    assert "reason" not in entry
    assert entry["literal"] == "<p>the clause is 0.8x off-peak</p>"


def test_transient_failure_after_recovery_clears_the_permanent_condition(
    tmp_path: Path,
) -> None:
    state = tmp_path / "pricing-state.json"
    adapter = ProviderAdapter("supplier", "https://supplier.example/pricing", "clause")
    declared = ProviderAdapter(
        "supplier", "https://supplier.example/pricing", "clause", watchable=False,
        reason="shell only",
    )
    common = dict(
        state_path=state, registered_providers={"supplier"}, policy_providers={"supplier"},
        registry={}, create_card=lambda card: pytest.fail(f"unexpected card: {card}"),
        now=lambda: NOW,
    )
    run_watch(**common, adapters=[adapter], fetch=lambda url: "<p>the clause is 0.8x off-peak</p>")

    def unavailable(url: str) -> str:
        raise OSError("network down")
    result = run_watch(
        **common, adapters=[declared],
        fetch=lambda url: pytest.fail(f"unwatchable must not be fetched: {url}"),
    )
    run_watch(**common, adapters=[adapter], fetch=unavailable)

    assert result.unwatchable == ["supplier"]
    entry = json.loads(state.read_text(encoding="utf-8"))["providers"]["supplier"]
    assert "permanent" not in entry
    assert entry["last_failure"] == "network down"
    assert entry["literal"] == "<p>the clause is 0.8x off-peak</p>"


def test_re_anchor_establishes_a_new_baseline_without_a_review_card(
    tmp_path: Path,
) -> None:
    # The migration case: the state held "Token Plan" -> og:title literal. After
    # re-anchoring to the rule phrase, the old literal belongs to a different
    # clause; comparing them would fire a false "change". The new anchor becomes
    # the baseline silently.
    state = tmp_path / "pricing-state.json"
    state.write_text(
        json.dumps(
            {
                "providers": {
                    "xiaomi-token-plan": {
                        "url": "https://mimo.mi.com/docs/zh-CN/quick-start/faq/token-plan",
                        "anchor": "Token Plan",
                        "literal": '<meta property="og:title" content="Token Plan Global Launch" />',
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    adapter = ProviderAdapter(
        "xiaomi", "https://mimo.mi.com/docs/zh-CN/quick-start/faq/token-plan", "非高峰期",
        key="xiaomi-token-plan",
    )
    cards: list[dict[str, str]] = []
    result = run_watch(
        state_path=state, adapters=[adapter], registered_providers={"xiaomi"},
        policy_providers={"xiaomi"}, registry={}, fetch=lambda url: XIAOMI_TRAP_PAGE,
        create_card=cards.append, now=lambda: NOW,
    )

    assert result.checked == ["xiaomi-token-plan"]
    assert result.changed == []
    assert cards == []
    entry = json.loads(state.read_text(encoding="utf-8"))["providers"]["xiaomi-token-plan"]
    assert entry["anchor"] == "非高峰期"
    assert "0.8x 消耗系数" in entry["literal"]
