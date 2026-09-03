#!/usr/bin/env python3
"""
CodeFixVerificationWorkflow — the log-based recovery oracle for AI-generated
code fixes.

This workflow answers exactly one question: *did applying this patch restore
the system to the state its logs showed before the incident?* It does this by
replaying the same log-producing workload twice inside an isolated,
guardrailed K8s Job (see edge_mcp_servers/mcp_servers/sandbox_real) — once
unpatched (the baseline, which must reproduce the original failure signature
or the run is INCONCLUSIVE) and once with the candidate patch applied — and
diffing the two logs. It is not a general-purpose code interpreter or test
runner; every stage exists only to produce log evidence for that diff.

Pipeline (see docs/... plan for the full diagram):
    run_baseline_activity   -> reproduce the original failure, or INCONCLUSIVE
    apply_patch_activity    -> compose the candidate run's command/env (pure)
    run_candidate_activity  -> re-run with the patch applied
    verify_recovery_activity -> pure log diff -> RESOLVED / REGRESSED / INCONCLUSIVE
    emit_verdict_activity   -> post the verdict into the incident timeline
    cleanup_activity        -> guaranteed teardown (workflow try/finally)

Every sandbox mutation (Job create/teardown) goes through
`sandbox_gateway.authorize_and_provision_sandbox` — activities never call the
sandbox MCP tools directly.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_POLL_TIMEOUT_SECONDS = 900
TERMINAL_JOB_STATUSES = {"SUCCEEDED", "FAILED", "NOT_FOUND", "ERROR", "REFUSED"}


# ── Data contracts (JSON-serializable — cross the Temporal wire) ──────────────


@dataclass
class CodeFixVerificationInput:
    incident_id: str
    organization_id: str
    cluster_id: str
    runner_image: str
    baseline_command: List[str]
    candidate_command: List[str]
    patch: str
    failure_signature: str
    env: Dict[str, str] = field(default_factory=dict)
    active_deadline_seconds: int = 300


@dataclass
class SandboxRunRequest:
    incident_id: str
    organization_id: str
    cluster_id: str
    workflow_id: str
    stage: str  # "baseline" | "candidate"
    image: str
    command: List[str]
    env: Dict[str, str]
    active_deadline_seconds: int


@dataclass
class SandboxRunResult:
    job_name: str
    status: str  # SUCCEEDED | FAILED | ERROR | REFUSED | NOT_FOUND
    logs: str = ""
    detail: str = ""


@dataclass
class VerdictResult:
    status: str  # RESOLVED | REGRESSED | INCONCLUSIVE
    detail: str


# ── Pure logic (no I/O — unit-testable directly) ──────────────────────────────


def diff_logs(
    failure_signature: str, baseline: SandboxRunResult, candidate: SandboxRunResult
) -> VerdictResult:
    """The actual recovery oracle: did the candidate's logs stop showing the
    failure signature that the baseline reproduced?

    INCONCLUSIVE whenever the baseline itself didn't reproduce the original
    failure (or errored) — a candidate can't be judged against a baseline that
    never proved the bug existed in the sandbox in the first place.
    """
    signature = (failure_signature or "").strip()
    if not signature:
        return VerdictResult("INCONCLUSIVE", "No failure signature was provided to verify against.")
    if baseline.status != "SUCCEEDED" and baseline.status != "FAILED":
        return VerdictResult(
            "INCONCLUSIVE", f"Baseline sandbox run did not complete (status={baseline.status})."
        )
    if signature not in (baseline.logs or ""):
        return VerdictResult(
            "INCONCLUSIVE",
            "Baseline run did not reproduce the original failure signature in the sandbox; "
            "cannot verify the candidate against an unreproduced failure.",
        )
    if candidate.status not in ("SUCCEEDED", "FAILED"):
        return VerdictResult(
            "INCONCLUSIVE", f"Candidate sandbox run did not complete (status={candidate.status})."
        )
    if signature in (candidate.logs or ""):
        return VerdictResult(
            "REGRESSED", "Candidate logs still show the original failure signature after the patch."
        )
    return VerdictResult(
        "RESOLVED", "Candidate logs no longer show the failure signature the baseline reproduced."
    )


def compose_candidate_request(
    baseline_request: SandboxRunRequest, candidate_command: List[str], patch: str
) -> SandboxRunRequest:
    """Pure: build the candidate stage's request, with the patch handed to the
    runner image via env. The runner image owns applying it (e.g. `git apply`)
    before executing candidate_command — this workflow only ever supplies log
    evidence and a patch, never executes untrusted code itself.
    """
    env = dict(baseline_request.env)
    env["SANDBOX_PATCH_DIFF"] = patch
    return SandboxRunRequest(
        incident_id=baseline_request.incident_id,
        organization_id=baseline_request.organization_id,
        cluster_id=baseline_request.cluster_id,
        workflow_id=baseline_request.workflow_id,
        stage="candidate",
        image=baseline_request.image,
        command=candidate_command,
        env=env,
        active_deadline_seconds=baseline_request.active_deadline_seconds,
    )


def _job_name(workflow_id: str, stage: str) -> str:
    digest = _uuid.uuid5(_uuid.NAMESPACE_URL, f"{workflow_id}:{stage}").hex[:16]
    return f"sbx-{digest}-{stage}"[:63]


# ── Activities (all I/O goes through sandbox_gateway) ─────────────────────────


async def _execution_context_for(organization_id: str, cluster_id: str):
    from backend import crud, database

    from .execution_context import ExecutionContext

    async with database.AsyncSessionLocal() as db:
        cluster = await crud.get_cluster_by_id(db, _uuid.UUID(cluster_id))
    if cluster is None or str(cluster.org_id) != str(organization_id):
        raise RuntimeError(f"Cluster {cluster_id} not found for organization {organization_id}")
    return ExecutionContext.from_cluster(cluster)


async def _run_sandbox_stage(request: SandboxRunRequest) -> SandboxRunResult:
    """Provision one ephemeral Job, poll it to a terminal status, fetch logs,
    and tear it down. Each K8s Job is one-shot, so "provisioning" a stage IS
    running it to completion — there is no longer-lived sandbox to reuse
    across stages.
    """
    import asyncio

    from .executor import build_sandbox_tool_caller
    from .incident_timeline import emit_trace_step_event, truncate_for_timeline
    from .sandbox_gateway import SandboxGateContext, authorize_and_provision_sandbox

    gate_context = SandboxGateContext(
        incident_id=request.incident_id,
        organization_id=request.organization_id,
        cluster_id=request.cluster_id,
        actor="sandbox-workflow",
    )
    execution_context = await _execution_context_for(request.organization_id, request.cluster_id)
    tool_caller = await build_sandbox_tool_caller(execution_context)

    job_name = _job_name(request.workflow_id, request.stage)

    await emit_trace_step_event(
        request.incident_id, request.workflow_id,
        source="sandbox_workflow", step=f"sandbox_{request.stage}", status="STARTED",
        detail=f"Provisioning {job_name}", job_name=job_name,
    )

    provision = await authorize_and_provision_sandbox(
        gate_context,
        execution_context,
        tool_caller,
        "sandbox_provision",
        {
            "image": request.image,
            "command": request.command,
            "job_name": job_name,
            "env": request.env,
            "active_deadline_seconds": request.active_deadline_seconds,
            "dry_run": False,
        },
        idempotency_key=f"sandbox-provision:{job_name}",
    )
    if str(provision.get("status")).upper() not in ("OK", "SKIPPED"):
        detail = str(provision.get("reason") or provision)
        await emit_trace_step_event(
            request.incident_id, request.workflow_id,
            source="sandbox_workflow", step=f"sandbox_{request.stage}", status="REFUSED",
            detail=detail, job_name=job_name,
        )
        return SandboxRunResult(job_name, "REFUSED", detail=detail)

    deadline = timedelta(seconds=request.active_deadline_seconds + 60).total_seconds()
    elapsed = 0.0
    status = "PENDING"
    while elapsed < deadline:
        activity.heartbeat({"job_name": job_name, "stage": request.stage, "elapsed": elapsed})
        result = await authorize_and_provision_sandbox(
            gate_context,
            execution_context,
            tool_caller,
            "sandbox_status",
            {"job_name": job_name},
            idempotency_key=f"sandbox-status:{job_name}:{_uuid.uuid4().hex}",
        )
        status = str(result.get("status", "UNKNOWN")).upper()
        if status in TERMINAL_JOB_STATUSES:
            break
        await asyncio.sleep(DEFAULT_POLL_INTERVAL_SECONDS)
        elapsed += DEFAULT_POLL_INTERVAL_SECONDS

    logs = ""
    if status in ("SUCCEEDED", "FAILED"):
        logs_result = await authorize_and_provision_sandbox(
            gate_context,
            execution_context,
            tool_caller,
            "sandbox_logs",
            {"job_name": job_name},
            idempotency_key=f"sandbox-logs:{job_name}:{_uuid.uuid4().hex}",
        )
        logs = str(logs_result.get("logs", ""))

    try:
        await authorize_and_provision_sandbox(
            gate_context,
            execution_context,
            tool_caller,
            "sandbox_teardown",
            {"job_name": job_name},
            idempotency_key=f"sandbox-teardown:{job_name}:{_uuid.uuid4().hex}",
        )
    except Exception as exc:
        logger.warning("Sandbox teardown failed for %s (non-fatal, cleanup_activity retries): %s", job_name, exc)

    await emit_trace_step_event(
        request.incident_id, request.workflow_id,
        source="sandbox_workflow", step=f"sandbox_{request.stage}", status=status,
        detail=f"{job_name} finished: {status}", job_name=job_name,
        logs=truncate_for_timeline(logs) if logs else "",
    )
    return SandboxRunResult(job_name, status, logs=logs)


@activity.defn
async def run_baseline_activity(params: CodeFixVerificationInput) -> SandboxRunResult:
    request = SandboxRunRequest(
        incident_id=params.incident_id,
        organization_id=params.organization_id,
        cluster_id=params.cluster_id,
        workflow_id=activity.info().workflow_id,
        stage="baseline",
        image=params.runner_image,
        command=params.baseline_command,
        env=params.env,
        active_deadline_seconds=params.active_deadline_seconds,
    )
    return await _run_sandbox_stage(request)


@activity.defn
async def apply_patch_activity(params: CodeFixVerificationInput) -> SandboxRunRequest:
    baseline_request = SandboxRunRequest(
        incident_id=params.incident_id,
        organization_id=params.organization_id,
        cluster_id=params.cluster_id,
        workflow_id=activity.info().workflow_id,
        stage="baseline",
        image=params.runner_image,
        command=params.baseline_command,
        env=params.env,
        active_deadline_seconds=params.active_deadline_seconds,
    )
    return compose_candidate_request(baseline_request, params.candidate_command, params.patch)


@activity.defn
async def run_candidate_activity(request: SandboxRunRequest) -> SandboxRunResult:
    return await _run_sandbox_stage(request)


@activity.defn
async def verify_recovery_activity(
    failure_signature: str, baseline: SandboxRunResult, candidate: SandboxRunResult
) -> VerdictResult:
    return diff_logs(failure_signature, baseline, candidate)


@activity.defn
async def emit_verdict_activity(
    incident_id: str, workflow_id: str, verdict: VerdictResult, patch: str
) -> None:
    from .incident_timeline import emit_timeline_event

    emoji = {"RESOLVED": "✅", "REGRESSED": "⚠️", "INCONCLUSIVE": "ℹ️"}.get(verdict.status, "")
    await emit_timeline_event(
        incident_id,
        event_type="act",
        speaker_role="executor",
        title="Sandbox verification",
        content=f"{emoji} Sandbox verification **{verdict.status}** — {verdict.detail}",
        payload={
            "source": "sandbox_workflow",
            "code_fix": {
                "status": verdict.status,
                "detail": verdict.detail,
                "diff": patch,
                "workflow_id": workflow_id,
            },
        },
    )


@activity.defn
async def cleanup_activity(incident_id: str, organization_id: str, cluster_id: str, workflow_id: str) -> None:
    """Guaranteed best-effort teardown of both stages' Jobs, even if an earlier
    activity failed partway through. Idempotent — sandbox_real's teardown tool
    treats a missing Job as success.
    """
    from .executor import build_sandbox_tool_caller
    from .sandbox_gateway import SandboxGateContext, authorize_and_provision_sandbox

    gate_context = SandboxGateContext(
        incident_id=incident_id, organization_id=organization_id, cluster_id=cluster_id, actor="sandbox-workflow"
    )
    try:
        execution_context = await _execution_context_for(organization_id, cluster_id)
        tool_caller = await build_sandbox_tool_caller(execution_context)
    except Exception as exc:
        logger.warning("cleanup_activity could not build execution context (non-fatal): %s", exc)
        return

    for stage in ("baseline", "candidate"):
        job_name = _job_name(workflow_id, stage)
        try:
            await authorize_and_provision_sandbox(
                gate_context,
                execution_context,
                tool_caller,
                "sandbox_teardown",
                {"job_name": job_name},
                idempotency_key=f"sandbox-cleanup:{job_name}:{_uuid.uuid4().hex}",
            )
        except Exception as exc:
            logger.warning("cleanup_activity teardown failed for %s (non-fatal): %s", job_name, exc)


# ── Workflow ───────────────────────────────────────────────────────────────


ACTIVITY_TIMEOUT = timedelta(minutes=20)
RETRY_POLICY_ATTEMPTS = 3
# Bounded so a persistently-broken sandbox run (bad image, malformed command)
# fails into an INCONCLUSIVE verdict within a predictable time instead of
# Temporal's unbounded-by-default activity retries stalling the workflow (and
# its guaranteed cleanup) indefinitely.
DEFAULT_RETRY_POLICY = RetryPolicy(maximum_attempts=RETRY_POLICY_ATTEMPTS)


@workflow.defn
class CodeFixVerificationWorkflow:
    @workflow.run
    async def run(self, params: CodeFixVerificationInput) -> VerdictResult:
        workflow_id = workflow.info().workflow_id
        try:
            baseline = await workflow.execute_activity(
                run_baseline_activity,
                params,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=DEFAULT_RETRY_POLICY,
            )

            if baseline.status != "SUCCEEDED" and baseline.status != "FAILED":
                verdict = VerdictResult(
                    "INCONCLUSIVE", f"Baseline sandbox run did not complete (status={baseline.status})."
                )
            else:
                candidate_request = await workflow.execute_activity(
                    apply_patch_activity,
                    params,
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=DEFAULT_RETRY_POLICY,
                )
                candidate = await workflow.execute_activity(
                    run_candidate_activity,
                    candidate_request,
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=DEFAULT_RETRY_POLICY,
                )
                verdict = await workflow.execute_activity(
                    verify_recovery_activity,
                    args=[params.failure_signature, baseline, candidate],
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=DEFAULT_RETRY_POLICY,
                )

            await workflow.execute_activity(
                emit_verdict_activity,
                args=[params.incident_id, workflow_id, verdict, params.patch],
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            return verdict
        finally:
            await workflow.execute_activity(
                cleanup_activity,
                args=[params.incident_id, params.organization_id, params.cluster_id, workflow_id],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=DEFAULT_RETRY_POLICY,
            )


ACTIVITIES = [
    run_baseline_activity,
    apply_patch_activity,
    run_candidate_activity,
    verify_recovery_activity,
    emit_verdict_activity,
    cleanup_activity,
]
