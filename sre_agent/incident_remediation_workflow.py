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
    0. (Phase F, only when the planner didn't supply a patch) generate_patch_activity
       — the pluggable actor runtime (sre_agent/actor_runtime.py) proposes a
       patch + baseline/candidate verification commands against a throwaway
       clone of the incident's repo. Failure escalates immediately; gate 1
       never opens without a real diff to show.
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
from dataclasses import dataclass, field, replace as _dc_replace
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

# Max end-to-end remediation attempts (generate -> verify) before the
# workflow gives up and escalates to on-call for a manual close-out. Each
# retry beyond attempt 1 requires its own human approval (the "retry_fix"
# gate) -- this is an activity-retry-policy-independent business rule, not
# a Temporal transport retry.
RETRY_MAX_ATTEMPTS = 3


# ── Data contracts (JSON-serializable — cross the Temporal wire) ──────────────


@dataclass
class IncidentRemediationInput:
    incident_id: str
    organization_id: str
    cluster_id: str
    action_type: str
    target: str
    runner_image: str
    failure_signature: str
    repo: str = ""
    # Phase F: when patch is empty, generate_patch_activity produces patch +
    # baseline/candidate commands before gate 1 opens (docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md
    # Phase F) — the planner only has to identify a code_fix is needed, not
    # author the diff itself.
    patch: str = ""
    baseline_command: List[str] = field(default_factory=list)
    candidate_command: List[str] = field(default_factory=list)
    fix_description: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    approval_timeout_seconds: int = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    # Populated on attempt 2+ of the retry loop with a summary of why prior
    # attempts failed, so generate_patch_activity's actor can try something
    # different instead of repeating the same failing diff.
    retry_context: str = ""


@dataclass
class PrResult:
    status: str  # "PR_CREATED" | "PR_CREATION_FAILED"
    detail: str
    pr_url: Optional[str] = None


@dataclass
class PatchGenerationResult:
    status: str  # "GENERATED" | "FAILED"
    patch: str = ""
    baseline_command: List[str] = field(default_factory=list)
    candidate_command: List[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class RemediationVerdict:
    status: str
    # "DENIED_START_FIX" | "EXPIRED_START_FIX" |
    # "VERIFICATION_REGRESSED" | "VERIFICATION_INCONCLUSIVE" |
    # "DENIED_RAISE_PR" | "EXPIRED_RAISE_PR" | "PR_CREATED" | "PR_CREATION_FAILED" |
    # "CLOSED_NEEDS_MANUAL_REVIEW" | "EXPIRED_CLOSE_INCIDENT" | "ESCALATION_UNACKNOWLEDGED"
    #
    # NOTE: "PATCH_GENERATION_FAILED" alone is no longer terminal on its own —
    # it now also flows into the retry loop like a verification failure would.
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


def _repo_clone_url(repo: str, token: str = "") -> str:
    """`repo` is always `owner/repo` form (the existing GITHUB_REPO convention
    — see edge_mcp_servers/mcp_servers/github_real/README.md). Pure so the
    credential-embedding logic is unit-testable without a real clone.
    """
    return f"https://{token}@github.com/{repo}.git" if token else f"https://github.com/{repo}.git"


def _command_from_marker_text(text: str) -> List[str]:
    """Turn one captured BASELINE_COMMAND/CANDIDATE_COMMAND value into an argv
    list suitable for a K8s Job's `command` field (no shell involved there).

    The task prompt has the actor `echo '<TYPE>_COMMAND: <cmd>'` as its final
    shell command. That means the marker text we regex-capture out of the
    transcript's "$ echo '...'" invocation line always carries one trailing
    stray `'` — the shell's own closing quote for the echo argument, not part
    of the actor's intended command — which must be stripped before use.

    The captured command itself is free-form shell text the actor wrote (may
    contain &&, &, $(...), ;, pipes, env-var assignments, etc. — verification
    commands routinely need these, e.g. to start a server in the background
    then curl it). shlex.split() cannot execute shell control operators even
    when it manages to tokenize them, and a K8s Job's command array runs with
    no shell at all — so anything containing shell metacharacters is wrapped
    as ["sh", "-c", text] instead of being split into argv tokens. Only a
    plain, argument-only command (the common case for a single test/lint
    invocation) is shlex-split, matching what a human would expect to see in
    baseline_command for e.g. "go test ./... -run TestHandler".
    """
    import re
    import shlex

    text = text.strip()
    if text.endswith("'"):
        text = text[:-1].rstrip()
    if not text:
        return []
    if re.search(r"[&|;`$(){}<>]", text):
        return ["sh", "-c", text]
    try:
        return shlex.split(text)
    except ValueError:
        # Not valid shell-quoted tokens either (e.g. an unbalanced quote the
        # actor typed) — still safer to hand the whole line to a real shell
        # than to silently drop it.
        return ["sh", "-c", text]


def _parse_verification_commands(
    actor_output: str, fallback_baseline: List[str], fallback_candidate: List[str]
) -> "tuple[List[str], List[str]]":
    """Extract the BASELINE_COMMAND/CANDIDATE_COMMAND lines the generate_patch_activity
    task prompt asks the actor to end its response with, falling back to
    caller-supplied commands (if any) when the actor didn't report them. Pure
    text parsing, factored out of the activity so it's unit-testable without
    mocking git/the actor runtime.
    """
    import re

    baseline_command = list(fallback_baseline)
    candidate_command = list(fallback_candidate)
    # The marker text can appear more than once in the transcript: once in the
    # "$ echo '...'" command-invocation line (the actor's literal intended
    # text, verbatim, modulo the trailing closing quote handled above), and
    # again in its "[exit 0] ..." stdout line — except some shells' `echo`
    # interpret backslash escapes (e.g. \n) the actor put in the command text
    # for its OWN purposes (like curl's -w "...\n"), which splits that stdout
    # line across a real newline our (non-DOTALL) regex can't see past,
    # silently truncating it. The invocation line has no such hazard, so take
    # the FIRST occurrence, not the last.
    all_b = re.findall(r"BASELINE_COMMAND:\s*(.+)", actor_output)
    all_c = re.findall(r"CANDIDATE_COMMAND:\s*(.+)", actor_output)
    if all_b:
        baseline_command = _command_from_marker_text(all_b[0])
    if all_c:
        candidate_command = _command_from_marker_text(all_c[0])
    return baseline_command, candidate_command


async def _run_git(args: List[str], cwd: str, redact: str = "") -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")[:2000]
        if redact:
            err = err.replace(redact, "***")
        raise RuntimeError(f"git {' '.join(args)} failed: {err}")
    return stdout.decode(errors="replace")


@activity.defn
async def generate_patch_activity(params: IncidentRemediationInput) -> PatchGenerationResult:
    """Phase F (docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md): generate the
    missing patch upstream of gate 1, using the pluggable actor runtime
    (sre_agent/actor_runtime.py) against a throwaway clone of the incident's
    repo. Deliberately narrow: runner_image/failure_signature stay whatever
    the planner/env already supplied (deterministic, never agent-invented);
    only the patch and its baseline/candidate verification commands come from
    the agent, since nothing else in the pipeline can produce them (see the
    Phase F "verified gap" writeup in docs/ai/PROJECT_STATE.md).
    """
    import os
    import shutil
    import tempfile
    import uuid as _uuid

    from .actor_runtime import get_agent_runtime
    from .incident_timeline import emit_trace_step_event, truncate_for_timeline

    workflow_id = activity.info().workflow_id

    async def _finish(result: PatchGenerationResult) -> PatchGenerationResult:
        await emit_trace_step_event(
            params.incident_id,
            workflow_id,
            source="generate_patch_activity",
            step="generate_patch",
            status="SUCCEEDED" if result.status == "GENERATED" else "FAILED",
            detail=result.detail,
            diff=truncate_for_timeline(result.patch) if result.patch else "",
        )
        return result

    if not params.repo:
        return await _finish(
            PatchGenerationResult("FAILED", detail="No repo configured (GITHUB_REPO env var); cannot generate a patch.")
        )

    token = os.getenv("GITHUB_TOKEN", "")
    repo_url = _repo_clone_url(params.repo, token)

    clone_dir = tempfile.mkdtemp(prefix=f"sentinel-patchgen-{_uuid.uuid4().hex[:8]}-")
    try:
        activity.heartbeat("cloning")
        await emit_trace_step_event(
            params.incident_id, workflow_id, source="generate_patch_activity",
            step="clone_repo", status="STARTED", detail=f"Cloning {params.repo}",
        )
        await _run_git(["clone", "--depth", "1", repo_url, clone_dir], cwd=tempfile.gettempdir(), redact=token)

        retry_note = (
            f"\nPrior attempt(s) at this incident failed:\n{params.retry_context}\n"
            "Try a genuinely different approach than what's summarized above — "
            "don't just resubmit the same fix.\n"
        ) if params.retry_context else ""
        task = (
            "You are fixing a production incident in this repository.\n"
            f"Failure signature: {params.failure_signature}\n"
            f"Target: {params.target}\n"
            "Root cause / desired fix (from the on-call remediation plan): "
            f"{params.fix_description or '(no description provided)'}\n"
            f"{retry_note}\n"
            "Make the minimal source change that fixes this. Do not commit or "
            "push. Before you finish, run a shell command as your final action "
            "that echoes exactly these two lines (fill in real shell commands, "
            "keep each on its own line):\n"
            "echo 'BASELINE_COMMAND: <command that reproduces the failure on the original code>'\n"
            "echo 'CANDIDATE_COMMAND: <command that verifies the fix, usually the same command>'\n"
            "Only after that command has run should you report done."
        )
        activity.heartbeat("running actor")
        await emit_trace_step_event(
            params.incident_id, workflow_id, source="generate_patch_activity",
            step="run_actor", status="STARTED", detail=f"Target: {params.target}",
        )
        runtime = get_agent_runtime(workdir=clone_dir)
        result = await asyncio.to_thread(runtime.run, task)

        if result.status not in ("SOLVED", "DONE"):
            return await _finish(PatchGenerationResult(
                "FAILED",
                detail=f"Actor runtime ({runtime.name}) did not produce a fix: "
                f"{result.status} — {result.detail or result.output[:500]}",
            ))

        activity.heartbeat("diffing")
        patch = await _run_git(["diff"], cwd=clone_dir)
        if not patch.strip():
            return await _finish(PatchGenerationResult(
                "FAILED",
                detail=f"Actor runtime ({runtime.name}) reported {result.status} but the working tree has no diff.",
            ))

        baseline_command, candidate_command = _parse_verification_commands(
            result.output, params.baseline_command, params.candidate_command
        )
        if not baseline_command or not candidate_command:
            return await _finish(PatchGenerationResult(
                "FAILED",
                detail=f"Actor runtime ({runtime.name}) produced a patch but no "
                "baseline/candidate verification command; refusing to open gate 1 "
                "without a real sandbox oracle.",
            ))

        return await _finish(PatchGenerationResult(
            "GENERATED",
            patch=patch,
            baseline_command=baseline_command,
            candidate_command=candidate_command,
            detail=f"Patch generated by {runtime.name}.",
        ))
    except Exception as exc:
        logger.error(f"generate_patch_activity failed: {exc}")
        return await _finish(PatchGenerationResult("FAILED", detail=str(exc)))
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


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


@activity.defn
async def mark_incident_needs_manual_review_activity(incident_id: str) -> None:
    """Automated remediation exhausted (retries used up or a retry was
    declined) and on-call acknowledged the close-incident gate: flag the
    incident so it stops looking like something the pipeline is still
    working. `REMEDIATION_FAILED` already exists in IncidentStatus for
    exactly this state — on-call later clears it via the manual
    mark-resolved endpoint once they've verified the fix themselves.
    """
    import uuid as _uuid

    from backend import database, models

    async with database.AsyncSessionLocal() as db:
        incident = await db.get(models.Incident, _uuid.UUID(incident_id))
        if incident is not None:
            incident.status = models.IncidentStatus.REMEDIATION_FAILED
            await db.commit()


ACTIVITIES = [
    emit_gate_event_activity,
    generate_patch_activity,
    raise_pr_activity,
    open_gate_activity,
    expire_gate_approval_activity,
    mark_incident_needs_manual_review_activity,
]


# ── Workflow ───────────────────────────────────────────────────────────────


@workflow.defn
class IncidentRemediationWorkflow:
    def __init__(self) -> None:
        self._start_fix_decision: Optional[bool] = None
        self._start_fix_actor: Optional[str] = None
        self._raise_pr_decision: Optional[bool] = None
        self._raise_pr_actor: Optional[str] = None
        self._retry_decision: Optional[bool] = None
        self._retry_actor: Optional[str] = None
        self._close_decision: Optional[bool] = None
        self._close_actor: Optional[str] = None
        self._attempt_history: List[str] = []
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

    @workflow.signal
    def decide_retry_fix(self, approved: bool, actor: str = "") -> None:
        if self._retry_decision is None:
            self._retry_decision = bool(approved)
            self._retry_actor = actor or "unknown"

    @workflow.signal
    def decide_close_incident(self, approved: bool, actor: str = "") -> None:
        if self._close_decision is None:
            self._close_decision = bool(approved)
            self._close_actor = actor or "unknown"

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

    async def _maybe_retry(
        self, params: IncidentRemediationInput, workflow_id: str, attempt: int, timeout: timedelta
    ) -> bool:
        """After a failed attempt, ask a fresh 'retry_fix' gate whether to try
        again. Returns False (caller should escalate to close-out) when
        attempts are exhausted, or the gate is denied/expires; True when a
        human approved another attempt. Every retry gets its own gate —
        approving attempt 2 does not pre-approve attempt 3.
        """
        if attempt >= RETRY_MAX_ATTEMPTS:
            return False

        self._retry_decision = None
        self._retry_actor = None
        self._phase = f"AWAITING_RETRY_{attempt}"
        await self._open_gate(params, workflow_id, "retry_fix")
        await self._emit(
            params.incident_id, workflow_id, "retry_fix", "PENDING",
            f"Attempt {attempt}/{RETRY_MAX_ATTEMPTS} did not resolve the incident. "
            f"Awaiting approval to try attempt {attempt + 1}/{RETRY_MAX_ATTEMPTS}.",
        )
        try:
            await workflow.wait_condition(lambda: self._retry_decision is not None, timeout=timeout)
        except asyncio.TimeoutError:
            await self._expire_gate(workflow_id, "retry_fix")
            await self._emit(
                params.incident_id, workflow_id, "retry_fix", "EXPIRED",
                "Approval to retry expired without a decision.",
            )
            return False

        if not self._retry_decision:
            await self._emit(
                params.incident_id, workflow_id, "retry_fix", "DENIED",
                f"Denied by {self._retry_actor}.",
            )
            return False

        await self._emit(
            params.incident_id, workflow_id, "retry_fix", "APPROVED",
            f"Approved by {self._retry_actor}.",
        )
        return True

    async def _close_out(
        self, params: IncidentRemediationInput, workflow_id: str, timeout: timedelta
    ) -> RemediationVerdict:
        """Automated remediation is exhausted (retries used up, or a retry was
        declined/expired): give on-call full context on what was tried and
        why it failed, and require an explicit acknowledgement (its own gate,
        Slack-notified like every other gate) before marking the incident as
        needing manual review. Never mutates Incident.status on its own —
        only a human's APPROVED decision does that, via
        mark_incident_needs_manual_review_activity.
        """
        self._phase = "AWAITING_CLOSE_INCIDENT"
        context = "\n".join(self._attempt_history) or "No attempt details were recorded."
        summary = (
            f"Automated remediation could not resolve this incident after "
            f"{len(self._attempt_history)} attempt(s). Here's what was tried and why "
            f"each attempt failed:\n\n{context}\n\n"
            "Awaiting acknowledgement to hand this off for manual review."
        )
        await self._open_gate(params, workflow_id, "close_incident")
        await self._emit(params.incident_id, workflow_id, "close_incident", "PENDING", summary)

        try:
            await workflow.wait_condition(lambda: self._close_decision is not None, timeout=timeout)
        except asyncio.TimeoutError:
            self._phase = "EXPIRED_CLOSE_INCIDENT"
            await self._expire_gate(workflow_id, "close_incident")
            await self._emit(
                params.incident_id, workflow_id, "close_incident", "EXPIRED",
                "Acknowledgement to hand off for manual review expired without a decision.",
            )
            return RemediationVerdict("EXPIRED_CLOSE_INCIDENT", context)

        if not self._close_decision:
            await self._emit(
                params.incident_id, workflow_id, "close_incident", "DENIED",
                f"Denied by {self._close_actor}.",
            )
            return RemediationVerdict("ESCALATION_UNACKNOWLEDGED", context)

        self._phase = "CLOSED_NEEDS_MANUAL_REVIEW"
        await workflow.execute_activity(
            mark_incident_needs_manual_review_activity,
            params.incident_id,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        await self._emit(
            params.incident_id, workflow_id, "close_incident", "APPROVED",
            f"Acknowledged by {self._close_actor}; incident marked as needing manual review.",
        )
        return RemediationVerdict("CLOSED_NEEDS_MANUAL_REVIEW", context)

    @workflow.run
    async def run(self, params: IncidentRemediationInput) -> RemediationVerdict:
        workflow_id = workflow.info().workflow_id
        timeout = timedelta(seconds=params.approval_timeout_seconds)

        attempt = 1
        while True:
            current_params = params

            # ── Phase F: generate the patch upstream of gate 1. Attempt 1 may
            # reuse a caller-supplied patch (used for deterministic testing,
            # e.g. e2e drivers that hand-set baseline/candidate commands);
            # every retry (attempt 2+) always regenerates from scratch, fed
            # the accumulated history of why prior attempts failed, so the
            # actor has a real chance to try something different rather than
            # resubmitting the same failing diff. No gate is open yet — a
            # failed/refused generation never reaches a human directly, it
            # flows into the same retry decision as a verification failure.
            if attempt > 1 or not params.patch:
                self._phase = "GENERATING_PATCH"
                await self._emit(
                    params.incident_id, workflow_id, "generate_patch", "PENDING",
                    f"Attempt {attempt}/{RETRY_MAX_ATTEMPTS}: generating a patch via "
                    "the actor runtime before gate 1 opens.",
                )
                gen_params = (
                    _dc_replace(params, retry_context="\n\n".join(self._attempt_history))
                    if self._attempt_history else params
                )
                generation: PatchGenerationResult = await workflow.execute_activity(
                    generate_patch_activity,
                    gen_params,
                    start_to_close_timeout=timedelta(minutes=10),
                    heartbeat_timeout=timedelta(seconds=60),
                    retry_policy=DEFAULT_RETRY_POLICY,
                )
                if generation.status != "GENERATED":
                    self._attempt_history.append(
                        f"Attempt {attempt}: patch generation failed — {generation.detail}"
                    )
                    await self._emit(
                        params.incident_id, workflow_id, "generate_patch", "ESCALATED",
                        f"Attempt {attempt}/{RETRY_MAX_ATTEMPTS} patch generation failed: "
                        f"{generation.detail}.",
                    )
                    if await self._maybe_retry(params, workflow_id, attempt, timeout):
                        attempt += 1
                        continue
                    return await self._close_out(params, workflow_id, timeout)
                current_params = _dc_replace(
                    params,
                    patch=generation.patch,
                    baseline_command=generation.baseline_command,
                    candidate_command=generation.candidate_command,
                )
                await self._emit(
                    params.incident_id, workflow_id, "generate_patch", "APPROVED",
                    generation.detail,
                )

            # ── Gate 1: approve starting the fix in Temporal (attempt 1 only —
            # retries are re-authorized via the "retry_fix" gate instead, so
            # they proceed straight to verification once approved) ─────────
            if attempt == 1:
                self._phase = "AWAITING_START_FIX"
                await self._open_gate(current_params, workflow_id, "start_fix")
                await self._emit(
                    current_params.incident_id, workflow_id, "start_fix", "PENDING",
                    "Awaiting approval to start the fix in Temporal.",
                )
                try:
                    await workflow.wait_condition(
                        lambda: self._start_fix_decision is not None, timeout=timeout
                    )
                except asyncio.TimeoutError:
                    self._phase = "EXPIRED_START_FIX"
                    await self._expire_gate(workflow_id, "start_fix")
                    await self._emit(
                        current_params.incident_id, workflow_id, "start_fix", "EXPIRED",
                        "Approval to start the fix expired without a decision.",
                    )
                    return RemediationVerdict(
                        "EXPIRED_START_FIX", "Gate 1 (start fix) expired without a decision."
                    )

                if not self._start_fix_decision:
                    self._phase = "DENIED_START_FIX"
                    await self._emit(
                        current_params.incident_id, workflow_id, "start_fix", "DENIED",
                        f"Denied by {self._start_fix_actor}.",
                    )
                    return RemediationVerdict(
                        "DENIED_START_FIX", f"Gate 1 (start fix) was denied by {self._start_fix_actor}."
                    )

                self._phase = "VERIFYING"
                await self._emit(
                    current_params.incident_id, workflow_id, "start_fix", "APPROVED",
                    f"Approved by {self._start_fix_actor}; running sandbox verification.",
                )
            else:
                self._phase = "VERIFYING"
                await self._emit(
                    current_params.incident_id, workflow_id, "retry_fix", "APPROVED",
                    f"Attempt {attempt}/{RETRY_MAX_ATTEMPTS}: running sandbox verification "
                    "on the new patch.",
                )

            # ── Sandbox verification: EXISTING CodeFixVerificationWorkflow, unmodified,
            # now run as a child BEFORE any live/PR action (Phase 5A reordering) ──
            verification_input = CodeFixVerificationInput(
                incident_id=current_params.incident_id,
                organization_id=current_params.organization_id,
                cluster_id=current_params.cluster_id,
                runner_image=current_params.runner_image,
                baseline_command=list(current_params.baseline_command),
                candidate_command=list(current_params.candidate_command),
                patch=current_params.patch,
                failure_signature=current_params.failure_signature,
                env=dict(current_params.env),
            )
            verdict: VerdictResult = await workflow.execute_child_workflow(
                CodeFixVerificationWorkflow.run,
                verification_input,
                id=f"{workflow_id}-verify-{attempt}",
            )

            if verdict.status == "RESOLVED":
                params = current_params  # carry the winning patch forward to gate 2 / the PR
                break

            self._attempt_history.append(
                f"Attempt {attempt}: sandbox verification {verdict.status} — {verdict.detail}"
            )
            await self._emit(
                current_params.incident_id, workflow_id, "raise_pr", "ESCALATED",
                f"Attempt {attempt}/{RETRY_MAX_ATTEMPTS}: sandbox verification "
                f"{verdict.status}: {verdict.detail}.",
            )
            if await self._maybe_retry(current_params, workflow_id, attempt, timeout):
                attempt += 1
                continue
            return await self._close_out(current_params, workflow_id, timeout)

        # ── Gate 2: approve raising the PR (verdict is RESOLVED here) ──────
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
