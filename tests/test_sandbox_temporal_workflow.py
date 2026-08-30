"""Workflow-level tests for CodeFixVerificationWorkflow using Temporal's
time-skipping test environment with mocked activities (no real K8s/DB/MCP).

`temporalio` is an optional extra (`pip install sre-agent[temporal]`) not
installed by default — skip cleanly rather than erroring collection when it's
absent.
"""

import uuid

import pytest

pytest.importorskip("temporalio")

from temporalio import activity  # noqa: E402
from temporalio.client import WorkflowFailureError  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from sre_agent.sandbox_workflow import (  # noqa: E402
    CodeFixVerificationInput,
    CodeFixVerificationWorkflow,
    SandboxRunRequest,
    SandboxRunResult,
    VerdictResult,
)

SIGNATURE = "panic: nil pointer dereference"


def _input() -> CodeFixVerificationInput:
    return CodeFixVerificationInput(
        incident_id="incident-1",
        organization_id="org-1",
        cluster_id="cluster-1",
        runner_image="sentinel/runner:latest",
        baseline_command=["python", "baseline.py"],
        candidate_command=["python", "candidate.py"],
        patch="diff --git a/x b/x",
        failure_signature=SIGNATURE,
    )


async def _run_workflow(baseline_result, candidate_result, cleanup_calls, verdict_calls):
    @activity.defn(name="run_baseline_activity")
    async def fake_baseline(params: CodeFixVerificationInput) -> SandboxRunResult:
        return baseline_result

    @activity.defn(name="apply_patch_activity")
    async def fake_apply_patch(params: CodeFixVerificationInput) -> SandboxRunRequest:
        return SandboxRunRequest(
            incident_id=params.incident_id,
            organization_id=params.organization_id,
            cluster_id=params.cluster_id,
            workflow_id="wf-test",
            stage="candidate",
            image=params.runner_image,
            command=params.candidate_command,
            env={},
            active_deadline_seconds=params.active_deadline_seconds,
        )

    @activity.defn(name="run_candidate_activity")
    async def fake_candidate(request: SandboxRunRequest) -> SandboxRunResult:
        return candidate_result

    @activity.defn(name="verify_recovery_activity")
    async def fake_verify(failure_signature: str, baseline: SandboxRunResult, candidate: SandboxRunResult) -> VerdictResult:
        from sre_agent.sandbox_workflow import diff_logs

        return diff_logs(failure_signature, baseline, candidate)

    @activity.defn(name="emit_verdict_activity")
    async def fake_emit_verdict(incident_id: str, workflow_id: str, verdict: VerdictResult, patch: str) -> None:
        verdict_calls.append(verdict)

    @activity.defn(name="cleanup_activity")
    async def fake_cleanup(incident_id: str, organization_id: str, cluster_id: str, workflow_id: str) -> None:
        cleanup_calls.append(workflow_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[CodeFixVerificationWorkflow],
            activities=[
                fake_baseline,
                fake_apply_patch,
                fake_candidate,
                fake_verify,
                fake_emit_verdict,
                fake_cleanup,
            ],
        ):
            return await env.client.execute_workflow(
                CodeFixVerificationWorkflow.run,
                _input(),
                id=f"wf-{uuid.uuid4().hex}",
                task_queue=task_queue,
            )


@pytest.mark.asyncio
async def test_workflow_resolves_when_candidate_logs_are_clean():
    cleanup_calls, verdict_calls = [], []
    baseline = SandboxRunResult("sbx-baseline", "FAILED", logs=SIGNATURE)
    candidate = SandboxRunResult("sbx-candidate", "SUCCEEDED", logs="all clean")

    verdict = await _run_workflow(baseline, candidate, cleanup_calls, verdict_calls)

    assert verdict.status == "RESOLVED"
    assert len(cleanup_calls) == 1
    assert len(verdict_calls) == 1
    assert verdict_calls[0].status == "RESOLVED"


@pytest.mark.asyncio
async def test_workflow_regresses_when_candidate_still_fails():
    cleanup_calls, verdict_calls = [], []
    baseline = SandboxRunResult("sbx-baseline", "FAILED", logs=SIGNATURE)
    candidate = SandboxRunResult("sbx-candidate", "FAILED", logs=f"still: {SIGNATURE}")

    verdict = await _run_workflow(baseline, candidate, cleanup_calls, verdict_calls)

    assert verdict.status == "REGRESSED"
    assert len(cleanup_calls) == 1


@pytest.mark.asyncio
async def test_workflow_is_inconclusive_when_baseline_never_reproduces_failure():
    cleanup_calls, verdict_calls = [], []
    baseline = SandboxRunResult("sbx-baseline", "SUCCEEDED", logs="nothing wrong here")
    candidate = SandboxRunResult("sbx-candidate", "SUCCEEDED", logs="nothing wrong here")

    verdict = await _run_workflow(baseline, candidate, cleanup_calls, verdict_calls)

    assert verdict.status == "INCONCLUSIVE"
    # cleanup and verdict-emission must still run even on an inconclusive result.
    assert len(cleanup_calls) == 1
    assert len(verdict_calls) == 1


@pytest.mark.asyncio
async def test_workflow_runs_cleanup_even_when_baseline_activity_raises():
    @activity.defn(name="run_baseline_activity")
    async def failing_baseline(params: CodeFixVerificationInput) -> SandboxRunResult:
        raise RuntimeError("sandbox provisioning refused")

    @activity.defn(name="apply_patch_activity")
    async def unused_apply_patch(params: CodeFixVerificationInput) -> SandboxRunRequest:
        raise AssertionError("must not run when baseline fails")

    @activity.defn(name="run_candidate_activity")
    async def unused_candidate(request: SandboxRunRequest) -> SandboxRunResult:
        raise AssertionError("must not run when baseline fails")

    @activity.defn(name="verify_recovery_activity")
    async def unused_verify(failure_signature: str, baseline: SandboxRunResult, candidate: SandboxRunResult) -> VerdictResult:
        raise AssertionError("must not run when baseline fails")

    @activity.defn(name="emit_verdict_activity")
    async def unused_emit_verdict(incident_id: str, workflow_id: str, verdict: VerdictResult, patch: str) -> None:
        raise AssertionError("must not run when baseline activity itself raises")

    cleanup_calls = []

    @activity.defn(name="cleanup_activity")
    async def fake_cleanup(incident_id: str, organization_id: str, cluster_id: str, workflow_id: str) -> None:
        cleanup_calls.append(workflow_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[CodeFixVerificationWorkflow],
            activities=[
                failing_baseline,
                unused_apply_patch,
                unused_candidate,
                unused_verify,
                unused_emit_verdict,
                fake_cleanup,
            ],
        ):
            with pytest.raises(WorkflowFailureError):
                await env.client.execute_workflow(
                    CodeFixVerificationWorkflow.run,
                    _input(),
                    id=f"wf-{uuid.uuid4().hex}",
                    task_queue=task_queue,
                    execution_timeout=None,
                    retry_policy=None,
                )

    assert len(cleanup_calls) == 1
