"""Unit tests for IncidentRemediationWorkflow's non-Temporal-runtime logic:
the two approval-gate signal handlers' first-write-wins idempotency and the
deferral sentinel graph_builder.py relies on. Full @workflow.run orchestration
needs a real WorkflowEnvironment and is exercised in CI with the temporalio
extra installed, not here.

incident_remediation_workflow.py imports the `temporalio` SDK at module level
(needed for its @activity.defn/@workflow.defn decorators), an optional extra
(`pip install sre-agent[temporal]`) not installed by default — skip cleanly
rather than erroring collection when it's absent (same pattern as
test_sandbox_workflow.py).
"""

import asyncio
import uuid

import pytest

pytest.importorskip("temporalio")

from sre_agent.incident_remediation_workflow import (  # noqa: E402
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    DEFERRED_TO_DETERMINISTIC_PIPELINE,
    IncidentRemediationInput,
    IncidentRemediationWorkflow,
    PatchGenerationResult,
    PrResult,
    RemediationVerdict,
    _parse_verification_commands,
    _repo_clone_url,
)
from sre_agent.sandbox_workflow import (  # noqa: E402
    CodeFixVerificationWorkflow,
    CodeFixVerificationInput,
    SandboxRunRequest,
    SandboxRunResult,
    VerdictResult,
)


# Every wait in this file is a hang guard, not a latency assertion: the
# assertions below are all on returned values, never on how long they took.
# So the bound has to clear the worst case, and the worst case is not the
# happy path. A replacement worker runs with `max_cached_workflows=0`, which
# makes it replay the entire workflow history from scratch, and the
# time-skipping environment is a separate process whose RPCs queue behind
# whatever else the suite is doing.
#
# At ten seconds the restart test failed roughly one full-suite run in three
# while passing every time when this file was run on its own. The suite is not
# randomised -- pytest's default file order, no `pytest-randomly` -- so this
# was never an ordering problem: the wait simply expired against an external
# server on a shared VM once two thousand other tests had been through it.
#
# Raising the ceiling costs a passing run nothing -- each of these returns the
# moment the workflow does -- and only changes how long a genuine hang takes
# to report.
_HANG_GUARD_SECONDS = 60


async def _wait_for_phase(handle, phase: str) -> None:
    clock = asyncio.get_running_loop()
    deadline = clock.time() + _HANG_GUARD_SECONDS
    while clock.time() < deadline:
        if await handle.query(IncidentRemediationWorkflow.phase) == phase:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"workflow never reached phase {phase!r}")


def test_deferred_sentinel_is_not_a_known_autonomy_decision():
    from sre_agent.policy_gate import AutonomyDecision

    known = {d.value for d in AutonomyDecision}
    assert DEFERRED_TO_DETERMINISTIC_PIPELINE not in known


def test_incident_remediation_input_defaults():
    params = IncidentRemediationInput(
        incident_id="inc-1",
        organization_id="org-1",
        cluster_id="cluster-1",
        action_type="revert_commit",
        target="checkout-service",
        runner_image="sentinel/runner:latest",
        baseline_command=["python", "baseline.py"],
        candidate_command=["python", "candidate.py"],
        patch="diff --git a/x b/x",
        failure_signature="panic: nil pointer dereference",
    )
    assert params.repo == ""
    assert params.env == {}
    assert params.approval_timeout_seconds == DEFAULT_APPROVAL_TIMEOUT_SECONDS


def test_incident_remediation_input_patch_defaults_to_empty_for_phase_f():
    # Phase F: patch/baseline/candidate are now optional — generate_patch_activity
    # fills them in when the planner only identified a code_fix was needed.
    params = IncidentRemediationInput(
        incident_id="inc-1",
        organization_id="org-1",
        cluster_id="cluster-1",
        action_type="code_fix",
        target="checkout-service",
        runner_image="sentinel/runner:latest",
        failure_signature="panic: nil pointer dereference",
    )
    assert params.patch == ""
    assert params.baseline_command == []
    assert params.candidate_command == []
    assert params.fix_description == ""


# ── Phase F: patch-generation helpers (pure, no I/O) ─────────────────────────
def test_repo_clone_url_without_token():
    assert _repo_clone_url("org/repo") == "https://github.com/org/repo.git"


def test_repo_clone_url_embeds_token():
    url = _repo_clone_url("org/repo", "ghp_secret")
    assert url == "https://ghp_secret@github.com/org/repo.git"


def test_parse_verification_commands_from_actor_output():
    output = (
        "I fixed the nil pointer check in handler.go.\n"
        "BASELINE_COMMAND: go test ./... -run TestHandler\n"
        "CANDIDATE_COMMAND: go test ./... -run TestHandler"
    )
    baseline, candidate = _parse_verification_commands(output, [], [])
    assert baseline == ["go", "test", "./...", "-run", "TestHandler"]
    assert candidate == ["go", "test", "./...", "-run", "TestHandler"]


def test_parse_verification_commands_falls_back_when_absent():
    baseline, candidate = _parse_verification_commands(
        "no markers here", ["fallback-baseline"], ["fallback-candidate"]
    )
    assert baseline == ["fallback-baseline"]
    assert candidate == ["fallback-candidate"]


def test_patch_generation_result_defaults():
    result = PatchGenerationResult("FAILED", detail="no repo")
    assert result.patch == ""
    assert result.baseline_command == []
    assert result.candidate_command == []


def test_start_fix_signal_is_first_write_wins():
    wf = IncidentRemediationWorkflow()
    assert wf.phase() == "AWAITING_START_FIX"

    wf.decide_start_fix(True, "alice")
    assert wf._start_fix_decision is True
    assert wf._start_fix_actor == "alice"

    # A duplicate/late signal must not overwrite the first decision.
    wf.decide_start_fix(False, "bob")
    assert wf._start_fix_decision is True
    assert wf._start_fix_actor == "alice"


def test_start_fix_signal_denial_is_recorded():
    wf = IncidentRemediationWorkflow()
    wf.decide_start_fix(False, "alice")
    assert wf._start_fix_decision is False
    assert wf._start_fix_actor == "alice"


def test_raise_pr_signal_is_first_write_wins():
    wf = IncidentRemediationWorkflow()
    wf.decide_raise_pr(True, "carol")
    wf.decide_raise_pr(False, "dave")
    assert wf._raise_pr_decision is True
    assert wf._raise_pr_actor == "carol"


def test_gates_are_independent():
    wf = IncidentRemediationWorkflow()
    wf.decide_start_fix(True, "alice")
    assert wf._raise_pr_decision is None
    wf.decide_raise_pr(True, "alice")
    assert wf._start_fix_decision is True
    assert wf._raise_pr_decision is True


def test_signal_actor_defaults_to_unknown_when_blank():
    wf = IncidentRemediationWorkflow()
    wf.decide_start_fix(True, "")
    assert wf._start_fix_actor == "unknown"


def test_pr_result_and_verdict_are_plain_dataclasses():
    pr = PrResult("PR_CREATED", "PR created.", pr_url="https://github.com/org/repo/pull/1")
    assert pr.status == "PR_CREATED"
    verdict = RemediationVerdict("PR_CREATED", "done", pr_url=pr.pr_url, verification_status="RESOLVED")
    assert verdict.verification_status == "RESOLVED"


@pytest.mark.asyncio
async def test_full_workflow_passes_both_gates_before_raising_pr():
    """The real workflow sequencing is exercised without external writes."""
    gate_events = []
    cleanup_calls = []
    verdict_calls = []

    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    @activity.defn(name="emit_gate_event_activity")
    async def emit_gate_event(incident_id, workflow_id, gate, status, detail):
        gate_events.append((gate, status))

    @activity.defn(name="open_gate_activity")
    async def open_gate(*args):
        return None

    @activity.defn(name="expire_gate_approval_activity")
    async def expire_gate(*args):
        raise AssertionError("successful path must not expire a gate")

    @activity.defn(name="raise_pr_activity")
    async def raise_pr(params: IncidentRemediationInput):
        return PrResult("PR_CREATED", "created", pr_url="https://example.test/pr/1")

    @activity.defn(name="mark_incident_needs_manual_review_activity")
    async def mark_manual_review(*args):
        raise AssertionError("successful path must not escalate")

    @activity.defn(name="run_baseline_activity")
    async def run_baseline(params: CodeFixVerificationInput):
        return SandboxRunResult("baseline", "FAILED", logs="panic: boom")

    @activity.defn(name="apply_patch_activity")
    async def apply_patch(params: CodeFixVerificationInput):
        return SandboxRunRequest(
            incident_id=params.incident_id,
            organization_id=params.organization_id,
            cluster_id=params.cluster_id,
            workflow_id="child",
            stage="candidate",
            image=params.runner_image,
            command=params.candidate_command,
            env=params.env,
            active_deadline_seconds=params.active_deadline_seconds,
        )

    @activity.defn(name="run_candidate_activity")
    async def run_candidate(request: SandboxRunRequest):
        return SandboxRunResult("candidate", "SUCCEEDED", logs="all clean")

    @activity.defn(name="verify_recovery_activity")
    async def verify_recovery(
        failure_signature: str,
        baseline: SandboxRunResult,
        candidate: SandboxRunResult,
    ):
        return VerdictResult("RESOLVED", "candidate no longer fails")

    @activity.defn(name="emit_verdict_activity")
    async def emit_verdict(
        incident_id: str, workflow_id: str, verdict: VerdictResult, patch: str
    ):
        verdict_calls.append(verdict.status)

    @activity.defn(name="cleanup_activity")
    async def cleanup(*args):
        cleanup_calls.append(args[-1])

    params = IncidentRemediationInput(
        incident_id="incident-e2e",
        organization_id="org-e2e",
        cluster_id="cluster-e2e",
        action_type="code_fix",
        target="checkout-service",
        runner_image="sentinel/runner:latest",
        baseline_command=["python", "baseline.py"],
        candidate_command=["python", "candidate.py"],
        patch="diff --git a/app.py b/app.py",
        failure_signature="panic: boom",
        approval_timeout_seconds=60,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"incident-remediation-e2e-{uuid.uuid4().hex}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[IncidentRemediationWorkflow, CodeFixVerificationWorkflow],
            activities=[
                emit_gate_event,
                open_gate,
                expire_gate,
                raise_pr,
                mark_manual_review,
                run_baseline,
                apply_patch,
                run_candidate,
                verify_recovery,
                emit_verdict,
                cleanup,
            ],
        ):
            handle = await env.client.start_workflow(
                IncidentRemediationWorkflow.run,
                params,
                id=f"incident-remediation-e2e-{uuid.uuid4().hex}",
                task_queue=task_queue,
            )
            await _wait_for_phase(handle, "AWAITING_START_FIX")
            await handle.signal(
                IncidentRemediationWorkflow.decide_start_fix,
                args=[True, "alice"],
            )
            await _wait_for_phase(handle, "AWAITING_RAISE_PR")
            await handle.signal(
                IncidentRemediationWorkflow.decide_raise_pr,
                args=[True, "bob"],
            )
            result = await handle.result()

    assert result.status == "PR_CREATED"
    assert result.verification_status == "RESOLVED"
    assert result.pr_url == "https://example.test/pr/1"
    assert verdict_calls == ["RESOLVED"]
    assert len(cleanup_calls) == 1
    assert gate_events == [
        ("start_fix", "PENDING"),
        ("start_fix", "APPROVED"),
        ("raise_pr", "PENDING"),
        ("raise_pr", "PR_CREATED"),
    ]


@pytest.mark.asyncio
async def test_worker_restart_preserves_pending_gate_and_denial_stops_pipeline():
    """A replacement worker resumes the gate state without opening later gates."""
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    gate_events = []

    @activity.defn(name="emit_gate_event_activity")
    async def emit_gate_event(_incident, _workflow, gate, status, _detail):
        gate_events.append((gate, status))

    @activity.defn(name="open_gate_activity")
    async def open_gate(*_args):
        return None

    @activity.defn(name="expire_gate_approval_activity")
    async def expire_gate(*_args):
        raise AssertionError("restart/denial path must not expire the gate")

    params = IncidentRemediationInput(
        incident_id="incident-restart",
        organization_id="org-restart",
        cluster_id="cluster-restart",
        action_type="code_fix",
        target="checkout-service",
        runner_image="sentinel/runner:latest",
        patch="diff --git a/app.py b/app.py",
        baseline_command=["python", "baseline.py"],
        candidate_command=["python", "candidate.py"],
        failure_signature="panic: restart",
        approval_timeout_seconds=60,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"incident-remediation-restart-{uuid.uuid4().hex}"
        worker_one = Worker(
            env.client,
            task_queue=task_queue,
            workflows=[IncidentRemediationWorkflow],
            activities=[emit_gate_event, open_gate, expire_gate],
            max_cached_workflows=0,
        )
        worker_one_task = asyncio.create_task(worker_one.run())
        handle = await env.client.start_workflow(
            IncidentRemediationWorkflow.run,
            params,
            id=f"incident-remediation-restart-{uuid.uuid4().hex}",
            task_queue=task_queue,
        )
        await _wait_for_phase(handle, "AWAITING_START_FIX")
        await worker_one.shutdown()
        await asyncio.wait_for(worker_one_task, timeout=_HANG_GUARD_SECONDS)

        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[IncidentRemediationWorkflow],
            activities=[emit_gate_event, open_gate, expire_gate],
            max_cached_workflows=0,
        ):
            await handle.signal(
                IncidentRemediationWorkflow.decide_start_fix,
                args=[False, "replacement-worker-operator"],
            )
            result = await asyncio.wait_for(
                handle.result(), timeout=_HANG_GUARD_SECONDS
            )

    assert result.status == "DENIED_START_FIX"
    assert result.verification_status is None
    assert gate_events == [("start_fix", "PENDING"), ("start_fix", "DENIED")]
