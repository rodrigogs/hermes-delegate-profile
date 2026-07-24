"""Contract tests for dynamic compaction thresholds."""

from router.threshold import (
    PRESETS,
    apply_dynamic_thresholds,
    compute_model_thresholds,
    p_eff,
    summarizer_cap,
)


def test_effective_thresholds_match_the_calibrated_curve():
    assert p_eff(128000, 50) == 0.850
    assert p_eff(200000, 50) == 0.800
    assert p_eff(272000, 50) == 0.766
    assert p_eff(1000000, 50) == 0.620
    assert p_eff(1050000, 50) == 0.614


def test_effective_threshold_respects_small_window_floor_and_upper_clamp():
    assert p_eff(272000, 100) >= 0.750
    assert p_eff(128000, 0) <= 0.900


def test_compute_model_thresholds_preserves_model_keys():
    models = [("glm-4.5-flash", 272000), ("gpt-5.6-terra", 1000000)]

    assert compute_model_thresholds(models, 50) == {
        "glm-4.5-flash": 0.766,
        "gpt-5.6-terra": 0.620,
    }


def test_summarizer_cap_is_650k_and_binds_large_main_contexts():
    cap = summarizer_cap(272000)

    assert cap == 650000
    assert cap > 272000
    assert cap < 1000000


def test_presets_expose_the_requested_aggressiveness_values():
    assert PRESETS == {
        "Max-context": 0,
        "Conservative": 25,
        "Balanced": 50,
        "Aggressive": 100,
    }


def test_apply_dynamic_thresholds_returns_a_new_config_without_mutating_input():
    config = {
        "model": {"default": "gpt-5.6-terra"},
        "auxiliary": {"compression": {"provider": "auto"}},
    }
    original = {
        "model": {"default": "gpt-5.6-terra"},
        "auxiliary": {"compression": {"provider": "auto"}},
    }
    model_windows = {"glm-4.5-flash": 272000, "gpt-5.6-terra": 1000000}

    result = apply_dynamic_thresholds(config, 50, 272000, model_windows)

    assert config == original
    assert result is not config
    assert result["model"] == original["model"]
    assert result["auxiliary"] == original["auxiliary"]
    assert result["compression"]["aggressiveness"] == 50
    assert result["compression"]["model_thresholds"] == compute_model_thresholds(
        model_windows.items(), 50
    )
    assert result["compression"]["threshold_tokens"] == summarizer_cap(272000) == 650000


def test_apply_dynamic_thresholds_creates_compression_and_is_idempotent():
    config = {"model": {"default": "gpt-5.6-terra"}}
    model_windows = {"gpt-5.6-terra": 1000000}

    once = apply_dynamic_thresholds(config, 25, 272000, model_windows)
    twice = apply_dynamic_thresholds(once, 25, 272000, model_windows)

    assert "compression" not in config
    assert "compression" in once
    assert twice == once
