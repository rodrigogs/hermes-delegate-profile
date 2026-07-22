"""Unit tests for CLI governance (router/cli.py)."""

import io
import json
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from router.cli import cmd_explain, cmd_lint, cmd_blocklist, build_parser, load_config


@pytest.fixture
def router_yaml():
    return {
        "enabled": True,
        "classifier": {
            "model": "glm-5.2",
            "provider": "zai",
            "temperature": 0,
            "max_tokens": 128,
            "timeout_seconds": 8,
        },
        "fail_safe": {
            "profile": "coder",
            "model": "claude-opus",
            "provider": "anthropic",
        },
        "blocklist": {
            "manual_ban": [
                {"model": "gpt-5.6-sol", "provider": "openai-codex",
                 "reason": "accept-but-never-stream"},
            ],
            "fallback_chain": ["gpt-5.6-sol", "glm-5.2"],
            "auto_breaker": {"enabled": False},
        },
        "rules": [
            {
                "id": "trivial-mechanical-edit",
                "status": "stable",
                "when": {"verb_class": {"eq": "trivial"}, "has_code": {"eq": True},
                         "size_lines": {"lte": 40}},
                "then": {"profile": "coder", "model": "T1"},
            },
            {
                "id": "hard-verbs",
                "status": "stable",
                "when": {"verb_class": {"eq": "hard"}},
                "then": {"profile": "coder", "model": "T4"},
            },
        ],
        "default": {"action": "classify"},
        "tiers": {
            "T1": {"model": "glm-5.2-fast", "provider": "zai"},
            "T2": {"model": "glm-5.2", "provider": "zai"},
            "T3": {"model": "claude-sonnet", "provider": "anthropic"},
            "T4": {"model": "claude-opus", "provider": "anthropic"},
        },
    }


@pytest.fixture
def config_file(router_yaml, tmp_path):
    path = tmp_path / "router.yaml"
    with open(path, "w") as f:
        yaml.dump(router_yaml, f)
    return str(path)


class TestCLIExplain:
    def test_explain_trivial(self, config_file, capsys):
        cmd_explain(_ns("explain", {"task": "Rename getCwd in 3 files, 20 lines",
                                     "config": config_file, "model": ""}))
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result["matched_rule_id"] == "trivial-mechanical-edit"
        assert result["output"]["profile"] == "coder"

    def test_explain_blocklist(self, config_file, capsys):
        cmd_explain(_ns("explain", {"task": "test", "config": config_file,
                                     "model": "gpt-5.6-sol"}))
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result["cause"] == "blocklist_veto"
        assert result["output"]["deny"] is True

    def test_explain_default(self, config_file, capsys):
        cmd_explain(_ns("explain", {"task": "Hello world", "config": config_file,
                                     "model": ""}))
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result["cause"] == "default_fallthrough"


class TestCLILint:
    def test_lint_valid(self, config_file, capsys):
        cmd_lint(_ns("lint", {"config": config_file}))
        out = capsys.readouterr().out
        assert "valid" in out

    def test_lint_invalid(self, tmp_path, capsys):
        path = tmp_path / "bad.yaml"
        with open(path, "w") as f:
            yaml.dump({"rules": [{"id": "x"}], "tiers": {"T1": {}}}, f)
        with pytest.raises(SystemExit):
            cmd_lint(_ns("lint", {"config": str(path)}))


class TestCLIBlocklist:
    def test_blocklist_show(self, config_file, capsys):
        cmd_blocklist(_ns("blocklist", {"config": config_file}))
        out = capsys.readouterr().out
        assert "gpt-5.6-sol" in out
        assert "glm-5.2" in out


class TestCLIParser:
    def test_parser_explain(self):
        parser = build_parser()
        args = parser.parse_args(["explain", "test task"])
        assert args.command == "explain"
        assert args.task == "test task"

    def test_parser_lint(self):
        parser = build_parser()
        args = parser.parse_args(["lint"])
        assert args.command == "lint"

    def test_parser_blocklist(self):
        parser = build_parser()
        args = parser.parse_args(["blocklist"])
        assert args.command == "blocklist"


def _ns(command, overrides):
    """Build a simple namespace mimicking argparse."""
    class NS:
        pass
    ns = NS()
    ns.command = command
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns
