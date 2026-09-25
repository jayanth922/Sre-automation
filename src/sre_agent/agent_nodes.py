#!/usr/bin/env python3

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent

from .act_phase import measured_evidence_for_trace
from .agent_state import AgentState
from .audit_context import clear_audit_context, set_audit_context
from .constants import AgentMetadata
from .context_compaction import (
    capture_fit_reports,
    merge_fit_summaries,
    summarize_fit_reports,
)
from .evidence_artifacts import persist_specialist_trace
from .incident_timeline import (
    build_specialist_finding_content,
    emit_timeline_event,
    internal_agent_name,
    visible_specialist_role,
)
from .investigation_limits import investigation_limits
from .narrative import (
    SPECIALIST_LABELS,
    alert_start_time,
    build_specialist_task_brief,
    narrate_specialist_finding,
    runbook_text_for_alert,
)
from .prompt_loader import prompt_loader
from .runbook_probe import (
    metrics_probe_caller,
    probe_payload,
    probe_runbook_queries,
)

# Logging will be configured by the main entry point
logger = logging.getLogger(__name__)

_SPECIALIST_ROLE_METADATA_KEY = "sentinel.specialist_role"

# Seconds of the specialist wall-clock budget reserved so the last turn can
# finish. Measured p90 specialist model-call latency on the graded
# inventory_slow_queries run was 26.1s (p95 42.3s, max 68.6s), so a turn
# started with less than this left is likely to be killed mid-flight: paid
# for, and discarded.
_TURN_HEADROOM_SECONDS = 30
# How much of each already-collected tool result survives into the digest a
# cut-short lane reports.
_PARTIAL_EVIDENCE_TOOL_CHARS = 600
_PARTIAL_EVIDENCE_MAX_TOOLS = 12


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


def _turn_headroom_seconds(timeout_seconds: int) -> int:
    """Seconds of the lane budget reserved so the last started turn finishes.

    A short configured budget still spends two thirds of itself on turns;
    reserving a flat 30s out of the 15s minimum would stop the lane before
    its first tool round.
    """
    return min(_TURN_HEADROOM_SECONDS, max(int(timeout_seconds), 0) // 3)


# create_react_agent wires pre_model_hook → agent → tools → pre_model_hook,
# so one tool round costs three LangGraph steps, not two. Budgeting two made
# the framework backstop (14) bite in the middle of turn five of a six-turn
# budget, and GraphRecursionError is a crash, not the graceful boundary
# below: the 2026-09-22 trial lost its whole logs lane to it, twice.
_REACT_STEPS_PER_TURN = 3


def _specialist_recursion_limit(model_turns: int) -> int:
    """The framework backstop, sized to trip after the explicit turn budget."""
    return max(1, int(model_turns)) * _REACT_STEPS_PER_TURN + 2


# A lane that answered without calling a single tool did not investigate: the
# model returned its opening sentence and stopped. Every other boundary in this
# lane is detected and labelled; this one was not, so the 2026-09-22 metrics
# lane reported 19 characters ("I'll verify current") as a complete finding
# while Prometheus held the 1.59s fault that decided the incident. One retry is
# cheaper than an evidence lane silently contributing nothing to a whole run.
_NO_TOOL_RETRY_MIN_SECONDS = 20.0
_NO_TOOL_RETRY_DIRECTIVE = (
    "Your previous turn returned no tool calls, so you gathered no evidence. "
    "Do not answer from the brief alone. Call the tools you need now, then "
    "report what they actually returned."
)
_NO_TOOL_LANE_NOTE = (
    "This lane called no tools, so it collected no evidence of its own and "
    "the text above is a preamble rather than a finding. Do not treat it as "
    "observed data, and do not report this lane's subject as checked."
)
# ...with one exception. The runbooks lane's domain is the procedure itself,
# and build_specialist_task_brief() already inlines the authoritative runbook
# for this alert. When it is there, the lane has nothing left to retrieve and
# answering from it is the contract, not a skipped investigation. On
# 2026-09-22 the guard above read that as a silent lane and bought a second
# run of it. With no runbook inlined the lane does have to go and find one,
# so the exemption is conditional on the brief, not on the lane alone.
_RUNBOOK_SUFFICIENT_AGENTS = frozenset({"runbooks_agent"})

# A turn that ran out of output tokens and a model that declined to call a
# tool are different failures with opposite fixes, and until now they were
# recorded identically -- nothing in this lane read the provider's stop
# reason at all. LiteLLM normalises Anthropic's "max_tokens" to OpenAI's
# "length", so both spellings arrive depending on the backend in use.
_TRUNCATION_FINISH_REASONS = frozenset(
    {"length", "max_tokens", "max_output_tokens"}
)
_TRUNCATED_LANE_NOTE = (
    "This lane was cut off at its output-token ceiling before it produced a "
    "tool call or a finding, so it collected no evidence of its own. That is "
    "a budget boundary, not a finding that this lane's subject is healthy: "
    "do not report this lane's subject as checked."
)
# Telling a model that was cut off mid-sentence "do not answer from the brief
# alone" wastes the retry: it never got as far as an answer. Ask for brevity
# instead, which is the one thing that makes the second attempt fit.
_TRUNCATED_RETRY_DIRECTIVE = (
    "Your previous turn hit the output-token ceiling before it produced "
    "anything usable. Do not restate the brief and do not plan at length. "
    "Call your first tool immediately, then report only what it returned."
)

# The hard ceiling on everything salvaged for one lane, probe measurement
# and tool digest together. It sits under narrative._truncate's own 1800-char
# cap on a finding entering the next lane's brief, so salvage can never be
# larger than a finding that survives that cap -- it costs no payload an
# ordinary finding would not have cost. _partial_evidence_digest's own bounds
# (600 chars x 12 results) are deliberately looser, because on the cut-short
# paths the digest *is* the whole lane report; here it is an addition to one.
# Nothing in this path calls a model: the probe ran before the lane's first
# turn and the tool results were already on the wire.
_SALVAGE_MAX_CHARS = 1500
# Two thirds of it, so a long probe can never crowd the tool results out
# entirely. A real probe block is a few hundred chars.
_PROBE_SALVAGE_MAX_CHARS = 1000


def _finish_reason(message: Any) -> str:
    """The provider's stop reason for one model turn, lowercased."""
    for holder in ("response_metadata", "additional_kwargs"):
        payload = getattr(message, holder, None)
        if not isinstance(payload, dict):
            continue
        for key in ("finish_reason", "stop_reason"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return ""


def _bounded_lines(text: str, limit: int) -> str:
    """Trim to whole lines within `limit`, saying how many were dropped."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    kept = head.rsplit("\n", 1)[0] if "\n" in head else head
    dropped = text.count("\n") - kept.count("\n")
    return (
        f"{kept}\n[{dropped} further line(s) omitted here; the full "
        "transcript is in this run's evidence artifact]"
    )


# Matched against text that may already have been cut to a cap, so it is
# the opening clause rather than the whole note: a truncated copy still
# answers "is this lane already carrying the probe?" correctly.
_PROBE_NOTE_HEADER = "Runbook query probe -- measured by the runtime"


def _probe_measurement_note(runbook_probe_block: str) -> str:
    """The runbook probe's numbers, restated as a finding of the runtime's."""
    payload = probe_payload(runbook_probe_block)
    if not payload:
        return ""
    return (
        f"{_PROBE_NOTE_HEADER} before this lane's "
        "first turn, so these are arithmetic over live series and not a "
        "model's claim:\n" + payload[:_PROBE_SALVAGE_MAX_CHARS]
    )


def _salvaged_evidence(
    messages: List[Any], runbook_probe_block: str, existing: str = ""
) -> str:
    """Everything the runtime already holds for a lane that reported nothing.

    Both sources are deterministic and already bought. Neither is re-added
    when the text the lane is carrying already contains it: the timeout and
    recursion handlers append the same digest, and salvage must not say
    anything twice.
    """
    parts: List[str] = []
    probe_note = _probe_measurement_note(runbook_probe_block)
    if probe_note and _PROBE_NOTE_HEADER not in (existing or ""):
        parts.append(probe_note)
    if _EVIDENCE_DIGEST_HEADER not in (existing or ""):
        digest = _partial_evidence_digest(
            messages, reason="stopped without writing a finding"
        )
        if digest:
            parts.append(digest)
    # The probe goes first and is never the part that gets cut: it is a
    # single decisive number, and on 2026-09-23 it was the number the whole
    # incident turned on.
    return _bounded_lines("\n\n".join(parts), _SALVAGE_MAX_CHARS)


def _cut_short_reason(
    turn_budget: "SpecialistTurnBudget",
    *,
    budget_hit: bool,
    now: float,
    soft_deadline: float,
) -> str:
    """Which boundary, if any, should stop this lane before the next round.

    Both boundaries only bite when the model has actually asked for another
    tool round: a lane that is about to write its report is never cut off.
    """
    if budget_hit:
        return "turn_limit"
    if turn_budget.requested_another_round and now >= soft_deadline:
        return "soft_deadline"
    return ""


_EVIDENCE_DIGEST_HEADER = "Evidence collected before this lane "


def _partial_evidence_digest(
    messages: List[Any], *, reason: str = "was cut short"
) -> str:
    """Report what a cut-short lane actually collected.

    The tool results already on the wire were paid for and are as valid as
    any others. Replacing them with a bare "timed out" string is what made
    the graded inventory_slow_queries trial tell the supervisor it had no
    application logs -- after Loki had answered four times, in about 0.1s
    each. The deadline is a budget boundary, not a tool failure, and the
    brief should say so.
    """
    entries: List[str] = []
    for msg in messages:
        if not hasattr(msg, "tool_call_id"):
            continue
        name = getattr(msg, "name", None) or "unknown_tool"
        marker = " (tool failed)" if getattr(msg, "status", "success") == "error" else ""
        body = str(getattr(msg, "content", "") or "").strip()
        if len(body) > _PARTIAL_EVIDENCE_TOOL_CHARS:
            body = body[:_PARTIAL_EVIDENCE_TOOL_CHARS] + " …[truncated]"
        entries.append(f"- `{name}`{marker}: {body or '(empty result)'}")
    if not entries:
        return ""
    total = len(entries)
    dropped = max(total - _PARTIAL_EVIDENCE_MAX_TOOLS, 0)
    if dropped:
        entries = entries[-_PARTIAL_EVIDENCE_MAX_TOOLS:]
    header = f"{_EVIDENCE_DIGEST_HEADER}{reason} ({total} tool results"
    header += f", {dropped} older ones omitted here):" if dropped else "):"
    return header + "\n" + "\n".join(entries)


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


@dataclass
class SpecialistTurnBudget:
    """Count model turns and stop before a requested next tool round."""

    limit: int
    turns: int = 0
    exhausted: bool = False
    requested_another_round: bool = False

    def observe(self, agent_step: Any) -> bool:
        self.turns += 1
        messages = (
            agent_step.get("messages", [])
            if isinstance(agent_step, dict)
            else []
        )
        # Recorded, not just tested: the wall-clock deadline needs to know
        # whether the model is asking for another tool round before deciding
        # there is no time left to grant one.
        self.requested_another_round = any(
            bool(getattr(message, "tool_calls", None)) for message in messages
        )
        self.exhausted = (
            self.turns >= self.limit and self.requested_another_round
        )
        return self.exhausted


@lru_cache(maxsize=1)
def _load_agent_config() -> Dict[str, Any]:
    """Load agent configuration from YAML file."""
    config_path = Path(__file__).parent / "config" / "agent_config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _create_llm(provider: str = "anthropic", router_enabled: Optional[bool] = None, **kwargs):
    """Create a specialist LLM, routed to the balanced tier by the model router."""
    from .model_router import TaskType, route_llm

    # Bound the output length of a specialist turn. Without this the live
    # LiteLLM transport applies no ceiling at all (see model_router), and a
    # single turn can spend a third of the lane's wall-clock budget writing
    # prose no downstream consumer reads.
    kwargs.setdefault(
        "max_tokens", investigation_limits().specialist_max_output_tokens
    )
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
        prepare_for_model = None
        if llm_provider == "anthropic":
            from .model_router import cache_conversation_prefix, cached_tools

            agent_tools = cached_tools(self.tools)
            prepare_for_model = cache_conversation_prefix

        # Create the react agent. The tools go in as an explicit ToolNode so a
        # ToolExecutionError (an MCP server that stayed down through every
        # retry) becomes a ToolMessage with status="error" instead of killing
        # the run — langgraph's default handler re-raises anything that isn't a
        # bad-arguments error. That status is what `tool_failures` below is
        # keyed off, so this is the join between a tool being down and the
        # supervisor being able to say so.
        from langgraph.prebuilt import ToolNode

        from .context_compaction import make_pre_model_hook
        from .mcp_tool_wrapper import handle_tool_execution_error

        self.agent = create_react_agent(
            self.llm,
            ToolNode(agent_tools, handle_tool_errors=handle_tool_execution_error),
            # This loop, not the graph, is where context actually grows: each
            # iteration re-sends the whole transcript, and one `kubectl get -o
            # json` can be tens of thousands of tokens. Pre-run compaction
            # never sees any of it, because specialists run on an isolated
            # message list and do not write back to `state["messages"]`.
            #
            # The hook returns `llm_input_messages`, so it trims only what the
            # model is shown — graph state keeps the full transcript for
            # `tool_failures` detection and the stored evidence artifact.
            pre_model_hook=make_pre_model_hook(
                on_report=lambda report: logger.info(
                    f"[{self.name}] context fit: {report.before_tokens} → "
                    f"{report.after_tokens} tokens (budget {report.budget_tokens}); "
                    f"{report.capped_results} oversized result(s) capped, "
                    f"{report.truncated_results} tool result(s) truncated, "
                    f"{report.dropped_messages} message(s) elided"
                ),
                # The system prompt and tool catalog are already cached, but
                # they are the *static* part. This is the loop that re-sends
                # the transcript every iteration, so without a breakpoint at
                # its tail the history is billed at the full input rate once
                # per turn. Runs after fitting, so the marker lands on a
                # message that survived trimming.
                prepare_for_model=prepare_for_model,
            ),
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

    async def _probe_runbook_queries(self, state: AgentState) -> str:
        """Measure the runbook's own PromQL before this lane's first turn.

        Timing a rate window is arithmetic, and the 2026-09-22 trial showed
        what it costs to leave it to a model: the runbook's query ran at the
        alert stamp, read a pre-fault window, and routed a live regression to
        the runbook's do-nothing branch. Fail-soft by construction — any
        failure leaves the lane exactly as it was, with every tool bound.
        """
        try:
            runbook_text = runbook_text_for_alert(state.get("alert_context"))
            if not runbook_text:
                return ""
            caller = metrics_probe_caller(self.tools)
            if caller is None:
                return ""
            return await probe_runbook_queries(
                runbook_text,
                tool_caller=caller,
                alert_started_at=alert_start_time(state.get("alert_context")),
            )
        except Exception as probe_error:
            logger.warning(
                "%s - runbook query probe skipped (%s): %s",
                self.name,
                type(probe_error).__name__,
                probe_error,
            )
            return ""

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
            # Prior specialists' reports travel forward. Specialists still run
            # on isolated message lists (see the note above — sharing raw
            # transcripts breaks tool-call validation and causes cross-agent
            # hallucination), but their *conclusions* are exactly what stops
            # the next one re-deriving the same fact from raw evidence. This
            # forwards the compact report only, re-bounded inside the brief.
            prior_findings = {
                key: value
                for key, value in (state.get("agent_results") or {}).items()
                if key != agent_key and value
            }
            # Set Audit Context — before the runbook probe below, so its
            # tool call is attributed to this incident and lane exactly like
            # a model-issued one.
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

            set_audit_context(
                incident_id=incident_id,
                agent_name=self.name,
                investigation_scope=True,
            )

            # Only the lane that can run PromQL, plus the single-agent
            # ablation arm, which must differ from the full arm in the
            # ablated dimension and nothing else.
            runbook_hints = agent_type in ("metrics", "single")
            # Kept, not merely passed. These are the runtime's own numbers for
            # the runbook's gating query; if the lane that receives them never
            # reports, they are still the best evidence in the incident.
            runbook_probe_block = (
                await self._probe_runbook_queries(state) if runbook_hints else ""
            )
            agent_prompt = build_specialist_task_brief(
                specialist_role=specialist_role,
                objective=state.get("current_query", "") or self.name,
                alert_context=state.get("alert_context"),
                auto_approve=bool(state.get("auto_approve_plan", False)),
                prior_findings=prior_findings,
                namespace_scope=(state.get("metadata") or {}).get(
                    "cluster_namespace"
                ),
                runbook_query_hints=runbook_hints,
                runbook_probe=runbook_probe_block,
            )

            # Answering without a tool call is this lane's contract only
            # while the runbook it would have fetched is already in hand.
            runbook_answerable = bool(
                agent_key in _RUNBOOK_SUFFICIENT_AGENTS
                and runbook_text_for_alert(state.get("alert_context"))
            )

            # We'll collect all messages and the final response
            all_messages = []
            agent_response = ""
            # Two facts this lane never recorded, and could not report
            # without: whether the provider stopped a turn at the output
            # ceiling, and whether the model ever emitted a text block at
            # all. Without the first, a lane cut off mid-turn was labelled a
            # lane that declined to investigate. Without the second, deciding
            # whether anything is worth salvaging means pattern-matching our
            # own notes back out of the response.
            response_truncated = False
            model_text_captured = False
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

            fit_reports = []
            limits = investigation_limits()
            turn_budget = SpecialistTurnBudget(limits.specialist_model_turns)
            # "" while the lane ran to completion; otherwise the boundary that
            # stopped it. Drives both the evidence digest and the decision to
            # skip the narration model call for a lane that has nothing new
            # to narrate.
            cut_short_reason = ""
            # Counted across the retry too: a lane that never reaches a tool
            # has not investigated, however long its prose.
            tool_calls_made = 0
            try:
                timeout_seconds = limits.specialist_timeout_seconds
                # Stop *starting* a turn the clock cannot finish, instead of
                # paying for one and throwing it away at the deadline. A very
                # short configured budget still gets to spend two thirds of
                # itself on turns.
                soft_deadline = (
                    time.monotonic()
                    + timeout_seconds
                    - _turn_headroom_seconds(timeout_seconds)
                )

                async def execute_agent(extra_directive: str = ""):
                    nonlocal agent_response  # Fix scope issue - allow access to outer variable
                    nonlocal cut_short_reason
                    nonlocal tool_calls_made
                    nonlocal response_truncated
                    nonlocal model_text_captured
                    chunk_count = 0
                    # Isolated chat history: only this specialist's system
                    # prompt + alert-aware brief. See the note at the top
                    # of __call__ for why we don't include state["messages"].
                    isolated_messages = [system_message, user_message]
                    if extra_directive:
                        isolated_messages.append(
                            HumanMessage(content=extra_directive)
                        )
                    logger.info(
                        f"{self.name} - Executing agent with {isolated_messages}"
                    )
                    agent_stream = self.agent.astream(
                        {"messages": isolated_messages},
                        config={
                            "metadata": specialist_trace_metadata(self.agent_type),
                            # The explicit counter below is the graceful
                            # boundary; this is the framework backstop, and it
                            # has to sit above the step cost of a full turn
                            # budget or it fires first and raises.
                            "recursion_limit": _specialist_recursion_limit(
                                limits.specialist_model_turns
                            ),
                        },
                    )
                    async for chunk in agent_stream:
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
                                        tool_calls_made += len(msg.tool_calls)
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
                                            model_text_captured = True
                                            logger.info(
                                                f"{self.name} - Agent response captured: {agent_response[:100]}... (total: {len(str(agent_response))} chars)"
                                            )
                                        finish_reason = _finish_reason(msg)
                                        if finish_reason in _TRUNCATION_FINISH_REASONS:
                                            response_truncated = True
                                            logger.warning(
                                                "%s - model turn stopped on '%s': the "
                                                "output ceiling was reached before the "
                                                "turn finished (%d chars, %d tool call(s))",
                                                self.name,
                                                finish_reason,
                                                len(str(content or "")),
                                                len(getattr(msg, "tool_calls", []) or []),
                                            )

                            stop_reason = _cut_short_reason(
                                turn_budget,
                                budget_hit=turn_budget.observe(agent_step),
                                now=time.monotonic(),
                                soft_deadline=soft_deadline,
                            )
                            if stop_reason:
                                cut_short_reason = stop_reason
                                if stop_reason == "turn_limit":
                                    budget_note = (
                                        "Investigation turn limit reached after "
                                        f"{turn_budget.turns} model calls; the requested "
                                        "next tool round was not executed. Continue from "
                                        "the evidence already collected."
                                    )
                                else:
                                    budget_note = (
                                        "Investigation wall-clock budget nearly spent "
                                        f"after {turn_budget.turns} model calls; the "
                                        "requested next tool round was not started, so "
                                        "this lane reports what it has rather than being "
                                        "killed mid-call and losing it. Continue from "
                                        "the evidence already collected."
                                    )
                                agent_response = (
                                    f"{agent_response}\n\n{budget_note}"
                                    if agent_response
                                    else budget_note
                                )
                                logger.warning("%s - %s", self.name, budget_note)
                                close_stream = getattr(agent_stream, "aclose", None)
                                if close_stream is not None:
                                    try:
                                        await close_stream()
                                    except Exception as close_error:
                                        logger.debug(
                                            "%s - bounded stream close returned %s",
                                            self.name,
                                            type(close_error).__name__,
                                        )
                                break

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
                with capture_fit_reports() as fit_reports:
                    started_at = time.monotonic()
                    await asyncio.wait_for(execute_agent(), timeout=timeout_seconds)
                    # Nothing stopped this lane and it still never called a
                    # tool: the model wrote a preamble and returned. Ask once
                    # more, explicitly, while there is clock left to answer in.
                    if (
                        not cut_short_reason
                        and not tool_calls_made
                        and not runbook_answerable
                    ):
                        remaining = timeout_seconds - (time.monotonic() - started_at)
                        if remaining >= _NO_TOOL_RETRY_MIN_SECONDS:
                            logger.warning(
                                "%s - lane returned %d chars with no tool call "
                                "after %d model call(s); retrying once with an "
                                "explicit directive",
                                self.name,
                                len(str(agent_response or "")),
                                turn_budget.turns,
                            )
                            # The first attempt produced no evidence, so there
                            # is nothing in it worth carrying into the retry.
                            first_attempt_truncated = response_truncated
                            agent_response = ""
                            response_truncated = False
                            model_text_captured = False
                            # execute_agent reads soft_deadline from this
                            # scope at call time; the original one is already
                            # spent, and leaving it would cut the retry off
                            # before its first tool round.
                            soft_deadline = (
                                time.monotonic()
                                + remaining
                                - _turn_headroom_seconds(int(remaining))
                            )
                            await asyncio.wait_for(
                                execute_agent(
                                    extra_directive=(
                                        _TRUNCATED_RETRY_DIRECTIVE
                                        if first_attempt_truncated
                                        else _NO_TOOL_RETRY_DIRECTIVE
                                    )
                                ),
                                timeout=remaining,
                            )
                logger.info(f"{self.name} - Agent execution completed")

            except asyncio.TimeoutError:
                cut_short_reason = "timeout"
                logger.error(
                    f"{self.name} - Agent execution timed out after {timeout_seconds} seconds"
                )
                # Keep what was already collected. Overwriting agent_response
                # here used to discard every tool result this lane had paid
                # for and report the lane as having returned nothing.
                timeout_note = (
                    f"Investigation stopped at the {timeout_seconds}s wall-clock "
                    "limit while a model call was still in flight. This is a "
                    "budget boundary, not a tool failure: the evidence below was "
                    "collected before the cut-off and is as valid as any other. "
                    "Reason from it, and do not report this lane as having "
                    "returned no data."
                )
                agent_response = "\n\n".join(
                    part
                    for part in (
                        agent_response,
                        timeout_note,
                        _partial_evidence_digest(all_messages),
                    )
                    if part
                )

            except GraphRecursionError as recursion_error:
                # A step ceiling is a budget, and every budget in this lane
                # keeps the evidence it has already paid for. Losing it here
                # is what made the 2026-09-22 trial report "the logs agent
                # hit a recursion limit before returning anything" twice,
                # after Loki had already answered.
                cut_short_reason = "recursion_limit"
                logger.error(
                    "%s - LangGraph step backstop reached after %d model "
                    "call(s): %s",
                    self.name,
                    turn_budget.turns,
                    recursion_error,
                )
                recursion_note = (
                    "Investigation stopped at the framework's step ceiling "
                    f"after {turn_budget.turns} model calls, with a tool "
                    "round in flight. This is a budget boundary, not a tool "
                    "failure: the evidence below was collected before the "
                    "cut-off and is as valid as any other. Reason from it, "
                    "and do not report this lane as having returned no data."
                )
                agent_response = "\n\n".join(
                    part
                    for part in (
                        agent_response,
                        recursion_note,
                        _partial_evidence_digest(all_messages),
                    )
                    if part
                )

            except Exception as e:
                logger.error(f"{self.name} - Agent execution failed: {e}")
                logger.exception("Full exception details:")
                # Say what broke, then still hand over what was collected:
                # a failure on the fourth tool round does not un-answer the
                # first three.
                agent_response = "\n\n".join(
                    part
                    for part in (
                        agent_response,
                        f"Agent execution failed: {str(e)}",
                        _partial_evidence_digest(all_messages),
                    )
                    if part
                )

            # A lane that never called a tool has not given a short answer;
            # it has given an absent one. Label it so the reflector and the
            # trial record both see a missing lane, instead of reading its
            # opening sentence as though it were observed data.
            if not tool_calls_made and not cut_short_reason and not runbook_answerable:
                cut_short_reason = (
                    "output_truncated" if response_truncated else "no_tool_calls"
                )
                logger.error(
                    "%s - lane produced no tool calls in %d model call(s) (%s); "
                    "reporting it as collecting no evidence",
                    self.name,
                    turn_budget.turns,
                    (
                        "cut off at the output ceiling"
                        if response_truncated
                        else "the model returned without calling one"
                    ),
                )
                agent_response = "\n\n".join(
                    part
                    for part in (
                        agent_response,
                        (
                            _TRUNCATED_LANE_NOTE
                            if response_truncated
                            else _NO_TOOL_LANE_NOTE
                        ),
                    )
                    if part
                )

            # Whatever stopped the lane, evidence the runtime already holds is
            # not the model's to lose. A probe measurement and a completed
            # tool round are facts about the incident; the notes above are
            # facts about the lane. On 2026-09-23 two lanes wrote no text --
            # one cut off at the ceiling holding a pre-measured p90 of 2.023s
            # against a 1.0s threshold, one out of turns holding ten tool
            # results -- and the reflector was handed neither, so it reported
            # the gating measurement as never taken. Both are bounded above
            # and neither costs a model call.
            if not model_text_captured:
                salvage = _salvaged_evidence(
                    all_messages, runbook_probe_block, agent_response
                )
                if salvage:
                    agent_response = (
                        f"{agent_response}\n\n{salvage}" if agent_response else salvage
                    )
                    logger.info(
                        "%s - lane wrote no finding; salvaged %d chars of "
                        "evidence the runtime already held",
                        self.name,
                        len(salvage),
                    )

            # Salvage above fires only for a lane that wrote nothing at all.
            # A lane that wrote one sentence of preamble and then ran out of
            # turns is not silent, so it kept the sentence and dropped the
            # measurement: on 2026-09-25 the metrics lane did exactly that,
            # and the reflector -- told never to invent a locator, and handed
            # four prose reports containing none -- returned an empty evidence
            # list twice, which cost that trial both the evidence and the
            # timeline criterion. Whether the lane found words for the probe
            # is a fact about the lane; the probe is arithmetic over live
            # series either way, and it is the only exact query string and
            # observation time anyone downstream ever gets.
            probe_note = _probe_measurement_note(runbook_probe_block)
            if probe_note and _PROBE_NOTE_HEADER not in (agent_response or ""):
                # Appended last on purpose: the finding is cut to 1800 chars
                # before it enters the next lane's brief, so the lane's own
                # prose is what survives there, while the reflector -- which
                # reads the untruncated report -- gets both.
                agent_response = (
                    f"{agent_response}\n\n{probe_note}"
                    if agent_response
                    else probe_note
                )
                logger.info(
                    "%s - carried the runbook probe into the lane report",
                    self.name,
                )

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
            # The fitter used to log only changed calls, so there was no
            # durable way to measure how often it engaged or how many input
            # tokens it kept out of repeated ReAct turns. Persist aggregate
            # counters only — never prompt or tool content — alongside the
            # specialist evidence references.
            context_fitting = dict(
                artifact_metadata.get("context_fitting", {}) or {}
            )
            context_fitting[agent_key] = merge_fit_summaries(
                context_fitting.get(agent_key),
                summarize_fit_reports(fit_reports),
            )
            artifact_metadata["context_fitting"] = context_fitting
            specialist_budgets = dict(
                artifact_metadata.get("specialist_turn_budgets", {}) or {}
            )
            specialist_budgets[agent_key] = {
                "turns": turn_budget.turns,
                "limit": turn_budget.limit,
                "exhausted": turn_budget.exhausted,
                "cut_short": cut_short_reason,
            }
            artifact_metadata["specialist_turn_budgets"] = specialist_budgets
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
                # A lane stopped at a boundary has already appended the note
                # that explains itself; paying for a narration call to restate
                # a truncated report is the one model call here with no reader.
                if not cut_short_reason:
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
                finding_payload["specialist_turn_budget"] = {
                    "turns": turn_budget.turns,
                    "limit": turn_budget.limit,
                    "exhausted": turn_budget.exhausted,
                    "cut_short": cut_short_reason,
                }
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


def create_single_agent(
    tools: List[BaseTool], agent_metadata: AgentMetadata = None, **kwargs
) -> BaseAgentNode:
    """Create the single-investigator baseline used by the ablation harness.

    This is the same `BaseAgentNode` every specialist is — same react loop,
    same context hook, same tool-error handling, same model routing — holding
    the union of their tools instead of one domain's. That sameness is the
    point: the `single_agent` arm must differ from the full architecture in
    supervisor routing and specialist isolation and in nothing else, or the
    measured difference is not attributable to multi-agent design.

    Never constructed in production; see `src/sre_agent/ablation.py`.
    """
    config = _load_agent_config()
    filtered_tools = _filter_tools_for_agent(tools, "single_agent", config)

    return BaseAgentNode(
        name="Single Investigator Agent",  # Fallback for backward compatibility
        description="One ReAct loop holding every specialist's read-only tools",  # Fallback
        tools=filtered_tools,
        agent_metadata=agent_metadata,
        **kwargs,
    )
