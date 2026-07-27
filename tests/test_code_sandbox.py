#!/usr/bin/env python3
"""Unit tests for the code-fix sandbox (apply + test LLM code in isolation)."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "code_sandbox.py"
_spec = importlib.util.spec_from_file_location("code_sandbox", _MODULE_PATH)
cs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cs
_spec.loader.exec_module(cs)


def _make_repo(tmp_path, check_body: str) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "check.sh").write_text(check_body)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return str(repo)


def test_fix_that_makes_tests_pass(tmp_path):
    repo = _make_repo(tmp_path, "exit 1\n")  # starts broken
    result = cs.apply_and_test(
        repo,
        patch_files={"check.sh": "exit 0\n"},  # the LLM's fix
        test_command="bash check.sh",
    )
    assert result.status == "TESTED_PASS"
    assert result.applied is True
    assert result.tests_passed is True
    assert "check.sh" in result.diff


def test_fix_that_fails_tests(tmp_path):
    repo = _make_repo(tmp_path, "exit 1\n")
    result = cs.apply_and_test(
        repo,
        patch_files={"check.sh": "exit 3\n"},  # still broken
        test_command="bash check.sh",
    )
    assert result.status == "TESTED_FAIL"
    assert result.tests_passed is False


def test_apply_without_tests(tmp_path):
    repo = _make_repo(tmp_path, "exit 0\n")
    result = cs.apply_and_test(repo, patch_files={"note.txt": "hi"}, test_command=None)
    assert result.status == "APPLIED_NO_TESTS"
    assert result.applied is True


def test_unified_diff_patch(tmp_path):
    repo = _make_repo(tmp_path, "exit 1\n")
    patch = (
        "diff --git a/check.sh b/check.sh\n"
        "--- a/check.sh\n+++ b/check.sh\n"
        "@@ -1 +1 @@\n-exit 1\n+exit 0\n"
    )
    result = cs.apply_and_test(repo, patch=patch, test_command="bash check.sh")
    assert result.status == "TESTED_PASS"


def test_workspace_cleaned_up_by_default(tmp_path):
    repo = _make_repo(tmp_path, "exit 0\n")
    result = cs.apply_and_test(repo, patch_files={"x": "y"}, test_command="bash check.sh")
    assert result.workspace is not None
    assert not Path(result.workspace).exists()  # cleaned up


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
