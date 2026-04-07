#!/usr/bin/env python3

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field, field_validator

from .agent_state import AgentState
from .constants import SREConstants
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
        description="Sequence of agents to invoke (kubernetes_agent, logs_agent, metrics_agent, runbooks_agent)"
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
        "kubernetes_agent", "logs_agent", "metrics_agent", "runbooks_agent", "FINISH"
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
- agents_sequence: List of agents to invoke (kubernetes_agent, logs_agent, metrics_agent, runbooks_agent)
- complexity: "simple" or "complex"
- auto_execute: true or false
- reasoning: Brief explanation of the investigation approach"""


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
        plan_text = "## 🔍 Investigation Plan\n\n"

        # Add steps with proper numbering and formatting
        for i, step in enumerate(plan.steps, 1):
            plan_text += f"**{i}.** {step}\n\n"

        # Add metadata
        plan_text += f"**📊 Complexity:** {plan.complexity.title()}\n"
        plan_text += f"**🤖 Auto-execute:** {'Yes' if plan.auto_execute else 'No'}\n"
        if plan.reasoning:
            plan_text += f"**💭 Reasoning:** {plan.reasoning}\n"

        # Add agents involved
        if plan.agents_sequence:
            agents_list = ", ".join(
                [agent.replace("_", " ").title() for agent in plan.agents_sequence]
            )
            plan_text += f"**👥 Agents involved:** {agents_list}\n"

        return plan_text

    async def route(self, state: AgentState) -> Dict[str, Any]:
        """Determine which agent should handle the query next."""
        agents_invoked = state.get("agents_invoked", [])

        # Check if we have an existing plan
        existing_plan = state.get("metadata", {}).get("investigation_plan")

        if not existing_plan:
            # First time - create investigation plan
            plan = await self.create_investigation_plan(state)

            # Check if we should auto-approve the plan (defaults to False if not set)
            auto_approve = state.get("auto_approve_plan", False)

            if not plan.auto_execute and not auto_approve:
                # Complex plan - present to user for approval
                plan_text = self._format_plan_markdown(plan)
                return {
                    "next": "FINISH",
                    "metadata": {
                        **state.get("metadata", {}),
                        "investigation_plan": plan.model_dump(),
                        "routing_reasoning": f"Created investigation plan. Complexity: {plan.complexity}",
                        "plan_pending_approval": True,
                        "plan_text": plan_text,
                    },
                }
            else:
                # Simple plan - start execution
                next_agent = (
                    plan.agents_sequence[0] if plan.agents_sequence else "FINISH"
                )
                plan_text = self._format_plan_markdown(plan)
                return {
                    "next": next_agent,
                    "metadata": {
                        **state.get("metadata", {}),
                        "investigation_plan": plan.model_dump(),
                        "routing_reasoning": f"Executing plan step 1: {plan.steps[0] if plan.steps else 'Start'}",
                        "plan_step": 0,
                        "plan_text": plan_text,
                        "show_plan": True,
                    },
                }
        else:
            # Continue executing existing plan
            plan = InvestigationPlan(**existing_plan)
            current_step = state.get("metadata", {}).get("plan_step", 0)

            # Check if plan is complete
            if current_step >= len(plan.agents_sequence) or not agents_invoked:
                next_step = current_step
            else:
                next_step = current_step + 1

            if next_step >= len(plan.agents_sequence):
                # Plan complete
                return {
                    "next": "FINISH",
                    "metadata": {
                        **state.get("metadata", {}),
                        "routing_reasoning": "Investigation plan completed. Presenting results.",
                        "plan_step": next_step,
                    },
                }
            else:
                # Continue with next agent in plan
                next_agent = plan.agents_sequence[next_step]
                step_description = (
                    plan.steps[next_step]
                    if next_step < len(plan.steps)
                    else f"Execute {next_agent}"
                )

                return {
                    "next": next_agent,
                    "metadata": {
                        **state.get("metadata", {}),
                        "routing_reasoning": f"Executing plan step {next_step + 1}: {step_description}",
                        "plan_step": next_step,
                    },
                }

    async def aggregate_responses(self, state: AgentState) -> Dict[str, Any]:
        """Aggregate responses from multiple agents into a final response."""
        agent_results = state.get("agent_results", {})
        metadata = state.get("metadata", {})
        reflector_analysis = state.get("reflector_analysis")
        remediation_plan = state.get("remediation_plan")

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
            
            return {"final_response": final_response, "next": "FINISH"}

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

        return {"final_response": final_response, "next": "FINISH"}
