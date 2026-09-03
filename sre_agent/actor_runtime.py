#!/usr/bin/env python3
"""
Pluggable actor runtime (project #2, proper — actually using Hermes/OpenClaw).

Design decision (see docs): LangGraph stays the **orchestrator** of the safe,
auditable incident-response flow (supervisor → specialists → reflector → planner
→ policy gate → executor, with checkpoints and human approval). An open-source
*autonomous agent framework* like Nous Research's **Hermes Agent** belongs at the
**"hands"** — the bounded actor that autonomously carries out a task with tools.

So the actor is pluggable behind ``AgentRuntime``:
- ``LocalTerminalRuntime`` — our own ``TerminalAgent`` (default; no extra deps).
- ``HermesRuntime`` — Nous Research Hermes Agent (its self-improving, tool-using
  autonomous loop). Written against Hermes's documented Python API
  (``from run_agent import AIAgent``; ``run_conversation``/``chat``); it is a real
  dependency you install only when you select this backend.

Select via ``AGENT_RUNTIME=local|hermes``. This is how the project "uses Hermes"
without replacing the orchestration engine.

Ref: https://hermes-agent.nousresearch.com/docs/guides/python-library
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ActorResult:
    backend: str
    status: str        # SOLVED | DONE | GAVE_UP | ERROR
    output: str
    detail: str = ""


class AgentRuntime:
    """Interface every actor backend implements."""

    name = "base"

    def run(self, task: str) -> ActorResult:  # pragma: no cover - abstract
        raise NotImplementedError


class LocalTerminalRuntime(AgentRuntime):
    """Default backend: our own agentic terminal loop (see terminal_agent.py)."""

    name = "local-terminal"

    def __init__(self, decider: Optional[Callable] = None, workdir: Optional[str] = None, max_steps: int = 15):
        self._decider = decider
        self.workdir = workdir
        self.max_steps = max_steps

    def _resolve_decider(self) -> Callable:
        if self._decider is not None:
            return self._decider
        from .model_router import TaskType, route_llm
        from .terminal_agent import make_llm_decider

        return make_llm_decider(route_llm(TaskType.SPECIALIST, use_fallback=True))

    def run(self, task: str) -> ActorResult:
        from .terminal_agent import TerminalAgent

        result = TerminalAgent(self._resolve_decider(), workdir=self.workdir, max_steps=self.max_steps).run(task)
        # TerminalAgent's final "done" reasoning lands in result.summary, not
        # the command transcript — callers (e.g. generate_patch_activity's
        # BASELINE_COMMAND/CANDIDATE_COMMAND markers) ask the actor to end its
        # *response* with specific text, so that reasoning must be part of
        # ActorResult.output for downstream text parsing to ever see it.
        output = result.transcript()
        if result.summary:
            output = f"{output}\n\n{result.summary}".strip()
        return ActorResult(self.name, result.status, output, result.summary)


class HermesRuntime(AgentRuntime):
    """Nous Research Hermes Agent as the autonomous actor.

    Requires ``hermes-agent`` installed and an ``OPENROUTER_API_KEY`` (or
    ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``). Its constructor + method surface
    matches the documented Python-library API.
    """

    name = "hermes"

    def __init__(
        self,
        model: str = "",
        max_iterations: int = 20,
        disabled_toolsets: Optional[List[str]] = None,
        ephemeral_system_prompt: Optional[str] = None,
        task_id: str = "sre-actor",
        workdir: Optional[str] = None,
    ):
        self.model = model
        self.max_iterations = max_iterations
        self.disabled_toolsets = disabled_toolsets
        self.ephemeral_system_prompt = ephemeral_system_prompt
        self.task_id = task_id
        self.workdir = workdir

    def _build_agent(self):
        try:
            from run_agent import AIAgent  # provided by the hermes-agent package
        except Exception as e:
            raise RuntimeError(
                "hermes-agent not installed. Install with: "
                "pip install git+https://github.com/NousResearch/hermes-agent.git"
            ) from e

        kwargs = dict(
            model=self.model,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,  # keep Hermes's self-improving skill/memory loop on
            max_iterations=self.max_iterations,
            disabled_toolsets=self.disabled_toolsets,
            ephemeral_system_prompt=self.ephemeral_system_prompt,
        )
        if self.workdir:
            # Best-effort: `workdir` is the param name our own LocalTerminalRuntime
            # uses; not independently verified against hermes-agent's actual
            # constructor signature (never installed in this environment — see
            # docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md Phase F). Dropped
            # rather than failing construction if the package rejects it.
            try:
                return AIAgent(workdir=self.workdir, **kwargs)
            except TypeError:
                logger.warning("HermesRuntime: AIAgent does not accept workdir=; running without it")
        return AIAgent(**kwargs)

    def run(self, task: str) -> ActorResult:
        agent = self._build_agent()
        try:
            result = agent.run_conversation(user_message=task, task_id=self.task_id)
            output = result.get("final_response", "") if isinstance(result, dict) else str(result)
            return ActorResult(self.name, "DONE", output, f"messages={len(result.get('messages', [])) if isinstance(result, dict) else 0}")
        except Exception as e:
            logger.error(f"HermesRuntime failed: {e}")
            return ActorResult(self.name, "ERROR", "", str(e))


def get_agent_runtime(name: Optional[str] = None, **kwargs) -> AgentRuntime:
    """Factory. ``AGENT_RUNTIME=local|hermes`` (default local)."""
    name = (name or os.getenv("AGENT_RUNTIME", "local")).lower()
    if name in ("hermes", "openclaw"):  # OpenClaw shares Hermes's migration-compatible surface
        return HermesRuntime(**kwargs)
    return LocalTerminalRuntime(**kwargs)
