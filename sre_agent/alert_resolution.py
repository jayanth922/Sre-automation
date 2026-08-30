#!/usr/bin/env python3
"""Reconcile Alertmanager *resolved* notifications with open incidents.

Firing alerts create incidents; resolved alerts must not be ignored. External
clear is treated as verification evidence, but never masks a failed remediation
(`REMEDIATION_FAILED` stays failed with a timeline note).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# Compare by value so unit tests that load models via importlib stay compatible.
_ACTIVE_VALUES = frozenset(
    {
        "open",
        "investigating",
        "investigated",
        "awaiting_approval",
        "remediation_in_progress",
        "remediation_failed",
        "verification_unknown",
    }
)
_FAILED = "remediation_failed"
_RESOLVED = "resolved"


def _status_value(status: Any) -> Optional[str]:
    if status is None:
        return None
    if hasattr(status, "value"):
        return str(status.value)
    return str(status)


@dataclass(frozen=True)
class ResolvedAlertDecision:
    """Outcome of correlating a resolved alert with an incident."""

    matched: bool
    previous_status: Optional[str]
    new_status: Optional[str]
    mark_resolved: bool
    masked_failed_remediation: bool
    reason: str


def is_active_incident_status(status: Any) -> bool:
    """True when the incident is still open for alert correlation."""
    value = _status_value(status)
    return value in _ACTIVE_VALUES if value else False


def reconcile_resolved_alert(current_status: Any) -> ResolvedAlertDecision:
    """Decide how an external alert-clear updates an incident.

    Rules:
    - No active incident → no-op (caller may have nothing to update).
    - ``remediation_failed`` → keep failed; do **not** set resolved.
    - Any other active status → resolved (alert clear is external verification).
    """
    value = _status_value(current_status)
    if value not in _ACTIVE_VALUES:
        return ResolvedAlertDecision(
            matched=False,
            previous_status=value,
            new_status=None,
            mark_resolved=False,
            masked_failed_remediation=False,
            reason="no_active_incident",
        )

    if value == _FAILED:
        return ResolvedAlertDecision(
            matched=True,
            previous_status=value,
            new_status=_FAILED,
            mark_resolved=False,
            masked_failed_remediation=True,
            reason="alert_cleared_but_remediation_failed",
        )

    return ResolvedAlertDecision(
        matched=True,
        previous_status=value,
        new_status=_RESOLVED,
        mark_resolved=True,
        masked_failed_remediation=False,
        reason="alert_cleared_external_verification",
    )
