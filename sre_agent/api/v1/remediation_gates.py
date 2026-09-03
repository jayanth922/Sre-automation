"""API endpoints for Phase 5's two Temporal remediation gates (start_fix,
raise_pr) — docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md, Phase B/C.

Distinct from mission_control.approve_incident_action: these decide a
RemediationGateApproval row (keyed off a Temporal workflow_id + gate) rather
than resuming a LangGraph checkpoint interrupt, and finish by signaling the
running IncidentRemediationWorkflow via temporal_client.signal_workflow()
instead of graph.ainvoke(Command(resume=...)).
"""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import database, models, schemas
from sre_agent.api.v1.auth_deps import get_current_user_and_org, require_admin
from sre_agent.api.v1.ownership import get_owned_incident
from sre_agent.approval_flow import ApprovalValidationError, decide_gate_approval

router = APIRouter(
    prefix="/incidents",
    tags=["remediation_gates"],
    dependencies=[Depends(get_current_user_and_org)],
)

_SIGNAL_NAME = {"start_fix": "decide_start_fix", "raise_pr": "decide_raise_pr"}


@router.get(
    "/{incident_id}/remediation-gates",
    response_model=List[schemas.GateApprovalResponse],
)
async def list_remediation_gates(
    incident_id: str,
    db: AsyncSession = Depends(database.get_db),
    owned_incident: models.Incident = Depends(get_owned_incident),
):
    """List this incident's gate-approval rows, newest first, for the
    dashboard's approve/deny buttons and history view."""
    result = await db.execute(
        select(models.RemediationGateApproval)
        .where(models.RemediationGateApproval.incident_id == owned_incident.id)
        .order_by(desc(models.RemediationGateApproval.created_at))
    )
    return result.scalars().all()


@router.post(
    "/{incident_id}/remediation-gates/{gate_approval_id}/decide",
    response_model=schemas.GateApprovalResponse,
)
async def decide_remediation_gate(
    incident_id: str,
    gate_approval_id: str,
    decision: schemas.GateApprovalDecisionRequest,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_incident: models.Incident = Depends(get_owned_incident),
):
    """Atomically approve/deny one gate, then signal the waiting Temporal
    workflow. Every issue, both gates, always human-gated — no default-approve
    path exists in IncidentRemediationWorkflow itself."""
    await require_admin(user)

    try:
        uuid.UUID(str(gate_approval_id))
    except ValueError:
        raise HTTPException(status_code=422, detail="gate_approval_id is not a valid UUID")

    try:
        row = await decide_gate_approval(
            gate_approval_id=gate_approval_id,
            incident_id=str(owned_incident.id),
            organization_id=str(user.org_id),
            cluster_id=str(owned_incident.cluster_id),
            approved=decision.approved,
            approver_user_id=str(user.id),
        )
    except ApprovalValidationError as exc:
        if exc.reason == "not_pending":
            raise HTTPException(
                status_code=409, detail="Gate approval is no longer pending"
            ) from exc
        raise HTTPException(status_code=410, detail="Gate approval expired") from exc

    if row is None:
        raise HTTPException(status_code=404, detail="Gate approval not found")

    signal_name = _SIGNAL_NAME.get(row.gate)
    if signal_name is None:
        raise HTTPException(status_code=500, detail=f"Unknown gate {row.gate!r}")

    from sre_agent.temporal_client import signal_workflow

    delivered = await signal_workflow(
        row.workflow_id, signal_name, args=[decision.approved, user.email or str(user.id)]
    )
    if not delivered:
        # The DB decision is durable regardless — this only means the running
        # workflow (if any) didn't get the memo yet. Surface it rather than
        # silently pretending the pipeline will proceed.
        raise HTTPException(
            status_code=502,
            detail="Decision recorded, but the remediation workflow could not be signaled",
        )

    return row
