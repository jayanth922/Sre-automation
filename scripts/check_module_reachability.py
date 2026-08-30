#!/usr/bin/env python3
"""Fail if top-level sre_agent modules drift outside the owned surface (P10)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRE = ROOT / "sre_agent"

# Explicitly allowed to exist without being imported by the FastAPI/graph path.
EXPERIMENTAL = frozenset(
    {
        "actor_runtime",
        "terminal_agent",
        "code_sandbox",
        "toolsets",
        "agent_audit",
        "models",
    }
)

ENTRY_FILES = [
    SRE / "agent_runtime.py",
    SRE / "agent_runtime_tasks.py",
    SRE / "graph_builder.py",
    SRE / "multi_agent_langgraph.py",
    # Standalone `python -m` worker processes: never imported by the API
    # itself, so they need to be declared as their own reachability roots.
    SRE / "sandbox_worker.py",
]


def _iter_local_imports(path: Path) -> set[str]:
    """Return relative import targets resolved to sre_agent top-level module names."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name.startswith("sre_agent."):
                    parts = name.split(".")
                    if len(parts) >= 2:
                        found.add(parts[1])
                elif name == "sre_agent":
                    pass
            continue

        # ImportFrom
        if node.module and node.module.startswith("sre_agent"):
            parts = node.module.split(".")
            if len(parts) == 1:
                for alias in node.names:
                    found.add(alias.name)
            elif len(parts) >= 2:
                found.add(parts[1])
        elif node.level and path.is_relative_to(SRE):
            # from .foo import bar  or  from . import foo
            if node.module:
                found.add(node.module.split(".")[0])
            else:
                for alias in node.names:
                    found.add(alias.name)
        elif node.module is None and node.level == 0:
            pass
    return found


def reachable_top_level() -> set[str]:
    queue = [p for p in ENTRY_FILES if p.exists()]
    # All API routers are production surface.
    queue.extend((SRE / "api").rglob("*.py"))
    queue.extend((SRE / "integrations").rglob("*.py"))

    seen_files: set[Path] = set()
    reach: set[str] = set()

    while queue:
        path = queue.pop()
        path = path.resolve()
        if path in seen_files or not path.exists():
            continue
        seen_files.add(path)
        if path.parent == SRE.resolve() and path.suffix == ".py" and path.stem != "__init__":
            reach.add(path.stem)

        for name in _iter_local_imports(path):
            candidate = SRE / f"{name}.py"
            if candidate.exists():
                queue.append(candidate)
            pkg = SRE / name / "__init__.py"
            if pkg.exists():
                queue.append(pkg)
                # Pull package modules shallowly
                queue.extend((SRE / name).glob("*.py"))

    return reach


def main() -> int:
    top = {
        p.stem
        for p in SRE.glob("*.py")
        if p.stem not in {"__init__"}
    }
    reach = reachable_top_level()
    unmanaged = sorted(top - reach - EXPERIMENTAL)
    if unmanaged:
        print("Unmanaged sre_agent top-level modules (not reachable, not experimental):")
        for name in unmanaged:
            print(f"  - {name}")
        print(
            "\nAdd an entry point, list in EXPERIMENTAL, or move to archive/experimental/."
        )
        return 1

    print(f"Reachability OK: {len(reach)} reachable, {len(EXPERIMENTAL)} experimental.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
