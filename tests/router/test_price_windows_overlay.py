"""The global ``price_windows`` overlay — the writable surface above the registry.

Spec t_c90c5336: a model-keyed overlay at the top of ``router.yaml``, merged
BELOW an explicit per-elo declaration and ABOVE the code registry. These tests
prove the whole contract: routing applies it, the hot write path persists it,
malformed edits are refused with a defect-naming message, and the read surfaces
report WHICH side produced each window (a fact, not a deduction).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from router.service import RouterService
from router import rules as rules_mod


# Monday 07:00 UTC — inside deepseek's weekday 06:00-10:00 peak as the registry
# declares it, so "still 2.0x" vs "now 1.0x" discriminates the sources.
PEAK = datetime(2026, 8, 17, 7, tzinfo=timezone.utc)


def _pin_clock(monkeypatch, when):
    import router.service as service_mod
    monkeypatch.setattr(service_mod, "_utc_now", lambda: when)


def _base_config(**extra):
    return {
        "enabled": True,
        "classifier": {"model": "glm-4.6", "provider": "zai"},
        "fail_safe": {"profile": "coder", "model": "glm-4.6", "provider": "zai"},
        "blocklist": {"manual_ban": [], "fallback_chain": [],
                      "auto_breaker": {"enabled": False}},
        "rules": [],
        "default": {"profile": "coder", "model": "T1"},
        "tiers": {
            "T1": {"model": "deepseek-v4-pro", "provider": "deepseek",
                   "billing_mode": "metered",
                   "fallback": [{"model": "deepseek-v4-flash",
                                 "provider": "deepseek",
                                 "billing_mode": "metered"}]},
            "T2": {"model": "glm-4.6", "provider": "zai"},
            "T3": {"model": "glm-4.6", "provider": "zai"},
            "T4": {"model": "glm-4.6", "provider": "zai"},
        },
        **extra,
    }


@pytest.fixture
def overlay_config_path(tmp_path):
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(_base_config(price_windows={
            # The registry peaks deepseek at 06:00-10:00; the operator moves it
            # to 20:00-22:00 at 3.0x, and flattens the flash sibling entirely.
            "deepseek-v4-pro": [
                {"hours_utc": [20, 22], "multiplier": 3.0},
            ],
            "deepseek-v4-flash": [],
        }), sort_keys=False),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 1. The overlay reaches the pricing readers (multiplier / effective price /
#    in_expensive_window / next_window_change)
# ---------------------------------------------------------------------------


def test_overlay_moves_the_multiplier_and_the_change_instant(
        overlay_config_path, monkeypatch):
    """At 07:00 the registry peak is gone; at 21:00 the operator peak is in force."""
    _pin_clock(monkeypatch, PEAK)
    liveness = RouterService(overlay_config_path).liveness()
    by_key = {e["model_key"]: e for e in liveness["models"]}
    pro = by_key["deepseek-v4-pro@deepseek"]

    # 07:00 UTC Monday: the registry said 2.0x here, the overlay moved the peak
    # to 20:00-22:00, so the answer is the base rate and the next change is the
    # operator's peak itself (13 hours ahead, same Monday).
    assert pro["price_multiplier"] == 1.0
    assert pro["in_expensive_window"] is False
    assert pro["next_window_change"] == {
        "hour": 20, "weekday": 0, "hours_ahead": 13, "multiplier": 3.0,
    }
    assert pro["price_windows_origin"] == "overlay"

    # The flash sibling was flattened with the explicit empty list: no windows,
    # never expensive, no change ever — and still priced (0.22 base in, 0.66 out).
    flash = by_key["deepseek-v4-flash@deepseek"]
    assert flash["price_multiplier"] == 1.0
    assert flash["next_window_change"] is None
    assert flash["price_windows_origin"] == "overlay"


def test_overlay_reaches_effective_price_through_the_running_path(
        overlay_config_path, monkeypatch):
    """`cheapest_now` ranks by effective_price, so the overlay must reach it.

    At 21:00 UTC the overlay's 3.0x makes deepseek-v4-pro output 5.94 (1.98*3),
    above glm-4.6's flat 2.2 — a reordering only the running path can produce,
    asserted through /explain rather than through a re-derived price table.
    """
    _pin_clock(monkeypatch, PEAK.replace(hour=21))
    service = RouterService(overlay_config_path)
    config = yaml.safe_load(overlay_config_path.read_text(encoding="utf-8"))
    config["tiers"]["T1"]["fallback_strategy"] = "cheapest_now"
    config["tiers"]["T1"]["pin_primary"] = False
    config["tiers"]["T1"]["fallback"].append(
        {"model": "glm-4.6", "provider": "zai", "billing_mode": "metered"}
    )

    traced = _explain_over(service, config, "Summarize this note",
                           at=PEAK.replace(hour=21))
    chain = [hop["model"] for hop in traced["chain_plan"]["chain"]]
    # 21:00 is INSIDE the operator peak (20:00-22:00): pro is 3x and last.
    assert chain[-1] == "deepseek-v4-pro"
    assert chain[0] != "deepseek-v4-pro"


def test_overlay_never_mutates_the_parsed_config(overlay_config_path):
    """Applying the overlay is a copy, not a write into the operator's config."""
    config = yaml.safe_load(overlay_config_path.read_text(encoding="utf-8"))
    snapshot = yaml.safe_dump(config, sort_keys=True)
    merged = rules_mod.with_global_price_windows(config)
    # The merged view carries the windows...
    hop = merged["tiers"]["T1"]
    assert hop.get("price_windows") == [{"hours_utc": [20, 22], "multiplier": 3.0}]
    # ...the original does not, and is byte-identical to itself afterwards.
    assert "price_windows" not in config["tiers"]["T1"]
    assert yaml.safe_dump(config, sort_keys=True) == snapshot
    # And mutating the merged copy cannot reach the operator's block either.
    hop["price_windows"][0]["multiplier"] = 99.0
    assert config["price_windows"]["deepseek-v4-pro"][0]["multiplier"] == 3.0


def test_a_local_declaration_still_wins_over_the_overlay(tmp_path, monkeypatch):
    """The overlay is global preference; a per-elo `declared` window is the exception."""
    _pin_clock(monkeypatch, PEAK.replace(hour=2))
    path = tmp_path / "router.yaml"
    cfg = _base_config(price_windows={
        "deepseek-v4-pro": [{"hours_utc": [20, 22], "multiplier": 3.0}],
    })
    cfg["tiers"]["T1"]["price_windows"] = [{"hours_utc": [1, 3], "multiplier": 5.0}]
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    liveness = RouterService(path).liveness()
    pro = {e["model_key"]: e for e in liveness["models"]}["deepseek-v4-pro@deepseek"]
    # 02:00 UTC is inside the DECLARED 01:00-03:00 window at 5.0x, not the
    # overlay's 20:00-22:00 and not the registry's peaks (registry peaks are
    # 01:00-04:00 at 2.0x — the DECLARED window replaces them).
    assert pro["price_multiplier"] == 5.0
    assert pro["price_windows_origin"] == "declared"


# ---------------------------------------------------------------------------
# 2. The hot write path: _HOT_KEYS, plan/apply, lag detection
# ---------------------------------------------------------------------------


def test_price_windows_is_a_hot_key_and_round_trips(overlay_config_path):
    service = RouterService(overlay_config_path)
    changes = {"price_windows": {"deepseek-v4-pro": [
        {"hours_utc": [22, 24], "weekdays": [5, 6], "multiplier": 0.5},
    ]}}

    plan = service.plan(changes)
    assert plan["valid"] is True
    assert plan["policy"]["price_windows"]["deepseek-v4-pro"] == [
        {"hours_utc": [22, 24], "weekdays": [5, 6], "multiplier": 0.5},
    ]
    # plan() is pure: the file still holds the old overlay.
    assert yaml.safe_load(overlay_config_path.read_text(encoding="utf-8"))[
        "price_windows"]["deepseek-v4-pro"] == [
        {"hours_utc": [20, 22], "multiplier": 3.0},
    ]

    result = service.apply(plan["base_hash"], changes)
    assert result["ok"] is True and result.get("no_op") is False
    written = yaml.safe_load(overlay_config_path.read_text(encoding="utf-8"))
    assert written["price_windows"]["deepseek-v4-pro"] == [
        {"hours_utc": [22, 24], "weekdays": [5, 6], "multiplier": 0.5},
    ]
    # The untouched sibling survives the merge.
    assert written["price_windows"]["deepseek-v4-flash"] == []

    # And a null removes one model's entry without touching the rest.
    plan2 = service.plan({"price_windows": {"deepseek-v4-flash": None}})
    assert plan2["valid"] is True
    assert "deepseek-v4-flash" not in plan2["policy"]["price_windows"]
    assert "deepseek-v4-pro" in plan2["policy"]["price_windows"]


def test_stale_hash_refuses_the_overlay_write(overlay_config_path):
    service = RouterService(overlay_config_path)
    before = overlay_config_path.read_bytes()
    result = service.apply("deadbeef" * 8, {"price_windows": {"glm-4.6": []}})
    assert result["ok"] is False and result["conflict"] is True
    assert overlay_config_path.read_bytes() == before


# ---------------------------------------------------------------------------
# 3. Malformed overlays are refused naming the defect — never half-applied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("windows,expected_fragment", [
    ([{"hours_utc": [6, 10], "multiplier": 2.0},
      {"hours_utc": [8, 12], "multiplier": 1.5}], "entries overlap"),
    ([{"hours_utc": [22, 2], "multiplier": 0.8}], "cross midnight with two entries"),
    ([{"hours_utc": [16.5, 24], "multiplier": 0.8}], "WHOLE"),
    ([{"hours_utc": [6, 10], "multiplier": 0}], "positive number"),
    ([{"hours_utc": [6, 10], "multiplier": 2.0, "weekdays": [0, 7]}], "0..6"),
    ([{"hours_utc": [6, 10], "multiplier": 2.0, "weekdays": []}], "non-empty list"),
    ([{"hours_utc": [6, 10], "multiplier": 2.0},
      "not-a-window"], "entry 1 is not a mapping"),
])
def test_malformed_overlay_is_refused_naming_the_defect(
        tmp_path, windows, expected_fragment):
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(_base_config(price_windows={"deepseek-v4-pro": windows}),
                       sort_keys=False),
        encoding="utf-8",
    )
    service = RouterService(path)
    status = service.status()
    assert status["valid"] is False
    assert any(
        "deepseek-v4-pro" in e and expected_fragment in e
        for e in status["validation_errors"]
    ), status["validation_errors"]
    # The write gate refuses it too: an identical edit cannot be applied.
    plan = service.plan({"price_windows": {"deepseek-v4-pro": windows}})
    assert plan["valid"] is False
    result = service.apply(plan["base_hash"], plan["policy"])
    assert result["ok"] is False


def test_a_non_mapping_overlay_block_is_refused(tmp_path):
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(_base_config(price_windows=[{"hours_utc": [6, 10]}]),
                       sort_keys=False),
        encoding="utf-8",
    )
    service = RouterService(path)
    assert service.status()["valid"] is False
    assert any(
        "price_windows must be a mapping" in e
        for e in service.status()["validation_errors"]
    )


def test_bad_verified_date_is_refused(tmp_path):
    """`price_windows_verified` is a HUMAN confirmation date, ISO-shaped only."""
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(_base_config(price_windows={
            "deepseek-v4-pro": {
                "price_windows": [{"hours_utc": [20, 22], "multiplier": 3.0}],
                "price_windows_verified": "2026-08-26T09:00:00Z",
            },
        }), sort_keys=False),
        encoding="utf-8",
    )
    service = RouterService(path)
    assert service.status()["valid"] is False
    assert any(
        "price_windows_verified must be an ISO date string" in e
        for e in service.status()["validation_errors"]
    )


def test_a_good_verified_date_is_accepted_and_served(tmp_path):
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(_base_config(price_windows={
            "deepseek-v4-pro": {
                "price_windows": [{"hours_utc": [20, 22], "multiplier": 3.0}],
                "price_windows_verified": "2026-08-26",
            },
        }), sort_keys=False),
        encoding="utf-8",
    )
    service = RouterService(path)
    assert service.status()["valid"] is True
    entry = service.capabilities()["models"]["deepseek-v4-pro"]
    assert entry["price_windows_origin"] == "overlay"
    assert entry["price_windows_verified"] == "2026-08-26"


# ---------------------------------------------------------------------------
# 4. The read surfaces report provenance as a fact
# ---------------------------------------------------------------------------


def test_capabilities_reports_origin_and_verified_for_all_three_sources(
        overlay_config_path):
    models = RouterService(overlay_config_path).capabilities()["models"]

    # Overlay-declared: origin overlay, verified from the overlay entry.
    pro = models["deepseek-v4-pro"]
    assert pro["price_windows_origin"] == "overlay"
    assert pro["price_windows"] == [{"hours_utc": [20, 22], "multiplier": 3.0}]

    # Registry-owned: a model the overlay does not mention keeps the registry's
    # windows, the registry's verified date, and origin=registry.
    glm = models["glm-5.3-flash"]
    assert glm["price_windows_origin"] == "registry"
    assert glm["price_windows_verified"] == "2026-08-27"
    assert glm["price_windows"] == [
        {"hours_utc": [6, 10], "weekdays": [0, 1, 2, 3, 4], "multiplier": 2.0},
    ]

    # The explicit empty list is a REPLACEMENT (flat pricing), not a fallthrough
    # to the registry: origin stays overlay and the windows are absent.
    flash = models["deepseek-v4-flash"]
    assert flash["price_windows_origin"] == "overlay"
    assert "price_windows" not in flash


def test_liveness_reports_origin_per_elo(overlay_config_path):
    by_key = {
        e["model_key"]: e
        for e in RouterService(overlay_config_path).liveness()["models"]
    }
    assert by_key["deepseek-v4-pro@deepseek"]["price_windows_origin"] == "overlay"
    assert by_key["glm-4.6@zai"]["price_windows_origin"] == "registry"


def test_registry_pricing_is_untouched_without_an_overlay(
        tmp_path, monkeypatch):
    """No price_windows key: every answer is the registry's own, origin registry."""
    _pin_clock(monkeypatch, PEAK)
    path = tmp_path / "router.yaml"
    path.write_text(yaml.safe_dump(_base_config(), sort_keys=False), encoding="utf-8")
    service = RouterService(path)
    liveness = service.liveness()
    pro = {e["model_key"]: e for e in liveness["models"]}["deepseek-v4-pro@deepseek"]
    # 07:00 UTC Monday: the registry's own weekday peak, at the registry's 2.0x.
    assert pro["price_multiplier"] == 2.0
    assert pro["in_expensive_window"] is True
    assert pro["price_windows_origin"] == "registry"
    assert pro["price_windows"] == [
        {"hours_utc": [1, 4], "weekdays": [0, 1, 2, 3, 4], "multiplier": 2.0},
        {"hours_utc": [6, 10], "weekdays": [0, 1, 2, 3, 4], "multiplier": 2.0},
    ]


# ---------------------------------------------------------------------------
# 5. The decision order is unchanged: filter -> time_cap -> time_policy ->
#    fallback_strategy, with a configured window in play
# ---------------------------------------------------------------------------


def test_decision_order_is_unchanged_with_a_configured_window(
        overlay_config_path, monkeypatch):
    """The overlay prices the stages; it must not reorder them.

    Stage discipline with the overlay in force, asserted through /explain on
    the REAL read path (a variant file): the capability filter removes the
    vision-less deepseek elos while a vision-capable elo survives, the cap then
    acts on the SURVIVORS (nothing left to cap here), and the strategy orders
    what remains. A promotion the filter would remove can never appear — the
    documented order filter -> time_cap -> time_policy -> fallback_strategy.
    """
    _pin_clock(monkeypatch, PEAK.replace(hour=21))
    service = RouterService(overlay_config_path)
    config = yaml.safe_load(overlay_config_path.read_text(encoding="utf-8"))
    # A vision requirement the deepseek elos cannot meet; gpt-5.6-sol can.
    config["tiers"]["T1"]["requirements"] = {"vision": True}
    config["tiers"]["T1"]["fallback"] = [
        {"model": "gpt-5.6-sol", "provider": "openai-codex",
         "billing_mode": "metered"},
        {"model": "deepseek-v4-flash", "provider": "deepseek",
         "billing_mode": "metered"},
    ]
    config["tiers"]["T1"]["time_cap"] = {"max_multiplier": 1.5}
    config["tiers"]["T1"]["time_policy"] = {"avoid_peak": ["deepseek"]}
    config["tiers"]["T1"]["fallback_strategy"] = "cheapest_now"

    traced = _explain_over(service, config, "Describe a screenshot in detail",
                           at=PEAK.replace(hour=21))
    plan = traced["chain_plan"]
    rejected = {
        hop["model"]: hop.get("reject_reason") for hop in plan.get("rejected", [])
    }
    # The filter removed the vision-less deepseek elo FIRST, before any time
    # stage could price or promote it.
    assert rejected.get("deepseek-v4-flash") == "no_vision"
    assert plan["bypassed"] is False
    # The survivor set is exactly the vision-capable elo; nothing remained for
    # the cap to remove or the policy to demote.
    assert [hop["model"] for hop in plan["chain"]] == ["gpt-5.6-sol"]
    assert plan["capped"] == [] and plan["demoted"] == []


# ---------------------------------------------------------------------------
# Helper: explain against an in-memory config (the file keeps the overlay)
# ---------------------------------------------------------------------------


def _explain_over(service, config, task, at=None):
    """Run the deterministic dry-run over ``config`` without touching the file.

    Writes the variant to the SAME directory the service already reads from —
    a sibling file — so the answer comes from the real read path (file bytes,
    lint, tier materialisation) rather than from a patched method that could
    diverge from production.
    """
    variant = service._config_path.with_name("router.variant.yaml")
    variant.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return RouterService(variant).explain(task, at=at)


# ---------------------------------------------------------------------------
# 6. Defensive paths: the merge and the lint degrade, never raise
# ---------------------------------------------------------------------------


def test_overlay_merge_tolerates_a_non_dict_config():
    assert rules_mod.with_global_price_windows("nope") == "nope"
    assert rules_mod.with_global_price_windows(None) is None


def test_overlay_merge_tolerates_a_non_mapping_overlay():
    config = {"price_windows": [{"hours_utc": [6, 10]}], "tiers": {}}
    # A malformed overlay is a no-op for the MERGE — lint is the gate that
    # refuses it — and the caller's object is handed back untouched.
    assert rules_mod.with_global_price_windows(config) is config


def test_overlay_merge_treats_an_empty_overlay_as_absent():
    config = {"price_windows": {}, "tiers": {"T1": {"model": "m", "provider": "p"}}}
    # ``price_windows: {}`` is "no overlay", and the hot path must not pay a deep
    # copy for the empty table the console's write path can leave behind.
    assert rules_mod.with_global_price_windows(config) is config


def test_overlay_merge_tolerates_a_non_dict_tiers_block():
    config = {
        "price_windows": {"m": [{"hours_utc": [6, 10], "multiplier": 2.0}]},
        "tiers": "nope",
    }
    merged = rules_mod.with_global_price_windows(config)
    assert merged["tiers"] == "nope"
    assert config["price_windows"]["m"][0]["multiplier"] == 2.0


def test_overlay_merge_skips_non_dict_tier_and_hop():
    config = {
        "price_windows": {"m": [{"hours_utc": [6, 10], "multiplier": 2.0}]},
        "tiers": {
            "T1": "not-a-tier",
            "T2": {"model": "m", "provider": "p", "fallback": ["not-a-hop"]},
        },
    }
    merged = rules_mod.with_global_price_windows(config)
    assert merged["tiers"]["T1"] == "not-a-tier"
    assert merged["tiers"]["T2"]["fallback"] == ["not-a-hop"]
    # The original is untouched even though the T2 primary matched the overlay.
    assert "price_windows" not in config["tiers"]["T2"]


def test_inject_skips_a_hop_without_a_usable_model():
    hop: dict = {"provider": "zai"}
    rules_mod._inject_overlay_windows(hop, {"glm-4.6": []})
    assert "price_windows" not in hop


def test_inject_extended_form_without_a_verified_date():
    hop: dict = {"model": "glm-4.6"}
    rules_mod._inject_overlay_windows(
        hop, {"glm-4.6": {"price_windows": [{"hours_utc": [6, 10], "multiplier": 2.0}]}}
    )
    assert hop["price_windows"] == [{"hours_utc": [6, 10], "multiplier": 2.0}]
    assert "price_windows_verified" not in hop


def test_inject_extended_form_with_a_null_windows_list():
    # `price_windows: null` in the extended form injects nothing: it is the same
    # "absent, keep the registry" spelling the simple form's `model: null` is.
    hop: dict = {"model": "glm-4.6"}
    rules_mod._inject_overlay_windows(
        hop, {"glm-4.6": {"price_windows": None, "price_windows_verified": "2026-08-26"}}
    )
    assert "price_windows" not in hop
    assert hop["price_windows_verified"] == "2026-08-26"


def test_overlay_lint_rejects_extended_form_without_price_windows(tmp_path):
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(_base_config(price_windows={
            "deepseek-v4-pro": {"price_windows_verified": "2026-08-26"},
        }), sort_keys=False),
        encoding="utf-8",
    )
    service = RouterService(path)
    assert service.status()["valid"] is False
    assert any(
        "must be a list of windows or a mapping" in e
        for e in service.status()["validation_errors"]
    )


def test_overlay_lint_rejects_an_unknown_overlay_field(tmp_path):
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(_base_config(price_windows={
            "deepseek-v4-pro": {"price_windows": [], "verifed": "2026-08-26"},
        }), sort_keys=False),
        encoding="utf-8",
    )
    service = RouterService(path)
    assert service.status()["valid"] is False
    assert any(
        "unrecognized overlay field 'verifed'" in e
        for e in service.status()["validation_errors"]
    )


def test_overlay_lint_degrades_without_the_registry(monkeypatch):
    # Without the registry the window-shape check cannot run — the same degrade
    # `_lint_price_windows` already makes — but the pure shape checks (a mapping,
    # a mapping per model, no unknown field) still hold.
    import router.rules as rules_module
    monkeypatch.setattr(rules_module, "_caps", None)
    config = {"price_windows": {"m": [{"hours_utc": [6, 10], "multiplier": 2.0}]}}
    assert rules_module._lint_global_price_windows(config) == []


def test_origin_degrades_on_a_non_dict_declared_index():
    import router.service as service_mod
    assert RouterService._price_windows_origin({}, None, "deepseek-v4-pro") is None


def test_served_windows_degrades_when_the_registry_raises(monkeypatch):
    import router.service as service_mod

    class Exploding:
        @staticmethod
        def capabilities_for(*_a, **_kw):
            raise TypeError("stale registry")

    monkeypatch.setattr(service_mod, "_caps", Exploding)
    assert RouterService._served_windows("deepseek-v4-pro", None) is None
