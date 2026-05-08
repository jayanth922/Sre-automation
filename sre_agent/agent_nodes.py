#!/usr/bin/env python3

import asyncio
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt import create_react_agent

from .agent_state import AgentState
from .audit_context import set_audit_context, clear_audit_context
from .constants import AgentMetadata
from .incident_timeline import (
    build_specialist_finding_content,
    emit_timeline_event,
    internal_agent_name,
    visible_specialist_role,
)
from .llm_utils import create_llm_with_error_handling
from .narrative import (
    SPECIALIST_LABELS,
    build_specialist_task_brief,
    narrate_specialist_finding,
)
from .prompt_loader import prompt_loader

# Logging will be configured by the main entry point
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_agent_config() -> Dict[str, Any]:
    """Load agent configuration from YAML file."""
    config_path = Path(__file__).parent / "config" / "agent_config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _create_llm(provider: str = "ollama", **kwargs):
    """Create LLM instance for the given provider."""
    return create_llm_with_error_handling(provider, **kwargs)


def _filter_tools_for_agent(
    all_tools: List[BaseTool], agent_name: str, config: Dict[str, Any]
) -> List[BaseTool]:
    """Filter tools based on agent configuration."""
    agent_config = config["agents"].get(agent_name, {})
    allowed_tools = agent_config.get("tools", [])

    # Also include global tools
    global_tools = config.get("global_tools", [])
    allowed_tools.extend(global_tools)

    # Filter tools based on their names
    filtered_tools = []
    for tool in all_tools:
        tool_name = getattr(tool, "name", "")
        # Remove any prefix from tool name for matching
        base_tool_name = tool_name.split("___")[-1] if "___" in tool_name else tool_name

        if base_tool_name in allowed_tools:
            filtered_tools.append(tool)

    logger.info(f"Agent {agent_name} has access to {len(filtered_tools)} tools")

    # Debug: Show which tools are being added to this agent
    logger.info(f"Agent {agent_name} tool names:")
    for tool in filtered_tools:
        tool_name = getattr(tool, "name", "unknown")
        tool_description = getattr(tool, "description", "No description")
        # Extract just the first line of description for cleaner logging
        description_first_line = (
            tool_description.split("\n")[0].strip()
            if tool_description
            else "No description"
        )
        logger.info(f"  - {tool_name}: {description_first_line}")

    # Debug: Show what was allowed vs what was available
    logger.debug(f"Agent {agent_name} allowed tools: {allowed_tools}")
    all_tool_names = [getattr(tool, "name", "unknown") for tool in all_tools]
    logger.debug(f"Agent {agent_name} available tools: {all_tool_names}")

    return filtered_tools


class BaseAgentNode:
    """Base class for all agent nodes."""

    def __init__(
        self,
        name: str,
        description: str,
        tools: List[BaseTool],
        llm_provider: str = "ollama",
        agent_metadata: AgentMetadata = None,
        **llm_kwargs,
    ):
        # Use agent_metadata if provided, otherwise fall back to individual parameters
        if agent_metadata:
            self.name = agent_metadata.display_name
            self.description = agent_metadata.description
            self.actor_id = agent_metadata.actor_id
            self.agent_type = agent_metadata.agent_type
        else:
            # Backward compatibility - use provided name/description
            self.name = name
            self.description = description
            self.actor_id = None  # No actor_id available in legacy mode
            self.agent_type = "unknown"

        self.tools = tools
        self.llm_provider = llm_provider
        self.llm_kwargs = llm_kwargs  # Store for later use in memory client creation

        logger.info(
            f"Initializing {self.name} with LLM provider: {llm_provider}, actor_id: {self.actor_id}, tools: {[tool.name for tool in tools]}"
        )
        self.llm = _create_llm(llm_provider, **llm_kwargs)

        # Create the react agent
        self.agent = create_react_agent(self.llm, self.tools)

    def _get_system_prompt(self) -> str:
        """Get system prompt for this agent using prompt loader."""
        try:
            # Determine agent type based on name
            agent_type = self._get_agent_type()

            # Use prompt loader to get complete prompt
            return prompt_loader.get_agent_prompt(
                agent_type=agent_type,
                agent_name=self.name,
                agent_description=self.description,
            )
        except Exception as e:
            logger.error(f"Error loading prompt for agent {self.name}: {e}")
            # Fallback to basic prompt if loading fails
            return f"You are the {self.name}. {self.description}"

    def _get_agent_type(self) -> str:
        """Determine agent type based on agent metadata or fallback to name parsing."""
        # Use agent_type from metadata if available
        if hasattr(self, "agent_type") and self.agent_type != "unknown":
            return self.agent_type

        # Fallback to name-based detection for backward compatibility
        name_lower = self.name.lower()

        if "kubernetes" in name_lower:
            return "kubernetes"
        elif "logs" in name_lower or "application" in name_lower:
            return "logs"
        elif "metrics" in name_lower or "performance" in name_lower:
            return "metrics"
        elif "runbooks" in name_lower or "operational" in name_lower:
            return "runbooks"
        elif "github" in name_lower or "code" in name_lower or "git" in name_lower:
            return "github"
        else:
            logger.warning(f"Unknown agent type for agent: {self.name}")
            return "unknown"

    async def __call__(self, state: AgentState) -> Dict[str, Any]:
        """Process the current state and return updated state."""
        try:
            # NOTE: We intentionally do NOT pull state["messages"] here. Each
            # specialist runs in isolation with only its own system prompt +
            # task brief. Including the accumulated state["messages"] caused
            # two real bugs in the live audit:
            #   1. The next specialist would see prior specialists' tool_call
            #      AIMessages that referenced tools NOT bound to its own
            #      react agent, triggering LangChain's INVALID_CHAT_HISTORY
            #      validation error ("Found AIMessages with tool_calls that
            #      do not have a corresponding ToolMessage" / unknown tool).
            #   2. LLMs would hallucinate that they had called tools that
            #      another specialist actually called (e.g. the Loki
            #      Specialist reporting on a get_metric_range failure that
            #      really belonged to the Prometheus Specialist).
            # The full alert-aware context the specialist needs is already
            # baked into user_message via build_specialist_task_brief().
            agent_type = self._get_agent_type()
            agent_key = internal_agent_name(agent_type)

            # Build a rich, alert-aware task brief. This is the single
            # most important fix for diagnostic quality: previously the
            # specialist only saw the alert NAME, so it would query with
            # hardcoded labels (e.g. service="web-service") that didn't
            # match the actual alert and come back empty. The brief now
            # includes the alert payload's label values, time window,
            # and an explicit instruction to reuse them in tool calls.
            specialist_role = SPECIALIST_LABELS.get(
                agent_key, self.name.replace("_", " ").title()
            )
            agent_prompt = build_specialist_task_brief(
                specialist_role=specialist_role,
                objective=state.get("current_query", "") or self.name,
                alert_context=state.get("alert_context"),
                auto_approve=bool(state.get("auto_approve_plan", False)),
            )

            # We'll collect all messages and the final response
            all_messages = []
            agent_response = ""

            # Add system prompt and user prompt
            system_message = SystemMessage(content=self._get_system_prompt())
            user_message = HumanMessage(content=agent_prompt)

            # Stream the agent execution to capture tool calls with timeout
            logger.info(f"{self.name} - Starting agent execution")

            # Set Audit Context
            incident_id = None
            if state.get("alert_context"):
                # alert_context is a Pydantic model, or dict? 
                # Check type or try access
                ac = state.get("alert_context")
                if hasattr(ac, "incident_id"):
                     incident_id = str(ac.incident_id) if ac.incident_id else None
                # If incident_id not directly on alert_context, maybe we need to pass it in state separately 
                # or derive it. For now, we'll try to use what we have.
            
            # Also try to get from metadata if set by higher level
            if not incident_id:
                incident_id = state.get("metadata", {}).get("incident_id")
            
            set_audit_context(incident_id=incident_id, agent_name=self.name)

            try:
                # Add timeout to prevent infinite hanging (120 seconds)
                timeout_seconds = 120

                async def execute_agent():
                    nonlocal agent_response  # Fix scope issue - allow access to outer variable
                    chunk_count = 0
                    # Isolated chat history: only this specialist's system
                    # prompt + alert-aware brief. See the note at the top
                    # of __call__ for why we don't include state["messages"].
                    isolated_messages = [system_message, user_message]
                    logger.info(
                        f"{self.name} - Executing agent with {isolated_messages}"
                    )
                    async for chunk in self.agent.astream(
                        {"messages": isolated_messages}
                    ):
                        chunk_count += 1
                        logger.info(
                            f"{self.name} - Processing chunk #{chunk_count}: {list(chunk.keys())}"
                        )

                        if "agent" in chunk:
                            agent_step = chunk["agent"]
                            if "messages" in agent_step:
                                for msg in agent_step["messages"]:
                                    all_messages.append(msg)
                                    # Log tool calls being made
                                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                                        logger.info(
                                            f"{self.name} - Agent making {len(msg.tool_calls)} tool calls"
                                        )
                                        for tc in msg.tool_calls:
                                            tool_name = tc.get("name", "unknown")
                                            tool_args = tc.get("args", {})
                                            tool_id = tc.get("id", "unknown")
                                            
                                            # Intercept actual agent reasoning/tool usage for the transcript
                                            traces = state.get("thought_traces", {})
                                            if agent_key not in traces:
                                                traces[agent_key] = []
                                            
                                            reasoning = ""
                                            if hasattr(msg, "content") and isinstance(msg.content, str) and msg.content.strip():
                                                reasoning = msg.content.strip() + "\n"
                                                
                                            actual_thought = f"{reasoning} *(Action: Invoking `{tool_name}` to gather context)*"
                                            
                                            # Avoid duplicate reasoning lines on multi-tool outputs
                                            if actual_thought not in traces[agent_key]:
                                                traces[agent_key].append(actual_thought)
                                            
                                            state["thought_traces"] = traces
                                            
                                            logger.info(
                                                f"{self.name} - Tool call: {tool_name} (id: {tool_id})"
                                            )
                                            logger.debug(
                                                f"{self.name} - Tool args: {tool_args}"
                                            )
                                    # Always capture the latest content from AIMessages
                                    if (
                                        hasattr(msg, "content")
                                        and hasattr(msg, "__class__")
                                        and "AIMessage" in str(msg.__class__)
                                    ):
                                        agent_response = msg.content
                                        logger.info(
                                            f"{self.name} - Agent response captured: {agent_response[:100]}... (total: {len(str(agent_response))} chars)"
                                        )

                        elif "tools" in chunk:
                            tools_step = chunk["tools"]
                            logger.info(
                                f"{self.name} - Tools chunk received, processing {len(tools_step.get('messages', []))} messages"
                            )
                            if "messages" in tools_step:
                                for msg in tools_step["messages"]:
                                    all_messages.append(msg)
                                    # Log tool executions
                                    if hasattr(msg, "tool_call_id"):
                                        tool_name = getattr(msg, "name", "unknown")
                                        tool_call_id = getattr(
                                            msg, "tool_call_id", "unknown"
                                        )
                                        content_preview = (
                                            str(msg.content)[:200]
                                            if hasattr(msg, "content")
                                            else "No content"
                                        )
                                        logger.info(
                                            f"{self.name} - Tool response received: {tool_name} (id: {tool_call_id}), content: {content_preview}..."
                                        )
                                        logger.debug(
                                            f"{self.name} - Full tool response: {msg.content if hasattr(msg, 'content') else 'No content'}"
                                        )

                logger.info(
                    f"{self.name} - Executing agent with timeout of {timeout_seconds} seconds"
                )
                await asyncio.wait_for(execute_agent(), timeout=timeout_seconds)
                logger.info(f"{self.name} - Agent execution completed")

            except asyncio.TimeoutError:
                logger.error(
                    f"{self.name} - Agent execution timed out after {timeout_seconds} seconds"
                )
                agent_response = f"Agent execution timed out after {timeout_seconds} seconds. The agent may be stuck on a tool call or LLM response."

            except Exception as e:
                logger.error(f"{self.name} - Agent execution failed: {e}")
                logger.exception("Full exception details:")
                agent_response = f"Agent execution failed: {str(e)}"

            # Debug: Check what we captured
            logger.info(
                f"{self.name} - Captured response length: {len(agent_response) if agent_response else 0}"
            )
            if agent_response:
                logger.info(f"{self.name} - Full response: {str(agent_response)}")

            # Update state with streaming info
            agent_type = self._get_agent_type()
            specialist_agent_name = agent_key
            speaker_role = visible_specialist_role(specialist_agent_name)
            if speaker_role != "system":
                # Narrate the finding conversationally before persisting it.
                # The specialist's raw markdown response (with its tables, tool
                # output, etc.) is preserved in the structured payload, but the
                # visible chat content reads like a teammate's Slack post.
                narrative_text = ""
                try:
                    narrative_text = await narrate_specialist_finding(
                        self.llm,
                        agent_name=specialist_agent_name,
                        objective=state.get("current_query", "") or self.name,
                        alert_context=state.get("alert_context"),
                        raw_response=agent_response,
                    )
                except Exception as narration_error:
                    logger.warning(
                        f"{self.name} - finding narration failed, falling back: {narration_error}"
                    )

                finding_content, finding_payload = build_specialist_finding_content(
                    specialist_agent_name,
                    state.get("current_query", ""),
                    agent_response,
                    narrative=narrative_text,
                )
                finding_payload["narrative"] = narrative_text or finding_content

                await emit_timeline_event(
                    incident_id,
                    event_type="finding",
                    speaker_role=speaker_role,
                    title=self.name,
                    content=finding_content,
                    payload=finding_payload,
                )

            # Intentionally do NOT return "messages" here. The specialist's
            # internal tool_call AIMessages and ToolMessages are an
            # implementation detail of THIS specialist's react loop and
            # must not leak into state["messages"] — otherwise the next
            # specialist (with a different bound tool set) would see
            # tool_calls referencing tools it doesn't have, triggering
            # LangChain's INVALID_CHAT_HISTORY error. The full raw response
            # is preserved in agent_results[agent_key] for the supervisor
            # to synthesize from, and in metadata[..._trace] for debugging.
            return {
                "agent_results": {
                    **state.get("agent_results", {}),
                    agent_key: agent_response,
                },
                "agents_invoked": state.get("agents_invoked", []) + [agent_key],
                "metadata": {
                    **state.get("metadata", {}),
                    f"{agent_key}_trace": all_messages,
                },
            }

        except Exception as e:
            logger.error(f"Error in {self.name}: {e}")
            return {
                "agent_results": {
                    **state.get("agent_results", {}),
                    agent_key: f"Error: {str(e)}",
                },
                "agents_invoked": state.get("agents_invoked", []) + [agent_key],
            }
        finally:
            clear_audit_context()


def create_kubernetes_agent(
    tools: List[BaseTool], agent_metadata: AgentMetadata = None, **kwargs
) -> BaseAgentNode:
    """Create Kubernetes infrastructure agent."""
    config = _load_agent_config()
    filtered_tools = _filter_tools_for_agent(tools, "kubernetes_agent", config)

    return BaseAgentNode(
        name="Kubernetes Infrastructure Agent",  # Fallback for backward compatibility
        description="Manages Kubernetes cluster operations and monitoring",  # Fallback
        tools=filtered_tools,
        agent_metadata=agent_metadata,
        **kwargs,
    )


def create_logs_agent(
    tools: List[BaseTool], agent_metadata: AgentMetadata = None, **kwargs
) -> BaseAgentNode:
    """Create application logs agent."""
    config = _load_agent_config()
    filtered_tools = _filter_tools_for_agent(tools, "logs_agent", config)

    return BaseAgentNode(
        name="Application Logs Agent",  # Fallback for backward compatibility
        description="Handles application log analysis and searching",  # Fallback
        tools=filtered_tools,
        agent_metadata=agent_metadata,
        **kwargs,
    )


def create_metrics_agent(
    tools: List[BaseTool], agent_metadata: AgentMetadata = None, **kwargs
) -> BaseAgentNode:
    """Create performance metrics agent."""
    config = _load_agent_config()
    filtered_tools = _filter_tools_for_agent(tools, "metrics_agent", config)

    return BaseAgentNode(
        name="Performance Metrics Agent",  # Fallback for backward compatibility
        description="Provides application performance and resource metrics",  # Fallback
        tools=filtered_tools,
        agent_metadata=agent_metadata,
        **kwargs,
    )


def create_runbooks_agent(
    tools: List[BaseTool], agent_metadata: AgentMetadata = None, **kwargs
) -> BaseAgentNode:
    """Create operational runbooks agent."""
    config = _load_agent_config()
    filtered_tools = _filter_tools_for_agent(tools, "runbooks_agent", config)

    return BaseAgentNode(
        name="Operational Runbooks Agent",  # Fallback for backward compatibility
        description="Provides operational procedures and troubleshooting guides",  # Fallback
        tools=filtered_tools,
        agent_metadata=agent_metadata,
        **kwargs,
    )


def create_github_agent(
    tools: List[BaseTool], agent_metadata: AgentMetadata = None, **kwargs
) -> BaseAgentNode:
    """Create code change intelligence agent (GitHub)."""
    config = _load_agent_config()
    filtered_tools = _filter_tools_for_agent(tools, "github_agent", config)

    return BaseAgentNode(
        name="Code Change Intelligence Agent",  # Fallback for backward compatibility
        description="Correlates code changes (commits, PRs) with incidents and identifies bad commits",  # Fallback
        tools=filtered_tools,
        agent_metadata=agent_metadata,
        **kwargs,
    )
