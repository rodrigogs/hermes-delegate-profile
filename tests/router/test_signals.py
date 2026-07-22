"""Unit tests for signal extraction (router/signals.py)."""

import pytest
from router.signals import extract


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
