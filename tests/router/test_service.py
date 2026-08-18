"""Tests for the router service shared by web surfaces (read + write paths).

Every time-dependent case pins the clock — either by passing ``at`` or by
replacing ``service._utc_now`` — so no test here depends on the hour it runs at.
A test that read the real clock would be the defect the injected-clock design
exists to prevent.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

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

    # `warnings` is purely ADDITIVE: pulling it out must leave the exact prior
    # response. These tier models are unknown to the capability registry, which
    # is advisory only — note `valid` stays True below.
    warnings = status.pop("warnings")
    assert warnings and all(
        "unknown to the capability registry" in warning for warning in warnings
    )

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


def test_plan_previews_without_writing(config_path):
    service = RouterService(config_path)
    before = config_path.read_bytes()

    plan = service.plan({"default": {"action": "T1"}})

    assert plan["valid"] is True
    assert plan["base_hash"]
    assert plan["policy"]["default"] == {"action": "T1"}
    assert "default" in plan["diff"]
    # plan() must be pure: the file is byte-identical afterwards.
    assert config_path.read_bytes() == before


def test_plan_rejects_unknown_tier_and_ignores_non_allowlisted_keys(config_path):
    service = RouterService(config_path)

    invalid = service.plan({"rules": [
        {"id": "x", "when": {"verb_class": {"eq": "hard"}}, "then": {"model": "T9"}}
    ]})
    assert invalid["valid"] is False
    assert any("T9" in e for e in invalid["errors"])

    # A key outside the hot allowlist is silently dropped, never written.
    plan = service.plan({"secrets": {"api_key": "leak"}, "default": {"action": "T2"}})
    assert "secrets" not in plan["policy"]
    assert plan["policy"]["default"] == {"action": "T2"}


def test_plan_flags_structurally_broken_fail_safe(config_path):
    service = RouterService(config_path)
    # rules.lint never inspects fail_safe; blanking its model must still be
    # caught by the structural guard before it reaches the hot file.
    plan = service.plan({"fail_safe": {"model": ""}})
    assert plan["valid"] is False
    assert any("fail_safe.model" in e for e in plan["errors"])


def test_apply_commits_on_matching_hash_and_reverts(config_path):
    service = RouterService(config_path)
    plan = service.plan({"tiers": {"T4": {"model": "stronger", "provider": "strong-rail"}}})

    result = service.apply(plan["base_hash"], plan["policy"])
    assert result["ok"] is True

    # Hot-reload: a fresh read reflects the change, and key order is preserved.
    reloaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert reloaded["tiers"]["T4"]["model"] == "stronger"
    assert list(reloaded.keys())[0] == "enabled"

    backup = config_path.with_suffix(config_path.suffix + ".bak")
    assert backup.exists()

    revert = service.apply_revert()
    assert revert["ok"] is True
    assert yaml.safe_load(config_path.read_text())["tiers"]["T4"]["model"] == "strong"


def test_apply_returned_hash_matches_next_plan_no_false_409(config_path):
    """The base_hash apply returns must equal the next plan's base_hash.

    Guards against hashing a re-read (which can differ from the bytes written by
    a trailing newline/formatting nudge and turn every follow-up apply into a
    false 409 conflict).
    """
    service = RouterService(config_path)
    plan1 = service.plan({"default": {"action": "T1"}})
    applied = service.apply(plan1["base_hash"], plan1["policy"])
    assert applied["ok"] is True

    plan2 = service.plan({"default": {"action": "T2"}})
    assert plan2["base_hash"] == applied["base_hash"]
    # And a second apply against that hash commits cleanly (no false conflict).
    assert service.apply(plan2["base_hash"], plan2["policy"])["ok"] is True


def test_revert_restores_byte_identical_original(config_path):
    """apply → revert returns the file to the exact original bytes."""
    service = RouterService(config_path)
    original = config_path.read_bytes()
    plan = service.plan({"tiers": {"T4": {"model": "x", "provider": "y"}}})
    service.apply(plan["base_hash"], plan["policy"])
    assert config_path.read_bytes() != original
    revert = service.apply_revert()
    assert revert["ok"] is True
    assert config_path.read_bytes() == original
    assert revert["base_hash"] == RouterService._hash_bytes(original)


def test_apply_refuses_on_stale_hash_and_leaves_file_untouched(config_path):
    service = RouterService(config_path)
    before = config_path.read_bytes()

    result = service.apply("deadbeef" * 8, {"default": {"action": "T1"}})
    assert result["ok"] is False
    assert result["conflict"] is True
    assert result["base_hash"] == RouterService._hash_bytes(before)
    assert config_path.read_bytes() == before


def test_apply_refuses_lint_invalid_merge(config_path):
    service = RouterService(config_path)
    # A rule referencing a nonexistent tier fails lint; apply must refuse.
    plan = service.plan({"rules": [
        {"id": "bad", "when": {"verb_class": {"eq": "hard"}}, "then": {"model": "T9"}}
    ]})
    assert plan["valid"] is False
    before = config_path.read_bytes()
    result = service.apply(plan["base_hash"], plan["policy"])
    assert result["ok"] is False
    assert result["errors"]
    assert config_path.read_bytes() == before  # refused write leaves file intact


def test_apply_lists_replace_wholesale(config_path):
    """Sending a shorter rules list must DELETE rules, not union them."""
    service = RouterService(config_path)
    plan = service.plan({"rules": []})
    service.apply(plan["base_hash"], plan["policy"])
    assert yaml.safe_load(config_path.read_text())["rules"] == []


def test_apply_is_serialized_under_concurrency(config_path):
    """Two concurrent applies with the SAME base_hash: exactly one commits."""
    service = RouterService(config_path)
    base = RouterService._hash_bytes(config_path.read_bytes())
    results = []
    barrier = threading.Barrier(2)

    def worker(model_name):
        barrier.wait()
        results.append(service.apply(base, {"tiers": {"T4": {"model": model_name, "provider": "strong-rail"}}}))

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if r.get("ok")]
    conflicts = [r for r in results if r.get("conflict")]
    # The lock forces serialization: the first wins, the second sees drift.
    assert len(oks) == 1
    assert len(conflicts) == 1


def test_apply_revert_without_snapshot_is_explicit(config_path):
    service = RouterService(config_path)
    assert service.apply_revert() == {"ok": False, "error": "no snapshot"}


def test_fail_safe_guard_covers_mapping_and_fallback_shape(config_path):
    service = RouterService(config_path)
    # fail_safe present but not a mapping.
    bad_type = service.plan({"fail_safe": []})
    assert any("must be a mapping" in e for e in bad_type["errors"])
    # fallback present but not a list.
    bad_fallback = service.plan({"fail_safe": {"fallback": "nope"}})
    assert any("fail_safe.fallback must be a list" in e for e in bad_fallback["errors"])


def test_plan_and_apply_reject_non_mapping_changes(config_path):
    service = RouterService(config_path)
    with pytest.raises(ValueError, match="changes must be a mapping"):
        service.plan(["not", "a", "dict"])
    with pytest.raises(ValueError, match="changes must be a mapping"):
        service.apply("hash", ["nope"])


def test_plan_reports_unreadable_config(tmp_path):
    missing = tmp_path / "gone.yaml"
    plan = RouterService(missing).plan({"default": {"action": "T1"}})
    assert plan["valid"] is False
    assert any("could not read router config" in e for e in plan["errors"])


def test_apply_reports_unreadable_config(tmp_path):
    missing = tmp_path / "gone.yaml"
    result = RouterService(missing).apply("hash", {"default": {"action": "T1"}})
    assert result["ok"] is False
    assert any("could not read router config" in e for e in result["errors"])


def test_apply_reports_malformed_yaml_after_hash_match(tmp_path):
    """A config that hashes fine but parses to a non-mapping is a parse error."""
    path = tmp_path / "router.yaml"
    path.write_text("just-a-scalar", encoding="utf-8")
    service = RouterService(path)
    base = RouterService._hash_bytes(path.read_bytes())
    result = service.apply(base, {"default": {"action": "T1"}})
    assert result["ok"] is False
    assert any("could not parse router config" in e for e in result["errors"])


def test_apply_write_failure_leaves_config_and_backup_consistent(config_path, monkeypatch):
    """If the config write fails after the .bak snapshot, os.replace atomicity
    guarantees the config stays at the OLD bytes — which is exactly what .bak
    holds — so a later revert restores a state that matches, never a mismatch.
    """
    import router.service as service_mod

    service = RouterService(config_path)
    original = config_path.read_bytes()
    plan = service.plan({"default": {"action": "T1"}})

    # Fail ONLY the main-config replace (the second _atomic_write_bytes call),
    # after the .bak snapshot has been written.
    calls = {"n": 0}
    real_replace = service_mod.os.replace

    def flaky_replace(src, dst):
        calls["n"] += 1
        if str(dst) == str(config_path):
            raise OSError("write interrupted")
        return real_replace(src, dst)

    monkeypatch.setattr(service_mod.os, "replace", flaky_replace)
    with pytest.raises(OSError, match="write interrupted"):
        service.apply(plan["base_hash"], plan["policy"])

    # Config untouched (atomic replace never happened) and the .bak equals it.
    assert config_path.read_bytes() == original
    backup = config_path.with_suffix(config_path.suffix + ".bak")
    assert backup.read_bytes() == original


def _seed_traces(tmp_path, monkeypatch, entries, backups=None):
    """Write route traces to a temp HERMES_HOME state dir; return the base path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from router.durable_decision_log import routes_path
    base = routes_path()
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    if backups:
        for suffix, backup_entries in backups.items():
            p = base.with_suffix(base.suffix + suffix)
            p.write_text("".join(json.dumps(e) + "\n" for e in backup_entries), encoding="utf-8")
    return base


def test_routes_lists_recent_first_with_projection(tmp_path, monkeypatch, config_path):
    _seed_traces(tmp_path, monkeypatch, [
        {"ts": 1.0, "cause": "hard_rule", "task": "a", "output": {"model": "m1"}},
        {"ts": 2.0, "cause": "classifier", "task": "b", "output": {"model": "m2"}},
    ])
    svc = RouterService(config_path)
    result = svc.routes()
    assert result["count"] == 2
    assert result["trace_path"].endswith("routes.jsonl")
    # Most recent first.
    assert result["routes"][0]["cause"] == "classifier"
    assert result["routes"][0]["model"] == "m2"
    assert result["routes"][1]["task"] == "a"


def test_routes_honors_limit_and_bad_limit_falls_back(tmp_path, monkeypatch, config_path):
    _seed_traces(tmp_path, monkeypatch, [
        {"ts": float(i), "cause": "classifier", "task": f"t{i}", "output": {"model": f"m{i}"}}
        for i in range(5)
    ])
    svc = RouterService(config_path)
    assert len(svc.routes(limit=2)["routes"]) == 2
    # A non-numeric limit falls back to the default, not a crash.
    assert svc.routes(limit="oops")["count"] == 5


def test_routes_skips_corrupt_lines_and_missing_file(tmp_path, monkeypatch, config_path):
    # This test asserts the HERMES_HOME-derived path, so it opts out of the
    # blanket HERMES_ROUTE_TRACE_FILE guard in conftest.
    monkeypatch.delenv("HERMES_ROUTE_TRACE_FILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from router.durable_decision_log import routes_path
    base = routes_path()
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(
        json.dumps({"ts": 1.0, "cause": "hard_rule", "output": {"model": "ok"}}) + "\n"
        + "\n"  # blank line — skipped
        + "   \n"  # whitespace-only — skipped
        + "{ this is not json\n"
        + json.dumps("a-string-not-a-dict") + "\n",
        encoding="utf-8",
    )
    svc = RouterService(config_path)
    assert svc.routes()["count"] == 1  # only the valid dict line

    # Missing file → empty, never raises.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty"))
    assert svc.routes()["count"] == 0
    assert svc.routes()["routes"] == []


def test_route_by_id_returns_full_entry_with_steps(tmp_path, monkeypatch, config_path):
    _seed_traces(tmp_path, monkeypatch, [
        {"ts": 7.0, "cause": "classifier", "task": "x", "output": {"model": "m"},
         "steps": [{"stage": "blocklist"}, {"stage": "classifier"}]},
    ])
    svc = RouterService(config_path)
    listed = svc.routes()["routes"][0]
    full = svc.route(listed["id"])
    assert full is not None
    assert full["steps"][1]["stage"] == "classifier"
    assert svc.route("nonexistent-id") is None
    assert svc.route("") is None


def test_routes_backfills_from_rotated_backup(tmp_path, monkeypatch, config_path):
    # Current file has 1, backup .1 has 2 → limit 3 back-fills across rotation.
    _seed_traces(
        tmp_path, monkeypatch,
        [{"ts": 3.0, "cause": "classifier", "task": "new", "output": {}}],
        backups={".1": [
            {"ts": 1.0, "cause": "hard_rule", "task": "old1", "output": {}},
            {"ts": 2.0, "cause": "hard_rule", "task": "old2", "output": {}},
        ]},
    )
    svc = RouterService(config_path)
    result = svc.routes(limit=3)
    assert result["count"] == 3
    assert result["routes"][0]["task"] == "new"  # most recent first


def test_routes_skips_absent_backup_in_chain(tmp_path, monkeypatch, config_path):
    # A gap in the backup chain (.1 absent, .2 present) exercises the
    # missing-file continue without raising.
    base = _seed_traces(
        tmp_path, monkeypatch,
        [{"ts": float(i), "cause": "classifier", "task": f"c{i}", "output": {}} for i in range(3)],
        backups={".2": [{"ts": 99.0, "cause": "hard_rule", "task": "deep", "output": {}}]},
    )
    assert not base.with_suffix(base.suffix + ".1").exists()  # gap in the chain
    svc = RouterService(config_path)
    result = svc.routes(limit=2)
    assert len(result["routes"]) == 2  # limit caps the projection
    # The reader walks past the absent .1 (continue) into .2 without raising;
    # count reflects all readable entries across the chain.
    assert result["count"] == 4  # 3 current + 1 from .2
    assert any(r["task"] == "deep" for r in svc.routes(limit=100)["routes"])


def test_validate_fail_safe_is_noop_when_absent():
    """No fail_safe key -> nothing to validate."""
    assert RouterService._validate_fail_safe({"default": {}}) == []


def test_atomic_write_cleans_up_temp_on_failure(config_path, monkeypatch):
    """If os.replace fails, the temp file is unlinked and the error propagates."""
    import router.service as service_mod

    monkeypatch.setattr(
        service_mod.os, "replace",
        lambda *_a: (_ for _ in ()).throw(OSError("disk full")),
    )
    unlinked = {"n": 0}
    real_unlink = service_mod.os.unlink
    monkeypatch.setattr(
        service_mod.os, "unlink",
        lambda p: (unlinked.__setitem__("n", unlinked["n"] + 1), real_unlink(p))[1],
    )
    with pytest.raises(OSError, match="disk full"):
        RouterService._atomic_write_bytes(config_path, b"data")
    assert unlinked["n"] == 1  # temp file was cleaned up


def test_atomic_write_swallows_unlink_error_during_cleanup(config_path, monkeypatch):
    """If cleanup unlink ALSO fails, the original error still propagates."""
    import router.service as service_mod

    monkeypatch.setattr(
        service_mod.os, "replace",
        lambda *_a: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        service_mod.os, "unlink",
        lambda *_a: (_ for _ in ()).throw(OSError("cleanup failed")),
    )
    with pytest.raises(OSError, match="disk full"):
        RouterService._atomic_write_bytes(config_path, b"data")


def test_policy_references_skips_malformed_declarations():
    """_policy_references ignores non-dict entries, blank/typed model/provider,
    non-dict tiers, and maps dict-form fallback-chain entries."""
    config = {
        "classifier": "not-a-dict",                       # add() early-return (non-dict)
        "tiers": {
            "T1": {"model": "", "provider": "x"},          # blank model -> skipped
            "T2": {"model": "m", "provider": 5},           # non-str provider -> skipped
            "T3": {"model": "good", "provider": "rail",
                   "fallback": [{"model": "fb", "provider": "fbrail"}, "loose"]},
            "T4": "not-a-dict-tier",                        # non-dict tier -> add() skips, no fallback recurse
        },
        "fail_safe": {"model": "fs", "provider": "fsrail",
                      "fallback": [{"model": "fsfb", "provider": "fsfbrail"}]},
    }
    # chain has: a known-model string (continue branch), an UNKNOWN-model string
    # (falls through, added to neither), and a dict form (added).
    refs = RouterService._policy_references(
        config, ["good", "totally-unknown", {"model": "chain", "provider": "chainrail"}]
    )
    pairs = set(refs)
    assert ("good", "rail") in pairs
    assert ("fb", "fbrail") in pairs
    assert ("fs", "fsrail") in pairs
    assert ("chain", "chainrail") in pairs      # dict-form chain entry added
    assert ("", "x") not in pairs               # blank model dropped
    assert ("m", 5) not in pairs                # typed provider dropped


def test_policy_references_fail_safe_dict_without_fallback():
    """fail_safe is a dict but has no fallback list -> the fallback loop is skipped."""
    refs = RouterService._policy_references(
        {"fail_safe": {"model": "fs", "provider": "fsrail"}}, []
    )
    assert ("fs", "fsrail") in set(refs)


def test_policy_references_handles_non_dict_tiers_block():
    """A tiers value that is not a mapping is tolerated (no crash, no refs)."""
    assert RouterService._policy_references({"tiers": "nope"}, []) == []


def test_policy_references_empty_config_skips_all_loops():
    """No classifier/tiers/fail_safe: every add() and both fallback loops are
    no-ops (covers the fail_safe-is-{} -> skip-fallback-loop branch)."""
    assert RouterService._policy_references({}, []) == []


def test_policy_references_non_dict_fail_safe_skips_block():
    """A fail_safe that is not a mapping skips the whole fail_safe.fallback
    block (the isinstance(fail_safe, dict) False branch, 222->229)."""
    refs = RouterService._policy_references(
        {"fail_safe": "not-a-dict", "classifier": {"model": "c", "provider": "r"}}, []
    )
    assert ("c", "r") in set(refs)


def test_is_manually_banned_skips_non_dict_and_matches_blank_provider():
    # A non-dict ban entry is skipped; a ban with no provider matches any.
    bans = ["not-a-dict", {"model": "x", "provider": ""}]
    assert RouterService._is_manually_banned(bans, "x", "any-rail") is True
    assert RouterService._is_manually_banned(bans, "other", "rail") is False


def test_is_manually_banned_specific_provider_must_match():
    """A ban scoped to a specific provider does NOT fire for another provider
    (exercises the same-model/different-provider fall-through)."""
    bans = [{"model": "x", "provider": "rail-a"}]
    assert RouterService._is_manually_banned(bans, "x", "rail-b") is False
    assert RouterService._is_manually_banned(bans, "x", "rail-a") is True


def test_liveness_reports_validation_errors_and_survives_internal_error(tmp_path, monkeypatch):
    # (a) invalid config -> liveness still returns, carrying validation_errors.
    invalid = tmp_path / "bad.yaml"
    invalid.write_text("enabled: true\n", encoding="utf-8")  # missing default/tiers
    result = RouterService(invalid).liveness()
    assert result.get("validation_errors")

    # (b) an unexpected internal error is caught -> degraded envelope, no raise.
    service = RouterService(invalid)
    monkeypatch.setattr(
        "router.service.Blocklist.fallback_chain",
        lambda _self: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    degraded = service.liveness()
    assert degraded["worst"] == "degraded"
    assert "could not compose liveness" in degraded["error"]


def test_liveness_composes_states(config_path, monkeypatch):
    """Policy references are composed with breaker and manual-ban state."""
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["tiers"] = {
        "T1": {"model": "alive", "provider": "cheap"},
        "T2": {"model": "probing", "provider": "cheap"},
        "T3": {
            "model": "quota", "provider": "primary",
            "fallback": [{"model": "backup", "provider": "backup-rail"}],
        },
        "T4": {"model": "manual", "provider": "blocked-rail"},
    }
    config["classifier"] = {"model": "judge", "provider": "judge-rail"}
    config["fail_safe"] = {
        "model": "safe", "provider": "safe-rail",
        "fallback": [{"model": "backup", "provider": "backup-rail"}],
    }
    config["blocklist"] = {
        "manual_ban": [{"model": "manual", "provider": "blocked-rail"}],
        "fallback_chain": ["quota", "backup"],
        "auto_breaker": {"enabled": True},
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    monkeypatch.setattr(
        "router.service.Blocklist.breaker_status",
        lambda _self: [
            {
                "model_key": "quota@primary",
                "state": "OPEN",
                "cooldown_remaining_s": 42.0,
                "last_failure_kind": "quota_exhausted",
            },
            {
                "model_key": "probing@cheap",
                "state": "HALF_OPEN",
                "cooldown_remaining_s": 0.0,
                "last_failure_kind": "hard_timeout",
            },
        ],
    )

    liveness = RouterService(config_path).liveness()

    states = {entry["model_key"]: entry["state"] for entry in liveness["models"]}
    assert states == {
        "alive@cheap": "alive",
        "backup@backup-rail": "alive",
        "judge@judge-rail": "alive",
        "manual@blocked-rail": "dead",
        "probing@cheap": "degraded",
        "quota@primary": "quota_exhausted",
        "safe@safe-rail": "alive",
    }
    assert liveness["worst"] == "dead"
    assert "429" not in repr(liveness)


# ---------------------------------------------------------------------------
# Capability routing: chain preview, tier knobs, warnings, registry coverage
# ---------------------------------------------------------------------------


@pytest.fixture
def capability_config_path(tmp_path):
    """A policy that exercises every new tier field with REAL registry models.

    T2 is the interesting one: its primary cannot see an image (glm-5.3 has
    vision False in the registry) and all three fallback hops can, so a vision
    task rejects the primary and leaves three eligible hops to order — enough
    permutations that a reshuffling preview would be obvious.
    """
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "classifier": {
                    # Top-level pair is the compatibility mirror of chain[0].
                    "model": "glm-4.7",
                    "provider": "zai",
                    "chain": [
                        {"model": "glm-4.7", "provider": "zai",
                         "billing_mode": "plan"},
                        {"model": "deepseek-v4-flash", "provider": "deepseek",
                         "billing_mode": "metered"},
                    ],
                    "temperature": 0,
                    "timeout_seconds": 15,
                },
                "fail_safe": {
                    "profile": "coder", "model": "glm-4.7", "provider": "zai",
                },
                "blocklist": {
                    "manual_ban": [],
                    "fallback_chain": [],
                    "auto_breaker": {"enabled": False},
                },
                "rules": [
                    {
                        "id": "vision-required",
                        "status": "stable",
                        "when": {"needs_vision": {"eq": True}},
                        "then": {"profile": "coder", "model": "T2"},
                    }
                ],
                "default": {"action": "classify"},
                "tiers": {
                    "T1": {"model": "glm-4.7", "provider": "zai",
                           "billing_mode": "plan"},
                    "T2": {
                        "model": "glm-5.3",
                        "provider": "zai",
                        "billing_mode": "plan",
                        "fallback": [
                            {"model": "mimo-v2.5", "provider": "xiaomi",
                             "billing_mode": "metered"},
                            {"model": "kimi-k3", "provider": "moonshot",
                             "billing_mode": "metered"},
                            {"model": "dots-studio/dots-3-note-preview:free",
                             "provider": "openrouter", "billing_mode": "free"},
                        ],
                        "fallback_strategy": "random",
                        "pin_primary": False,
                    },
                    "T3": {
                        "model": "gpt-5.6-terra", "provider": "openai-codex",
                        "requirements": {"min_context": 200000},
                    },
                    "T4": {"model": "gpt-5.5", "provider": "openai-codex"},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


_VISION_TASK = "Look at this screenshot of the ui and fix the layout"


def test_explain_surfaces_chain_plan_with_requirements_and_rejected(
    capability_config_path,
):
    """explain() reaches the chain plan: requirements, survivors, rejections."""
    service = RouterService(capability_config_path)

    result = service.explain(_VISION_TASK)

    plan = result["chain_plan"]
    # Lifted to the top level AND still inside the decision trace.
    assert plan == result["decision"]["chain_plan"]

    # Derived from the signal vector: a screenshot task needs vision.
    assert plan["requirements"]["vision"] is True
    assert plan["requirements"]["min_context"] > 0

    # The primary is dropped with a reason from the closed reason set, and the
    # rejection names the elo so the console can render "skipped, because".
    assert [(r["model"], r["reject_reason"]) for r in plan["rejected"]] == [
        ("glm-5.3", "no_vision")
    ]
    # Only vision-capable hops survive; the rejected primary is gone.
    assert {hop["model"] for hop in plan["chain"]} == {
        "mimo-v2.5", "kimi-k3", "dots-studio/dots-3-note-preview:free",
    }
    assert plan["strategy"] == "random"
    assert plan["bypassed"] is False
    assert plan["independent_rails"] == 3


def test_explain_preview_is_stable_across_calls(capability_config_path):
    """Two explains of the same task preview the SAME order, shuffle included.

    A console polls /explain. Under fallback_strategy: random an unseeded rng
    would reorder the chain on every poll, so the operator could not tell a
    policy change from shuffle noise.
    """
    service = RouterService(capability_config_path)

    first = service.explain(_VISION_TASK)
    second = service.explain(_VISION_TASK)

    assert first["chain_plan"]["strategy"] == "random"  # the churn-prone path
    assert [hop["model"] for hop in first["chain_plan"]["chain"]] == [
        hop["model"] for hop in second["chain_plan"]["chain"]
    ]
    assert first == second  # whole response, not just the chain


def test_explain_injects_a_deterministic_rng(capability_config_path, monkeypatch):
    """The stability above comes from an rng service OWNS, not from luck."""
    import router.service as service_mod

    seen = []
    real_explain = service_mod.rules_explain

    def spy(*args, **kwargs):
        seen.append(kwargs.get("rng"))
        return real_explain(*args, **kwargs)

    monkeypatch.setattr(service_mod, "rules_explain", spy)
    service = RouterService(capability_config_path)
    service.explain(_VISION_TASK)
    service.explain(_VISION_TASK)

    assert len(seen) == 2
    assert all(rng is not None for rng in seen)
    # Same seed => identical stream, which is what makes the preview stable.
    assert seen[0].random() == seen[1].random()


def test_explain_chain_plan_shape_survives_a_planless_rules(
    capability_config_path, monkeypatch
):
    """A rules.py that returns no chain_plan still yields the plan SHAPE.

    And the shape is the PLANNER's degraded shape, phase-2 keys included. The
    console branches on these keys and cannot see which module produced the plan
    it was handed, so a narrower shape here would make "no clock" indistinguishable
    from "unreported" — and the console's planWhen falls back to the BROWSER's hour
    on that ambiguity, pricing a plan that never saw a clock.
    """
    import router.rules as rules_mod
    import router.service as service_mod

    monkeypatch.setattr(
        service_mod, "rules_explain",
        lambda *_a, **_kw: {"matched_rule_id": None, "output": {"action": "classify"},
                            "matched_clauses": {}, "cause": "default_fallthrough"},
    )
    result = RouterService(capability_config_path).explain(_VISION_TASK)
    assert result["chain_plan"] == {
        "chain": [], "requirements": {}, "rejected": [], "unknown": [],
        "bypassed": False, "unsatisfiable": [],
        "strategy": "sequential", "strategy_declared": "sequential",
        "strategy_degraded": False, "strategy_degraded_reason": "",
        "pin_primary": True, "independent_rails": 0,
        "time_agnostic": True, "time_cap_bypassed": False,
        "capped": [], "demoted": [], "promoted": [], "peak_priced": [],
        "multipliers": {},
    }
    # time_agnostic is the key the console needs: it says "no clock", which is a
    # state planWhen renders, instead of leaving it to guess.
    assert result["chain_plan"]["time_agnostic"] is True
    # utc_hour/utc_weekday/time_cap stay ABSENT rather than null, for the reason
    # rules.plan_chain documents: a JSON consumer reads a null hour as midnight,
    # which is a specific wrong answer where absence is a correct one.
    for absent in ("utc_hour", "utc_weekday", "time_cap"):
        assert absent not in result["chain_plan"]
    # One shape, one definition: service's mirror must not drift from the
    # planner's own degraded plan, or two surfaces disagree about what an empty
    # plan looks like.
    assert service_mod._empty_chain_plan() == rules_mod._empty_chain_plan()
    # No plan was made, so no hour can be claimed for one: the clock is still
    # reported (it is what was asked about) but flagged as never having landed,
    # and nothing is described as hour-relative.
    assert result["evaluated_at"]["time_aware"] is False
    assert result["preview"]["time_relative"] is False
    assert result["preview"]["time_relative_reasons"] == []


def test_policy_exposes_the_new_tier_fields(capability_config_path):
    """All four knobs reach the console, and absent ones are NOT invented."""
    policy = RouterService(capability_config_path).policy()

    t2 = policy["tiers"]["T2"]
    assert t2["fallback_strategy"] == "random"
    assert t2["pin_primary"] is False
    assert t2["billing_mode"] == "plan"
    assert policy["tiers"]["T3"]["requirements"] == {"min_context": 200000}

    # T1 declares none of the ordering knobs. They must NOT be materialised
    # with their defaults: the console posts this policy back through
    # plan()/apply(), which would then write knobs nobody chose.
    assert "fallback_strategy" not in policy["tiers"]["T1"]
    assert "pin_primary" not in policy["tiers"]["T1"]

    # The tier mapping is copied whole, so the fallback hops keep their own
    # per-elo fields too.
    assert t2["fallback"][0] == {
        "model": "mimo-v2.5", "provider": "xiaomi", "billing_mode": "metered",
    }


def test_policy_tiers_are_a_copy_and_degrade_on_bad_yaml(capability_config_path):
    """Mutating the returned policy cannot corrupt anything; junk yields {}."""
    service = RouterService(capability_config_path)
    policy = service.policy()
    policy["tiers"]["T2"]["model"] = "mutated"
    assert service.policy()["tiers"]["T2"]["model"] == "glm-5.3"

    # A read path degrades instead of handing a scalar to the console.
    assert RouterService._policy_tiers("nope") == {}
    assert RouterService._policy_tiers(None) == {}
    assert RouterService._policy_tiers({"T1": "not-a-mapping"}) == {
        "T1": "not-a-mapping"
    }


@pytest.fixture
def warned_config_path(tmp_path):
    """A VALID policy that nonetheless has two advisory findings.

    T1's two hops sit behind one upstream (nous is a white-label reseller in
    front of openrouter), and T3's second hop is unknown to the registry.
    """
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "classifier": {"model": "glm-4.7", "provider": "zai"},
                "fail_safe": {"profile": "coder", "model": "glm-4.7",
                              "provider": "zai"},
                "blocklist": {"manual_ban": [], "fallback_chain": [],
                              "auto_breaker": {"enabled": False}},
                "rules": [],
                "default": {"action": "classify"},
                "tiers": {
                    "T1": {
                        "model": "meituan/longcat-2.0:free", "provider": "nous",
                        "fallback": [{"model": "openrouter/free",
                                      "provider": "openrouter"}],
                    },
                    "T2": {"model": "glm-5.3", "provider": "zai"},
                    "T3": {
                        # Unknown to the registry but DESCRIBED in yaml, so it
                        # is verifiable; its fallback hop describes nothing.
                        "model": "house-model", "provider": "local-rail",
                        "context_window": 500000, "vision": True,
                        "fallback": [{"model": "ghost-model",
                                      "provider": "other-rail"}],
                    },
                    "T4": {"model": "gpt-5.5", "provider": "openai-codex"},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_status_reports_warnings_without_flipping_valid(warned_config_path):
    """Warnings inform; errors block. A warning must never do an error's job."""
    service = RouterService(warned_config_path)
    status = service.status()

    assert status["valid"] is True
    assert status["validation_errors"] == []
    assert any("share upstream 'openrouter'" in w for w in status["warnings"])
    assert any("unknown to the capability registry" in w
               for w in status["warnings"])
    # Strictly separate channels: no warning text leaks into the blocking list.
    assert not set(status["warnings"]) & set(status["validation_errors"])

    # And the write gate is untouched by them: a warned config still applies.
    plan = service.plan({"default": {"action": "classify"}})
    assert plan["valid"] is True
    assert plan["errors"] == []
    assert service.apply(plan["base_hash"], plan["policy"])["ok"] is True

    # lint() stays the pure error gate — warnings are status()'s job alone.
    assert service.lint() == {"valid": True, "errors": []}


def test_status_warnings_degrade_when_rules_cannot_warn(config_path, monkeypatch):
    """No lint_warnings, or a raising one, yields [] — never a broken status."""
    import router.service as service_mod

    monkeypatch.setattr(service_mod, "rules_lint_warnings", None)
    assert RouterService(config_path).status()["warnings"] == []

    monkeypatch.setattr(
        service_mod, "rules_lint_warnings",
        lambda _c: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert RouterService(config_path).status()["warnings"] == []

    # A non-list return is ignored rather than passed through.
    monkeypatch.setattr(service_mod, "rules_lint_warnings", lambda _c: "nope")
    assert RouterService(config_path).status()["warnings"] == []


def test_hot_apply_round_trips_fallback_strategy_and_reordered_fallback(
    capability_config_path,
):
    """A tier carrying the new knobs survives _merge_hot/_deep_merge_value.

    No new _HOT_KEYS member was needed: these fields live inside `tiers`, which
    is already hot. The reordered `fallback` also proves lists still REPLACE
    wholesale — that is what makes reordering and deleting an elo expressible.
    """
    service = RouterService(capability_config_path)
    reordered = [
        {"model": "kimi-k3", "provider": "moonshot", "billing_mode": "metered"},
        {"model": "mimo-v2.5", "provider": "xiaomi", "billing_mode": "metered"},
    ]

    plan = service.plan({"tiers": {"T2": {
        "fallback_strategy": "sequential",
        "pin_primary": True,
        "requirements": {"min_context": 128000},
        "fallback": reordered,
    }}})
    assert plan["valid"] is True
    assert service.apply(plan["base_hash"], plan["policy"])["ok"] is True

    written = yaml.safe_load(capability_config_path.read_text(encoding="utf-8"))
    t2 = written["tiers"]["T2"]
    assert t2["fallback_strategy"] == "sequential"
    assert t2["pin_primary"] is True
    assert t2["requirements"] == {"min_context": 128000}
    # Reordered AND shortened: the list replaced, it did not union (3 -> 2).
    assert t2["fallback"] == reordered
    # Untouched sibling keys survive the deep merge.
    assert t2["model"] == "glm-5.3"
    assert t2["billing_mode"] == "plan"
    # Sibling tiers are untouched.
    assert written["tiers"]["T3"]["requirements"] == {"min_context": 200000}

    # The edit is live on the next read, with no restart.
    assert service.policy()["tiers"]["T2"]["fallback_strategy"] == "sequential"
    assert service.explain(_VISION_TASK)["chain_plan"]["strategy"] == "sequential"


def test_apply_rejects_an_invalid_fallback_strategy(capability_config_path):
    """The write gate still fails closed on the new fields."""
    service = RouterService(capability_config_path)
    before = capability_config_path.read_bytes()

    plan = service.plan({"tiers": {"T2": {"fallback_strategy": "round-robin"}}})
    assert plan["valid"] is False
    assert any("fallback_strategy" in e for e in plan["errors"])

    result = service.apply(plan["base_hash"], plan["policy"])
    assert result["ok"] is False
    assert capability_config_path.read_bytes() == before


def test_liveness_covers_every_classifier_chain_hop_without_duplicates(
    capability_config_path,
):
    """Both classifier hops are watched, and the mirror is not double-counted."""
    liveness = RouterService(capability_config_path).liveness()
    keys = [entry["model_key"] for entry in liveness["models"]]

    # chain[1] is only reachable by walking the chain.
    assert "deepseek-v4-flash@deepseek" in keys
    # chain[0] and the top-level compatibility mirror are the same elo.
    assert keys.count("glm-4.7@zai") == 1
    assert len(keys) == len(set(keys))


def test_liveness_flags_a_registry_unknown_elo(warned_config_path):
    """capabilities_known is an extra FIELD, not a fifth state."""
    liveness = RouterService(warned_config_path).liveness()
    known = {e["model_key"]: e["capabilities_known"] for e in liveness["models"]}

    # Declares nothing and the registry has never heard of it -> flagged.
    assert known["ghost-model@other-rail"] is False
    # Also unknown to the registry, but described in yaml: `declared` wins over
    # the registry, so it is verifiable and must NOT be flagged.
    assert known["house-model@local-rail"] is True
    assert known["glm-5.3@zai"] is True

    # The state set stays closed, and coverage does not degrade health.
    assert {e["state"] for e in liveness["models"]} == {"alive"}
    assert liveness["worst"] == "alive"


def test_liveness_capability_coverage_fails_open_without_a_registry(monkeypatch):
    """No registry (or a raising one) means nothing is PROVABLY unknown."""
    import router.service as service_mod

    monkeypatch.setattr(service_mod, "_caps", None)
    assert RouterService._capabilities_known("who-knows") is True

    class Exploding:
        @staticmethod
        def capabilities_for(*_a, **_kw):
            raise TypeError("stale registry")

    monkeypatch.setattr(service_mod, "_caps", Exploding)
    assert RouterService._capabilities_known("who-knows") is True


def test_declared_capability_index_ignores_identity_and_junk_keys():
    """Only capability-ish keys count as a declaration.

    `provider` is identity and IS a registry field, so counting it would make
    every elo in policy look known; tuning keys are dropped by the registry.
    """
    index = RouterService._declared_capability_index({
        "classifier": {"model": "c", "provider": "rail", "temperature": 0,
                       "chain": [{"model": "c2", "provider": "rail",
                                  "context_window": 1000}]},
        "fail_safe": {"model": "fs", "provider": "rail",
                      "fallback": [{"model": "fsfb", "provider": "rail"}]},
        "tiers": {
            "T1": {"model": "t1", "provider": "rail", "fallback_strategy": "random",
                   "pin_primary": True, "requirements": {"min_context": 1}},
            "T2": "not-a-mapping",
            "T3": {"model": "t3", "provider": "rail", "vision": True,
                   "fallback": "not-a-list"},
            "T4": {"provider": "rail"},  # no model -> nothing to index
        },
    })
    # Identity + routing keys only -> no declaration recorded at all.
    assert "t1" not in index
    assert "fsfb" not in index
    # Capability keys are recorded; a stray tuning key is recorded but the
    # registry filters it, so it cannot fake knowledge.
    assert index["c2"] == {"context_window": 1000}
    assert index["t3"] == {"vision": True}
    assert RouterService._capabilities_known("c", index.get("c")) is False
    assert RouterService._capabilities_known("t3", index.get("t3")) is True


def test_config_without_any_new_keys_returns_the_prior_responses(config_path):
    """The pre-capability policy still produces exactly the old answers."""
    service = RouterService(config_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    # policy(): tiers pass through unchanged, with no defaults invented.
    assert service.policy()["tiers"] == raw["tiers"]

    # status(): every prior field keeps its prior value; only `warnings` is new.
    status = service.status()
    assert status["valid"] is True
    assert status["validation_errors"] == []
    assert status["classifier"] == {"model": "judge", "provider": "judge-rail"}

    # explain(): the prior decision, plus an additive chain_plan describing the
    # declared chain in declared order (no strategy, nothing rejected).
    result = service.explain("Debug a race condition")
    assert result["mode"] == "deterministic_dry_run"
    assert result["requires_classifier"] is False
    assert result["decision"]["cause"] == "hard_rule"
    plan = result["chain_plan"]
    assert [hop["model"] for hop in plan["chain"]] == ["strong", "backup"]
    assert plan["strategy"] == "sequential"
    assert plan["rejected"] == []
    assert plan["bypassed"] is False
    # These elos are unknown to the registry, which never rejects — it reports.
    assert set(plan["unknown"]) == {"strong", "backup"}

    # liveness(): the same states as before, plus the coverage field.
    liveness = service.liveness()
    assert {e["state"] for e in liveness["models"]} == {"alive"}
    assert all(e["capabilities_known"] is False for e in liveness["models"])


@pytest.mark.parametrize(
    "accepts_rng, accepts_when, expected_kwargs",
    [
        (False, False, set()),          # a rules.py predating both injections
        (True, False, {"rng"}),         # fallback strategies but no time layer
        (False, True, {"when"}),        # time layer but no rng (not shipped, still safe)
    ],
)
def test_explain_omits_injected_parameters_an_older_rules_lacks(
    capability_config_path, monkeypatch, accepts_rng, accepts_when, expected_kwargs
):
    """Each injected parameter is passed only if rules.explain declares it.

    service.py is deployed by file copy, so it can land beside a rules.py that
    predates fallback strategies, the time layer, or both. The two flags are
    INDEPENDENT: losing one injection must not cost the other, and neither may
    become a TypeError inside a read path. Such a rules.py orders sequentially and
    plans time-agnostically, so what matters is that explain() still answers — and
    says the plan was not time-aware instead of claiming an hour it never used.
    """
    import router.service as service_mod

    seen = []
    real_explain = service_mod.rules_explain

    def older_explain(*args, **kwargs):
        seen.append(set(kwargs))
        return real_explain(*args, **kwargs)

    monkeypatch.setattr(service_mod, "_EXPLAIN_ACCEPTS_RNG", accepts_rng)
    monkeypatch.setattr(service_mod, "_EXPLAIN_ACCEPTS_WHEN", accepts_when)
    monkeypatch.setattr(service_mod, "rules_explain", older_explain)

    result = RouterService(capability_config_path).explain(_VISION_TASK)

    assert seen == [expected_kwargs]
    assert result["mode"] == "deterministic_dry_run"
    assert isinstance(result["chain_plan"], dict)
    # The clock is reported either way; `time_aware` says whether it landed.
    assert result["evaluated_at"]["utc_hour"] in range(24)
    assert result["evaluated_at"]["time_aware"] is accepts_when


# ---------------------------------------------------------------------------
# Time-windowed pricing: the injected clock on the read paths
# ---------------------------------------------------------------------------

# Fixed clocks, all Monday 2026-08-17 unless the weekday is the point.
# deepseek is 2.0x at 01:00-04:00 and 06:00-10:00 UTC every day; zai's plan
# models are 2.0x at 06:00-10:00 on WEEKDAYS; glm-4.6 and MiniMax-M3 are flat.
_PEAK_0200 = datetime(2026, 8, 17, 2, tzinfo=timezone.utc)      # deepseek peak
_PEAK_0300 = datetime(2026, 8, 17, 3, tzinfo=timezone.utc)      # last peak hour
_OFFPEAK_0400 = datetime(2026, 8, 17, 4, tzinfo=timezone.utc)   # [1, 4) is half-open
_PEAK_0700 = datetime(2026, 8, 17, 7, tzinfo=timezone.utc)      # both rails peak
_OFFPEAK_1500 = datetime(2026, 8, 17, 15, tzinfo=timezone.utc)

_TRIVIAL_TASK = "Rename a variable"          # verb_class trivial -> T1
_HARD_TASK = "Refactor the parser module"    # verb_class hard    -> T4
_PLAIN_TASK = "Summarize this note"          # verb_class unknown -> T3 at peak, else T2


@pytest.fixture
def time_config_path(tmp_path):
    """A policy whose plan legitimately depends on the hour, with REAL prices.

    One tier per time-layer stage, so a failure names the stage:

    * T1 — ``time_cap`` over two deepseek elos, so the cap EMPTIES the chain in a
      peak window and has to bypass itself (a cost control must not cause an
      outage) while keeping its per-elo diagnostics.
    * T2 — ``cheapest_now`` over one windowed elo and two flat ones. Off-peak
      output prices are MiniMax-M3 1.2 < deepseek-v4-pro 1.98 < glm-4.6 2.2; in a
      deepseek peak the primary doubles to 3.96 and lands last, so the ORDER is
      hour-relative without anything being dropped.
    * T3 — ``time_cap`` 1.5 over a windowed and a flat elo, so the peak makes
      deepseek ineligible and glm-4.6 still routes.
    * T4 — ``avoid_peak`` demotes deepseek while it is peak-priced and never
      removes it.

    The ``peak-hours`` rule is keyed on the INJECTED ``utc_hour`` feature, which is
    the 4am-cron question: at 06:00-10:00 UTC the plain task routes to the capped
    tier, and at any other hour it falls through to the cheapest_now default.
    """
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "classifier": {"model": "glm-4.7", "provider": "zai"},
                "fail_safe": {"profile": "coder", "model": "glm-4.6",
                              "provider": "zai"},
                "blocklist": {"manual_ban": [], "fallback_chain": [],
                              "auto_breaker": {"enabled": False}},
                "rules": [
                    {"id": "trivial-verbs", "status": "stable",
                     "when": {"verb_class": {"eq": "trivial"}},
                     "then": {"profile": "coder", "model": "T1"}},
                    {"id": "hard-verbs", "status": "stable",
                     "when": {"verb_class": {"eq": "hard"}},
                     "then": {"profile": "coder", "model": "T4"}},
                    {"id": "peak-hours", "status": "stable",
                     "when": {"utc_hour": {"gte": 6, "lt": 10}},
                     "then": {"profile": "coder", "model": "T3"}},
                ],
                "default": {"profile": "coder", "model": "T2"},
                "tiers": {
                    "T1": {
                        "model": "deepseek-v4-flash", "provider": "deepseek",
                        "billing_mode": "metered",
                        "fallback": [{"model": "deepseek-v4-pro",
                                      "provider": "deepseek",
                                      "billing_mode": "metered"}],
                        "time_cap": {"max_multiplier": 1.5},
                    },
                    "T2": {
                        "model": "deepseek-v4-pro", "provider": "deepseek",
                        "billing_mode": "metered",
                        "fallback": [
                            {"model": "glm-4.6", "provider": "zai",
                             "billing_mode": "metered"},
                            {"model": "MiniMax-M3", "provider": "minimax",
                             "billing_mode": "metered"},
                        ],
                        "fallback_strategy": "cheapest_now",
                        "pin_primary": False,
                    },
                    "T3": {
                        "model": "deepseek-v4-pro", "provider": "deepseek",
                        "billing_mode": "metered",
                        "fallback": [{"model": "glm-4.6", "provider": "zai",
                                      "billing_mode": "metered"}],
                        "time_cap": {"max_multiplier": 1.5},
                    },
                    "T4": {
                        "model": "deepseek-v4-pro", "provider": "deepseek",
                        "billing_mode": "metered",
                        "fallback": [{"model": "glm-4.6", "provider": "zai",
                                      "billing_mode": "metered"}],
                        "time_policy": {"avoid_peak": ["deepseek"]},
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _chain(result):
    return [hop["model"] for hop in result["chain_plan"]["chain"]]


def test_explain_reports_the_evaluation_time_it_defaulted_to(
    time_config_path, monkeypatch
):
    """With no explicit time, explain evaluates at the real current UTC hour.

    A console rendering multipliers and a cheapest_now order has to know which
    hour they belong to; an unlabelled time-relative answer is indistinguishable
    from a wrong one.
    """
    import router.service as service_mod

    monkeypatch.setattr(
        service_mod, "_utc_now",
        lambda: datetime(2026, 8, 17, 15, 42, 17, 123456, tzinfo=timezone.utc),
    )

    result = RouterService(time_config_path).explain(_PLAIN_TASK)

    assert result["evaluated_at"] == {
        # Truncated to the top of the hour: price windows are declared in whole
        # UTC hours, so minutes could only churn the payload, never the answer.
        "at": "2026-08-17T15:00:00+00:00",
        "at_source": "now",
        "utc_hour": 15,
        "utc_weekday": 0,
        "time_aware": True,
    }
    # The hour explain reports and the hour the PLAN was made at are one reading.
    assert result["chain_plan"]["utc_hour"] == 15
    assert result["chain_plan"]["utc_weekday"] == 0
    assert result["chain_plan"]["time_agnostic"] is False


def test_explain_at_an_explicit_hour_changes_the_plan(time_config_path):
    """The 4am-cron question: ask for 07:00 UTC and get 07:00 UTC's plan.

    At 07:00 the utc_hour rule fires (the injected clock feature reaches match())
    and the capped tier drops the peak-priced primary; at 15:00 the same task
    falls through to the cheapest_now default with nothing capped.
    """
    service = RouterService(time_config_path)

    peak = service.explain(_PLAIN_TASK, _PEAK_0700)
    offpeak = service.explain(_PLAIN_TASK, _OFFPEAK_1500)

    assert peak["evaluated_at"]["at_source"] == "explicit"
    assert peak["evaluated_at"]["utc_hour"] == 7
    assert peak["decision"]["matched_rule_id"] == "peak-hours"
    # The rule matched ON the injected feature, so the trace can show it.
    assert peak["decision"]["matched_clauses"] == {"utc_hour": {"gte": 6, "lt": 10}}
    assert _chain(peak) == ["glm-4.6"]
    assert [(c["model"], c["multiplier"]) for c in peak["chain_plan"]["capped"]] == [
        ("deepseek-v4-pro", 2.0)
    ]
    assert peak["chain_plan"]["time_cap"] == {"max_multiplier": 1.5}
    assert peak["chain_plan"]["multipliers"] == {
        "deepseek-v4-pro": 2.0, "glm-4.6": 1.0,
    }

    assert offpeak["decision"]["matched_rule_id"] is None
    assert offpeak["chain_plan"]["capped"] == []
    assert _chain(offpeak) == ["MiniMax-M3", "deepseek-v4-pro", "glm-4.6"]
    # Same task, same policy, different hour -> a different plan, on purpose.
    assert _chain(peak) != _chain(offpeak)


def test_explain_cheapest_now_order_is_hour_relative_and_labelled(time_config_path):
    """The order flips across a window boundary, and the response says why.

    01:00-04:00 UTC is half-open, so 03:00 is inside deepseek's peak and 04:00 is
    outside it. Both hours fall through to the cheapest_now tier, so the only
    thing that changed is the price — which is exactly what an operator must be
    able to conclude instead of suspecting a shuffle.
    """
    service = RouterService(time_config_path)

    inside = service.explain(_PLAIN_TASK, _PEAK_0300)
    outside = service.explain(_PLAIN_TASK, _OFFPEAK_0400)

    assert inside["chain_plan"]["strategy"] == "cheapest_now"
    # deepseek-v4-pro output 1.98 -> 3.96 at 2.0x, so it sinks behind glm-4.6 2.2.
    assert _chain(inside) == ["MiniMax-M3", "glm-4.6", "deepseek-v4-pro"]
    assert _chain(outside) == ["MiniMax-M3", "deepseek-v4-pro", "glm-4.6"]

    assert inside["preview"] == {
        "seed": 0,
        "reproducible_within": "utc_hour",
        "time_relative": True,
        "time_relative_reasons": ["cheapest_now", "price_window"],
        # No prompt_text was passed, so the preview measured the goal line and
        # says so — the alternative is a response that looks identical whether it
        # reproduced the real turn or six tokens of goal.
        "sized_from": "task",
        "prompt_chars": len(_PLAIN_TASK),
    }
    # Outside every window the ORDER is still price-derived, but no elo is priced
    # off its base rate, so `price_window` drops out.
    assert outside["preview"]["time_relative_reasons"] == ["cheapest_now"]


def test_explain_is_identical_within_an_hour_and_free_to_differ_across_one(
    time_config_path, monkeypatch
):
    """Two polls seconds apart are byte-identical; the next hour may differ.

    Those two properties are the whole contract: a polled preview that churned
    every second would be unreadable, and a preview frozen against the clock
    would show an order production is not using.
    """
    import router.service as service_mod

    service = RouterService(time_config_path)
    clock = {"now": datetime(2026, 8, 17, 3, 0, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(service_mod, "_utc_now", lambda: clock["now"])

    first = service.explain(_PLAIN_TASK)
    clock["now"] += timedelta(seconds=59, microseconds=999999)
    second = service.explain(_PLAIN_TASK)

    assert first == second
    assert json.dumps(first, default=str) == json.dumps(second, default=str)

    # One hour later the same task legitimately orders differently, and the
    # response already carries the reason: the hour moved out of deepseek's peak.
    clock["now"] = datetime(2026, 8, 17, 4, 0, 0, tzinfo=timezone.utc)
    next_hour = service.explain(_PLAIN_TASK)
    assert _chain(next_hour) != _chain(first)
    assert first["preview"]["time_relative"] is True
    assert first["evaluated_at"]["utc_hour"] == 3
    assert next_hour["evaluated_at"]["utc_hour"] == 4


def test_explain_accepts_an_iso_string_and_refuses_anything_else(time_config_path):
    """A query parameter arrives as text, so ISO-8601 is accepted here.

    Every caller parsing the string itself is how two surfaces end up disagreeing
    about what "07:00" meant. An unusable value fails CLOSED (ValueError, which the
    sidecar renders as a 400) rather than silently answering about "now" — an audit
    surface that answers a different question is worse than one that refuses.
    """
    service = RouterService(time_config_path)

    zulu = service.explain(_PLAIN_TASK, "2026-08-17T07:30:00Z")
    offset = service.explain(_PLAIN_TASK, "2026-08-17T04:30:00-03:00")
    naive = service.explain(_PLAIN_TASK, "2026-08-17T07:30:00")

    # All three name the same instant; a naive string is read as UTC.
    assert zulu["evaluated_at"]["at"] == "2026-08-17T07:00:00+00:00"
    assert offset["evaluated_at"] == zulu["evaluated_at"]
    assert naive["evaluated_at"] == zulu["evaluated_at"]
    assert zulu["decision"]["matched_rule_id"] == "peak-hours"

    # A naive datetime is read as UTC too, and an aware one is converted.
    assert service.explain(
        _PLAIN_TASK, datetime(2026, 8, 17, 7, 30)
    )["evaluated_at"] == zulu["evaluated_at"]

    with pytest.raises(ValueError, match="ISO-8601"):
        service.explain(_PLAIN_TASK, "half past seven")
    with pytest.raises(ValueError, match="ISO-8601"):
        service.explain(_PLAIN_TASK, "")
    with pytest.raises(ValueError, match="must be a datetime"):
        service.explain(_PLAIN_TASK, 7)
    with pytest.raises(ValueError, match="must be a datetime"):
        service.explain(_PLAIN_TASK, ["2026-08-17T07:00:00Z"])


def test_explain_shows_a_bypassing_time_cap_with_its_diagnostics(time_config_path):
    """A cap that would empty the chain gives way, and says which elos it hit.

    T1's only elos are two deepseek models, both 2.0x in the peak window, so the
    1.5x cap cannot be satisfied. The chain must survive intact — a cost control
    must never cause an outage — and the per-elo multipliers must survive with it,
    because the flag alone cannot tell an operator whether the cap or the tier is
    wrong.
    """
    service = RouterService(time_config_path)

    peak = service.explain(_TRIVIAL_TASK, _PEAK_0200)
    plan = peak["chain_plan"]

    assert plan["time_cap_bypassed"] is True
    assert _chain(peak) == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert [(c["model"], c["multiplier"]) for c in plan["capped"]] == [
        ("deepseek-v4-flash", 2.0), ("deepseek-v4-pro", 2.0),
    ]
    assert peak["preview"]["time_relative_reasons"] == ["time_cap", "price_window"]

    # Off-peak the same tier needs no bypass at all.
    calm = service.explain(_TRIVIAL_TASK, _OFFPEAK_1500)
    assert calm["chain_plan"]["time_cap_bypassed"] is False
    assert calm["chain_plan"]["capped"] == []
    assert _chain(calm) == ["deepseek-v4-flash", "deepseek-v4-pro"]


def test_explain_shows_time_policy_demotion_without_dropping_an_elo(time_config_path):
    """avoid_peak moves a peak-priced rail to the back; it never removes it."""
    service = RouterService(time_config_path)

    peak = service.explain(_HARD_TASK, _PEAK_0700)
    offpeak = service.explain(_HARD_TASK, _OFFPEAK_1500)

    assert peak["decision"]["output"]["time_policy"] == {"avoid_peak": ["deepseek"]}
    assert peak["chain_plan"]["demoted"] == ["deepseek-v4-pro"]
    assert _chain(peak) == ["glm-4.6", "deepseek-v4-pro"]
    # A permutation, not a filter: the demoted rail is still attemptable.
    assert sorted(_chain(peak)) == sorted(_chain(offpeak))
    assert _chain(offpeak) == ["deepseek-v4-pro", "glm-4.6"]
    assert offpeak["chain_plan"]["demoted"] == []
    assert peak["preview"]["time_relative_reasons"] == ["time_policy", "price_window"]
    # The declared knob is what makes it hour-relative, so the off-peak plan —
    # where the policy moved nothing — is still labelled time-relative.
    assert offpeak["preview"]["time_relative"] is True
    assert "time_policy" in offpeak["preview"]["time_relative_reasons"]


def test_explain_weekend_is_off_peak_for_the_plan_rails(tmp_path):
    """zai's peak is weekday-only, so Saturday 07:00 prices at the base rate.

    Asserted through the service because the clock the console injects is the one
    that has to carry the weekday: an hour with no date could not answer this.
    """
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump({
            "enabled": True,
            "classifier": {"model": "glm-4.7", "provider": "zai"},
            "fail_safe": {"profile": "coder", "model": "glm-4.6", "provider": "zai"},
            "blocklist": {"manual_ban": [], "fallback_chain": [],
                          "auto_breaker": {"enabled": False}},
            "rules": [],
            "default": {"profile": "coder", "model": "T1"},
            "tiers": {
                "T1": {"model": "glm-4.7", "provider": "zai", "billing_mode": "plan",
                       "fallback": [{"model": "glm-4.6", "provider": "zai",
                                     "billing_mode": "metered"}]},
                "T2": {"model": "glm-4.6", "provider": "zai"},
                "T3": {"model": "glm-4.6", "provider": "zai"},
                "T4": {"model": "glm-4.6", "provider": "zai"},
            },
        }, sort_keys=False),
        encoding="utf-8",
    )
    service = RouterService(path)

    monday = service.explain(_PLAIN_TASK, datetime(2026, 8, 17, 7, tzinfo=timezone.utc))
    saturday = service.explain(_PLAIN_TASK, datetime(2026, 8, 22, 7, tzinfo=timezone.utc))

    assert monday["chain_plan"]["multipliers"]["glm-4.7"] == 2.0
    assert saturday["chain_plan"]["multipliers"]["glm-4.7"] == 1.0
    assert saturday["evaluated_at"]["utc_weekday"] == 5
    assert saturday["preview"]["time_relative"] is False


def test_policy_exposes_time_policy_and_time_cap(time_config_path):
    """Both time knobs reach the console verbatim, nested values included."""
    service = RouterService(time_config_path)
    policy = service.policy()

    assert policy["tiers"]["T3"]["time_cap"] == {"max_multiplier": 1.5}
    assert policy["tiers"]["T4"]["time_policy"] == {"avoid_peak": ["deepseek"]}
    assert policy["tiers"]["T2"]["fallback_strategy"] == "cheapest_now"
    assert policy["tiers"]["T2"]["pin_primary"] is False
    # Absent knobs are still not invented: T1 declares no time_policy.
    assert "time_policy" not in policy["tiers"]["T1"]

    # The copy is deep, so a console normalising the nested knobs in place cannot
    # edit the policy it is only displaying.
    policy["tiers"]["T4"]["time_policy"]["avoid_peak"].append("zai")
    policy["tiers"]["T3"]["time_cap"]["max_multiplier"] = 99
    fresh = service.policy()
    assert fresh["tiers"]["T4"]["time_policy"] == {"avoid_peak": ["deepseek"]}
    assert fresh["tiers"]["T3"]["time_cap"] == {"max_multiplier": 1.5}


def test_status_carries_time_warnings_without_flipping_valid(time_config_path):
    """A cap that will bypass is advisory: reported, never write-blocking."""
    service = RouterService(time_config_path)
    status = service.status()

    assert status["valid"] is True
    assert status["validation_errors"] == []
    assert any("time_cap will bypass" in w for w in status["warnings"])
    assert any("share upstream 'deepseek'" in w for w in status["warnings"])
    assert not set(status["warnings"]) & set(status["validation_errors"])

    # The write gate is untouched by them: the warned config still applies.
    plan = service.plan({"default": {"profile": "coder", "model": "T2"}})
    assert plan["valid"] is True
    assert service.apply(plan["base_hash"], plan["policy"])["ok"] is True
    # lint() stays the pure error gate.
    assert service.lint() == {"valid": True, "errors": []}


def test_status_passes_registry_diagnostics_through_verbatim(
    config_path, monkeypatch
):
    """Whatever lint_warnings reports reaches the operator unfiltered.

    rules.lint_warnings folds capabilities.registry_diagnostics() in — a check
    that had no caller at all before phase 2 — and status() is the only surface an
    operator reads warnings from. A projection here would make a registry defect
    (an overlapping price window, say) unobservable, which is the same as not
    having written the check.
    """
    import router.service as service_mod

    diagnostics = [
        "model 'glm-5.3': price_windows entries overlap",
        "model 'mimo-v2.5': price_windows[0].hours_utc must be [start, end)",
    ]
    monkeypatch.setattr(service_mod, "rules_lint_warnings", lambda _c: diagnostics)

    status = RouterService(config_path).status()

    assert status["warnings"] == diagnostics
    assert status["valid"] is True
    assert status["validation_errors"] == []


def test_liveness_reports_price_window_state_as_extra_fields(
    time_config_path, monkeypatch
):
    """Window state rides as FIELDS; the four-value state enum is untouched."""
    import router.service as service_mod

    monkeypatch.setattr(
        service_mod, "_utc_now",
        lambda: datetime(2026, 8, 17, 7, 12, tzinfo=timezone.utc),
    )

    liveness = RouterService(time_config_path).liveness()
    by_key = {entry["model_key"]: entry for entry in liveness["models"]}

    # Both primary rails are peak-priced at 07:00 UTC on a weekday.
    deepseek = by_key["deepseek-v4-pro@deepseek"]
    assert deepseek["in_expensive_window"] is True
    assert deepseek["price_multiplier"] == 2.0
    # The registry's own MAPPING, carried through whole rather than reduced to its
    # hour: a weekday-gated window makes a bare hour ambiguous by up to two days,
    # so a console rendering "peak ends in 2h" needs hours_ahead, not hour.
    # The peak ends at 10:00 UTC, which is `hour` here.
    assert deepseek["next_window_change"] == {
        "hour": 10, "weekday": 0, "hours_ahead": 3, "multiplier": 1.0,
    }
    assert by_key["glm-4.7@zai"]["price_multiplier"] == 2.0

    # A flat-priced elo is never "in a window", and nothing ever changes for it.
    flat = by_key["glm-4.6@zai"]
    assert flat["in_expensive_window"] is False
    assert flat["price_multiplier"] == 1.0
    assert flat["next_window_change"] is None

    # The state enum stays a closed set of four, and a peak price is not a health
    # problem: an expensive rail is still perfectly alive.
    assert {entry["state"] for entry in liveness["models"]} <= {
        "alive", "degraded", "quota_exhausted", "dead",
    }
    assert liveness["worst"] == "alive"
    assert liveness["evaluated_at"] == {
        "at": "2026-08-17T07:00:00+00:00",
        "at_source": "now",
        "utc_hour": 7,
        "utc_weekday": 0,
    }


def test_liveness_price_window_state_degrades_per_elo(monkeypatch):
    """No registry, a raising registry, or a nonsense multiplier -> the neutral.

    The neutral is the registry's OWN answer for "no window matched or nothing is
    known", and `capabilities_known` beside it is what distinguishes the two.
    Degrading per elo means one unpriceable model cannot blank every other rail.
    """
    import router.service as service_mod

    neutral = {
        "in_expensive_window": False,
        "price_multiplier": 1.0,
        "next_window_change": None,
    }
    when = _PEAK_0700

    monkeypatch.setattr(service_mod, "_caps", None)
    assert RouterService._time_state("deepseek-v4-pro", None, when) == neutral

    class Exploding:
        @staticmethod
        def price_multiplier(*_a, **_kw):
            raise TypeError("stale registry")

    monkeypatch.setattr(service_mod, "_caps", Exploding)
    assert RouterService._time_state("deepseek-v4-pro", None, when) == neutral

    class Nonsense:
        @staticmethod
        def price_multiplier(*_a, **_kw):
            return "2x"          # not a number -> nothing usable to report

        @staticmethod
        def in_expensive_window(*_a, **_kw):
            return True

        @staticmethod
        def next_window_change(*_a, **_kw):
            return True          # a bool is not an hour

    monkeypatch.setattr(service_mod, "_caps", Nonsense)
    assert RouterService._time_state("deepseek-v4-pro", None, when) == neutral


def test_liveness_honours_a_declared_price_window_override(tmp_path, monkeypatch):
    """A per-elo price_windows override in YAML wins over the registry.

    `declared` beats the registry everywhere else, so an operator who corrected a
    stale window in the file must see the corrected window here too — otherwise
    the console reports a peak the router is not charging for.
    """
    import router.service as service_mod

    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump({
            "enabled": True,
            "classifier": {"model": "glm-4.6", "provider": "zai"},
            "fail_safe": {"profile": "coder", "model": "glm-4.6", "provider": "zai"},
            "blocklist": {"manual_ban": [], "fallback_chain": [],
                          "auto_breaker": {"enabled": False}},
            "rules": [],
            "default": {"profile": "coder", "model": "T1"},
            "tiers": {
                # The registry says deepseek is 2.0x at 07:00; the file says the
                # peak moved to 20:00-22:00, and the file wins.
                "T1": {"model": "deepseek-v4-pro", "provider": "deepseek",
                       "billing_mode": "metered",
                       "price_windows": [{"hours_utc": [20, 22],
                                          "multiplier": 3.0}]},
                "T2": {"model": "glm-4.6", "provider": "zai"},
                "T3": {"model": "glm-4.6", "provider": "zai"},
                "T4": {"model": "glm-4.6", "provider": "zai"},
            },
        }, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        service_mod, "_utc_now",
        lambda: datetime(2026, 8, 17, 21, tzinfo=timezone.utc),
    )

    liveness = RouterService(path).liveness()
    entry = {e["model_key"]: e for e in liveness["models"]}["deepseek-v4-pro@deepseek"]

    # The registry's peak (06:00-10:00) is over by now, so 3.0x can only come from
    # the declared window — and the declared window is what the change instant is
    # read off too: the override ends at 22:00 UTC, one hour ahead, back to base.
    assert entry["price_multiplier"] == 3.0
    assert entry["in_expensive_window"] is True
    assert entry["next_window_change"] == {
        "hour": 22, "weekday": 0, "hours_ahead": 1, "multiplier": 1.0,
    }
    # The four-value enum is untouched by an override, same as by a registry window.
    assert {e["state"] for e in liveness["models"]} <= {
        "alive", "degraded", "quota_exhausted", "dead",
    }


def test_hot_apply_round_trips_time_cap_and_a_reordered_fallback(time_config_path):
    """A tier carrying `time_cap` survives the write path with no new hot key.

    `_HOT_KEYS` still allows exactly `tiers`, because both time knobs live INSIDE a
    tier; `_deep_merge_value` recurses into the tier mapping, so sibling keys
    survive, while the `fallback` LIST replaces wholesale — which is what makes
    reordering hops and DELETING an elo expressible at all.
    """
    service = RouterService(time_config_path)
    # Step 1: add the cap and the policy, and REORDER the two declared hops (plus
    # one more, so step 2 has something to delete).
    reordered = [
        {"model": "MiniMax-M3", "provider": "minimax", "billing_mode": "metered"},
        {"model": "kimi-k3", "provider": "moonshot", "billing_mode": "metered"},
        {"model": "glm-4.6", "provider": "zai", "billing_mode": "metered"},
    ]

    plan = service.plan({"tiers": {
        "T2": {
            "time_cap": {"max_multiplier": 1.5},
            "time_policy": {"avoid_peak": ["deepseek"], "prefer": ["MiniMax-M3"]},
            "fallback": reordered,
        },
    }})
    assert plan["valid"] is True
    assert service.apply(plan["base_hash"], plan["policy"])["ok"] is True

    written = yaml.safe_load(time_config_path.read_text(encoding="utf-8"))
    t2 = written["tiers"]["T2"]
    assert t2["time_cap"] == {"max_multiplier": 1.5}
    assert t2["time_policy"] == {"avoid_peak": ["deepseek"], "prefer": ["MiniMax-M3"]}
    assert t2["fallback"] == reordered          # replaced in the order sent
    # Untouched sibling keys survive the deep merge.
    assert t2["model"] == "deepseek-v4-pro"
    assert t2["fallback_strategy"] == "cheapest_now"
    assert t2["pin_primary"] is False
    # Sibling tiers are untouched.
    assert written["tiers"]["T4"]["time_policy"] == {"avoid_peak": ["deepseek"]}

    # Step 2: DELETE an elo by sending the shorter list. A union/index merge would
    # make deletion inexpressible, so this is the guarantee, not a side effect.
    shortened = [hop for hop in reordered if hop["model"] != "kimi-k3"]
    plan2 = service.plan({"tiers": {"T2": {"fallback": shortened}}})
    assert service.apply(plan2["base_hash"], plan2["policy"])["ok"] is True
    t2_after = yaml.safe_load(time_config_path.read_text(encoding="utf-8"))["tiers"]["T2"]
    assert t2_after["fallback"] == shortened            # 3 -> 2, kimi-k3 is gone
    assert t2_after["time_cap"] == {"max_multiplier": 1.5}   # cap survived step 2

    # Live on the next read, no restart: the cap now fires for this tier and the
    # surviving hops are the ones the operator left behind.
    assert service.policy()["tiers"]["T2"]["time_cap"] == {"max_multiplier": 1.5}
    peak = service.explain(_PLAIN_TASK, _PEAK_0200)
    assert peak["chain_plan"]["time_cap"] == {"max_multiplier": 1.5}
    assert [c["model"] for c in peak["chain_plan"]["capped"]] == ["deepseek-v4-pro"]
    assert _chain(peak) == ["MiniMax-M3", "glm-4.6"]


def test_apply_rejects_an_invalid_time_cap_and_time_policy(time_config_path):
    """The write gate still fails closed on the time knobs."""
    service = RouterService(time_config_path)
    before = time_config_path.read_bytes()

    # A cap below the base rate could only ever empty the chain and bypass itself.
    low = service.plan({"tiers": {"T3": {"time_cap": {"max_multiplier": 0.5}}}})
    assert low["valid"] is False
    assert any("time_cap.max_multiplier" in e for e in low["errors"])

    bad_policy = service.plan({"tiers": {"T4": {"time_policy": {"avoid_peak": "zai"}}}})
    assert bad_policy["valid"] is False
    assert any("time_policy.avoid_peak" in e for e in bad_policy["errors"])

    assert service.apply(low["base_hash"], low["policy"])["ok"] is False
    assert time_config_path.read_bytes() == before


def test_config_without_time_keys_is_unaffected_by_the_hour(config_path, monkeypatch):
    """A policy with none of the new keys answers the same at every hour.

    The clock is now always injected, so this is the guard that injecting it
    changed nothing for a config that does not use it: same decision, same chain,
    no elo capped, demoted or promoted, and a preview explicitly labelled NOT
    time-relative so the console does not warn about an hour that cannot matter.
    """
    import router.service as service_mod

    service = RouterService(config_path)

    peak = service.explain("Debug a race condition", _PEAK_0700)
    offpeak = service.explain("Debug a race condition", _OFFPEAK_1500)

    for result in (peak, offpeak):
        plan = result["chain_plan"]
        assert result["mode"] == "deterministic_dry_run"
        assert result["decision"]["cause"] == "hard_rule"
        assert [hop["model"] for hop in plan["chain"]] == ["strong", "backup"]
        assert plan["strategy"] == "sequential"
        assert plan["capped"] == [] and plan["demoted"] == [] and plan["promoted"] == []
        assert plan["time_cap_bypassed"] is False
        assert "time_cap" not in plan
        assert result["preview"]["time_relative"] is False
        assert result["preview"]["time_relative_reasons"] == []

    # The plan's CONTENT is identical across the two hours. Only the hour it
    # reports differs, which is the plan being honest about when it was made and
    # not a routing difference: nothing here reads a clock.
    clock_keys = {"utc_hour", "utc_weekday"}
    assert {k: v for k, v in peak["chain_plan"].items() if k not in clock_keys} == {
        k: v for k, v in offpeak["chain_plan"].items() if k not in clock_keys
    }
    assert peak["decision"]["output"] == offpeak["decision"]["output"]
    assert peak["decision"]["matched_clauses"] == offpeak["decision"]["matched_clauses"]
    assert peak["preview"] == offpeak["preview"]
    assert peak["requires_classifier"] == offpeak["requires_classifier"]

    # liveness(): the prior states, plus neutral window fields for elos the
    # registry has never heard of — a 1.0 multiplier is "no window known", which
    # `capabilities_known: False` beside it makes unambiguous.
    monkeypatch.setattr(service_mod, "_utc_now", lambda: _PEAK_0700)
    liveness = service.liveness()
    assert {e["state"] for e in liveness["models"]} == {"alive"}
    assert all(e["capabilities_known"] is False for e in liveness["models"])
    assert all(e["price_multiplier"] == 1.0 for e in liveness["models"])
    assert all(e["in_expensive_window"] is False for e in liveness["models"])
    assert all(e["next_window_change"] is None for e in liveness["models"])


def test_read_paths_survive_a_loadable_but_wrong_config(tmp_path):
    """status()/policy() must not raise on a config that parses but is nonsense.

    ``rules: 5`` and ``tiers: nope`` load fine and lint rejects both, and status is
    the endpoint an operator opens BECAUSE the config is broken. Reporting the
    breakage in ``validation_errors`` while raising a TypeError out of the same call
    would kill the one surface that could explain it.
    """
    path = tmp_path / "router.yaml"
    path.write_text(
        "enabled: true\nrules: 5\ntiers: nope\ndefault: {}\n"
        "classifier: [not, a, mapping]\nblocklist: {auto_breaker: not-a-mapping}\n",
        encoding="utf-8",
    )
    service = RouterService(path)

    status = service.status()
    assert status["valid"] is False
    assert any("'rules' must be a list" in e for e in status["validation_errors"])
    # Degraded, never invented: no rules, no tiers, no classifier, no breaker.
    assert status["rules_count"] == 0
    assert status["tiers"] == []
    assert status["classifier"] == {"model": "", "provider": ""}
    assert status["breaker_enabled"] is False

    assert service.policy()["rules"] == []
    assert service.policy()["tiers"] == {}
    # The other read paths already held this line; assert it so a regression in
    # any one of them is caught here.
    assert service.lint()["valid"] is False
    assert service.blocklist()["manual_bans"] == []
    assert service.liveness()["models"] == []


# ---------------------------------------------------------------------------
# The capability catalogue: the read path behind the console's price audit
# ---------------------------------------------------------------------------


def _multiplier_from_served_windows(entry, hour, weekday):
    """Price one hour from the ``price_windows`` the catalogue SERVED.

    A deliberate SECOND implementation of the reading a consumer has to make —
    half-open ``[start, end)`` hours, absent ``weekdays`` meaning every day, 1.0
    when nothing matches — so the assertions below compare the audit surface with
    the running planner through a third computation instead of trusting either
    one's word. It is the console's own rule, written out in the test, because a
    helper shared with the code under test would agree with it by construction.
    """
    multiplier = 1.0
    for window in entry.get("price_windows") or []:
        start, end = window["hours_utc"]
        if not start <= hour < end:
            continue
        weekdays = window.get("weekdays")
        if weekdays is not None and weekday not in weekdays:
            continue
        multiplier = window["multiplier"]
    return multiplier


def test_capabilities_serves_the_facts_the_price_audit_needs(capability_config_path):
    """Per model: the capability facts, the billing mode, the prices, the windows."""
    catalogue = RouterService(capability_config_path).capabilities()

    assert catalogue["registry_available"] is True
    # Base rates with no hour applied — `liveness` is the surface that prices NOW.
    assert catalogue["time_agnostic"] is True
    assert catalogue["unknown_models"] == []
    assert catalogue["warnings"] == []

    flash = catalogue["models"]["deepseek-v4-flash"]
    # The finding's own case: with no /capabilities route the console rendered
    # this rail as publishing no per-token price. It publishes two.
    assert (flash["price_in"], flash["price_out"]) == (0.22, 0.66)
    assert flash["price_published"] is True
    # The capability facts the filter decides on, so a rejection is checkable.
    assert flash["context_window"] == 1_048_576
    assert flash["vision"] is False
    assert flash["tool_calling"] is True
    assert flash["structured_output"] is True
    assert flash["billing_mode"] == "metered"
    assert flash["provider"] == "deepseek"
    assert flash["in_registry"] is True
    # Windows verbatim, so a consumer can price ANY hour instead of one.
    assert flash["price_windows"] == [
        {"hours_utc": [1, 4], "multiplier": 2.0},
        {"hours_utc": [6, 10], "multiplier": 2.0},
    ]
    # A catalogue, not a credential store — and no free-text `notes` either,
    # which is the field a pasted key would land in.
    assert "notes" not in flash
    body = json.dumps(catalogue).lower()
    assert "api_key" not in body
    assert "secret" not in body


def test_capabilities_tells_an_unpublished_price_apart_from_a_price_of_zero(
    capability_config_path,
):
    """The distinction the panel exists for: None is not 0.0.

    A plan rail bills in credits off an allowance already bought. Rendered as $0
    it would look like the cheapest thing on the screen when it is merely the
    least priced — the opposite of the truth.
    """
    models = RouterService(capability_config_path).capabilities()["models"]

    plan_rail = models["glm-5.3"]
    assert plan_rail["billing_mode"] == "plan"
    assert plan_rail["price_in"] is None
    assert plan_rail["price_out"] is None
    assert plan_rail["price_published"] is False
    # It still declares a window, so "no dollar price" is not "no cost variation".
    assert plan_rail["price_windows"]

    # A genuinely free rail publishes 0.0, which IS a price and survives as one.
    free_rail = models["glm-4.7-flash"]
    assert free_rail["billing_mode"] == "free"
    assert (free_rail["price_in"], free_rail["price_out"]) == (0.0, 0.0)
    assert free_rail["price_published"] is True


def test_capabilities_price_publication_agrees_with_the_running_cost_path(
    capability_config_path,
):
    """The audit surface and the path that RANKS on cost may not disagree.

    ``cheapest_now`` buckets an elo on whether ``capabilities.effective_price``
    answers at all, so a catalogue calling a rail priced where the ordering calls
    it unpriced would explain a decision the router never made. Asserted as an
    AGREEMENT between the two surfaces plus the fields actually served, never as a
    claim about one of them.
    """
    from router import capabilities as caps

    service = RouterService(capability_config_path)
    catalogue = service.capabilities()
    declared = RouterService._declared_capability_index(
        yaml.safe_load(capability_config_path.read_text(encoding="utf-8"))
    )

    assert catalogue["models"]
    for model, entry in catalogue["models"].items():
        overrides = declared.get(model) or None
        priced = caps.effective_price(model, None, overrides)

        # The flag agrees with the cost path...
        assert entry["price_published"] is (priced is not None), model
        # ...and with the two fields a console reads instead of the flag.
        both_real = all(
            isinstance(entry.get(key), (int, float))
            and not isinstance(entry.get(key), bool)
            for key in ("price_in", "price_out")
        )
        assert entry["price_published"] is both_real, model
        # A served price is the BASE rate: no hour has been folded into it.
        if priced is not None:
            assert (entry["price_in"], entry["price_out"]) == priced, model

        # And the capability half agrees with the filter's own verdict.
        assert RouterService._capabilities_known(model, overrides) is True, model


def test_capabilities_windows_reprice_the_hour_the_plan_was_made_at(time_config_path):
    """The served windows must reproduce the multipliers the planner used.

    The plan is the path that RUNS; the catalogue is the surface that DISPLAYS.
    If pricing the plan's own hour from the served windows gave a different
    number, the panel would be explaining a cost the router never applied.
    """
    service = RouterService(time_config_path)
    plan = service.explain(_HARD_TASK, at=_PEAK_0700)["chain_plan"]
    models = service.capabilities()["models"]

    # Both a windowed and a flat elo, so the agreement is not vacuous.
    assert plan["multipliers"] == {"deepseek-v4-pro": 2.0, "glm-4.6": 1.0}
    for model, multiplier in plan["multipliers"].items():
        assert model in models, model
        assert _multiplier_from_served_windows(
            models[model], plan["utc_hour"], plan["utc_weekday"]
        ) == multiplier, model

    # Every elo the plan could not describe is exactly one the catalogue omits.
    assert set(plan["unknown"]) & set(models) == set()


def test_capabilities_reads_no_clock(capability_config_path, monkeypatch):
    """Same catalogue at 07:00 and at 15:00 — this surface is time-agnostic.

    The prices are base rates and the windows are declared data; folding an hour
    in here would give the console a second, competing answer to a question
    ``liveness`` already answers at an hour it names.
    """
    import router.service as service_mod

    service = RouterService(capability_config_path)
    monkeypatch.setattr(service_mod, "_utc_now", lambda: _PEAK_0700)
    peak = service.capabilities()
    monkeypatch.setattr(service_mod, "_utc_now", lambda: _OFFPEAK_1500)

    assert service.capabilities() == peak


def test_capabilities_declared_overrides_win_and_are_named(tmp_path):
    """An operator's corrected number is served, and attributed to them.

    ``declared`` WINS over the registry on the filter's path, so it has to win
    here too; ``declared_overrides`` names which fields came from router.yaml so
    an operator can tell their own correction from the registry's data.
    """
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "classifier": {"model": "glm-4.7", "provider": "zai"},
                "fail_safe": {"profile": "coder", "model": "glm-4.7",
                              "provider": "zai"},
                "blocklist": {"manual_ban": [], "fallback_chain": [],
                              "auto_breaker": {"enabled": False}},
                "rules": [],
                "default": {"action": "classify"},
                "tiers": {
                    "T1": {
                        # A stale registry rate and window, corrected in YAML.
                        "model": "deepseek-v4-flash", "provider": "deepseek",
                        "billing_mode": "metered",
                        "price_in": 0.11, "price_out": 0.33,
                        "price_windows": [
                            {"hours_utc": [6, 10], "multiplier": 3.0},
                        ],
                    },
                    "T2": {"model": "glm-4.7", "provider": "zai",
                           "billing_mode": "plan"},
                    "T3": {"model": "glm-4.6", "provider": "zai"},
                    "T4": {"model": "gpt-5.5", "provider": "openai-codex"},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    corrected = RouterService(path).capabilities()["models"]["deepseek-v4-flash"]

    assert (corrected["price_in"], corrected["price_out"]) == (0.11, 0.33)
    assert corrected["price_published"] is True
    assert corrected["price_windows"] == [{"hours_utc": [6, 10], "multiplier": 3.0}]
    assert corrected["declared_overrides"] == [
        "billing_mode", "price_in", "price_out", "price_windows",
    ]
    # Still the registry's model, so the untouched facts are still the registry's.
    assert corrected["in_registry"] is True
    assert corrected["context_window"] == 1_048_576
    # A copy, never a live view: a consumer mutating this cannot edit the registry.
    from router import capabilities as caps

    assert caps.MODEL_CAPABILITIES["deepseek-v4-flash"]["price_in"] == 0.22


def test_capabilities_flags_an_unknown_elo_instead_of_vouching_for_it(
    warned_config_path,
):
    """An unknown model fails OPEN with a loud flag — and stays OUT of `models`.

    A console reads the PRESENCE of an entry as "these capabilities are verified",
    so a hollow entry would silence the unverified badge on the one elo that
    routes unchecked. Dropping such an elo from a chain could empty it, so it
    still routes; it just routes visibly unverified.
    """
    service = RouterService(warned_config_path)
    catalogue = service.capabilities()

    # Unknown to the registry AND describing nothing: absent, listed, loud.
    assert "ghost-model" not in catalogue["models"]
    assert "ghost-model" in catalogue["unknown_models"]
    assert any(
        "ghost-model" in warning and "UNCHECKED" in warning
        for warning in catalogue["warnings"]
    )

    # Unknown to the registry but DESCRIBED in yaml: declared wins, so it is
    # auditable — with the operator's numbers and the provider policy names it by.
    house = catalogue["models"]["house-model"]
    assert house["context_window"] == 500_000
    assert house["vision"] is True
    assert house["provider"] == "local-rail"
    assert house["in_registry"] is False
    assert house["declared_overrides"] == ["context_window", "vision"]
    # It publishes no price, which is not a price of zero here either.
    assert house["price_published"] is False
    assert "price_in" not in house

    # The catalogue and the running filter agree, elo by elo, about which are
    # verifiable: presence here means exactly `capabilities_known` there.
    for entry in service.liveness()["models"]:
        assert (entry["model"] in catalogue["models"]) is entry[
            "capabilities_known"
        ], entry["model"]


def test_capabilities_serves_an_allowlist_not_whatever_the_registry_grows(
    capability_config_path, monkeypatch
):
    """A field the registry gains later is NOT published by default.

    This read path hands registry material to a browser. The day an entry grows a
    field carrying a credential, a passthrough would publish it with no edit and
    no review — ``capabilities_for`` merges an entry WHOLE, unknown fields
    included, so the allowlist here is the only thing standing between the two.
    """
    from router import capabilities as caps

    entry = dict(caps.MODEL_CAPABILITIES["glm-4.7"])
    entry["api_key"] = "sk-must-never-be-served"
    entry["notes"] = "an operator pasted sk-live-0000 in here"
    monkeypatch.setitem(caps.MODEL_CAPABILITIES, "glm-4.7", entry)

    catalogue = RouterService(capability_config_path).capabilities()
    served = catalogue["models"]["glm-4.7"]

    assert "api_key" not in served
    assert "notes" not in served
    assert "sk-" not in json.dumps(catalogue)
    # The facts it does serve still arrive intact.
    assert served["context_window"] == 200_000
    assert served["price_published"] is True


def test_capabilities_never_raises_and_degrades_out_loud(tmp_path, monkeypatch):
    """Read-path contract: a broken config or registry degrades, never 500s."""
    import router.service as service_mod

    # A config that parses but is nonsense: the registry does not depend on it,
    # so the catalogue is still served in full.
    path = tmp_path / "router.yaml"
    path.write_text("enabled: true\nrules: 5\ntiers: nope\n", encoding="utf-8")
    catalogue = RouterService(path).capabilities()
    assert catalogue["models"]
    assert "error" not in catalogue

    # No registry importable at all: an empty catalogue, said out loud. A console
    # renders that as "unverified", which is the honest answer for "unreadable".
    monkeypatch.setattr(service_mod, "_caps", None)
    assert RouterService(path).capabilities() == {
        "models": {},
        "unknown_models": [],
        "warnings": [],
        "registry_available": False,
        "time_agnostic": True,
    }

    # A registry that raises is the same answer, not a traceback — and the model
    # it could not describe is flagged rather than served hollow.
    class Exploding:
        MODEL_CAPABILITIES = {"boom": {"context_window": 1}}

        @staticmethod
        def capabilities_for(*_a, **_kw):
            raise TypeError("stale registry")

    monkeypatch.setattr(service_mod, "_caps", Exploding)
    exploding = RouterService(path).capabilities()
    assert exploding["models"] == {}
    assert exploding["unknown_models"] == ["boom"]
    assert exploding["warnings"]
