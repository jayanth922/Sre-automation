"""Jira ticketing integration (real Jira Cloud REST API v3, per-tenant).

Each customer has their own Jira Cloud site and project, so — unlike
oncall.py's PagerDuty lookup, which is a single platform-wide credential —
Jira credentials live on the ``Cluster`` row (``jira_url``/``jira_email``/
``jira_api_token``/``jira_project_key``), the same per-tenant pattern already
used for Notion runbooks (``Cluster.notion_api_key``). Every call here is
guarded and non-fatal: an unconfigured or failing Jira integration never
blocks incident processing, matching war_room_service.py's convention.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Incident.severity -> Jira priority name.
SEVERITY_TO_PRIORITY = {
    "critical": "Highest",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}

# Incident.status -> Jira transition name. Real Jira workflows are
# per-project, so each mapping is overridable via env
# (JIRA_TRANSITION_<STATUS>) rather than hardcoded to one workflow.
DEFAULT_STATUS_TRANSITIONS = {
    "investigating": "In Progress",
    "investigated": "In Progress",
    "awaiting_approval": "In Progress",
    "remediation_in_progress": "In Progress",
    "remediation_failed": "In Progress",
    "verification_unknown": "In Progress",
    "resolved": "Done",
}


def _status_transition_name(status: str) -> Optional[str]:
    override = os.getenv(f"JIRA_TRANSITION_{str(status).upper()}")
    if override:
        return override
    return DEFAULT_STATUS_TRANSITIONS.get(str(status))


def jira_configured(cluster: Any) -> bool:
    """True when this cluster has a complete Jira credential set."""
    return bool(
        cluster
        and getattr(cluster, "jira_url", None)
        and getattr(cluster, "jira_email", None)
        and getattr(cluster, "jira_api_token", None)
        and getattr(cluster, "jira_project_key", None)
    )


def _adf_doc(text: str) -> dict:
    """Wrap plain text as Atlassian Document Format, which Jira Cloud v3 requires
    for description/comment bodies."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


async def maybe_create_jira_issue(
    incident_id: str,
    cluster_id: str,
    alert_name: str,
    summary: str,
    severity: str = "medium",
) -> None:
    """Create a Jira issue for a newly opened incident and record its key.
    No-op unless the owning cluster has Jira configured."""
    try:
        from backend import crud, database

        async with database.AsyncSessionLocal() as db:
            cluster = await crud.get_cluster_by_id(db, uuid.UUID(str(cluster_id)))
    except Exception as e:
        logger.debug(f"jira: cluster lookup skipped ({e})")
        return
    if not jira_configured(cluster):
        return

    try:
        import httpx

        base = cluster.jira_url.rstrip("/")
        auth = (cluster.jira_email, cluster.jira_api_token)
        payload = {
            "fields": {
                "project": {"key": cluster.jira_project_key},
                "summary": f"[Sentinel] {alert_name}"[:255],
                "description": _adf_doc(summary or alert_name),
                "issuetype": {"name": os.getenv("JIRA_ISSUE_TYPE", "Task")},
                "priority": {"name": SEVERITY_TO_PRIORITY.get(str(severity), "Medium")},
            }
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{base}/rest/api/3/issue", json=payload, auth=auth)
            resp.raise_for_status()
            issue_key = resp.json().get("key")

        if issue_key:
            async with database.AsyncSessionLocal() as db:
                await crud.set_incident_jira_key(db, uuid.UUID(str(incident_id)), issue_key)
            logger.info(f"jira: created issue {issue_key} for incident {incident_id}")
    except Exception as e:
        logger.warning(f"jira: issue creation failed (non-fatal): {e}")


async def transition_jira_issue(
    incident_id: str,
    cluster_id: str,
    status: str,
    comment: Optional[str] = None,
) -> None:
    """Transition the incident's linked Jira issue and optionally attach a
    postmortem comment. No-op unless Jira is configured and the incident has
    a linked issue key."""
    try:
        from backend import crud, database

        async with database.AsyncSessionLocal() as db:
            cluster = await crud.get_cluster_by_id(db, uuid.UUID(str(cluster_id)))
            incident = await crud.get_incident_by_id(db, uuid.UUID(str(incident_id)))
    except Exception as e:
        logger.debug(f"jira: lookup skipped ({e})")
        return

    issue_key = getattr(incident, "jira_issue_key", None)
    if not jira_configured(cluster) or not issue_key:
        return

    base = cluster.jira_url.rstrip("/")
    auth = (cluster.jira_email, cluster.jira_api_token)
    transition_name = _status_transition_name(status)

    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            if transition_name:
                trans_resp = await client.get(
                    f"{base}/rest/api/3/issue/{issue_key}/transitions", auth=auth
                )
                trans_resp.raise_for_status()
                transitions = trans_resp.json().get("transitions", [])
                match = next(
                    (t for t in transitions if t.get("name") == transition_name), None
                )
                if match:
                    await client.post(
                        f"{base}/rest/api/3/issue/{issue_key}/transitions",
                        json={"transition": {"id": match["id"]}},
                        auth=auth,
                    )
                else:
                    logger.info(
                        f"jira: no transition named '{transition_name}' available on {issue_key}"
                    )
            if comment:
                await client.post(
                    f"{base}/rest/api/3/issue/{issue_key}/comment",
                    json={"body": _adf_doc(comment)},
                    auth=auth,
                )
        logger.info(f"jira: transitioned {issue_key} -> {status}")
    except Exception as e:
        logger.warning(f"jira: transition/comment failed (non-fatal): {e}")


async def add_jira_comment(cluster_id: str, issue_key: str, body: str) -> None:
    """Add a standalone comment to an existing Jira issue. No-op unless Jira
    is configured for this cluster."""
    try:
        from backend import crud, database

        async with database.AsyncSessionLocal() as db:
            cluster = await crud.get_cluster_by_id(db, uuid.UUID(str(cluster_id)))
    except Exception as e:
        logger.debug(f"jira: cluster lookup skipped ({e})")
        return
    if not jira_configured(cluster) or not issue_key:
        return

    try:
        import httpx

        base = cluster.jira_url.rstrip("/")
        auth = (cluster.jira_email, cluster.jira_api_token)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base}/rest/api/3/issue/{issue_key}/comment",
                json={"body": _adf_doc(body)},
                auth=auth,
            )
            resp.raise_for_status()
    except Exception as e:
        logger.warning(f"jira: comment failed (non-fatal): {e}")
