#!/usr/bin/env python3
"""
IncidentRemediationWorkflow — the deterministic remediation pipeline
(docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md, Phase B).

Per the user's explicit requirement: an SRE can't fully rely on AI, so the
*sequencing* of detect → fix → verify → PR is a fixed Temporal state machine,
not an LLM decision. The only AI involvement is the patch itself (proposed
upstream, before this workflow ever starts) and only two things ever require
a human: starting the fix, and raising the PR. Both are hard gates — this
workflow blocks on a signal for each and takes no default-approve path.

Pipeline:
    1. emit_gate_event_activity("start_fix", PENDING)
    2. wait_condition on decide_start_fix signal (bounded by approval_timeout_seconds)
       - denied/expired -> terminal, no verification, no PR
    3. CodeFixVerificationWorkflow (EXISTING, unmodified) as a child workflow —
       baseline vs. patched sandbox log diff, same oracle Phase 5A already reuses.
    4. verdict != RESOLVED -> terminal (escalate via timeline event), no PR
    5. emit_gate_event_activity("raise_pr", PENDING)
    6. wait_condition on decide_raise_pr signal
       - denied/expired -> terminal, patch verified but not shipped
    7. raise_pr_activity -> edge_mcp_servers github_exec's create_fix_pr tool
       (Phase C, folded into this pass — mirrors create_revert_pr's pattern)

One workflow instance per incident/bundle (docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md's
per-issue-isolated-PR requirement) — the caller is responsible for keying
workflow_id off the incident/bundle, not raw alerts.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from .sandbox_workflow import CodeFixVerificationInput, CodeFixVerificationWorkflow, VerdictResult

logger = logging.getLogger(__name__)

# Decision value the old single-gate live-execution path in graph_builder.py
# mutates a *copy* of one action_report to, so its own `decision not in
# {"autonomous", "requires_approval"}` skip-logic (act_phase.py::execute_autonomous_live)
# naturally leaves that one action alone — it is now owned end-to-end by this
# workflow's own two gates instead of the old single approval.
DEFERRED_TO_DETERMINISTIC_PIPELINE = "deferred_to_deterministic_pipeline"

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 1800  # matches the existing ~30min approval-expiry convention

ACTIVITY_TIMEOUT = timedelta(minutes=2)
RETRY_POLICY_ATTEMPTS = 3
DEFAULT_RETRY_POLICY = RetryPolicy(maximum_attempts=RETRY_POLICY_ATTEMPTS)


# ── Data contracts (JSON-serializable — cross the Temporal wire) ──────────────


@dataclass
class IncidentRemediationInput:
    incident_id: str
    organization_id: str
    cluster_id: str
    action_type: str
    target: str
    runner_image: str
    baseline_command: List[str]
    candidate_command: List[str]
    patch: str
    failure_signature: str
    repo: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    approval_timeout_seconds: int = DEFAULT_APPROVAL_TIMEOUT_SECONDS


@dataclass
class PrResult:
    status: str  # "PR_CREATED" | "PR_CREATION_FAILED"
    detail: str
    pr_url: Optional[str] = None


@dataclass
class RemediationVerdict:
    status: str
    # "DENIED_START_FIX" | "EXPIRED_START_FIX" | "VERIFICATION_REGRESSED" |
    # "VERIFICATION_INCONCLUSIVE" | "DENIED_RAISE_PR" | "EXPIRED_RAISE_PR" |
    # "PR_CREATED" | "PR_CREATION_FAILED"
    detail: str
    pr_url: Optional[str] = None
    verification_status: Optional[str] = None


# ── Activities ──────────────────────────────────────────────────────────────


async def _execution_context_for(organization_id: str, cluster_id: str):
    """Independent of sandbox_workflow's identical-shaped helper: activities in
    different workflow modules may run on different workers, so each stays
    self-sufficient rather than importing another module's private function.
    """
    import uuid as _uuid

    from backend import crud, database

    from .execution_context import ExecutionContext

    async with database.AsyncSessionLocal() as db:
        cluster = await crud.get_cluster_by_id(db, _uuid.UUID(cluster_id))
    if cluster is None or str(cluster.org_id) != str(organization_id):
        raise RuntimeError(f"Cluster {cluster_id} not found for organization {organization_id}")
    return ExecutionContext.from_cluster(cluster)


@activity.defn
async def emit_gate_event_activity(
    incident_id: str, workflow_id: str, gate: str, status: str, detail: str
) -> None:
    from .incident_timeline import emit_timeline_event

    emoji = {"PENDING": "⏳", "APPROVED": "✅", "DENIED": "🚫", "EXPIRED": "⌛", "ESCALATED": "🆘"}.get(status, "")
    await emit_timeline_event(
        incident_id,
        event_type="act",
        speaker_role="executor",
        title=f"Remediation gate: {gate}",
        content=f"{emoji} {gate} — **{status}**: {detail}",
        payload={
            "source": "incident_remediation_workflow",
            "gate": gate,
            "status": status,
            "detail": detail,
            "workflow_id": workflow_id,
        },
    )


@activity.defn
async def raise_pr_activity(params: IncidentRemediationInput) -> PrResult:
    """Phase C, folded into Phase B: mirrors create_revert_pr's pattern
    (edge_mcp_servers/mcp_servers/github_exec/server.py) but for an arbitrary
    AI-generated, sandbox-verified patch instead of a revert.
    """
    from .executor import _structured_payload, build_github_exec_tool_caller

    try:
        ctx = await _execution_context_for(params.organization_id, params.cluster_id)
        github_caller = await build_github_exec_tool_caller(ctx)
    except Exception as exc:
        return PrResult("PR_CREATION_FAILED", f"Could not build github-exec tool caller: {exc}")

    branch_name = f"sentinel-fix/{params.incident_id[:8]}"
    title = f"Sentinel: automated fix for incident {params.incident_id}"
    body = (
        f"Automated fix verified in an isolated sandbox "
        f"(failure signature: `{params.failure_signature}`).\n\n"
        "Generated and verified by Sentinel's deterministic remediation "
        "pipeline (docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md). Both "
        "approval gates (start-fix, raise-PR) were human-approved before "
        "this PR was opened."
    )
    try:
        raw = await github_caller(
            "create_fix_pr",
            {
                "branch_name": branch_name,
                "patch": params.patch,
                "title": title,
                "body": body,
                "dry_run": False,
            },
        )
    except Exception as exc:
        return PrResult("PR_CREATION_FAILED", f"create_fix_pr call raised: {exc}")

    payload = _structured_payload(raw) or {}
    status = str(payload.get("status", "")).upper()
    if status == "CREATED" and payload.get("pr_url"):
        return PrResult("PR_CREATED", payload.get("detail") or "PR created.", pr_url=payload.get("pr_url"))
    detail = (
        payload.get("note")
        or payload.get("detail")
        or payload.get("error")
        or payload.get("reason")
        or f"create_fix_pr returned status={status or 'UNKNOWN'}"
    )
    return PrResult("PR_CREATION_FAILED", str(detail))


@activity.defn
async def open_gate_activity(
    incident_id: str,
    organization_id: str,
    cluster_id: str,
    workflow_id: str,
    gate: str,
    timeout_seconds: int,
) -> None:
    """Create the durable RemediationGateApproval row a gate's PENDING state
    needs — emit_gate_event_activity only writes a timeline event, which the
    dashboard/API has nothing to CAS-decide against.
    """
    from .approval_flow import create_or_reuse_pending_gate_approval

    await create_or_reuse_pending_gate_approval(
        incident_id=incident_id,
        organization_id=organization_id,
        cluster_id=cluster_id,
        workflow_id=workflow_id,
        gate=gate,
        ttl_seconds=timeout_seconds,
    )


@activity.defn
async def expire_gate_approval_activity(workflow_id: str, gate: str) -> None:
    """Reflect a workflow-driven wait_condition timeout into the DB row —
    nothing else observes that timeout.
    """
    from .approval_flow import expire_gate_approval

    await expire_gate_approval(workflow_id=workflow_id, gate=gate)


ACTIVITIES = [
    emit_gate_event_activity,
    raise_pr_activity,
    open_gate_activity,
    expire_gate_approval_activity,
]


# ── Workflow ───────────────────────────────────────────────────────────────


@workflow.defn
class IncidentRemediationWorkflow:
    def __init__(self) -> None:
        self._start_fix_decision: Optional[bool] = None
        self._start_fix_actor: Optional[str] = None
        self._raise_pr_decision: Optional[bool] = None
        self._raise_pr_actor: Optional[str] = None
        self._phase = "AWAITING_START_FIX"

    @workflow.signal
    def decide_start_fix(self, approved: bool, actor: str = "") -> None:
        if self._start_fix_decision is None:
            self._start_fix_decision = bool(approved)
            self._start_fix_actor = actor or "unknown"

    @workflow.signal
    def decide_raise_pr(self, approved: bool, actor: str = "") -> None:
        if self._raise_pr_decision is None:
            self._raise_pr_decision = bool(approved)
            self._raise_pr_actor = actor or "unknown"

    @workflow.query
    def phase(self) -> str:
        return self._phase

    async def _emit(self, incident_id: str, workflow_id: str, gate: str, status: str, detail: str) -> None:
        await workflow.execute_activity(
            emit_gate_event_activity,
            args=[incident_id, workflow_id, gate, status, detail],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

    async def _open_gate(
        self, params: IncidentRemediationInput, workflow_id: str, gate: str
    ) -> None:
        await workflow.execute_activity(
            open_gate_activity,
            args=[
                params.incident_id,
                params.organization_id,
                params.cluster_id,
                workflow_id,
                gate,
                params.approval_timeout_seconds,
            ],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

    async def _expire_gate(self, workflow_id: str, gate: str) -> None:
        await workflow.execute_activity(
            expire_gate_approval_activity,
            args=[workflow_id, gate],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

    @workflow.run
    async def run(self, params: IncidentRemediationInput) -> RemediationVerdict:
        workflow_id = workflow.info().workflow_id
        timeout = timedelta(seconds=params.approval_timeout_seconds)

        # ── Gate 1: approve starting the fix in Temporal ───────────────────
        self._phase = "AWAITING_START_FIX"
        await self._open_gate(params, workflow_id, "start_fix")
        await self._emit(
            params.incident_id, workflow_id, "start_fix", "PENDING",
            "Awaiting approval to start the fix in Temporal.",
        )
        try:
            await workflow.wait_condition(lambda: self._start_fix_decision is not None, timeout=timeout)
        except asyncio.TimeoutError:
            self._phase = "EXPIRED_START_FIX"
            await self._expire_gate(workflow_id, "start_fix")
            await self._emit(
                params.incident_id, workflow_id, "start_fix", "EXPIRED",
                "Approval to start the fix expired without a decision.",
            )
            return RemediationVerdict("EXPIRED_START_FIX", "Gate 1 (start fix) expired without a decision.")

        if not self._start_fix_decision:
            self._phase = "DENIED_START_FIX"
            await self._emit(
                params.incident_id, workflow_id, "start_fix", "DENIED",
                f"Denied by {self._start_fix_actor}.",
            )
            return RemediationVerdict(
                "DENIED_START_FIX", f"Gate 1 (start fix) was denied by {self._start_fix_actor}."
            )

        self._phase = "VERIFYING"
        await self._emit(
            params.incident_id, workflow_id, "start_fix", "APPROVED",
            f"Approved by {self._start_fix_actor}; running sandbox verification.",
        )

        # ── Sandbox verification: EXISTING CodeFixVerificationWorkflow, unmodified,
        # now run as a child BEFORE any live/PR action (Phase 5A reordering) ──
        verification_input = CodeFixVerificationInput(
            incident_id=params.incident_id,
            organization_id=params.organization_id,
            cluster_id=params.cluster_id,
            runner_image=params.runner_image,
            baseline_command=list(params.baseline_command),
            candidate_command=list(params.candidate_command),
            patch=params.patch,
            failure_signature=params.failure_signature,
            env=dict(params.env),
        )
        verdict: VerdictResult = await workflow.execute_child_workflow(
            CodeFixVerificationWorkflow.run,
            verification_input,
            id=f"{workflow_id}-verify",
        )

        if verdict.status != "RESOLVED":
            self._phase = f"VERIFICATION_{verdict.status}"
            await self._emit(
                params.incident_id, workflow_id, "raise_pr", "ESCALATED",
                f"Sandbox verification {verdict.status}: {verdict.detail}. "
                "Escalating to on-call; no PR will be raised.",
            )
            return RemediationVerdict(
                f"VERIFICATION_{verdict.status}", verdict.detail, verification_status=verdict.status
            )

        # ── Gate 2: approve raising the PR ──────────────────────────────────
        self._phase = "AWAITING_RAISE_PR"
        await self._open_gate(params, workflow_id, "raise_pr")
        await self._emit(
            params.incident_id, workflow_id, "raise_pr", "PENDING",
            "Sandbox verification RESOLVED; awaiting approval to raise a PR.",
        )
        try:
            await workflow.wait_condition(lambda: self._raise_pr_decision is not None, timeout=timeout)
        except asyncio.TimeoutError:
            self._phase = "EXPIRED_RAISE_PR"
            await self._expire_gate(workflow_id, "raise_pr")
            await self._emit(
                params.incident_id, workflow_id, "raise_pr", "EXPIRED",
                "Approval to raise the PR expired without a decision.",
            )
            return RemediationVerdict(
                "EXPIRED_RAISE_PR", "Gate 2 (raise PR) expired without a decision.",
                verification_status="RESOLVED",
            )

        if not self._raise_pr_decision:
            self._phase = "DENIED_RAISE_PR"
            await self._emit(
                params.incident_id, workflow_id, "raise_pr", "DENIED",
                f"Denied by {self._raise_pr_actor}.",
            )
            return RemediationVerdict(
                "DENIED_RAISE_PR", f"Gate 2 (raise PR) was denied by {self._raise_pr_actor}.",
                verification_status="RESOLVED",
            )

        self._phase = "RAISING_PR"
        pr_result: PrResult = await workflow.execute_activity(
            raise_pr_activity,
            params,
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        self._phase = pr_result.status
        await self._emit(
            params.incident_id, workflow_id, "raise_pr", pr_result.status, pr_result.detail
        )
        return RemediationVerdict(
            pr_result.status, pr_result.detail, pr_url=pr_result.pr_url, verification_status="RESOLVED"
        )
