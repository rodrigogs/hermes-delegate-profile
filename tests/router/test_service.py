"""Tests for the read-only router service shared by web surfaces."""

from __future__ import annotations

import json

import pytest
import yaml

from router.service import RouterService


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "classifier": {"model": "judge", "provider": "judge-rail"},
                "fail_safe": {
                    "profile": "coder",
                    "model": "strong",
                    "provider": "safe-rail",
                },
                "blocklist": {
                    "manual_ban": [
                        {"model": "bad", "provider": "rail", "reason": "stalls"}
                    ],
                    "fallback_chain": ["strong"],
                    "auto_breaker": {"enabled": False},
                },
                "rules": [
                    {
                        "id": "hard-verbs",
                        "status": "stable",
                        "when": {"verb_class": {"eq": "hard"}},
                        "then": {"profile": "coder", "model": "T4"},
                    }
                ],
                "default": {"action": "classify"},
                "tiers": {
                    "T1": {"model": "tiny", "provider": "cheap"},
                    "T2": {"model": "small", "provider": "cheap"},
                    "T3": {"model": "medium", "provider": "strong-rail"},
                    "T4": {
                        "model": "strong",
                        "provider": "strong-rail",
                        "fallback": [
                            {"model": "backup", "provider": "backup-rail"}
                        ],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_status_and_policy_are_read_only_snapshots(config_path):
    service = RouterService(config_path)

    status = service.status()
    policy = service.policy()

    assert status == {
        "valid": True,
        "validation_errors": [],
        "enabled": True,
        "rules_count": 1,
        "tiers": ["T1", "T2", "T3", "T4"],
        "classifier": {"model": "judge", "provider": "judge-rail"},
        "breaker_enabled": False,
    }
    assert policy["rules"][0]["id"] == "hard-verbs"
    assert policy["tiers"]["T4"]["fallback"] == [
        {"model": "backup", "provider": "backup-rail"}
    ]
    assert "api_key" not in json.dumps(policy)


def test_explain_is_deterministic_and_never_calls_classifier(config_path):
    service = RouterService(config_path)

    hard = service.explain("Debug a race condition")
    uncertain = service.explain("Summarize this note")

    assert hard["mode"] == "deterministic_dry_run"
    assert hard["requires_classifier"] is False
    assert hard["decision"]["cause"] == "hard_rule"
    assert hard["decision"]["output"]["fallback"][0]["provider"] == "backup-rail"
    assert uncertain["requires_classifier"] is True
    assert uncertain["decision"]["output"] == {"action": "classify"}


def test_blocklist_and_invalid_config_are_explicit(config_path, tmp_path):
    service = RouterService(config_path)
    blocklist = service.blocklist()
    assert blocklist["manual_bans"][0]["model"] == "bad"
    assert blocklist["fallback_chain"] == ["strong"]
    assert blocklist["breaker_enabled"] is False

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("enabled: [", encoding="utf-8")
    broken = RouterService(invalid)
    status = broken.status()
    assert status["valid"] is False
    assert status["enabled"] is False
    assert status["validation_errors"]


def test_explain_rejects_empty_or_oversized_tasks(config_path):
    service = RouterService(config_path, max_task_chars=12)
    with pytest.raises(ValueError, match="required"):
        service.explain("   ")
    with pytest.raises(ValueError, match="12 characters"):
        service.explain("x" * 13)


def test_scalar_and_invalid_policy_cannot_be_explained(tmp_path):
    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("just-a-string", encoding="utf-8")
    scalar_service = RouterService(scalar)
    assert scalar_service.status()["validation_errors"] == ["router config root must be a mapping"]

    invalid = tmp_path / "incomplete.yaml"
    invalid.write_text("enabled: true", encoding="utf-8")
    with pytest.raises(ValueError, match="policy is invalid"):
        RouterService(invalid).explain("Describe a task")
