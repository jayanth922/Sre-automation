#!/usr/bin/env python3
"""
Resolution report — the "here's what happened and how we fixed it" message.

After the ACT phase acts and verification confirms system state, we post a
clear, human-readable report back into the incident conversation: what the issue
was, the root cause, what the agent did autonomously (and its verified result),
and — for code-level causes — the **sandbox-tested suggested fix** for the human
to apply on their side (we recommend, they merge).

Deterministic assembly from the structured act_report + verification (+ optional
sandbox-tested code fix), with an optional LLM-authored narrative on top.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def build_resolution_report(
    state: Any,
    act_report: Dict[str, Any],
    verification: Optional[Dict[str, Any]] = None,
    code_fix: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble a structured + markdown resolution report. Deterministic."""
    alert = _get(state, "alert_context")
    labels = _get(alert, "labels", {}) or {}
    alert_name = str(_get(alert, "alert_name", labels.get("alertname", "incident")))
    service = str(labels.get("service", "the affected service"))
    reflector = _get(state, "reflector_analysis")
    hypothesis = str(_get(reflector, "hypothesis", act_report.get("severity_rationale", "See investigation."))
                     or "See investigation.")

    executed = act_report.get("executed") or []
    live_results = act_report.get("live_results") or []
    applied = live_results or executed
    severity = act_report.get("severity", "?")
    decision = act_report.get("aggregate_decision", "?")

    v_status = (verification or {}).get("status")
    resolved = v_status == "RESOLVED"

    # ── markdown ──────────────────────────────────────────────
    lines = [f"## 🩹 Incident resolution — {alert_name}", ""]
    lines.append(f"**Issue:** `{alert_name}` on `{service}` (severity **{severity}**).")
    lines.append(f"**Root cause:** {hypothesis}")
    lines.append("")
    lines.append("**What the agent did:**")
    if applied:
        for a in applied:
            cmd = a.get("command") or a.get("action_type")
            lines.append(f"- `{a.get('action_type')}` → {cmd}")
    elif decision == "requires_approval":
        lines.append("- Held for human approval (higher severity); no autonomous action taken.")
    else:
        lines.append("- No autonomous action was required.")
    lines.append("")
    if verification:
        emoji = "✅" if resolved else ("⚠️" if v_status == "FAILED" else "ℹ️")
        lines.append(f"**Verification:** {emoji} {v_status or 'n/a'} — {verification.get('detail', '')}")
        lines.append("")

    if code_fix:
        status = code_fix.get("status")
        tested = "sandbox-tested ✅ PASS" if status == "TESTED_PASS" else (
            "sandbox-tested ⚠️ FAIL" if status == "TESTED_FAIL" else status)
        lines.append(f"**Suggested code fix ({tested}) — apply on your side:**")
        diff = code_fix.get("diff") or ""
        if diff:
            lines.append("```diff")
            lines.append(diff[:4000])
            lines.append("```")
        lines.append("")

    lines.append("**Next steps:** " + (
        "System state is back to normal. Review the suggested code fix above and apply it to prevent recurrence."
        if resolved else
        "Please review — the incident may need manual attention."
    ))

    markdown = "\n".join(lines)
    return {
        "alert_name": alert_name,
        "service": service,
        "severity": severity,
        "root_cause": hypothesis,
        "actions_applied": applied,
        "verification_status": v_status,
        "resolved": resolved,
        "code_fix": code_fix,
        "markdown": markdown,
    }
