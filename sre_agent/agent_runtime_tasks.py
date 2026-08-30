"""QUARANTINED: Old runner script.

Use sre_agent.agent_runtime instead.
"""
import warnings
from typing import Any
from .incident_runner import run_incident_investigation

async def run_graph_background_saas(incident_id: str, cluster_id: str, alert_name: str, job_id: str = None) -> Any:
    warnings.warn("agent_runtime_tasks is quarantined", DeprecationWarning)
    return await run_incident_investigation(
        incident_id=incident_id,
        cluster_id=cluster_id,
        alert_name=alert_name,
        job_id=job_id,
    )
