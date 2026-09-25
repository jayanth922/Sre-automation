#!/usr/bin/env python3
"""
Actor runtime: the bounded, tool-using "hands" that carries out a task.

LangGraph stays the **orchestrator** of the safe, auditable incident-response
flow (supervisor → specialists → reflector → planner → policy gate → executor,
with checkpoints and human approval). ``LocalTerminalRuntime`` — our own
Temporal-orchestrated ``TerminalAgent`` — is the deterministic actor behind
that boundary: first-party, no extra deps, live-fire validated.

(An earlier revision evaluated Nous Research's third-party Hermes Agent
framework as a pluggable alternative actor backend. Removed 2026-09-03 after
a safety review found it added risk — no filesystem sandbox, undocumented
toolset surface — without functional gain over the deterministic actor
already in place; see "Hermes safety review" and its follow-up in
docs/ai/DECISIONS.md.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

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


def get_agent_runtime(**kwargs) -> AgentRuntime:
    """Return the deterministic, Temporal-orchestrated actor backend."""
    return LocalTerminalRuntime(**kwargs)
