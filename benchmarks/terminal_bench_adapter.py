#!/usr/bin/env python3
"""
Terminal-Bench adapter (project #1 external validation).

Terminal-Bench (tbench.ai) runs agents inside containerized terminal tasks via
the Harbor framework. A custom agent is registered either with
``tb run --agent-import-path terminal_bench_adapter:SRETerminalAgent`` or by
implementing Harbor's ``AbstractInstalledAgent`` interface — an install script,
a run command, env vars, and a name.

This module provides that adapter so the project's terminal-executing agent can
be scored on the public Terminal-Bench leaderboard (submissions require public
trajectories). It wraps the same command-execution surface the ACT phase uses.

NOTE: `terminal-bench` is not a project dependency (you install it only when
benchmarking). The Harbor base class is imported lazily; if it is missing, a
minimal local stub with the same method surface is used so this file still
imports and is unit-testable. Confirm the import path and method signatures
against your installed terminal-bench version before a real run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List

try:  # real Harbor base class when terminal-bench is installed
    from terminal_bench.agents.installed_agents.abstract_installed_agent import (  # type: ignore
        AbstractInstalledAgent as _BaseAgent,
    )

    _HARBOR_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only without terminal-bench
    class _BaseAgent:  # minimal stub mirroring the interface we implement
        pass

    _HARBOR_AVAILABLE = False


# The install script Harbor runs inside the task container to set our agent up.
INSTALL_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
# Install the lightweight terminal-agent runtime and its deps.
pip install --no-cache-dir httpx >/dev/null 2>&1 || true
echo "sre-terminal-agent installed"
"""


class SRETerminalAgent(_BaseAgent):
    """Harbor-installable agent wrapping our terminal-executing runtime."""

    @staticmethod
    def name() -> str:
        return "sre-terminal-agent"

    def _install_agent_script_path(self) -> str:
        """Write and return the path to the container-side install script."""
        path = Path(os.getenv("TB_INSTALL_SCRIPT_PATH", "/tmp/sre_agent_install.sh"))
        path.write_text(INSTALL_SCRIPT)
        path.chmod(0o755)
        return str(path)

    def _env(self) -> Dict[str, str]:
        """Environment passed into the task container (model provider, keys)."""
        env = {"LLM_PROVIDER": os.getenv("LLM_PROVIDER", "ollama")}
        for key in ("GROQ_API_KEY", "GOOGLE_API_KEY", "NVIDIA_API_KEY", "OLLAMA_BASE_URL"):
            if os.getenv(key):
                env[key] = os.environ[key]
        return env

    def _run_agent_commands(self, instruction: str) -> List[str]:
        """Command(s) Harbor runs to have the agent attempt the task."""
        # The agent reads the task instruction and drives the terminal to solve it.
        safe = instruction.replace("'", "'\\''")
        return [f"python -m sre_terminal_agent --task '{safe}'"]


def harbor_available() -> bool:
    return _HARBOR_AVAILABLE
