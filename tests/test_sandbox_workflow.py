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


def test_candidate_failed_without_signature_is_regressed_not_resolved():
    # The old error is gone, but the run still failed: a different failure is not
    # recovery.
    baseline = _result("FAILED", SIGNATURE)
    candidate = _result("FAILED", "ImportError: cannot import name 'retry'")
    verdict = diff_logs(SIGNATURE, baseline, candidate)
    assert verdict.status == "REGRESSED"
    assert "different failure" in verdict.detail


def test_verdict_carries_bounded_evidence_for_both_runs():
    noise = [f"request {i} ok" for i in range(50)]
    baseline_logs = "\n".join(noise[:20] + [SIGNATURE] + noise[20:])
    candidate_logs = "\n".join(noise + ["x" * 1000])
    verdict = diff_logs(
        SIGNATURE, _result("FAILED", baseline_logs), _result("SUCCEEDED", candidate_logs)
    )
    assert verdict.status == "RESOLVED"
    base = verdict.evidence["baseline"]
    assert base["signature_found"] is True
    assert base["signature_line"] == 21
    assert base["excerpt_kind"] == "signature"
    assert base["excerpt_start_line"] == 19
    assert len(base["excerpt"]) == 5
    assert SIGNATURE in base["excerpt"][2]
    cand = verdict.evidence["candidate"]
    assert cand["signature_found"] is False
    assert cand["signature_line"] is None
    assert cand["excerpt_kind"] == "tail"
    assert cand["total_lines"] == 51
    assert len(cand["excerpt"]) == 6
    assert len(cand["excerpt"][-1]) == 240


def test_inconclusive_verdicts_still_carry_evidence():
    verdict = diff_logs(SIGNATURE, _result("FAILED", "clean"), _result("SUCCEEDED", "clean"))
    assert verdict.status == "INCONCLUSIVE"
    assert verdict.evidence["baseline"]["signature_found"] is False


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
