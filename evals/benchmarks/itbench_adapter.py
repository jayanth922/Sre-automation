#!/usr/bin/env python3
"""
ITBench adapter (competitive-audit upgrade #1: run against the real benchmark).

IBM's **ITBench** (AAAI'26; SRE/FinOps/CISO; open, with a public leaderboard) is
the credible, comparable way to score an SRE agent — vs. our home-grown
benchmark. ITBench injects a fault into a live K8s cluster and grades the agent's
**diagnosis** (root cause + affected entities) and **remediation**.

This adapter maps our agent's output (the ACT report + transcript) into the
diagnosis shape an ITBench grader expects, and provides a scenario runner that
drives our pipeline. ITBench agents run containerized with a mounted KUBECONFIG
and register via the ITBench GitHub app; align the output fields below with the
current ITBench-CISO-SRE-FinOps-Agent harness contract before a scored run.

Ref: https://github.com/itbench-hub/ITBench
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


@dataclass
class ITBenchResult:
    fault_hypothesis: str
    affected_entities: List[str] = field(default_factory=list)
    remediation: List[Dict[str, str]] = field(default_factory=list)
    resolved: bool = False
    diagnosis: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def to_itbench_result(act_report: Dict[str, Any], alert: Any, summary: str = "") -> ITBenchResult:
    """Map our ACT report + alert into an ITBench-style diagnosis. Pure/testable."""
    labels = _get(alert, "labels", {}) or {}
    service = str(labels.get("service") or labels.get("app") or "unknown")
    hypothesis = (act_report.get("resolution_report", {}) or {}).get("root_cause") \
        or act_report.get("severity_rationale") \
        or "See investigation."

    entities = [service] if service != "unknown" else []
    for a in act_report.get("action_reports", []) or []:
        tgt = a.get("target")
        if tgt and tgt not in entities:
            entities.append(tgt)

    remediation = [
        {"action_type": a.get("action_type", ""), "target": a.get("target", "")}
        for a in (act_report.get("executed") or act_report.get("action_reports") or [])
    ]
    verification = act_report.get("verification") or {}
    resolved = verification.get("status") == "RESOLVED"

    return ITBenchResult(
        fault_hypothesis=str(hypothesis),
        affected_entities=entities,
        remediation=remediation,
        resolved=resolved,
        diagnosis=summary or str(hypothesis),
    )


async def run_scenario(
    scenario: Dict[str, Any],
    agent_invoke: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
) -> ITBenchResult:
    """Run one ITBench SRE scenario through our agent.

    ``scenario`` describes the injected fault (alert/labels). ``agent_invoke``
    fires our pipeline and returns ``{"act_report": ..., "summary": ..., "alert": ...}``
    (injected so this is testable without the ITBench cluster).
    """
    out = await agent_invoke(scenario)
    return to_itbench_result(out.get("act_report", {}), out.get("alert"), out.get("summary", ""))
