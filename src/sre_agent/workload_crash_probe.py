#!/usr/bin/env python3
"""Did the service's container terminate while its incident was open?

An alert that watches a process metric clears when the process dies: an
OOMKilled container restarts with a fresh heap, the gauge drops, and
Alertmanager reports the alert resolved while the fault is still there. That
clear is not recovery, so `api.v1.alerts._reconcile_resolved_alert` asks this
probe before it closes an incident.

The evidence is kube-state-metrics' `last_terminated_timestamp` /
`last_terminated_reason` for the service's container. They only move when a
container inside a pod terminates and is restarted in place, so a rollout
restart or a pod recreation — which replace the pod — never reads as a crash.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

import httpx

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 5.0
# Kubernetes and the platform keep separate clocks; a termination this close
# before the incident row was written still belongs to the same episode.
CLOCK_SKEW = timedelta(seconds=60)

# Label values are interpolated into PromQL, so anything outside the DNS-label
# alphabet Kubernetes allows for container and namespace names is refused
# rather than escaped.
_LABEL_VALUE = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,251}[a-z0-9])?$")

QueryFn = Callable[[str], Awaitable[List[Dict[str, Any]]]]


@dataclass(frozen=True)
class WorkloadCrash:
    pod: str
    container: str
    reason: str
    terminated_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pod": self.pod,
            "container": self.container,
            "reason": self.reason,
            "terminated_at": self.terminated_at.isoformat(),
        }


@dataclass(frozen=True)
class CrashProbe:
    state: Literal["crashed", "none", "unavailable"]
    crash: Optional[WorkloadCrash] = None
    detail: str = ""


def _prometheus_query_fn(prometheus_url: str) -> QueryFn:
    base = prometheus_url.rstrip("/")

    async def query(promql: str) -> List[Dict[str, Any]]:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"{base}/api/v1/query", params={"query": promql})
            resp.raise_for_status()
            body = resp.json()
        if body.get("status") != "success":
            raise ValueError(f"prometheus status {body.get('status')!r}")
        return list((body.get("data") or {}).get("result") or [])

    return query


async def probe_workload_crash(
    prometheus_url: Optional[str],
    service: Optional[str],
    since: Optional[datetime],
    *,
    namespace: Optional[str] = None,
    query: Optional[QueryFn] = None,
) -> CrashProbe:
    """Find the latest termination of ``service``'s container after ``since``."""
    container = str(service or "").strip().lower()
    ns = str(namespace or "").strip().lower()
    if not container or not _LABEL_VALUE.match(container):
        return CrashProbe("unavailable", detail="no usable service label")
    if ns and not _LABEL_VALUE.match(ns):
        return CrashProbe("unavailable", detail="unusable namespace label")
    if since is None:
        return CrashProbe("unavailable", detail="incident has no start time")
    if query is None:
        if not prometheus_url:
            return CrashProbe("unavailable", detail="cluster has no Prometheus URL")
        query = _prometheus_query_fn(prometheus_url)

    selector = f'container="{container}"' + (f',namespace="{ns}"' if ns else "")
    try:
        stamps = await query(
            f"kube_pod_container_status_last_terminated_timestamp{{{selector}}}"
        )
        reasons = await query(
            f"kube_pod_container_status_last_terminated_reason{{{selector}}} == 1"
        )
    except Exception as exc:
        logger.warning("crash probe for %s failed: %s", container, exc)
        return CrashProbe("unavailable", detail=f"query failed: {type(exc).__name__}")

    reason_by_pod = {
        str(r.get("metric", {}).get("pod", "")): str(
            r.get("metric", {}).get("reason", "") or "unknown"
        )
        for r in reasons
    }
    cutoff = since.astimezone(timezone.utc) - CLOCK_SKEW
    latest: Optional[WorkloadCrash] = None
    for row in stamps:
        try:
            ts = float(row["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        at = datetime.fromtimestamp(ts, tz=timezone.utc)
        if at < cutoff or (latest is not None and at <= latest.terminated_at):
            continue
        pod = str(row.get("metric", {}).get("pod", ""))
        latest = WorkloadCrash(
            pod=pod,
            container=container,
            reason=reason_by_pod.get(pod, "unknown"),
            terminated_at=at,
        )
    if latest is None:
        return CrashProbe("none", detail="no container termination since the incident opened")
    return CrashProbe("crashed", crash=latest)
