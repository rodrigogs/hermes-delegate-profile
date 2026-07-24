"""Tests for the router service shared by web surfaces (read + write paths)."""

from __future__ import annotations

import json
import threading

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
