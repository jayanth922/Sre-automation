#!/usr/bin/env python3
"""
AIOpsLab adapter (Phase 5 — real benchmark).

Microsoft's **AIOpsLab** (NeurIPS'24 / AAAI'26; open, OSS,
github.com/microsoft/AIOpsLab) is architected completely differently from
ITBench. It is not an HTTP fault-injection service you fire a webhook at and
poll — it is an in-process orchestrator (`aiopslab.orchestrator.Orchestrator`)
that owns fault injection, workload generation, and evaluation itself (via a
Helm/kind-deployed cluster + Prometheus it manages), and drives a registered
agent turn-by-turn: each turn it calls ``agent.get_action(state: str) -> str``,
parses the returned string as one Python-call-shaped action inside a single
markdown code fence (e.g. ``` ```\\nexec_shell("kubectl get pods -n ns")\\n``` ```
or ``` ```\\nsubmit(...)\\n``` ```), executes it against the problem's own
action surface, and — after ``submit`` or ``max_steps`` — grades the trace
itself, returning a results dict keyed by task type: ``TTD``/``TTL``/``TTA``/
``TTM`` (detect/localize/analyze/mitigate) plus accuracy fields.

This is why the plan's original sketch for this phase (fire a synthetic
Alertmanager webhook, then poll ``recovery_oracle.py``, ``sre_bench.py``-style)
does not apply here — confirmed by reading the live package (README.md,
``aiopslab/orchestrator/{orchestrator,parser}.py``,
``aiopslab/orchestrator/tasks/{detection,localization,analysis,mitigation}.py``)
before writing this file, per the plan's explicit caveat to check AIOpsLab's
actual scenario/API format first. The shape this adapter needs is closer to
``terminal_bench_adapter.py``'s: wrap our agent to satisfy an *external*
harness's per-step interface, not our own webhook+oracle harness.

Four task types, four ``submit()`` shapes (checked against the live task
sources):
    detection    -> submit("Yes" | "No")
    localization -> submit([<service names>]) | submit([])
    analysis     -> submit({"system_level": ..., "fault_type": ...}) | submit()
    mitigation   -> exec_shell(<cmd>)* then submit()  (no submit params)

Our pipeline investigates and remediates in one shot — it does not explore
turn-by-turn via AIOpsLab's own ``exec_shell`` the way the reference GPT
client does. So ``SREAIOpsLabAgent`` runs our pipeline once on the first
turn, then plays back a queue of AIOpsLab action strings built from that one
investigation: for mitigation, one ``exec_shell(...)`` per already-executed
remediation command (replaying the exact shell/kubectl string
``sre_agent.executor.build_command()`` already produced for each cleared
action — see ``ExecutionResult.command`` in ``sre_agent/executor.py``),
followed by the task-appropriate ``submit(...)``.

``agent_invoke`` uses the same ``Callable[[Dict], Awaitable[Dict]]`` ->
``{"act_report": ..., "summary": ...}`` shape as ``itbench_adapter.py``'s,
deliberately, so one investigation-invoking callable can drive both adapters.

NOTE: ``aiopslab`` is not a project dependency (same reasoning as
``terminal_bench_adapter.py`` for ``terminal-bench``: install only when
actually benchmarking — it pulls in Helm/kind/Prometheus). Imported lazily;
a minimal local stub keeps this file importable and unit-testable without
it. Confirm the import path and method signatures against your installed
aiopslab version before a real run; requires a local kind/minikube cluster
with Helm (no vendor account needed — OSS benchmark).

Ref: https://github.com/microsoft/AIOpsLab
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

try:  # real orchestrator when aiopslab is installed
    from aiopslab.orchestrator import Orchestrator  # type: ignore

    _AIOPSLAB_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only without aiopslab
    Orchestrator = None  # type: ignore
    _AIOPSLAB_AVAILABLE = False


def aiopslab_available() -> bool:
    return _AIOPSLAB_AVAILABLE


TASK_TYPES = ("detection", "localization", "analysis", "mitigation")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _fence(action: str) -> str:
    """Wrap one action in the single markdown code fence AIOpsLab's ResponseParser requires."""
    return f"```\n{action}\n```"


def _quote(value: str) -> str:
    """Render a Python double-quoted string literal AIOpsLab's ast-based parser accepts."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_submit_action(
    task_type: str, act_report: Dict[str, Any], summary: str = ""
) -> str:
    """Map our ACT report + summary into the AIOpsLab ``submit()`` call for one task type.

    Pure/testable — mirrors ``itbench_adapter.to_itbench_result``'s role, but the
    target shape here is a literal AIOpsLab action string, not a result object,
    because AIOpsLab (unlike ITBench) grades a submitted *action*, not a
    separately-shaped diagnosis payload.
    """
    if task_type not in TASK_TYPES:
        raise ValueError(f"unknown AIOpsLab task_type: {task_type!r}")

    resolution = act_report.get("resolution_report", {}) or {}
    root_cause = str(
        resolution.get("root_cause") or act_report.get("severity_rationale") or summary or ""
    )
    verification = act_report.get("verification") or {}
    anomaly_detected = bool(root_cause.strip()) or verification.get("status") not in (
        None,
        "NOT_APPLICABLE",
    )

    if task_type == "detection":
        return _fence(f"submit({_quote('Yes' if anomaly_detected else 'No')})")

    if task_type == "localization":
        services: List[str] = []
        for a in act_report.get("action_reports", []) or []:
            tgt = a.get("target")
            if tgt and tgt not in services:
                services.append(str(tgt))
        if not services and resolution.get("affected_service"):
            services.append(str(resolution["affected_service"]))
        rendered = ", ".join(_quote(s) for s in services)
        return _fence(f"submit([{rendered}])")

    if task_type == "analysis":
        if not anomaly_detected:
            return _fence("submit()")
        system_level = str(resolution.get("system_level") or "Application")
        fault_type = str(resolution.get("fault_type") or "Misconfiguration")
        return _fence(
            "submit({"
            f"{_quote('system_level')}: {_quote(system_level)}, "
            f"{_quote('fault_type')}: {_quote(fault_type)}"
            "})"
        )

    # mitigation: submit() takes no params; remediation happens via exec_shell first
    return _fence("submit()")


def build_mitigation_shell_actions(act_report: Dict[str, Any]) -> List[str]:
    """One ``exec_shell(...)`` per already-executed remediation command.

    Replays the exact shell/kubectl string ``sre_agent.executor.build_command()``
    already produced for each cleared action (see ``ExecutionResult.command``)
    against AIOpsLab's own cluster, rather than re-deriving a translation.
    """
    commands: List[str] = []
    for a in act_report.get("executed") or act_report.get("action_reports") or []:
        command = a.get("command")
        if command and not str(command).startswith("#"):
            commands.append(str(command))
    return [_fence(f"exec_shell({_quote(cmd)})") for cmd in commands]


@dataclass
class AIOpsLabRunResult:
    """Normalized shape of one AIOpsLab problem run, for reporting alongside our other benchmarks."""

    problem_id: str
    task_type: str
    steps_taken: int
    results: Dict[str, Any] = field(default_factory=dict)
    final_state: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def from_aiopslab_run(
    problem_id: str, task_type: str, orchestrator_output: Dict[str, Any]
) -> AIOpsLabRunResult:
    """Normalize ``Orchestrator.start_problem()``'s return dict. Pure/testable."""
    history = orchestrator_output.get("history") or []
    return AIOpsLabRunResult(
        problem_id=problem_id,
        task_type=task_type,
        steps_taken=sum(1 for item in history if _get(item, "role") == "assistant"),
        results=dict(orchestrator_output.get("results") or {}),
        final_state=str(orchestrator_output.get("final_state") or ""),
    )


class SREAIOpsLabAgent:
    """AIOpsLab-facing agent: satisfies ``get_action(state: str) -> str``.

    Runs our pipeline once on the first turn (``agent_invoke``, injected so
    this is testable without AIOpsLab or our platform running), then plays
    back a fixed queue of AIOpsLab action strings built from that one
    investigation.
    """

    def __init__(
        self,
        task_type: str,
        agent_invoke: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
        problem_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        if task_type not in TASK_TYPES:
            raise ValueError(f"unknown AIOpsLab task_type: {task_type!r}")
        self.task_type = task_type
        self._agent_invoke = agent_invoke
        self._problem_context: Dict[str, Any] = dict(problem_context or {})
        self._queue: Optional[List[str]] = None

    def init_context(self, problem_desc: str, instructions: str, apis: Dict[str, Any]) -> None:
        """Mirrors the AIOpsLab onboarding contract's step 2 (see README's "How to
        onboard your agent to AIOpsLab?"): the orchestrator hands us the problem
        description, task instructions, and available action docs after
        ``init_problem``, before ``start_problem`` begins driving turns.
        """
        self._problem_context.update(
            {"problem_desc": problem_desc, "instructions": instructions, "apis": apis}
        )

    async def get_action(self, state: str) -> str:
        if self._queue is None:
            out = await self._agent_invoke({**self._problem_context, "state": state})
            act_report = out.get("act_report", {}) or {}
            summary = out.get("summary", "")
            queue: List[str] = []
            if self.task_type == "mitigation":
                queue.extend(build_mitigation_shell_actions(act_report))
            queue.append(build_submit_action(self.task_type, act_report, summary))
            self._queue = queue
        return self._queue.pop(0) if self._queue else _fence("submit()")


async def run_problem(
    problem_id: str,
    task_type: str,
    agent_invoke: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
    *,
    max_steps: int = 30,
    results_dir: Optional[str] = None,
) -> AIOpsLabRunResult:
    """Drive one AIOpsLab problem through our agent using the real package.

    Requires ``aiopslab`` installed and a reachable kind/minikube cluster with
    Helm (see the module docstring) — not importable/runnable in CI. Use
    ``SREAIOpsLabAgent`` plus ``build_submit_action``/
    ``build_mitigation_shell_actions`` directly for unit tests.
    """
    if not _AIOPSLAB_AVAILABLE:
        raise RuntimeError(
            "aiopslab is not installed; `pip install -e` the AIOpsLab repo "
            "(github.com/microsoft/AIOpsLab) into this environment first"
        )
    orch = Orchestrator(results_dir=results_dir)
    agent = SREAIOpsLabAgent(task_type, agent_invoke)
    orch.register_agent(agent, name="sentinel")
    problem_desc, instructions, apis = orch.init_problem(problem_id)
    agent.init_context(problem_desc, instructions, apis)
    out = await orch.start_problem(max_steps=max_steps)
    return from_aiopslab_run(problem_id, task_type, out)
