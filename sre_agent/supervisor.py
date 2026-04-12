#!/usr/bin/env python3

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field, field_validator

from .agent_state import AgentState
from .constants import SREConstants
from .incident_timeline import (
    build_supervisor_decision_content,
    build_supervisor_direct_answer_content,
    build_supervisor_plan_content,
    build_supervisor_revised_plan_content,
    build_supervisor_summary_content,
    emit_timeline_event,
    filter_visible_specialists,
    load_pending_human_events,
    infer_visible_specialist_queue,
    mark_human_event_handled,
    visible_specialist_label,
)
from .llm_utils import create_llm_with_error_handling
from .output_formatter import create_formatter
from .prompt_loader import prompt_loader


def _get_user_from_env() -> str:
    """Get user_id from environment variable.

    Returns:
        user_id from USER_ID environment variable or default
    """
    user_id = os.getenv("USER_ID")
    if user_id:
        logger.info(f"Using user_id from environment: {user_id}")
        return user_id
    else:
        # Fallback to default user_id
        default_user_id = SREConstants.agents.default_user_id
        logger.warning(
            f"USER_ID not set in environment, using default: {default_user_id}"
        )
        return default_user_id


def _get_session_from_env(mode: str) -> str:
    """Get session_id from environment variable or generate one.

    Args:
        mode: "interactive" or "prompt" for auto-generation prefix

    Returns:
        session_id from SESSION_ID environment variable or auto-generated
    """
    session_id = os.getenv("SESSION_ID")
    if session_id:
        logger.info(f"Using session_id from environment: {session_id}")
        return session_id
    else:
        # Auto-generate session_id
        auto_session_id = f"{mode}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        logger.info(
            f"SESSION_ID not set in environment, auto-generated: {auto_session_id}"
        )
        return auto_session_id


# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,  # Set the log level to INFO
    # Define log message format
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

# Enable HTTP and MCP protocol logs for debugging
# Comment out the following lines to suppress these logs if needed
# mcp_loggers = ["streamable_http", "mcp.client.streamable_http", "httpx", "httpcore"]
#
# for logger_name in mcp_loggers:
#     mcp_logger = logging.getLogger(logger_name)
#     mcp_logger.setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _json_serializer(obj):
    """JSON serializer for objects not serializable by default json code."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


class InvestigationPlan(BaseModel):
    """Investigation plan created by supervisor."""

    steps: List[str] = Field(
        description="List of 3-5 investigation steps to be executed"
    )

    @field_validator("steps", mode="before")
    @classmethod
    def validate_steps(cls, v):
        """Convert string steps to list if needed."""
        if isinstance(v, str):
            # Split by numbered lines and clean up
            import re

            lines = v.strip().split("\n")
            steps = []
            for line in lines:
                line = line.strip()
                if line:
                    # Remove numbering like "1.", "2.", etc.
                    clean_line = re.sub(r"^\d+\.\s*", "", line)
                    if clean_line:
                        steps.append(clean_line)
            return steps
        return v

    agents_sequence: List[str] = Field(
        description="Sequence of agents to invoke (metrics_agent, logs_agent, github_agent, runbooks_agent)"
    )
    complexity: Literal["simple", "complex"] = Field(
        description="Whether this plan is simple (auto-execute) or complex (needs approval)"
    )
    auto_execute: bool = Field(
        description="Whether to execute automatically or ask for user approval"
    )
    reasoning: str = Field(
        description="Brief explanation of the investigation approach"
    )


class RouteDecision(BaseModel):
    """Decision made by supervisor for routing."""

    next: Literal[
        "logs_agent", "metrics_agent", "github_agent", "runbooks_agent", "FINISH"
    ] = Field(description="The next agent to route to, or FINISH if done")
    reasoning: str = Field(
        description="Brief explanation of why this routing decision was made"
    )


def _read_supervisor_prompt() -> str:
    """Read supervisor system prompt from file."""
    try:
        prompt_path = (
            Path(__file__).parent
            / "config"
            / "prompts"
            / "supervisor_multi_agent_prompt.txt"
        )
        if prompt_path.exists():
            return prompt_path.read_text().strip()
    except Exception as e:
        logger.warning(f"Could not read supervisor prompt file: {e}")

    # Fallback to supervisor fallback prompt file
    try:
        fallback_path = (
            Path(__file__).parent
            / "config"
            / "prompts"
            / "supervisor_fallback_prompt.txt"
        )
        if fallback_path.exists():
            return fallback_path.read_text().strip()
    except Exception as e:
        logger.warning(f"Could not read supervisor fallback prompt file: {e}")

    # Final hardcoded fallback if files not found
    return (
        "You are the Supervisor Agent orchestrating a team of specialized SRE agents."
    )


def _read_planning_prompt() -> str:
    """Read planning prompt from file."""
    try:
        prompt_path = (
            Path(__file__).parent
            / "config"
            / "prompts"
            / "supervisor_planning_prompt.txt"
        )
        if prompt_path.exists():
            return prompt_path.read_text().strip()
    except Exception as e:
        logger.warning(f"Could not read planning prompt file: {e}")

    # Fallback planning prompt
    return """Create a simple, focused investigation plan with 2-3 steps maximum.
Create the plan in JSON format with these fields:
- steps: List of 3-5 investigation steps
- agents_sequence: List of agents to invoke (metrics_agent, logs_agent, github_agent, runbooks_agent)
- complexity: "simple" or "complex"
- auto_execute: true or false
- reasoning: Brief explanation of the investigation approach"""


def _active_visible_specialist_queue(state: AgentState) -> List[str]:
    metadata = state.get("metadata", {})
    explicit_queue = metadata.get("specialist_queue")
    if isinstance(explicit_queue, list) and explicit_queue:
        return filter_visible_specialists(explicit_queue)

    plan = metadata.get("investigation_plan") or {}
    planned_agents = plan.get("agents_sequence", []) if isinstance(plan, dict) else []
    current_query = state.get("current_query", "")
    return infer_visible_specialist_queue(current_query, planned_agents)


def _assistant_mode_enabled(state: AgentState) -> bool:
    metadata = state.get("metadata", {})
    return bool(metadata.get("conversation_mode") == "assistant" or metadata.get("post_investigation_follow_up"))


def _follow_up_specialist_for_question(question: str) -> Optional[str]:
    normalized = " ".join(question.lower().split())
    if any(marker in normalized for marker in ("metric", "metrics", "prometheus", "latency", "throughput", "availability")):
        return "metrics_agent"
    if any(marker in normalized for marker in ("log", "logs", "error", "exception", "trace", "stack trace", "loki")):
        return "logs_agent"
    if any(marker in normalized for marker in ("github", "code", "commit", "pull request", "pr", "deploy", "release", "rollback", "what changed", "recent change", "change caused")):
        return "github_agent"
    if any(marker in normalized for marker in ("runbook", "runbooks", "playbook", "procedure", "next step", "remediation")):
        return "runbooks_agent"
    return None


def _is_casual_follow_up(question: str) -> bool:
    normalized = " ".join(question.lower().split())
    return normalized in {"hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay"}


def _friendly_follow_up_reply(question: str) -> str:
    normalized = " ".join(question.lower().split())
    if normalized in {"hi", "hello", "hey", "yo"}:
        return "I’m still here. Ask about the incident summary, the evidence, or the next steps."
    if normalized in {"thanks", "thank you"}:
        return "Glad to help. If you want, I can also break down the evidence or next steps."
    if normalized in {"ok", "okay"}:
        return "Understood. Ask a follow-up anytime if you want me to dig deeper."
    return "I’m here if you want to dig deeper into the completed incident."


def _summarize_for_direct_follow_up(summary_text: str) -> str:
    if not summary_text:
        return "The incident is closed, but I do not have the summary context available in this thread."

    summary_lines = []
    for raw_line in summary_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        cleaned = re.sub(r"^[\-*]\s*", "", line)
        if cleaned:
            summary_lines.append(cleaned)

    if not summary_lines:
        compact_summary = re.sub(r"\s+", " ", summary_text.strip())
        return f"Based on the completed investigation, {compact_summary}"

    excerpt = " ".join(summary_lines[:2])
    return f"Based on the completed investigation, {excerpt}"


def _classify_human_interrupt(message: str, current_queue: List[str]) -> Dict[str, Any]:
    normalized = " ".join(message.lower().split())
    reroute_markers = ("change", "modify", "instead", "focus", "prioritize", "pause", "stop", "skip")

    if any(marker in normalized for marker in reroute_markers):
        revised_queue = list(current_queue)
        preferred_agents: List[str] = []
        if any(marker in normalized for marker in ("logs", "log")):
            preferred_agents.append("logs_agent")
        if any(marker in normalized for marker in ("metric", "metrics", "prometheus")):
            preferred_agents.append("metrics_agent")
        if "github" in normalized or "code" in normalized:
            preferred_agents.append("github_agent")
        if "runbook" in normalized or "runbooks" in normalized or "doc" in normalized:
            preferred_agents.append("runbooks_agent")

        for agent_name in preferred_agents:
            if agent_name in revised_queue:
                revised_queue.remove(agent_name)
            revised_queue.insert(0, agent_name)

        return {
            "mode": "revised_plan",
            "revised_queue": filter_visible_specialists(revised_queue),
            "reason": "Human checkpoint asked for a reprioritized investigation path.",
        }

    return {
        "mode": "direct_answer",
        "basis": "Human checkpoint review can proceed without altering the specialist queue.",
    }


class SupervisorAgent:
    """Supervisor agent that orchestrates other agents."""

    def __init__(
        self,
        llm_provider: str = "ollama",
        **llm_kwargs,
    ):
        self.llm_provider = llm_provider
        self.llm = self._create_llm(**llm_kwargs)
        self.system_prompt = _read_supervisor_prompt()
        self.formatter = create_formatter(llm_provider=llm_provider)

        # Memory system removed
        self.memory_client = None
        self.memory_hooks = None
        self.conversation_manager = None
        self.memory_tools = []
        self.planning_agent = None
        logger.info("Memory system disabled")

    def _create_llm(self, **kwargs):
        """Create LLM instance with improved error handling."""
        return create_llm_with_error_handling(self.llm_provider, **kwargs)

    async def create_investigation_plan(self, state: AgentState) -> InvestigationPlan:
        """Create an investigation plan for the user's query."""
        current_query = state.get("current_query", "No query provided")
        user_id = state.get("user_id", SREConstants.agents.default_user_id)
        session_id = state.get("session_id")

        # Memory system removed - no memory context retrieval

        # Planning prompt without memory context
        planning_instructions = _read_planning_prompt()
        # Replace placeholders manually to avoid issues with JSON braces in the prompt
        formatted_planning_instructions = planning_instructions.replace(
            "{user_id}", user_id
        )
        if session_id:
            formatted_planning_instructions = formatted_planning_instructions.replace(
                "{session_id}", session_id
            )

        planning_prompt = f"""{self.system_prompt}

User's query: {current_query}

{formatted_planning_instructions}"""

        # Use structured output without memory tools
        structured_llm = self.llm.with_structured_output(InvestigationPlan)
        plan = await structured_llm.ainvoke(
            [
                SystemMessage(content=planning_prompt),
                HumanMessage(content=current_query),
            ]
        )

        logger.info(
            f"Created investigation plan: {len(plan.steps)} steps, complexity: {plan.complexity}"
        )

        # Memory conversation storage removed

        return plan

    def _format_plan_markdown(self, plan: InvestigationPlan) -> str:
        """Format investigation plan as properly formatted markdown."""
        step_text_by_agent = {
            "metrics_agent": "Check the metrics around the alert window and confirm the scope of the impact.",
            "logs_agent": "Review logs for matching error patterns, timeouts, or repeated failures.",
            "github_agent": "Correlate the incident with recent deployments, commits, or pull requests.",
            "runbooks_agent": "Check the safest runbook path and confirm the next operational step.",
        }

        plan_text = "## Investigation Plan\n\n"
        plan_text += "**Objective:** Investigate the incident with the selected specialists.\n"

        if plan.agents_sequence:
            agents_list = ", ".join(
                [agent.replace("_", " ").title() for agent in plan.agents_sequence]
            )
            plan_text += f"**Selected specialists:** {agents_list}\n\n"
            plan_text += "**Investigation steps:**\n"
            for i, agent_name in enumerate(plan.agents_sequence, 1):
                plan_text += f"{i}. {step_text_by_agent.get(agent_name, 'Review the incident evidence and report back clearly.')}\n"
        else:
            plan_text += "**Selected specialists:** None yet\n\n"
            plan_text += "**Investigation steps:**\n1. Review the incident evidence and report back clearly.\n"

        plan_text += f"\n**Complexity:** {plan.complexity.title()}\n"
        plan_text += f"**Auto-execute:** {'Yes' if plan.auto_execute else 'No'}\n"
        if plan.reasoning:
            plan_text += f"**Reasoning:** {plan.reasoning}\n"

        return plan_text

    async def route(self, state: AgentState) -> Dict[str, Any]:
        """Determine which agent should handle the query next."""
        agents_invoked = state.get("agents_invoked", [])
        existing_traces = dict(state.get("thought_traces", {}))
        metadata = dict(state.get("metadata", {}))
        incident_id = state.get("incident_id") or metadata.get("incident_id")

        if _assistant_mode_enabled(state):
            current_query = state.get("current_query", "") or "Follow-up question"
            final_response_context = (
                state.get("final_response")
                or metadata.get("final_response")
                or metadata.get("incident_summary")
                or ""
            )

            if _is_casual_follow_up(current_query):
                friendly_reply = _friendly_follow_up_reply(current_query)
                return {
                    "next": "FINISH",
                    "final_response": friendly_reply,
                    "thought_traces": {
                        **existing_traces,
                        "supervisor": [
                            *existing_traces.get("supervisor", []),
                            "I kept the reply conversational in the same incident thread.",
                        ],
                    },
                    "metadata": {
                        **metadata,
                        "conversation_mode": "assistant",
                        "post_investigation_follow_up": True,
                        "follow_up_mode": "direct",
                        "final_response": friendly_reply,
                        "follow_up_question": current_query,
                        "follow_up_basis": "A brief conversational follow-up after the incident summary.",
                    },
                }

            if metadata.get("follow_up_mode") == "specialist" and agents_invoked:
                return {
                    "next": "FINISH",
                    "thought_traces": {
                        **existing_traces,
                        "supervisor": [
                            *existing_traces.get("supervisor", []),
                            "The specialist answered the follow-up in the same incident thread.",
                        ],
                    },
                    "metadata": {
                        **metadata,
                        "conversation_mode": "assistant",
                        "post_investigation_follow_up": True,
                        "final_response": final_response_context,
                    },
                }

            selected_specialist = _follow_up_specialist_for_question(current_query)
            if selected_specialist:
                decision_content, decision_payload = build_supervisor_decision_content(
                    selected_specialist,
                    "Delegating the follow-up to the most relevant specialist.",
                    [],
                )
                await emit_timeline_event(
                    incident_id,
                    event_type="decision",
                    speaker_role="supervisor",
                    title="Supervisor",
                    content=decision_content,
                    payload={
                        **decision_payload,
                        "source": "post_investigation_follow_up",
                        "question": current_query,
                    },
                )
                return {
                    "next": selected_specialist,
                    "thought_traces": {
                        **existing_traces,
                        "supervisor": [
                            *existing_traces.get("supervisor", []),
                            f"I’m handing this follow-up to {visible_specialist_label(selected_specialist)}.",
                        ],
                    },
                    "metadata": {
                        **metadata,
                        "conversation_mode": "assistant",
                        "post_investigation_follow_up": True,
                        "follow_up_mode": "specialist",
                        "follow_up_specialist": selected_specialist,
                        "current_specialist": selected_specialist,
                        "final_response": final_response_context,
                    },
                }

            direct_answer = _summarize_for_direct_follow_up(final_response_context)
            return {
                "next": "FINISH",
                "final_response": direct_answer,
                "thought_traces": {
                    **existing_traces,
                    "supervisor": [
                        *existing_traces.get("supervisor", []),
                        "I answered the follow-up directly from the completed incident context.",
                    ],
                },
                "metadata": {
                    **metadata,
                    "conversation_mode": "assistant",
                    "post_investigation_follow_up": True,
                    "follow_up_mode": "direct",
                    "final_response": direct_answer,
                    "follow_up_question": current_query,
                    "follow_up_basis": "The completed incident summary already covers this follow-up.",
                },
            }

        pending_events = await load_pending_human_events(incident_id, limit=3)
        if pending_events:
            pending_messages = [
                {
                    "id": str(event.id),
                    "sequence": event.sequence,
                    "content": event.content,
                    "title": event.title,
                    "created_at": event.created_at.isoformat() if event.created_at else None,
                }
                for event in pending_events
            ]
            metadata = {
                **metadata,
                "pending_human_messages": pending_messages,
                "human_interrupt_pending": True,
                "last_processed_human_event_id": str(pending_events[0].id),
            }

            current_queue = _active_visible_specialist_queue({**state, "metadata": metadata})
            human_interrupt = _classify_human_interrupt(pending_events[0].content or "", current_queue)

            if human_interrupt["mode"] == "revised_plan":
                revised_queue = human_interrupt["revised_queue"]
                metadata = {
                    **metadata,
                    "specialist_queue": revised_queue,
                    "current_specialist": revised_queue[0] if revised_queue else None,
                }
                content, payload = build_supervisor_revised_plan_content(
                    pending_events[0].content or "",
                    revised_queue,
                    human_interrupt["reason"],
                )
            else:
                content, payload = build_supervisor_direct_answer_content(
                    pending_events[0].content or "",
                    "I have the message and will incorporate it at the next safe checkpoint.",
                    human_interrupt["basis"],
                )

            emitted_event = await emit_timeline_event(
                incident_id,
                event_type="decision",
                speaker_role="supervisor",
                title="Supervisor",
                content=content,
                payload={
                    **payload,
                    "source": "human_checkpoint",
                    "pending_event_id": str(pending_events[0].id),
                },
            )
            if emitted_event is not None:
                await mark_human_event_handled(incident_id, str(pending_events[0].id))
                metadata = {
                    **metadata,
                    "pending_human_messages": pending_messages[1:],
                    "human_interrupt_pending": len(pending_messages) > 1,
                }

        # Check if we have an existing plan
        existing_plan = metadata.get("investigation_plan")

        if not existing_plan:
            # First time - create investigation plan
            plan = await self.create_investigation_plan(state)
            plan_dict = plan.model_dump()
            visible_queue = _active_visible_specialist_queue({**state, "metadata": {**metadata, "investigation_plan": plan_dict}})
            if not visible_queue:
                visible_queue = ["metrics_agent", "logs_agent"]

            plan_content, plan_payload = build_supervisor_plan_content(
                state.get("current_query", "Incident investigation"),
                plan_dict,
                visible_queue,
            )
            await emit_timeline_event(
                incident_id,
                event_type="plan",
                speaker_role="supervisor",
                title="Supervisor",
                content=plan_content,
                payload=plan_payload,
            )

            # Check if we should auto-approve the plan (defaults to False if not set)
            auto_approve = state.get("auto_approve_plan", False)

            if not plan.auto_execute and not auto_approve:
                # Complex plan - present to user for approval
                plan_text = self._format_plan_markdown(plan)
                supervisor_thought = (
                    "I've drafted a coordinated investigation plan for the team. I'll pause here to get your approval before we proceed."
                )
                return {
                    "next": "FINISH",
                    "thought_traces": {
                        **existing_traces,
                        "supervisor": [
                            *existing_traces.get("supervisor", []),
                            supervisor_thought,
                        ],
                    },
                    "metadata": {
                        **metadata,
                        "investigation_plan": plan_dict,
                        "specialist_queue": visible_queue,
                        "routing_reasoning": f"Created investigation plan. Complexity: {plan.complexity}",
                        "plan_pending_approval": True,
                        "plan_text": plan_text,
                    },
                }
            else:
                # Simple plan - start execution
                next_agent = visible_queue[0] if visible_queue else "FINISH"
                plan_text = self._format_plan_markdown(plan)
                supervisor_thought = (
                    f"Alright team, let's execute the plan. I'm going to bring in {visible_specialist_label(next_agent)} first to start pulling evidence."
                )

                decision_content, decision_payload = build_supervisor_decision_content(
                    next_agent,
                    f"Executing plan step 1: {plan.steps[0] if plan.steps else 'Start'}",
                    visible_queue[1:],
                )
                await emit_timeline_event(
                    incident_id,
                    event_type="decision",
                    speaker_role="supervisor",
                    title="Supervisor",
                    content=decision_content,
                    payload=decision_payload,
                )
                return {
                    "next": next_agent,
                    "thought_traces": {
                        **existing_traces,
                        "supervisor": [
                            *existing_traces.get("supervisor", []),
                            supervisor_thought,
                        ],
                    },
                    "metadata": {
                        **metadata,
                        "investigation_plan": plan_dict,
                        "specialist_queue": visible_queue,
                        "routing_reasoning": f"Executing plan step 1: {plan.steps[0] if plan.steps else 'Start'}",
                        "plan_step": 0,
                        "plan_text": plan_text,
                        "show_plan": True,
                        "current_specialist": next_agent,
                    },
                }
        else:
            # Continue executing existing plan
            plan = InvestigationPlan(**existing_plan)
            visible_queue = _active_visible_specialist_queue({**state, "metadata": metadata})
            if not visible_queue:
                visible_queue = ["metrics_agent", "logs_agent"]

            next_agent = None
            for candidate in visible_queue:
                if candidate not in agents_invoked:
                    next_agent = candidate
                    break

            if not next_agent:
                # Plan complete
                supervisor_thought = (
                    "Okay, the team has finished gathering intel. I'm going to take all these findings and synthesize the final summary."
                )

                decision_content, decision_payload = build_supervisor_decision_content(
                    "aggregate",
                    "Visible specialists have reported back; consolidate the investigation.",
                    [],
                )
                await emit_timeline_event(
                    incident_id,
                    event_type="decision",
                    speaker_role="supervisor",
                    title="Supervisor",
                    content=decision_content,
                    payload=decision_payload,
                )
                return {
                    "next": "FINISH",
                    "thought_traces": {
                        **existing_traces,
                        "supervisor": [
                            *existing_traces.get("supervisor", []),
                            supervisor_thought,
                        ],
                    },
                    "metadata": {
                        **metadata,
                        "routing_reasoning": "Investigation plan completed. Presenting results.",
                        "plan_step": len(agents_invoked),
                        "specialist_queue": visible_queue,
                    },
                }
            else:
                # Continue with next agent in plan
                step_description = plan.steps[len(agents_invoked)] if len(agents_invoked) < len(plan.steps) else f"Execute {next_agent}"
                supervisor_thought = (
                    f"Good work so far, but we need more data. {visible_specialist_label(next_agent)}, can you take over?"
                )

                remaining_agents = [agent for agent in visible_queue if agent != next_agent and agent not in agents_invoked]
                decision_content, decision_payload = build_supervisor_decision_content(
                    next_agent,
                    f"Executing plan step {len(agents_invoked) + 1}: {step_description}",
                    remaining_agents,
                )
                await emit_timeline_event(
                    incident_id,
                    event_type="decision",
                    speaker_role="supervisor",
                    title="Supervisor",
                    content=decision_content,
                    payload=decision_payload,
                )

                return {
                    "next": next_agent,
                    "thought_traces": {
                        **existing_traces,
                        "supervisor": [
                            *existing_traces.get("supervisor", []),
                            supervisor_thought,
                        ],
                    },
                    "metadata": {
                        **metadata,
                        "routing_reasoning": f"Executing plan step {len(agents_invoked) + 1}: {step_description}",
                        "plan_step": len(agents_invoked),
                        "specialist_queue": visible_queue,
                        "current_specialist": next_agent,
                    },
                }

    async def aggregate_responses(self, state: AgentState) -> Dict[str, Any]:
        """Aggregate responses from multiple agents into a final response."""
        agent_results = state.get("agent_results", {})
        metadata = state.get("metadata", {})
        reflector_analysis = state.get("reflector_analysis")
        remediation_plan = state.get("remediation_plan")
        existing_traces = dict(state.get("thought_traces", {}))
        incident_id = state.get("incident_id") or metadata.get("incident_id")
        current_query = state.get("current_query", "") or "Follow-up question"
        alert_context = state.get("alert_context")

        if _assistant_mode_enabled(state):
            follow_up_mode = metadata.get("follow_up_mode")

            if follow_up_mode == "specialist" and agent_results:
                specialist_response = next(iter(agent_results.values())) or "Follow-up complete."
                return {
                    "final_response": specialist_response,
                    "next": "FINISH",
                    "thought_traces": {
                        **existing_traces,
                        "supervisor": [
                            *existing_traces.get("supervisor", []),
                            "A specialist answered the follow-up in the same incident thread.",
                        ],
                    },
                    "metadata": {
                        **metadata,
                        "conversation_mode": "assistant",
                        "post_investigation_follow_up": True,
                        "final_response": specialist_response,
                    },
                }

            follow_up_response = state.get("final_response") or metadata.get("final_response")
            if not follow_up_response and agent_results:
                follow_up_response = next(iter(agent_results.values()))
            follow_up_response = follow_up_response or "Follow-up complete."

            assistant_content, assistant_payload = build_supervisor_direct_answer_content(
                current_query,
                follow_up_response,
                metadata.get("follow_up_basis") or "The completed incident summary already covers this follow-up.",
            )
            await emit_timeline_event(
                incident_id,
                event_type="assistant_message",
                speaker_role="supervisor",
                title="Supervisor",
                content=assistant_content,
                payload={
                    **assistant_payload,
                    "source": "post_investigation_follow_up",
                    "question": current_query,
                },
            )

            return {
                "final_response": follow_up_response,
                "next": "FINISH",
                "thought_traces": {
                    **existing_traces,
                    "supervisor": [
                        *existing_traces.get("supervisor", []),
                        "I kept the follow-up in the same incident thread without reopening the investigation.",
                    ],
                },
                "metadata": {
                    **metadata,
                    "conversation_mode": "assistant",
                    "post_investigation_follow_up": True,
                    "final_response": follow_up_response,
                },
            }

        if remediation_plan and reflector_analysis:
            # Format the output from the OODA workflow
            final_response = f"## 🔍 Incident Investigation Summary\n\n"
            final_response += f"**Hypothesis:** {reflector_analysis.hypothesis}\n"
            final_response += f"**Confidence:** {reflector_analysis.confidence:.0%}\n\n"
            final_response += f"### 🧠 Reasoning\n{reflector_analysis.reasoning}\n\n"
            
            final_response += f"## 📋 Recommended Remediation Plan\n\n"
            if remediation_plan.source_runbook_url:
                final_response += f"*(Sourced from Runbook: {remediation_plan.source_runbook_url})*\n\n"
                
            for i, action in enumerate(remediation_plan.actions, 1):
                final_response += f"### Action {i}: {action.action_type.title()} `{action.target}`\n"
                final_response += f"- **Safety Check:** {action.safety_check}\n"
                if hasattr(action, "parameters") and getattr(action, "parameters"):
                    final_response += f"- **Parameters:** {action.parameters}\n"
                final_response += "\n"
                
            final_response += f"**Risk Level:** {remediation_plan.risk_level.title()}\n"
            final_response += f"**Estimated Duration:** {remediation_plan.estimated_duration}\n"
            final_response += f"\n*This is a suggested remediation. Automatic execution is disabled.*"

            supervisor_summary = (
                f"I confirmed the likely root cause as {reflector_analysis.hypothesis} and "
                f"assembled the remediation plan with {len(remediation_plan.actions)} action(s)."
            )

            summary_content, summary_payload = build_supervisor_summary_content(final_response, agent_results)
            await emit_timeline_event(
                incident_id,
                event_type="summary",
                speaker_role="supervisor",
                title="Supervisor",
                content=summary_content,
                payload={
                    **summary_payload,
                    "hypothesis": reflector_analysis.hypothesis,
                    "confidence": getattr(reflector_analysis, "confidence", None),
                    "remediation_actions": [
                        {
                            "action_type": action.action_type,
                            "target": action.target,
                        }
                        for action in remediation_plan.actions
                    ],
                    "conversation_mode": "assistant",
                    "lifecycle": "post_investigation_ready",
                },
            )
            
            return {
                "final_response": summary_content,
                "next": "FINISH",
                "thought_traces": {
                    **existing_traces,
                    "supervisor": [
                        *existing_traces.get("supervisor", []),
                        supervisor_summary,
                    ],
                },
                "metadata": {
                    **metadata,
                    "pending_human_messages": [],
                    "human_interrupt_pending": False,
                    "conversation_mode": "assistant",
                    "post_investigation_follow_up": False,
                    "final_response": summary_content,
                },
            }

        # Check if this is a plan approval request
        if metadata.get("plan_pending_approval"):
            plan = metadata.get("investigation_plan", {})
            query = state.get("current_query", "Investigation") or "Investigation"

            # Use enhanced formatting for plan approval
            try:
                approval_response = self.formatter.format_plan_approval(plan, query)
            except Exception as e:
                logger.warning(
                    f"Failed to use enhanced formatting: {e}, falling back to plain text"
                )
                plan_text = metadata.get("plan_text", "")
                approval_response = f"""## Investigation Plan

I've analyzed your query and created the following investigation plan:

{plan_text}

**Complexity:** {plan.get("complexity", "unknown").title()}
**Reasoning:** {plan.get("reasoning", "Standard investigation approach")}

This plan will help systematically investigate your issue. Would you like me to proceed with this plan, or would you prefer to modify it?

You can:
- Type "proceed" or "yes" to execute the plan
- Type "modify" to suggest changes
- Ask specific questions about any step"""

            return {"final_response": approval_response, "next": "FINISH"}

        if not agent_results:
            return {"final_response": "No agent responses to aggregate."}

        # Use enhanced formatting for investigation results
        query = state.get("current_query", "Investigation") or "Investigation"
        plan = metadata.get("investigation_plan")

        # Memory context removed - no user preferences
        user_preferences = []

        try:
            # Try enhanced formatting first
            final_response = self.formatter.format_investigation_response(
                query=query,
                agent_results=agent_results,
                metadata=metadata,
                plan=plan,
                user_preferences=user_preferences,
            )
        except Exception as e:
            logger.warning(
                f"Failed to use enhanced formatting: {e}, falling back to LLM aggregation"
            )

            # Fallback to LLM-based aggregation
            try:
                # Get system message from prompt loader
                system_prompt = prompt_loader.load_prompt(
                    "supervisor_aggregation_system"
                )

                # Determine if this is plan-based or standard aggregation
                is_plan_based = plan is not None

                # Prepare template variables
                query = (
                    state.get("current_query", "No query provided")
                    or "No query provided"
                )
                agent_results_json = json.dumps(
                    agent_results, indent=2, default=_json_serializer
                )
                auto_approve_plan = state.get("auto_approve_plan", False) or False

                # Use the user_preferences we already retrieved
                user_preferences_json = (
                    json.dumps(user_preferences, indent=2, default=_json_serializer)
                    if user_preferences
                    else ""
                )

                if is_plan_based:
                    current_step = metadata.get("plan_step", 0)
                    total_steps = len(plan.get("steps", []))
                    plan_json = json.dumps(
                        plan.get("steps", []), indent=2, default=_json_serializer
                    )

                    aggregation_prompt = (
                        prompt_loader.get_supervisor_aggregation_prompt(
                            is_plan_based=True,
                            query=query,
                            agent_results=agent_results_json,
                            auto_approve_plan=auto_approve_plan,
                            current_step=current_step + 1,
                            total_steps=total_steps,
                            plan=plan_json,
                            user_preferences=user_preferences_json,
                        )
                    )
                else:
                    aggregation_prompt = (
                        prompt_loader.get_supervisor_aggregation_prompt(
                            is_plan_based=False,
                            query=query,
                            agent_results=agent_results_json,
                            auto_approve_plan=auto_approve_plan,
                            user_preferences=user_preferences_json,
                        )
                    )

            except Exception as e:
                logger.error(f"Error loading aggregation prompts: {e}")
                # Fallback to simple prompt
                system_prompt = "You are an expert at presenting technical investigation results clearly and professionally."
                aggregation_prompt = f"Summarize these findings: {json.dumps(agent_results, indent=2, default=_json_serializer)}"

            response = await self.llm.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=aggregation_prompt),
                ]
            )

            final_response = response.content

        supervisor_summary = (
            "I merged the specialist replies into one conclusion and am handing the "
            "result back to the user as the final investigation summary."
        )

        summary_content, summary_payload = build_supervisor_summary_content(
            final_response,
            agent_results,
            query=state.get("current_query", ""),
            alert_context=alert_context,
        )
        await emit_timeline_event(
            incident_id,
            event_type="summary",
            speaker_role="supervisor",
            title="Supervisor",
            content=summary_content,
            payload=summary_payload,
        )

        # Store successful resolution in memory (if verification passed)
        try:
            verification_result = state.get("verification_result")
            if verification_result:
                # Handle both Pydantic model and dict
                if hasattr(verification_result, "status"):
                    status = verification_result.status
                    improvement = getattr(verification_result, "improvement_percentage", 0.0)
                elif isinstance(verification_result, dict):
                    status = verification_result.get("status")
                    improvement = verification_result.get("improvement_percentage", 0.0)
                else:
                    status = None

                if status == "RESOLVED":
                    # Try MCP memory server first
                    tools = metadata.get("tools", [])
                    store_tool = None
                    for tool in tools:
                        tool_name = getattr(tool, "name", "")
                        if "store_incident_memory" in tool_name.lower():
                            store_tool = tool
                            break

                    incident_id = state.get("incident_id", f"incident-{datetime.now(timezone.utc).isoformat()}")
                    alert_context = state.get("alert_context")
                    remediation_plan = state.get("remediation_plan")
                    reflector_analysis = state.get("reflector_analysis")

                    # Extract hypothesis
                    hypothesis = "Unknown"
                    if reflector_analysis:
                        if hasattr(reflector_analysis, "hypothesis"):
                            hypothesis = reflector_analysis.hypothesis
                        elif isinstance(reflector_analysis, dict):
                            hypothesis = reflector_analysis.get("hypothesis", "Unknown")

                    # Extract plan hypothesis
                    plan_hypothesis = "Unknown"
                    if remediation_plan:
                        if hasattr(remediation_plan, "hypothesis"):
                            plan_hypothesis = remediation_plan.hypothesis
                        elif isinstance(remediation_plan, dict):
                            plan_hypothesis = remediation_plan.get("hypothesis", "Unknown")

                    # Build incident text
                    alert_name = "Unknown"
                    if alert_context:
                        if hasattr(alert_context, "alert_name"):
                            alert_name = alert_context.alert_name
                        elif isinstance(alert_context, dict):
                            alert_name = alert_context.get("alert_name", "Unknown")

                    incident_text = f"""
Alert: {alert_name}
Hypothesis: {hypothesis}
Resolution: {plan_hypothesis}
Verification: {status}
Improvement: {improvement:.1f}%
                    """.strip()

                    metadata_dict = {
                        "alert_name": alert_name,
                        "resolution": plan_hypothesis,
                        "improvement": improvement,
                    }

                    if store_tool:
                        # Use MCP memory server
                        import json
                        metadata_json = json.dumps(metadata_dict)
                        logger.info("💾 Storing incident via MCP memory server")
                        if hasattr(store_tool, "ainvoke"):
                            await store_tool.ainvoke({
                                "incident_text": incident_text,
                                "incident_id": incident_id,
                                "metadata": metadata_json,
                            })
                        else:
                            store_tool.invoke({
                                "incident_text": incident_text,
                                "incident_id": incident_id,
                                "metadata": metadata_json,
                            })
                        logger.info(f"✅ Stored successful resolution in memory via MCP: {incident_id}")
                    else:
                        # Fallback to direct memory store
                        from .memory_store import get_memory_store
                        memory = get_memory_store()
                        if memory.is_available():
                            memory.store_incident(incident_text, incident_id, metadata_dict)
                            logger.info(f"✅ Stored successful resolution in memory: {incident_id}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to store incident in memory: {e}")

            return {
                "final_response": summary_content,
                "next": "FINISH",
                "thought_traces": {
                    **existing_traces,
                    "supervisor": [
                        *existing_traces.get("supervisor", []),
                        supervisor_summary,
                    ],
                },
            }
