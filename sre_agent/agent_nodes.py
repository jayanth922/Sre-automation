#!/usr/bin/env python3

import asyncio
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt import create_react_agent

from .agent_state import AgentState
from .act_phase import measured_evidence_for_trace
from .audit_context import set_audit_context, clear_audit_context
from .constants import AgentMetadata
from .evidence_artifacts import persist_specialist_trace
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

_SPECIALIST_ROLE_METADATA_KEY = "sentinel.specialist_role"


def _bounded_agent_result(response: str, max_chars: Optional[int] = None) -> str:
    """Keep active reasoning context bounded; the artifact remains lossless."""
    if max_chars is None:
        try:
            max_chars = int(os.getenv("AGENT_RESULT_MAX_CHARS", "12000"))
        except (TypeError, ValueError):
            max_chars = 12000
    max_chars = min(max(int(max_chars), 2000), 50000)
    text = str(response or "")
    if len(text) <= max_chars:
        return text
    marker = "\n\n… [middle omitted; full response stored in evidence artifact] …\n\n"
    available = max_chars - len(marker)
    head = available // 2
    tail = available - head
    return text[:head] + marker + text[-tail:]


async def _artifact_backed_trace_metadata(
    state: AgentState,
    *,
    incident_id: Optional[str],
    agent_key: str,
    messages: List[Any],
    raw_response: str,
    tool_failures: List[Dict[str, str]],
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Offload a transcript while preserving a lossless in-state fallback."""
    metadata = dict(state.get("metadata", {}) or {})
    measured_by_agent = dict(metadata.get("measured_evidence", {}) or {})
    measured_by_agent[agent_key] = measured_evidence_for_trace(agent_key, messages)
    metadata["measured_evidence"] = measured_by_agent

    try:
        reference = await persist_specialist_trace(
            incident_id=incident_id,
            root_trace_id=metadata.get("root_trace_id"),
            agent_name=agent_key,
            messages=messages,
            raw_response=raw_response,
            tool_failures=tool_failures,
        )
    except Exception as exc:
        # The graph must not silently discard evidence just because artifact
        # storage is unavailable. The old checkpoint shape is intentionally the
        # fallback, and the error records only its type (never evidence text).
        logger.warning(
            "%s - durable evidence artifact unavailable; retaining checkpoint trace: %s",
            agent_key,
            type(exc).__name__,
        )
        metadata[f"{agent_key}_trace"] = messages
        errors = dict(metadata.get("evidence_artifact_errors", {}) or {})
        errors[agent_key] = type(exc).__name__
        metadata["evidence_artifact_errors"] = errors
        return metadata, None

    if reference is None:
        # CLI/ad-hoc runs have no durable incident identity to own an artifact.
        metadata[f"{agent_key}_trace"] = messages
        return metadata, None

    references = dict(metadata.get("evidence_artifact_refs", {}) or {})
    prior = references.get(agent_key, [])
    if isinstance(prior, dict):
        prior = [prior]
    history = [item for item in prior if isinstance(item, dict)]
    if not any(item.get("artifact_id") == reference["artifact_id"] for item in history):
        history.append(reference)
    references[agent_key] = history
    metadata["evidence_artifact_refs"] = references
    metadata.pop(f"{agent_key}_trace", None)
    errors = dict(metadata.get("evidence_artifact_errors", {}) or {})
    errors.pop(agent_key, None)
    if errors:
        metadata["evidence_artifact_errors"] = errors
    else:
        metadata.pop("evidence_artifact_errors", None)
    return metadata, reference


def specialist_trace_metadata(agent_type: str) -> Dict[str, str]:
    """Stable metadata used to name the specialist's internal trace nodes."""
    return {
        _SPECIALIST_ROLE_METADATA_KEY: internal_agent_name(agent_type),
    }


@lru_cache(maxsize=1)
def _load_agent_config() -> Dict[str, Any]:
    """Load agent configuration from YAML file."""
    config_path = Path(__file__).parent / "config" / "agent_config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _create_llm(provider: str = "anthropic", router_enabled: Optional[bool] = None, **kwargs):
    """Create a specialist LLM, routed to the balanced tier by the model router."""
    from .model_router import TaskType, route_llm
    return route_llm(
        TaskType.SPECIALIST,
        provider=provider,
        use_fallback=False,
        router_enabled=router_enabled,
        **kwargs,
    )


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
        llm_provider: str = "anthropic",
        agent_metadata: AgentMetadata = None,
        llm_router_enabled: Optional[bool] = None,
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
        self.llm = _create_llm(llm_provider, router_enabled=llm_router_enabled, **llm_kwargs)

        # Tag the tool catalog for Anthropic prompt caching: this specialist's
        # tool set is stable across its entire multi-turn tool-calling loop
        # (and identical across incidents), so caching it turns every
        # follow-up round-trip in the loop into a ~90%-cheaper cache read
        # instead of a full-price re-send. No-op for non-Anthropic providers
        # or when ANTHROPIC_PROMPT_CACHE_ENABLED=false.
        agent_tools = self.tools
        if llm_provider == "anthropic":
            from .model_router import cached_tools

            agent_tools = cached_tools(self.tools)

        # Create the react agent. The tools go in as an explicit ToolNode so a
        # ToolExecutionError (an MCP server that stayed down through every
        # retry) becomes a ToolMessage with status="error" instead of killing
        # the run — langgraph's default handler re-raises anything that isn't a
        # bad-arguments error. That status is what `tool_failures` below is
        # keyed off, so this is the join between a tool being down and the
        # supervisor being able to say so.
        from langgraph.prebuilt import ToolNode

        from .mcp_tool_wrapper import handle_tool_execution_error

        self.agent = create_react_agent(
            self.llm,
            ToolNode(agent_tools, handle_tool_errors=handle_tool_execution_error),
            # LangGraph otherwise names every specialist subgraph `agent`.
            # Langfuse's Agent Graph keys nodes by this stable name, so the
            # generic default collapses distinct roles into one unreadable
            # node. Keep the low-cardinality internal role name instead of a
            # run-specific value or model name.
            name=internal_agent_name(self.agent_type),
        )

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
            # Genuine tool-call failures for this specialist, keyed off
            # ToolMessage.status == "error" (set by langgraph's ToolNode when
            # a bound tool raises). This is the ONLY reliable signal for "the
            # tool itself failed" — see the note on agent_tool_failures in
            # agent_state.py for why we don't scan the narrative text for it.
            tool_failures: List[Dict[str, str]] = []

            # Add system prompt and user prompt. The system prompt is static
            # per agent type (no per-incident data), so it's tagged for
            # Anthropic prompt caching alongside the tool catalog above.
            if self.llm_provider == "anthropic":
                from .model_router import cached_system_message

                system_message = cached_system_message(self._get_system_prompt())
            else:
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
                        {"messages": isolated_messages},
                        config={"metadata": specialist_trace_metadata(self.agent_type)},
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
                                        content = msg.content
                                        if isinstance(content, list):
                                            # Extended-thinking / multi-block Anthropic
                                            # responses return content as a list of blocks
                                            # (thinking, tool_use, text, ...) rather than a
                                            # plain string. Downstream code (narrative.py,
                                            # incident_timeline.py) expects agent_response
                                            # to be a str, so keep only the text blocks.
                                            content = "".join(
                                                block.get("text", "")
                                                for block in content
                                                if isinstance(block, dict)
                                                and block.get("type") == "text"
                                            )
                                        if content:
                                            agent_response = content
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
                                        # langgraph's ToolNode sets status="error" on the
                                        # ToolMessage when the bound tool itself raises
                                        # (network error, MCP call failed, etc) — this is
                                        # a real tool-infrastructure failure, distinct from
                                        # the tool successfully returning data that merely
                                        # *describes* the investigated service's own errors.
                                        if getattr(msg, "status", "success") == "error":
                                            tool_failures.append({
                                                "tool": tool_name,
                                                "error": content_preview,
                                            })
                                            logger.warning(
                                                f"{self.name} - Real tool failure: {tool_name} (id: {tool_call_id}): {content_preview}"
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

            # The internal React transcript can contain every raw tool payload
            # and is routinely much larger than the specialist's final report.
            # Persist it as a durable artifact before the graph checkpoints;
            # policy keeps only its compact measured projection in state.
            agent_type = self._get_agent_type()
            specialist_agent_name = agent_key
            artifact_metadata, artifact_reference = await _artifact_backed_trace_metadata(
                state,
                incident_id=incident_id,
                agent_key=agent_key,
                messages=all_messages,
                raw_response=agent_response,
                tool_failures=tool_failures,
            )
            state_agent_response = (
                _bounded_agent_result(agent_response)
                if artifact_reference is not None
                else agent_response
            )

            # Update state with streaming info
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
                # Persisted alongside raw_response so a later follow-up chat
                # (which reloads agent_results from this payload via
                # load_incident_chat_context) can still tell real tool
                # failures apart from the investigated service's own errors.
                finding_payload["tool_failures"] = tool_failures
                if artifact_reference is not None:
                    finding_payload["evidence_artifact_ref"] = artifact_reference

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
            # LangChain's INVALID_CHAT_HISTORY error. A bounded head-and-tail
            # view of the final response stays in agent_results for active
            # synthesis. The lossless response and tool evidence live in a
            # durable, content-addressed artifact; state carries its reference
            # and compact measured values. The legacy metadata trace is
            # retained only when no durable incident exists or artifact
            # storage fails.
            return {
                "agent_results": {
                    **state.get("agent_results", {}),
                    agent_key: state_agent_response,
                },
                "agent_tool_failures": {
                    **state.get("agent_tool_failures", {}),
                    agent_key: tool_failures,
                },
                "agents_invoked": state.get("agents_invoked", []) + [agent_key],
                "metadata": artifact_metadata,
            }

        except Exception as e:
            logger.error(f"Error in {self.name}: {e}")
            return {
                "agent_results": {
                    **state.get("agent_results", {}),
                    agent_key: f"Error: {str(e)}",
                },
                "agent_tool_failures": {
                    **state.get("agent_tool_failures", {}),
                    agent_key: [{"tool": "agent_execution", "error": str(e)}],
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
