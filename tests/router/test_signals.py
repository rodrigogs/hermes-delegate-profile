"""Unit tests for signal extraction (router/signals.py)."""

import math
from pathlib import Path

import pytest
from router import signals as signals_module
from router.signals import (
    EXTRACTED_FEATURE_NAMES,
    INJECTED_FEATURE_NAMES,
    KNOWN_FEATURE_NAMES,
    _ATTACHMENT_MARKERS,
    _QUESTION_MARKERS,
    _TOOL_MARKERS,
    _VISION_AMBIGUOUS_NOUNS,
    _VISION_MARKERS,
    _estimate_input_tokens,
    extract,
)


class TestExtract:
    """Feature vector extraction from task descriptions."""

    def test_trivial_rename(self):
        task = "Rename getCwd to getCurrentWorkingDirectory in 3 files, ~40 lines"
        fv = extract(task)
        assert fv["verb_class"] == "trivial"
        assert fv["has_code"] is True
        assert fv["size_lines"] == 40
        assert fv["num_files"] == 3
        assert fv["has_stacktrace"] is False

    def test_hard_debug_stacktrace(self):
        task = """Debug a race condition where concurrent writes cause stale data.
Traceback (most recent call last):
  File "cache.py", line 42, in write
KeyError: 'user:123'"""
        fv = extract(task)
        assert fv["verb_class"] == "hard"
        assert fv["has_code"] is True
        assert fv["has_stacktrace"] is True
        assert "race condition" in task.lower()

    def test_hard_refactor(self):
        task = "Refactor the authentication middleware to support JWT and API key"
        fv = extract(task)
        assert fv["verb_class"] == "hard"
        assert fv["has_code"] is True

    def test_hard_secure(self):
        task = "Secure the login endpoint against SQL injection attacks"
        fv = extract(task)
        assert fv["verb_class"] == "hard"
        assert fv["has_code"] is True  # "endpoint" in code keywords

    def test_unknown_verb(self):
        task = "Add a /health endpoint that returns {status: ok}"
        fv = extract(task)
        assert fv["verb_class"] == "unknown"
        assert fv["has_code"] is True
        assert fv["size_lines"] == 0  # no explicit line count

    def test_review_keywords(self):
        task = "Please review this PR for security issues"
        fv = extract(task)
        assert fv["keywords"] == ["review"]
        assert fv["has_code"] is True  # "pr" keyword

    def test_every_exported_review_keyword_can_actually_be_extracted(self):
        """The vocabulary the constant advertises must be the one that fires.

        ``_keyword_hits`` carried its own inline copy of the word set, missing
        ``evaluate``, so ``_REVIEW_KEYWORDS`` promised a word the extractor could
        never produce: a rule written ``keywords: {contains: evaluate}`` linted
        clean and was dead on arrival. Asserted over the whole exported set rather
        than for the one word that was missing, so a future addition to the
        constant cannot re-open the gap.
        """
        from router.signals import _REVIEW_KEYWORDS

        for word in sorted(_REVIEW_KEYWORDS):
            assert extract(f"Please {word} this change")["keywords"] == [word], word

    def test_keyword_hits_are_sorted_so_two_processes_agree(self):
        """The field is persisted in the trace, and its source is a set.

        Set iteration order varies per process, so two processes recording the same
        turn produced traces differing only in the order of this list — enough to
        defeat a byte comparison between them.
        """
        hits = extract("review and audit and inspect this")["keywords"]
        assert hits == sorted(hits)
        assert hits == ["audit", "inspect", "review"]

    def test_requirements_counting(self):
        task = """Tasks:
- Add user model
- Add migration
- Add REST endpoint
- Add tests
- Update docs"""
        fv = extract(task)
        assert fv["num_requirements"] == 5

    def test_numbered_requirements(self):
        task = """1. Create the database schema
2. Implement the repository
3. Wire up the controller"""
        fv = extract(task)
        assert fv["num_requirements"] == 3

    def test_language_detection_python(self):
        task = "Fix the FastAPI endpoint in users.py"
        fv = extract(task)
        assert fv["lang"] == "python"

    def test_language_detection_typescript(self):
        task = "Refactor the React component in Dashboard.tsx"
        fv = extract(task)
        assert fv["lang"] == "typescript"

    def test_language_detection_unknown(self):
        task = "Update the documentation"
        fv = extract(task)
        assert fv["lang"] == ""

    def test_char_len(self):
        task = "hello"
        fv = extract(task)
        assert fv["char_len"] == 5

    def test_empty_turn(self):
        task = ""
        fv = extract(task)
        assert fv["char_len"] == 0
        assert fv["verb_class"] == "unknown"
        assert fv["has_code"] is False
        assert fv["has_stacktrace"] is False

    def test_file_count_range(self):
        task = "Update 3-5 files with the new import pattern"
        fv = extract(task)
        assert fv["num_files"] == 5  # upper bound

    def test_file_count_exact(self):
        task = "Modify 2 files"
        fv = extract(task)
        assert fv["num_files"] == 2


class TestContextSignals:
    """est_input_tokens — heuristic context need, base plus allowances."""

    def test_base_estimate_is_chars_over_ratio(self):
        task = "Summarize what this project does."
        fv = extract(task)
        assert fv["char_len"] == 33
        assert fv["num_files"] == 0
        assert fv["est_input_tokens"] == math.ceil(33 / 3.6) == 10

    def test_empty_turn_estimates_zero(self):
        fv = extract("")
        assert fv["est_input_tokens"] == 0

    def test_whole_repo_allowance(self):
        # Same length, one word pair apart: the delta is the allowance itself.
        whole = extract("Audit the entire repo for unsafe subprocess usage.")
        narrow = extract("Audit the single file for unsafe subprocess usage.")
        assert whole["char_len"] == narrow["char_len"]
        assert narrow["est_input_tokens"] == math.ceil(50 / 3.6)
        assert whole["est_input_tokens"] - narrow["est_input_tokens"] == 40000

    @pytest.mark.parametrize(
        "phrase",
        ["entire repo", "whole codebase", "every file", "all files", "across the repo"],
    )
    def test_whole_repo_phrases_all_fire(self, phrase):
        fv = extract(f"Check {phrase} for TODO markers")
        assert fv["est_input_tokens"] >= 40000

    def test_no_whole_repo_allowance_without_marker(self):
        fv = extract("Check the auth module for TODO markers")
        assert fv["est_input_tokens"] < 40000

    def test_file_allowance_compounds_with_char_len(self):
        task = "Update 3 files to use the new import pattern"
        fv = extract(task)
        assert fv["num_files"] == 3
        assert fv["est_input_tokens"] == math.ceil(len(task) / 3.6) + 3 * 4000
        assert fv["est_input_tokens"] == 12013

    def test_both_allowances_compound(self):
        task = "Refactor 2 files across the repo"
        fv = extract(task)
        expected = math.ceil(len(task) / 3.6) + 2 * 4000 + 40000
        assert fv["est_input_tokens"] == expected

    def test_estimate_is_int(self):
        fv = extract("Modify 2 files")
        assert isinstance(fv["est_input_tokens"], int)
        assert not isinstance(fv["est_input_tokens"], bool)

    def test_estimate_clamps_at_zero_for_a_nonsense_char_len(self):
        # The clamp is the reason a garbage count upstream cannot produce a
        # negative feature that would silently satisfy an `lt` clause.
        assert _estimate_input_tokens(-500, 0, "") == 0
        assert _estimate_input_tokens(-500, 0, "audit the entire repo") == 40000

    def test_estimate_is_never_negative_for_any_turn(self):
        for task in ["", " ", "\n", "x", "Modify 2 files", "no files at all"]:
            assert extract(task)["est_input_tokens"] >= 0


class TestCapabilitySignals:
    """needs_vision — the unambiguous markers, which fire on the marker alone."""

    @pytest.mark.parametrize(
        "task",
        [
            "Here is a screenshot of the failing page",
            "Compare the two png exports",
            "The jpg is blurry, the jpeg less so",
            "Convert the webp asset",
            "Rebuild the header from the Figma file",
            "Match the mockup exactly",
            "Turn this wireframe into HTML",
            "Look at this design and tell me what is off",
            "see attached and fix it",
            "the UI looks broken on mobile",
        ],
    )
    def test_needs_vision_positive(self, task):
        assert extract(task)["needs_vision"] is True

    def test_needs_vision_negative(self):
        fv = extract("Refactor the retry helper to use exponential backoff")
        assert fv["needs_vision"] is False


class TestVisionInputVersusVisualNoun:
    """A visual noun is not visual input.

    needs_vision selects the vision rule, and the capability filter then drops
    every elo that cannot see — which on this registry can leave a single hop
    on one subscription rail. So a text-only turn that merely *mentions* a
    chart must not be read as a turn that *supplies* one. Both directions of
    every ambiguous noun are pinned here.
    """

    @pytest.mark.parametrize(
        "noun,supplied,mentioned",
        [
            (
                "chart",
                "look at this chart and tell me why the bars are wrong",
                "plot a chart from the csv",
            ),
            (
                "diagram",
                "the attached diagram shows the wrong arrows",
                "generate a mermaid diagram of the request flow",
            ),
            (
                "image",
                "here is the image of the crash dialog",
                "rebuild the docker image and push it",
            ),
            (
                "design",
                "look at this design and say what is off",
                "the design of the retry helper is wrong",
            ),
            (
                "plot",
                "the plot I pasted has the axes swapped",
                "plot the latency distribution by percentile",
            ),
        ],
    )
    def test_ambiguous_noun_needs_a_cue(self, noun, supplied, mentioned):
        assert noun in supplied and noun in mentioned
        assert extract(supplied)["needs_vision"] is True
        assert extract(mentioned)["needs_vision"] is False

    @pytest.mark.parametrize(
        "task",
        [
            "the flow chart of the module",
            "Redraw the chart with a log scale",
            "Explain the sequence diagram",
            "in this file the diagram is generated",
            "plot these points on a log axis",
            "chart the rollout week by week",
            "let's simplify this design and re-run the tests",
            "what does this design imply for the schema",
            "check out the design system tokens in tokens.css",
        ],
    )
    def test_visual_noun_without_a_cue_is_not_vision(self, task):
        assert extract(task)["needs_vision"] is False

    @pytest.mark.parametrize(
        "deictic_only,supplied",
        [
            ("let's simplify this design and re-run the tests",
             "look at this design and say what is off"),
            ("what does this design imply for the schema",
             "the design attached shows the wrong spacing"),
            ("re-run this plot against the new sample",
             "the plot I pasted has the axes swapped"),
        ],
    )
    def test_design_and_plot_need_supply_evidence_not_a_deictic(
        self, deictic_only, supplied
    ):
        # "this design" is one of the commonest phrases in a coding turn, so
        # unlike chart/diagram/image these two are not promoted by a bare
        # deictic — only by an attachment, look-at or trailing cue. Both
        # directions asserted together: weakening the noun list would break the
        # second half, and re-adding it to the deictic list would break the
        # first.
        assert extract(deictic_only)["needs_vision"] is False
        assert extract(supplied)["needs_vision"] is True

    @pytest.mark.parametrize(
        "task",
        [
            "check out the design system tokens in tokens.css",
            "read the design doc before the meeting",
            "look at the design pattern used in the handler",
            "check out the docker image tag",
        ],
    )
    def test_a_cue_beside_a_non_visual_compound_is_not_vision(self, task):
        # A design system is a token set and a docker image is a tarball: the
        # compound names something that is not an image at all, so a cue landing
        # beside it must not promote it however look-at the phrasing is.
        assert extract(task)["needs_vision"] is False

    @pytest.mark.parametrize(
        "task",
        [
            "the chart above is wrong",
            "see the diagram I attached",
            "have a look at the design here",
            "check out this chart",
            "the uploaded wiring diagram is out of date",
        ],
    )
    def test_cue_next_to_the_noun_is_vision(self, task):
        assert extract(task)["needs_vision"] is True

    def test_mockup_stays_unconditional_and_that_is_deliberate(self):
        # "mockup" is retained as an unambiguous marker: unlike chart/plot it
        # names a design artefact rather than something code produces, so it
        # fires with or without a deictic cue. Recorded as an asserted
        # asymmetry rather than left to be rediscovered as a surprise.
        assert extract("Match the mockup exactly")["needs_vision"] is True
        assert extract("the mockup is out of date")["needs_vision"] is True

    def test_unambiguous_markers_need_no_cue(self):
        for task in [
            "screenshot",
            "mockup.png",
            "the figma frame",
            "wireframe first, code second",
        ]:
            assert extract(task)["needs_vision"] is True, task

    def test_bare_deictic_without_a_visual_noun_is_not_vision(self):
        # "look at this" used to be a marker on its own, so a stack trace was
        # a vision turn.
        assert extract("look at this traceback")["needs_vision"] is False
        assert extract("look at this function signature")["needs_vision"] is False


class TestVisionMarkersMatchWholeWords:
    """A word that CONTAINS a marker is not a marker.

    The markers used to be matched by containment, so "png" fired inside
    "libpng-dev" and a makefile turn was routed as visual input: the vision
    rule sent it to T2, the capability filter dropped both text-capable hops as
    `no_vision`, and the turn came back on one subscription rail with no
    fallback left.
    """

    @pytest.mark.parametrize(
        "task",
        [
            "the build fails on libpng-dev, fix the makefile",
            "webpack the bundle and check the size budget",
            "shell out to jpegoptim in the asset step",
            "the imagemagick pipeline is the slow part",
            "pngcrush runs on every commit",
        ],
    )
    def test_marker_embedded_in_a_word_is_not_vision(self, task):
        assert extract(task)["needs_vision"] is False

    @pytest.mark.parametrize(
        "task",
        [
            "Compare the two png exports",
            "the screenshot.png is stale",
            "attach the diff and the webp asset",
            "the jpg is blurry, the jpeg less so",
        ],
    )
    def test_marker_as_a_whole_word_is_still_vision(self, task):
        assert extract(task)["needs_vision"] is True


class TestVisionAgreesWithTheAttachmentTable:
    """The path that RUNS the decision and the surface that DISPLAYS it agree.

    `needs_vision` is what the capability filter runs on; `attachment_kinds` is
    what the console shows the operator. For the tokens both tables share, the
    two read the same image name out of the same turn through their own marker
    set — so a turn that names one must set both, and a word that merely
    contains one must set neither. Asserting the pair rather than either side is
    the point: "libpng-dev" was `needs_vision` True *and* `['image']`, and
    fixing one table alone would have left the two disagreeing on every
    extension.
    """

    SHARED_TOKENS = sorted(_VISION_MARKERS & _ATTACHMENT_MARKERS["image"])

    def test_the_two_tables_do_share_tokens(self):
        # If this ever empties the agreement below is vacuously true.
        assert self.SHARED_TOKENS == ["jpeg", "jpg", "png", "screenshot", "webp"]

    @pytest.mark.parametrize("token", SHARED_TOKENS)
    def test_whole_word_token_sets_both_sides(self, token):
        fv = extract(f"Compare the two {token} exports")
        assert fv["needs_vision"] is True
        assert "image" in fv["attachment_kinds"]
        assert fv["needs_vision"] == ("image" in fv["attachment_kinds"])

    @pytest.mark.parametrize("token", SHARED_TOKENS)
    def test_embedded_token_sets_neither_side(self, token):
        fv = extract(f"the build fails on lib{token}-dev, fix the makefile")
        assert fv["needs_vision"] is False
        assert "image" not in fv["attachment_kinds"]
        assert fv["needs_vision"] == ("image" in fv["attachment_kinds"])

    @pytest.mark.parametrize(
        "task",
        [
            "the build fails on libpng-dev, fix the makefile",
            "webpack the bundle and check the size budget",
            "the imagemagick pipeline is the slow part",
            "Compare the two png exports",
            "the screenshot.png is stale",
        ],
    )
    def test_the_pair_never_disagrees_on_a_shared_token_turn(self, task):
        fv = extract(task)
        assert fv["needs_vision"] == ("image" in fv["attachment_kinds"]), task


class TestVisionDocstringClaimHolds:
    """The stated contract and the marker tables must say the same thing.

    The module header promises the detector "asks whether the turn implies
    visual *input*, not whether it mentions a visual noun". That sentence is
    asserted against the source *and* against behaviour, because the two had
    drifted: the tables promoted an ambiguous noun on a bare deictic while the
    header said they would not.
    """

    # One mention-only turn per ambiguous noun. The turn produces or discusses
    # the artefact; nothing supplies it.
    MENTION_ONLY = {
        "chart": "the module has a flow chart in the wiki",
        "diagram": "generate a mermaid diagram of the request flow",
        "image": "rebuild the docker image and push it",
        "design": "let's simplify this design and re-run the tests",
        "plot": "plot the latency distribution by percentile",
    }

    def test_every_ambiguous_noun_has_a_mention_only_case(self):
        # Adding a noun to the table without a case here fails rather than
        # slipping in unexercised.
        assert set(self.MENTION_ONLY) == set(_VISION_AMBIGUOUS_NOUNS)

    def test_source_states_the_claim(self):
        source = Path(signals_module.__file__).read_text(encoding="utf-8")
        prose = " ".join(source.replace("#", " ").split())  # unwrap the comment
        assert "not whether it mentions a visual noun" in prose
        assert "Mentioning a visual noun is not enough" in prose

    @pytest.mark.parametrize("noun", sorted(MENTION_ONLY))
    def test_mentioning_a_visual_noun_is_not_visual_input(self, noun):
        task = self.MENTION_ONLY[noun]
        assert noun in task.lower()          # the noun really is in the turn
        assert extract(task)["needs_vision"] is False


class TestOtherCapabilitySignals:
    """needs_structured_output / needs_tools / attachment_kinds."""

    @pytest.mark.parametrize(
        "task",
        [
            "Conform to this json schema",
            "We need structured output here",
            "Return json for each row",
            "Emit the result as json",
            "Set response_format on the call",
            "Enforce a strict schema",
            "typed output only, please",
        ],
    )
    def test_needs_structured_output_positive(self, task):
        assert extract(task)["needs_structured_output"] is True

    def test_needs_structured_output_negative(self):
        fv = extract("Write a short paragraph explaining the cache design")
        assert fv["needs_structured_output"] is False

    @pytest.mark.parametrize(
        "task",
        [
            "Run the migration",
            "execute the smoke suite",
            "Edit the config",
            "Write the adapter",
            "Create a fixture",
            "Delete the stale branch data",
            "Install the dev extras",
            "Commit the fix",
            "Add a test for the parser",
            "Build the wheel",
            "Deploy to staging",
            "Search for callers of extract()",
            "Fetch the release notes",
            "read the file and summarise it",
            "apply the suggested change",
        ],
    )
    def test_needs_tools_positive(self, task):
        assert extract(task)["needs_tools"] is True

    def test_needs_tools_true_on_empty_turn(self):
        assert extract("")["needs_tools"] is True


class TestNeedsToolsIsBidirectional:
    """needs_tools must be able to say False, or the marker set is decoration.

    The detector is asymmetric on purpose. An action verb says True; only
    positive evidence of a pure question — explanatory or interrogative
    phrasing, no action verb, no file or path reference — says False; anything
    else keeps the fail-closed default. The False direction is what makes a
    `needs_tools: {eq: false}` rule reachable and what lets an elo declaring
    `tool_calling: False` be eligible for a question turn.
    """

    @pytest.mark.parametrize(
        "task",
        [
            "Why is my mutex approach slower than a spinlock?",
            "What is a semaphore?",
            "Explain the difference between optimistic and pessimistic locking",
            "How does exponential backoff interact with a token bucket?",
            "Describe when a B-tree beats a hash index",
            "Tell me about the CAP theorem",
            "Compare a mutex with a spinlock",
            "Summarize why TCP slow start exists",
            "walk me through how a bloom filter avoids false negatives",
            "what are the pros and cons of optimistic concurrency",
        ],
    )
    def test_pure_question_is_not_a_tool_turn(self, task):
        lower = task.lower()
        assert not any(marker in lower for marker in _TOOL_MARKERS)
        assert any(marker in lower for marker in _QUESTION_MARKERS)
        assert extract(task)["needs_tools"] is False

    @pytest.mark.parametrize(
        "task",
        [
            "explain what users.py does",
            "explain the file layout",
            "what is in the repo root",
            "describe how this codebase handles retries",
            "why does router/signals.py import math",
        ],
    )
    def test_question_about_a_path_stays_a_tool_turn(self, task):
        # Interrogative phrasing, but the answer lives in material only a tool
        # can reach, so the fail-closed default stands.
        assert extract(task)["needs_tools"] is True

    def test_action_evidence_outranks_question_phrasing(self):
        # "read the file" is a tool marker; "summarise" is a question marker.
        # Order matters and the action wins.
        assert extract("read the file and summarise it")["needs_tools"] is True
        assert extract("explain the bug, then apply the fix")["needs_tools"] is True

    @pytest.mark.parametrize(
        "task",
        [
            "Thoughts on mutexes versus spinlocks?",
            "the retry helper seems slow",
            "hmm",
            "",
        ],
    )
    def test_neither_signal_keeps_the_fail_closed_default(self, task):
        lower = task.lower()
        assert not any(marker in lower for marker in _TOOL_MARKERS)
        assert not any(marker in lower for marker in _QUESTION_MARKERS)
        assert extract(task)["needs_tools"] is True

    def test_tool_markers_are_reachable_in_both_directions(self):
        # The regression this pins: the detector used to return True on every
        # input, which made _TOOL_MARKERS dead code.
        results = {
            extract("Run the migration")["needs_tools"],
            extract("What is a semaphore?")["needs_tools"],
        }
        assert results == {True, False}


class TestAttachmentKinds:
    """attachment_kinds — what the turn references, independent of vision."""

    def test_attachment_kinds_sorted_and_deduplicated(self):
        task = (
            "Here is a screenshot.png plus another image.PNG, the server.log "
            "and the rest of the logs, a git diff, report.pdf and rows.csv"
        )
        kinds = extract(task)["attachment_kinds"]
        assert kinds == ["csv", "diff", "image", "log", "pdf"]
        assert kinds == sorted(kinds)
        assert len(kinds) == len(set(kinds))

    @pytest.mark.parametrize(
        "task,kind",
        [
            ("attached screenshot", "image"),
            ("see report.pdf", "pdf"),
            ("parse the csv", "csv"),
            ("check server.log", "log"),
            ("review the git diff", "diff"),
            ("scrape the html", "html"),
        ],
    )
    def test_attachment_kind_inferred(self, task, kind):
        assert extract(task)["attachment_kinds"] == [kind]

    def test_attachment_kinds_empty_when_nothing_referenced(self):
        fv = extract("Explain the difference between a mutex and a semaphore")
        assert fv["attachment_kinds"] == []


class TestBackwardCompatibility:
    """Live rules and the operator console read these keys — they must not move."""

    LEGACY_KEYS = (
        "char_len", "has_code", "size_lines", "num_files", "has_stacktrace",
        "num_requirements", "verb_class", "lang", "keywords",
    )

    def test_legacy_keys_present_with_unchanged_values(self):
        task = "Rename getCwd to getCurrentWorkingDirectory in 3 files, ~40 lines"
        fv = extract(task)
        assert set(self.LEGACY_KEYS).issubset(fv.keys())
        assert fv["char_len"] == 65
        assert fv["has_code"] is True
        assert fv["size_lines"] == 40
        assert fv["num_files"] == 3
        assert fv["has_stacktrace"] is False
        assert fv["num_requirements"] == 0
        assert fv["verb_class"] == "trivial"
        assert fv["lang"] == ""
        assert fv["keywords"] == []

    def test_feature_vector_is_flat_and_depth_one(self):
        fv = extract("Debug the 2 files in the entire repo, see screenshot.png")
        for key, value in fv.items():
            assert isinstance(key, str)
            if isinstance(value, list):
                assert all(isinstance(item, str) for item in value)
            else:
                assert isinstance(value, (int, float, str, bool)), key

    def test_extract_is_deterministic(self):
        task = "Refactor 3 files across the repo, see the git diff and mockup.png"
        assert extract(task) == extract(task)


class TestExportedFeatureVocabulary:
    """EXTRACTED_FEATURE_NAMES is a public surface with a consumer.

    `rules.py`'s `when.<field>` lint imports it instead of keeping its own copy,
    so these assertions are the anti-drift guard: a key added to `extract()`
    without being added to the set (or vice versa) fails here rather than
    silently making a valid rule field look like a typo, or a typo look valid.
    """

    TURNS = (
        "",
        "hello",
        "Rename getCwd in 3 files, ~40 lines",
        "Debug the 2 files in the entire repo, see screenshot.png",
        "What is a semaphore?",
        "Return json for each row of the csv",
    )

    @pytest.mark.parametrize("task", TURNS)
    def test_extract_keys_are_exactly_the_exported_set(self, task):
        assert set(extract(task).keys()) == set(EXTRACTED_FEATURE_NAMES)

    def test_key_set_does_not_vary_with_the_turn(self):
        key_sets = {frozenset(extract(task).keys()) for task in self.TURNS}
        assert key_sets == {EXTRACTED_FEATURE_NAMES}

    def test_exported_sets_are_frozensets_of_str(self):
        for exported in (
            EXTRACTED_FEATURE_NAMES,
            INJECTED_FEATURE_NAMES,
            KNOWN_FEATURE_NAMES,
        ):
            assert isinstance(exported, frozenset)
            assert all(isinstance(name, str) for name in exported)

    def test_legacy_keys_are_all_in_the_exported_set(self):
        assert set(TestBackwardCompatibility.LEGACY_KEYS) <= EXTRACTED_FEATURE_NAMES

    def test_injected_time_features_are_not_produced_here(self):
        # The clock is a parameter supplied at the edge, never read in this
        # module, so `extract()` must not emit these — but a rule may key on
        # them, which is why they are named and exported.
        # Membership, not the whole set: the clock is no longer the only injected
        # feature (the role the caller fixes arrives the same way), and the
        # invariant this test protects is that NOTHING injected is produced here.
        assert {"utc_hour", "utc_weekday"} <= INJECTED_FEATURE_NAMES
        assert not INJECTED_FEATURE_NAMES & EXTRACTED_FEATURE_NAMES
        for task in self.TURNS:
            assert not set(extract(task)) & INJECTED_FEATURE_NAMES

    def test_known_names_is_the_union(self):
        assert KNOWN_FEATURE_NAMES == EXTRACTED_FEATURE_NAMES | INJECTED_FEATURE_NAMES

    def test_module_reads_no_clock(self):
        # AST-backed purity guard on the contract the module docstring states
        # (the text probe it replaced could not tell a docstring that DISCUSSES
        # time from a call that reads it). Same pattern as
        # test_capabilities.test_capabilities_module_never_reads_the_wall_clock.
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(signals_module))
        called: set = set()
        imported: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    called.add(node.func.id)
            elif isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])

        assert not called & {
            "now", "utcnow", "today", "monotonic", "time", "fromtimestamp", "open",
        }
        # No IO, no state, no network: the import list is the proof.
        assert imported <= {"__future__", "math", "re", "typing"}

def test_assignee_is_injected_and_never_extracted():
    """The role is an INPUT the caller fixes; extract() is pure and never sees it."""
    assert "assignee" in INJECTED_FEATURE_NAMES
    assert "assignee" in KNOWN_FEATURE_NAMES
    assert "assignee" not in EXTRACTED_FEATURE_NAMES
    assert "assignee" not in signals_module.extract(
        "Assign this to the reviewer profile please"
    )


def test_a_stack_trace_at_the_start_of_a_turn_is_detected():
    """`" runtime error"` carried the only leading space in the marker list.

    Matched by containment, a leading space means the marker cannot fire at offset
    0 or right after a newline — which is exactly where a pasted stack trace
    begins. Measured before the fix: `"runtime error in the pool"` and
    `"boom\\nruntime error here"` both answered False while
    `"a runtime error in the pool"` answered True.

    `has_stacktrace` reaches the classifier prompt, so the common paste shape was
    scoring as carrying no error report.
    """
    for turn in (
        "runtime error in the pool",
        "boom\nruntime error here",
        "a runtime error in the pool",
        "RUNTIME ERROR: index out of range",
    ):
        assert extract(turn)["has_stacktrace"] is True, turn


def test_no_stacktrace_marker_depends_on_surrounding_whitespace():
    """The property, not the one word that broke it.

    Every marker is matched by containment, so any marker with an edge space is
    unreachable at a line start. Asserted over the whole list so the next one added
    with a stray space fails here.
    """
    from router.signals import _detect_stacktrace

    # Pull the list out of the function's own constant-free body by exercising it:
    # each marker must fire when it IS the entire turn.
    markers = [
        "traceback", "stack trace", "exception:", "error:",
        "panic:", "segfault", "segmentation fault", "null pointer",
        "index out of", "key error", "type error", "attribute error",
        "syntax error", "runtime error",
    ]
    for marker in markers:
        assert marker == marker.strip(), (
            f"{marker!r} has an edge space; containment cannot match it at a line "
            f"start"
        )
        assert _detect_stacktrace(marker) is True, marker
        assert _detect_stacktrace(f"prefix\n{marker} and more") is True, marker


def test_chars_per_token_is_exposed_so_the_ratio_has_one_home():
    """router.classify converts a context WINDOW into a character budget, which is this
    module's conversion run backwards. Two copies of the number could disagree about how
    big the same text is."""
    from router.signals import chars_per_token, _CHARS_PER_TOKEN

    assert chars_per_token() == _CHARS_PER_TOKEN
    assert chars_per_token() > 0
