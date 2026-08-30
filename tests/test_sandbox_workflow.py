"""Unit tests for the pure log-diff oracle in sandbox_workflow.py.

No Temporal/K8s needed: diff_logs() is a pure function over SandboxRunResult.
sandbox_workflow.py itself imports the `temporalio` SDK at module level (needed
for its @activity.defn/@workflow.defn decorators), which is an optional extra
(`pip install sre-agent[temporal]`) not installed by default — skip cleanly
rather than erroring collection when it's absent.
"""

import pytest

pytest.importorskip("temporalio")

from sre_agent.sandbox_workflow import (  # noqa: E402
    SandboxRunRequest,
    SandboxRunResult,
    compose_candidate_request,
    diff_logs,
)

SIGNATURE = "panic: nil pointer dereference"


def _result(status: str, logs: str = "") -> SandboxRunResult:
    return SandboxRunResult(job_name="sbx-test", status=status, logs=logs)


def test_no_failure_signature_is_inconclusive():
    verdict = diff_logs("", _result("SUCCEEDED", SIGNATURE), _result("SUCCEEDED", ""))
    assert verdict.status == "INCONCLUSIVE"


def test_baseline_that_did_not_terminate_is_inconclusive():
    verdict = diff_logs(SIGNATURE, _result("ERROR"), _result("SUCCEEDED", ""))
    assert verdict.status == "INCONCLUSIVE"


def test_baseline_that_did_not_reproduce_failure_is_inconclusive():
    verdict = diff_logs(SIGNATURE, _result("SUCCEEDED", "all good"), _result("SUCCEEDED", ""))
    assert verdict.status == "INCONCLUSIVE"
    assert "did not reproduce" in verdict.detail


def test_candidate_that_did_not_terminate_is_inconclusive():
    baseline = _result("FAILED", SIGNATURE)
    candidate = _result("REFUSED")
    verdict = diff_logs(SIGNATURE, baseline, candidate)
    assert verdict.status == "INCONCLUSIVE"


def test_candidate_still_failing_is_regressed():
    baseline = _result("FAILED", SIGNATURE)
    candidate = _result("FAILED", f"still broken: {SIGNATURE}")
    verdict = diff_logs(SIGNATURE, baseline, candidate)
    assert verdict.status == "REGRESSED"


def test_candidate_clean_logs_is_resolved():
    baseline = _result("FAILED", SIGNATURE)
    candidate = _result("SUCCEEDED", "all requests handled cleanly")
    verdict = diff_logs(SIGNATURE, baseline, candidate)
    assert verdict.status == "RESOLVED"


def test_compose_candidate_request_carries_patch_via_env_and_swaps_command():
    baseline_request = SandboxRunRequest(
        incident_id="inc-1",
        organization_id="org-1",
        cluster_id="cluster-1",
        workflow_id="wf-1",
        stage="baseline",
        image="sentinel/runner:latest",
        command=["python", "baseline.py"],
        env={"EXISTING": "1"},
        active_deadline_seconds=300,
    )
    candidate_request = compose_candidate_request(
        baseline_request, ["python", "candidate.py"], "diff --git a/x b/x"
    )
    assert candidate_request.stage == "candidate"
    assert candidate_request.command == ["python", "candidate.py"]
    assert candidate_request.env["EXISTING"] == "1"
    assert candidate_request.env["SANDBOX_PATCH_DIFF"] == "diff --git a/x b/x"
    # Baseline request itself must not be mutated.
    assert "SANDBOX_PATCH_DIFF" not in baseline_request.env
