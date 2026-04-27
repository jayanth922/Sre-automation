"""General-purpose chat endpoint — talk to the SRE agent without an incident context."""
import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend import database, models
from sre_agent.api.v1.auth_deps import get_current_user_and_org

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    cluster_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str


async def _run_chat(message: str, session_id: str, cluster_id: str | None, user_id: str) -> str:
    """Run the agent graph with a free-form message, return the final response."""
    from sre_agent.agent_runtime import agent_graph, initialize_agent
    from langchain_core.messages import HumanMessage

    if agent_graph is None:
        await initialize_agent()

    graph = agent_graph
    if graph is None:
        return "Agent is still initializing. Please try again in a moment."

    config = {"configurable": {"thread_id": session_id}}

    state = {
        "messages": [HumanMessage(content=message)],
        "current_query": message,
        "agent_results": {},
        "agents_invoked": [],
        "current_specialist": None,
        "final_response": None,
        "session_id": session_id,
        "user_id": user_id,
        "metadata": {
            "conversation_mode": "general_chat",
            "cluster_id": cluster_id or "",
            "incident_id": "",
        },
    }

    try:
        result = await asyncio.wait_for(graph.ainvoke(state, config), timeout=120)
        return result.get("final_response") or "I wasn't able to generate a response. Please try again."
    except asyncio.TimeoutError:
        return "The agent took too long to respond. Please try a more specific question."
    except Exception as e:
        return f"Agent error: {str(e)}"


@router.post("", response_model=ChatResponse)
async def general_chat(
    payload: ChatRequest,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> ChatResponse:
    """Send a free-form message to the SRE agent."""
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    session_id = f"chat-{user.id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    reply = await _run_chat(
        message=message,
        session_id=session_id,
        cluster_id=payload.cluster_id,
        user_id=str(user.id),
    )

    return ChatResponse(reply=reply, session_id=session_id)
