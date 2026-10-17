#!/usr/bin/env python3
"""Every model the agent builds is chosen by the model router.

A module that builds its own LLM skips tier selection, dynamic routing and
cost accounting, and silently falls back to the ANTHROPIC_MODEL default. That
default once named a retired model (claude-3-5-sonnet-latest), and the Slack
chat path kept sending requests to it with no error surfacing.
"""

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"

# Calls that build a chat model directly, bypassing route_llm().
_BUILDERS = frozenset(
    {
        "create_llm_with_fallback",
        "create_llm_with_error_handling",
        "build_litellm_llm",
        "ChatAnthropic",
        "ChatLiteLLM",
    }
)

# The router and the factories it delegates to.
_ALLOWED = frozenset(
    {
        "sre_agent/model_router.py",
        "sre_agent/llm_utils.py",
        "sre_agent/litellm_backend.py",
    }
)


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_only_the_model_router_builds_llms():
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel in _ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) in _BUILDERS:
                offenders.append(f"{rel}:{node.lineno} {_called_name(node)}()")
    assert not offenders, (
        "Build models with model_router.route_llm(TaskType...), not directly:\n"
        + "\n".join(offenders)
    )
