"""Approval interrupt survives graph reconstruction (restart/resume)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import TypedDict

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("CHECKPOINTER_ENABLED", "true")

approval_path = ROOT / "sre_agent" / "approval_flow.py"
spec = importlib.util.spec_from_file_location("approval_flow_p09", approval_path)
approval_flow = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = approval_flow
spec.loader.exec_module(approval_flow)


@pytest.mark.integration
def test_approval_restart_resumes_same_thread(monkeypatch):
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, StateGraph
    from langgraph.types import Command, interrupt

    cp_path = ROOT / "sre_agent" / "checkpointer.py"
    cp_spec = importlib.util.spec_from_file_location("checkpointer_p09", cp_path)
    checkpointer = importlib.util.module_from_spec(cp_spec)
    sys.modules[cp_spec.name] = checkpointer
    cp_spec.loader.exec_module(checkpointer)

    class State(TypedDict, total=False):
        action_hash: str
        approved: bool

    saver = MemorySaver()
    monkeypatch.setenv("CHECKPOINTER_ENABLED", "true")
    monkeypatch.setattr(checkpointer, "_memory_saver", lambda: saver)

    async def build_graph():
        workflow = StateGraph(State)

        def gate(state):
            resumed = interrupt(
                {"type": "approval_required", "action_hash": state["action_hash"]}
            )
            return {"approved": resumed["action_hash"] == state["action_hash"]}

        workflow.add_node("gate", gate)
        workflow.set_entry_point("gate")
        workflow.add_edge("gate", END)
        return workflow.compile(checkpointer=await checkpointer.get_checkpointer())

    async def scenario():
        config = {"configurable": {"thread_id": "p09-incident-1"}}
        graph = await build_graph()
        await graph.ainvoke({"action_hash": "d" * 64}, config=config)
        assert approval_flow.current_approval_interrupt(await graph.aget_state(config))

        del graph
        reconstructed = await build_graph()
        output = await reconstructed.ainvoke(
            Command(resume={"action_hash": "d" * 64}), config=config
        )
        assert output["approved"] is True

    asyncio.run(scenario())
