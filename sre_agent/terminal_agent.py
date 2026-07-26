#!/usr/bin/env python3
"""
Terminal agent (project #1, proper) — an agentic terminal loop.

This is the actual "build a terminal agent" project from the video, not just the
ACT executor. It drives a real shell to accomplish a natural-language task:
decide → run a command → observe output → repeat, until the task is solved or a
budget is hit. It's the runtime the Terminal-Bench adapter registers.

Two things the video specifically calls out are implemented here:
- **Sub-agent orchestration** — the decider can spawn a nested agent for a
  sub-task (the technique that keeps a lean agent like `pi` competitive with
  Claude Code / Codex on terminal-bench).
- **Safety** — a deny-list blocks obviously destructive commands, every command
  runs in a sandbox working dir with a timeout and a step budget.

The reasoning is injected as a ``decider`` (an LLM in production via
``make_llm_decider``; a scripted function in tests), so the whole loop is
deterministic and unit-testable without a model.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# A decider maps (task, history) → the next action.
#   {"action": "run",      "command": "...", "reason": "..."}
#   {"action": "subagent", "task": "...",    "reason": "..."}   # spawn a nested agent
#   {"action": "done",     "reason": "...",  "success": true}
Decider = Callable[[str, List["TerminalStep"]], Dict[str, Any]]

# Obviously destructive commands are refused outright (defense-in-depth; the
# terminal-bench harness also sandboxes, but we never rely solely on that).
_DENY_PATTERNS = [
    r"\brm\s+-rf\s+/(?:\s|$)",
    r"\brm\s+-rf\s+~",
    r":\(\)\s*\{\s*:\|:&\s*\};:",   # fork bomb
    r"\bmkfs\b",
    r"\bdd\s+if=.*of=/dev/",
    r">\s*/dev/sd[a-z]",
    r"\bshutdown\b|\breboot\b|\bhalt\b",
    r"\bchmod\s+-R\s+777\s+/(?:\s|$)",
    r"\b(curl|wget)\b.*\|\s*(sh|bash)\b",   # pipe-to-shell
]


@dataclass
class TerminalStep:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    kind: str = "run"          # run | subagent | blocked


@dataclass
class TerminalRunResult:
    task: str
    steps: List[TerminalStep] = field(default_factory=list)
    status: str = "GAVE_UP"    # SOLVED | GAVE_UP | BLOCKED | ERROR
    summary: str = ""

    def transcript(self) -> str:
        return "\n".join(f"$ {s.command}\n[exit {s.exit_code}] {s.stdout}{s.stderr}".rstrip() for s in self.steps)


def is_command_safe(command: str) -> tuple[bool, str]:
    c = " ".join((command or "").split())
    if not c:
        return False, "empty command"
    for pat in _DENY_PATTERNS:
        if re.search(pat, c):
            return False, f"blocked by safety deny-list ({pat})"
    return True, "ok"


class TerminalAgent:
    """Agentic terminal loop with sub-agent orchestration and safety bounds."""

    def __init__(
        self,
        decider: Decider,
        workdir: Optional[str] = None,
        max_steps: int = 15,
        timeout: int = 60,
        allow_subagents: bool = True,
        _depth: int = 0,
    ) -> None:
        self.decider = decider
        self.workdir = workdir or os.getcwd()
        self.max_steps = max_steps
        self.timeout = timeout
        self.allow_subagents = allow_subagents
        self._depth = _depth

    def _run_command(self, command: str) -> TerminalStep:
        safe, reason = is_command_safe(command)
        if not safe:
            logger.warning(f"🛑 TerminalAgent: refused command: {command} ({reason})")
            return TerminalStep(command=command, exit_code=126, stdout="", stderr=f"REFUSED: {reason}", kind="blocked")
        try:
            proc = subprocess.run(
                command, shell=True, cwd=self.workdir, capture_output=True,
                text=True, timeout=self.timeout,
            )
            return TerminalStep(command, proc.returncode, proc.stdout[-4000:], proc.stderr[-2000:])
        except subprocess.TimeoutExpired:
            return TerminalStep(command, 124, "", f"timeout after {self.timeout}s")
        except Exception as e:
            return TerminalStep(command, 1, "", str(e))

    def _run_subagent(self, subtask: str) -> TerminalStep:
        if not self.allow_subagents or self._depth >= 2:
            return TerminalStep(f"[subagent] {subtask}", 1, "", "sub-agents disabled or max depth reached", kind="subagent")
        logger.info(f"🤝 TerminalAgent: spawning sub-agent for: {subtask}")
        sub = TerminalAgent(
            self.decider, workdir=self.workdir, max_steps=max(3, self.max_steps // 2),
            timeout=self.timeout, allow_subagents=True, _depth=self._depth + 1,
        )
        result = sub.run(subtask)
        return TerminalStep(f"[subagent] {subtask}", 0 if result.status == "SOLVED" else 1,
                            result.transcript()[-2000:], "", kind="subagent")

    def run(self, task: str) -> TerminalRunResult:
        result = TerminalRunResult(task=task)
        for _ in range(self.max_steps):
            decision = self.decider(task, result.steps)
            action = str(decision.get("action", "")).lower()

            if action == "done":
                result.status = "SOLVED" if decision.get("success", True) else "GAVE_UP"
                result.summary = decision.get("reason", "")
                return result
            if action == "subagent":
                result.steps.append(self._run_subagent(str(decision.get("task", ""))))
                continue
            if action == "run":
                step = self._run_command(str(decision.get("command", "")))
                result.steps.append(step)
                continue

            result.status = "ERROR"
            result.summary = f"unknown action: {action!r}"
            return result

        result.status = "GAVE_UP"
        result.summary = f"step budget ({self.max_steps}) exhausted"
        return result


def make_llm_decider(llm: Any) -> Decider:
    """Build a decider backed by an LLM (used at runtime / for terminal-bench)."""
    import json

    def _decide(task: str, history: List[TerminalStep]) -> Dict[str, Any]:
        from langchain_core.messages import HumanMessage, SystemMessage

        transcript = "\n".join(f"$ {s.command}\n[{s.exit_code}] {s.stdout}{s.stderr}"[:1500] for s in history[-6:])
        prompt = (
            f"TASK: {task}\n\nRECENT TERMINAL HISTORY:\n{transcript or '(none yet)'}\n\n"
            "Decide the next action. Respond with ONLY JSON: "
            '{"action":"run|subagent|done","command":"<shell cmd if run>",'
            '"task":"<subtask if subagent>","success":true,"reason":"<why>"}'
        )
        resp = llm.invoke([
            SystemMessage(content="You are a terminal agent. Solve the task with shell commands, one step at a time."),
            HumanMessage(content=prompt),
        ])
        text = str(getattr(resp, "content", resp)).strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            return json.loads(match.group(0)) if match else {"action": "done", "success": False, "reason": "no decision"}
        except Exception:
            return {"action": "done", "success": False, "reason": f"unparseable decision: {text[:120]}"}

    return _decide


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the terminal agent on a task.")
    parser.add_argument("--task", required=True)
    parser.add_argument("--workdir", default=os.getcwd())
    parser.add_argument("--max-steps", type=int, default=15)
    args = parser.parse_args()

    from .model_router import TaskType, route_llm

    llm = route_llm(TaskType.SPECIALIST, use_fallback=True)
    agent = TerminalAgent(make_llm_decider(llm), workdir=args.workdir, max_steps=args.max_steps)
    result = agent.run(args.task)
    print(result.transcript())
    print(f"\n== {result.status}: {result.summary}")
    return 0 if result.status == "SOLVED" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
