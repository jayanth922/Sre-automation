#!/usr/bin/env python3
"""
Code-fix sandbox — apply an LLM-suggested code change and TEST it in isolation.

This is the "Devon" mechanism: take a proposed code fix (a patch or new file
contents), apply it to a throwaway copy of the repo, run the repo's own tests,
and report pass/fail — so a code fix is *validated* before it's recommended to a
human. It is the code-change analogue of metric verification for infra fixes.

Posture (per the safe default): we **sandbox-test and recommend** the fix; we do
not auto-merge it. The human applies the verified diff.

Security note: this runs code in a temp-dir workspace with a timeout — a
demonstration of the mechanism. Production isolation for untrusted LLM code needs
a microVM/container sandbox (Firecracker / E2B), which is the sandboxing upgrade
tracked in the competitive audit.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CodeFixResult:
    status: str                 # TESTED_PASS | TESTED_FAIL | APPLIED_NO_TESTS | ERROR
    applied: bool
    tests_passed: Optional[bool]
    test_command: Optional[str]
    diff: str = ""
    output: str = ""
    detail: str = ""
    workspace: Optional[str] = None


def _run(cmd: List[str] | str, cwd: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, shell=isinstance(cmd, str),
        capture_output=True, text=True, timeout=timeout,
    )


def _materialize_repo(repo_source: str, workspace: str) -> str:
    """Copy a local repo (or shallow-clone a URL) into the workspace. Returns repo dir."""
    dest = os.path.join(workspace, "repo")
    if os.path.isdir(repo_source):
        shutil.copytree(repo_source, dest, symlinks=True)
    else:  # treat as a git URL
        _run(["git", "clone", "--depth", "1", repo_source, dest], cwd=workspace, timeout=180)
    return dest


def apply_and_test(
    repo_source: str,
    patch: Optional[str] = None,
    patch_files: Optional[Dict[str, str]] = None,
    test_command: Optional[str] = None,
    base_dir: Optional[str] = None,
    keep_workspace: bool = False,
    timeout: int = 180,
) -> CodeFixResult:
    """Apply a code change to an isolated repo copy and run its tests.

    Args:
        repo_source: local repo path (copied) or a git URL (shallow-cloned).
        patch: a unified diff applied with ``git apply``.
        patch_files: mapping of relative path → new file contents (written directly).
        test_command: shell command whose exit code decides pass/fail.
    """
    base_dir = base_dir or os.getenv("CODE_SANDBOX_DIR") or tempfile.gettempdir()
    workspace = tempfile.mkdtemp(prefix="codefix-", dir=base_dir)

    try:
        repo = _materialize_repo(repo_source, workspace)

        applied = False
        if patch:
            patch_path = os.path.join(workspace, "fix.patch")
            with open(patch_path, "w", encoding="utf-8") as f:
                f.write(patch)
            proc = _run(["git", "apply", patch_path], cwd=repo, timeout=60)
            if proc.returncode != 0:
                return CodeFixResult("ERROR", False, None, test_command,
                                     detail=f"git apply failed: {proc.stderr.strip()}", workspace=workspace)
            applied = True
        if patch_files:
            for rel, content in patch_files.items():
                target = os.path.join(repo, rel)
                os.makedirs(os.path.dirname(target) or repo, exist_ok=True)
                with open(target, "w", encoding="utf-8") as f:
                    f.write(content)
            applied = True

        diff = ""
        d = _run(["git", "diff"], cwd=repo, timeout=30)
        if d.returncode == 0:
            diff = d.stdout[-8000:]

        if not test_command:
            return CodeFixResult("APPLIED_NO_TESTS", applied, None, None, diff=diff,
                                 detail="Change applied; no test_command provided.", workspace=workspace)

        try:
            t = _run(test_command, cwd=repo, timeout=timeout)
        except subprocess.TimeoutExpired:
            return CodeFixResult("TESTED_FAIL", applied, False, test_command, diff=diff,
                                 output=f"timeout after {timeout}s", workspace=workspace)

        passed = t.returncode == 0
        return CodeFixResult(
            "TESTED_PASS" if passed else "TESTED_FAIL",
            applied, passed, test_command, diff=diff,
            output=(t.stdout + t.stderr)[-4000:],
            detail=f"tests {'passed' if passed else 'failed'} (exit {t.returncode})",
            workspace=workspace,
        )
    except Exception as e:
        return CodeFixResult("ERROR", False, None, test_command, detail=str(e), workspace=workspace)
    finally:
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)


def sandbox_backend() -> str:
    """local (temp dir + subprocess) | e2b (microVM isolation)."""
    return os.getenv("SANDBOX_BACKEND", "local").lower()


def apply_and_test_e2b(
    repo_source: str,
    patch: Optional[str] = None,
    patch_files: Optional[Dict[str, str]] = None,
    test_command: Optional[str] = None,
    timeout: int = 180,
) -> CodeFixResult:
    """Real-isolation backend (competitive-audit upgrade #5): run the fix in an
    E2B microVM instead of a local temp dir. Requires ``e2b`` + ``E2B_API_KEY``.

    Verified E2B API: ``Sandbox.create()`` context manager, ``commands.run(cmd)``
    (→ ``.stdout``/``.stderr``/``.exit_code``), ``files.write(path, content)``.
    """
    try:
        from e2b import Sandbox  # lazy; optional dep
    except Exception as e:
        raise RuntimeError(
            "E2B backend requested but e2b not installed. Install with: "
            "pip install e2b  (and set E2B_API_KEY)"
        ) from e

    repo = "/home/user/repo"
    with Sandbox.create() as sbx:  # pragma: no cover - requires E2B + key
        if os.path.isdir(repo_source):
            for root, _dirs, files in os.walk(repo_source):
                if ".git" in root.split(os.sep):
                    continue
                for fn in files:
                    local = os.path.join(root, fn)
                    rel = os.path.relpath(local, repo_source)
                    try:
                        sbx.files.write(f"{repo}/{rel}", open(local, "r", encoding="utf-8").read())
                    except Exception:
                        pass  # skip binaries
            sbx.commands.run(f"cd {repo} && git init -q && git add -A && git commit -qm base || true")
        else:
            sbx.commands.run(f"git clone --depth 1 {repo_source} {repo}")

        applied = False
        if patch_files:
            for rel, content in patch_files.items():
                sbx.files.write(f"{repo}/{rel}", content)
            applied = True
        if patch:
            sbx.files.write(f"{repo}/fix.patch", patch)
            sbx.commands.run(f"cd {repo} && git apply fix.patch")
            applied = True

        diff = sbx.commands.run(f"cd {repo} && git diff").stdout[-8000:]
        if not test_command:
            return CodeFixResult("APPLIED_NO_TESTS", applied, None, None, diff=diff, workspace="e2b")

        res = sbx.commands.run(f"cd {repo} && {test_command}", timeout=timeout)
        passed = getattr(res, "exit_code", 1) == 0
        return CodeFixResult(
            "TESTED_PASS" if passed else "TESTED_FAIL", applied, passed, test_command,
            diff=diff, output=(res.stdout + res.stderr)[-4000:], workspace="e2b",
        )


def run_code_fix(repo_source: str, **kwargs) -> CodeFixResult:
    """Dispatch to the configured sandbox backend (local default, e2b for isolation)."""
    if sandbox_backend() == "e2b":
        return apply_and_test_e2b(repo_source, **{k: v for k, v in kwargs.items() if k != "keep_workspace"})
    return apply_and_test(repo_source, **kwargs)
