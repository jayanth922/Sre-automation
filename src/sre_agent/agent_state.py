#!/usr/bin/env python3

import json
import logging
from typing import Annotated, Any, Dict, List, Literal, Optional, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, field_validator

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,  # Set the log level to INFO
    # Define log message format
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

logger = logging.getLogger(__name__)


def _decode_json_container(value: Any) -> Any:
    """Accept a JSON-encoded list/dict where a real one is expected.

    Function-calling models routinely serialize a nested container into the
    tool-call argument as a *string* — `"actions": "[{...}]"` instead of
    `"actions": [{...}]` — and Pydantic rejects that outright with
    `Input should be a valid list`. The model's answer was correct; only its
    encoding of it was not.

    That failure is not cosmetic here. `_planner_node` catches the
    ValidationError and substitutes a fallback plan of one `escalate
    manual_review` action, so a string-encoded `actions` field turns a real
    remediation plan into a page. Live on 2026-09-14 this happened on every
    planner invocation without exception — four for four — which is why
    `patch_resource_limits` had never once been proposed: the planner *was*
    proposing inspect/config_change steps and every one of them was thrown
    away before anything downstream could see it.

    Anything that is not a string, or is a string that does not parse, is
    handed back untouched so the field's own validation still produces the
    real error.

    Two things learned on 2026-09-19, when the planner failed this way again
    (incident `bc5c48b7`) despite `actions` being wired to this validator:

    * A strict parse is too strict for this input. Models routinely leave
      literal newlines and tabs inside the JSON *string* values they emit,
      which `json.loads` rejects as `Invalid control character` even though
      the structure is sound. `strict=False` accepts exactly that and nothing
      structurally looser, so a plan is no longer thrown away over whitespace.
    * A silent `except` made the cause unknowable. Pydantic's error truncates
      the middle of the value, so the log showed a string that looked like
      well-formed JSON at both ends and there was no record of *why* the parse
      failed. The failure is now logged with the decoder's own reason and a
      bounded head/tail, so the next occurrence is diagnosable from the log
      instead of requiring a reproduction.
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        # Only worth a second attempt — and a warning — if this was meant to
        # be a container at all. A plain string field that happens to reach
        # here is not a failed decode.
        if value.lstrip()[:1] not in ("[", "{"):
            return value
        try:
            return json.loads(value, strict=False)
        except (ValueError, TypeError) as error:
            logger.warning(
                "Undecodable JSON container (%d chars): %s | head=%r tail=%r",
                len(value),
                error,
                value[:120],
                value[-120:],
            )
            return value


# Pydantic Models for Structured State
class AlertContext(BaseModel):
    """Alert context from Prometheus Alertmanager webhook."""

    alert_name: str = Field(..., description="Name of the alert")
    severity: Literal["warning", "critical", "info"] = Field(
        ..., description="Alert severity level"
    )
    labels: Dict[str, str] = Field(
        default_factory=dict, description="Alert labels (pod, service, namespace, etc.)"
    )
    annotations: Dict[str, str] = Field(
        default_factory=dict, description="Alert annotations (summary, description)"
    )
    starts_at: Optional[str] = Field(None, description="Alert start timestamp")
    generator_url: Optional[str] = Field(None, description="URL to alert generator")


class InvestigationFindings(BaseModel):
    """Findings from parallel investigation agents."""

    infra_findings: Optional[Dict[str, Any]] = Field(
        None, description="Findings from infrastructure agent (K8s/Metrics)"
    )
    code_findings: Optional[Dict[str, Any]] = Field(
        None, description="Findings from code agent (Git commits, changes)"
    )
    logs_findings: Optional[Dict[str, Any]] = Field(
        None, description="Findings from logs agent"
    )
    correlation_timestamp: Optional[str] = Field(
        None, description="Timestamp when findings were correlated"
    )


class CausalLink(BaseModel):
    """One explicit cause→effect link used by structured evaluation."""

    cause: str = Field(..., description="Evidence-supported causal condition")
    effect: str = Field(..., description="Observed consequence of the cause")


class EvidenceReference(BaseModel):
    """A source locator supporting one diagnosis claim."""

    source: str = Field(
        ...,
        description="Evidence system, such as prometheus, loki, github, or kubernetes",
    )
    reference: str = Field(
        ...,
        description="Exact query, resource, log selector, commit, or trace reference",
    )
    claim: str = Field(..., description="Claim directly supported by this evidence")
    observed_at: Optional[str] = Field(
        None, description="Timezone-aware source observation timestamp when available"
    )


class ReflectorAnalysis(BaseModel):
    """Analysis from ReflectorNode identifying discrepancies and hypotheses."""

    discrepancies: List[str] = Field(
        default_factory=list,
        description="List of discrepancies found between agent findings",
    )
    hypothesis: str = Field(
        ..., description="Primary hypothesis explaining the incident"
    )
    affected_service: Optional[str] = Field(
        None, description="Exact service identifier implicated by the evidence"
    )
    fault_mode: Optional[str] = Field(
        None, description="Concise snake_case failure mode implicated by the evidence"
    )
    causal_chain: List[CausalLink] = Field(
        default_factory=list,
        description="Ordered evidence-supported links from cause to customer impact",
    )
    evidence: List[EvidenceReference] = Field(
        default_factory=list,
        description="Source references that support the diagnosis",
    )
    unknowns: List[str] = Field(
        default_factory=list,
        description="Material unresolved questions or missing evidence",
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence level in the hypothesis (0.0-1.0)"
    )
    requires_deeper_investigation: bool = Field(
        False, description="Whether deeper investigation is needed"
    )
    recommended_agents: List[str] = Field(
        default_factory=list,
        description="Agents recommended for deeper investigation",
    )
    reasoning: str = Field(..., description="Reasoning behind the analysis")

    _decode_containers = field_validator(
        "discrepancies",
        "causal_chain",
        "evidence",
        "unknowns",
        "recommended_agents",
        mode="before",
    )(_decode_json_container)


class RemediationAction(BaseModel):
    """Single remediation action in a plan."""

    action_type: Literal[
        "restart", "scale", "rollback", "config_change", "patch", "escalate",
        "revert_commit", "code_fix", "recreate_pod", "inspect",
    ] = Field(..., description="Type of remediation action")
    target: str = Field(..., description="Target resource (pod, deployment, service)")
    parameters: Dict[str, Any] = Field(
        default_factory=dict, description="Action-specific parameters"
    )
    safety_check: str = Field(
        ..., description="Safety check description for this action"
    )
    rollback_plan: Optional[str] = Field(
        None, description="How to rollback this action if it fails"
    )

    # `parameters` is what decides whether an action can run at all:
    # `live_tool_for_action` reads memory/cpu/env out of it, and a str here
    # resolves to no tool, which the executor then reports as a capability
    # gap — blaming the platform for a quoting artifact.
    _decode_containers = field_validator("parameters", mode="before")(
        _decode_json_container
    )


class RemediationPlan(BaseModel):
    """Structured remediation plan generated by PlannerNode."""

    plan_id: str = Field(..., description="Unique plan identifier")
    hypothesis: str = Field(..., description="Root cause hypothesis")
    actions: List[RemediationAction] = Field(
        ..., description="Ordered list of remediation actions"
    )
    estimated_duration: str = Field(
        ..., description="Estimated time to complete (e.g., '5 minutes')"
    )
    risk_level: Literal["low", "medium", "high"] = Field(
        ..., description="Overall risk level of the plan"
    )
    confidence: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description=(
            "Self-reported probability that the proposed remediation is correct "
            "and safe; calibration is required before it can affect autonomy"
        ),
    )
    requires_approval: bool = Field(
        True, description="Whether this plan requires human approval"
    )
    runbook_reference: Optional[str] = Field(
        None, description="Reference to runbook if plan follows one"
    )
    source_runbook_url: Optional[str] = Field(
        None, description="Reference to the runbook used"
    )
    verification_metrics: List[str] = Field(
        default_factory=list,
        description="Metrics to check for verification (Golden Signals)",
    )
    slo_impact: Optional[str] = Field(
        None, description="Expected impact on SLOs"
    )
    planning_failed: Optional[str] = Field(
        None,
        description=(
            "Set only by the planner's own except branch, to the error that "
            "killed it. A plan carrying this was not reasoned about — it is a "
            "placeholder standing in for one, and nothing may present it as a "
            "recommendation"
        ),
    )

    _decode_containers = field_validator(
        "actions", "verification_metrics", mode="before"
    )(_decode_json_container)


class AgentState(TypedDict):
    """State shared across all agents in the multi-agent system.

    This state implements the OODA Loop (Observe-Orient-Decide-Act) pattern
    for event-driven, closed-loop autonomic remediation.
    """

    # Conversation messages using LangGraph's message annotation
    messages: Annotated[List[BaseMessage], add_messages]

    # OODA Loop State Tracking
    ooda_phase: Literal[
        "OBSERVE", "ORIENT", "DECIDE", "COMPLETE"
    ]  # Current OODA phase

    # Alert Context (from webhook ingestion)
    alert_context: Optional[AlertContext]  # Prometheus alert context

    # Investigation Phase (OBSERVE)
    investigation_findings: Optional[InvestigationFindings]  # Parallel agent findings

    # Reflection Phase (ORIENT)
    reflector_analysis: Optional[ReflectorAnalysis]  # ReflectorNode analysis

    # Planning Phase (DECIDE)
    remediation_plan: Optional[RemediationPlan]  # Generated remediation plan

    # Execution Phase (ACT)
    execution_status: Optional[Literal["PENDING", "APPROVED", "EXECUTING", "COMPLETED", "FAILED"]]
    approval_status: Optional[Literal["PENDING", "APPROVED", "REJECTED"]]

    # Legacy fields (maintained for backward compatibility)
    next: Literal[
        "supervisor",
        "investigation_swarm",
        "reflector",
        "planner",
        "metrics_agent",
        "logs_agent",
        "github_agent",
        "runbooks_agent",
        "aggregate",
        "FINISH",
    ]  # Next node in graph

    # Intermediate results from each agent
    agent_results: Dict[str, Any]

    # Real tool-call failures per agent, e.g. {"metrics_agent": [{"tool": "get_metric_range",
    # "error": "..."}]}. Derived from ToolMessage.status == "error" during execution — NOT
    # from scanning free-text findings, since a specialist's narrative legitimately quotes
    # the *investigated* service's own 5xx/connection-error vocabulary (that's often the
    # incident itself). Consumed by narrative.py to flag genuine tooling bugs without
    # misattributing the monitored system's failures to the monitoring tools.
    agent_tool_failures: Dict[str, List[Dict[str, str]]]

    # Current query being processed
    current_query: Optional[str]

    # Phase 2 investigation routing state
    investigation_plan: Optional[Dict[str, Any]]
    specialist_queue: Optional[List[str]]
    current_specialist: Optional[str]
    investigation_complete: Optional[bool]

    # Phase 3 human checkpoint state
    pending_human_messages: Optional[List[Dict[str, Any]]]
    human_interrupt_pending: Optional[bool]
    last_processed_human_event_id: Optional[str]

    # Metadata about the conversation
    metadata: Dict[str, Any]

    # Flag to indicate if we need multiple agents
    requires_collaboration: bool

    # List of agents that have already responded
    agents_invoked: List[str]

    # Final aggregated response (set by supervisor)
    final_response: Optional[str]

    # Auto-approve plans without user confirmation (defaults to False)
    auto_approve_plan: Optional[bool]

    # Memory-related fields
    user_id: Optional[str]  # For user preference tracking
    incident_id: Optional[str]  # For investigation tracking
    actor_id: Optional[str]  # Actor ID for memory storage and retrieval
    session_id: Optional[str]  # Session ID for conversation grouping
    memory_context: Optional[Dict[str, Any]]  # Retrieved memory context
    captured_preferences: Optional[
        List[Dict[str, Any]]
    ]  # Preferences captured during session
    captured_knowledge: Optional[
        List[Dict[str, Any]]
    ]  # Infrastructure knowledge captured

    # Thought traces for observability
    thought_traces: Dict[str, List[str]]  # Agent name -> list of thought processes

    # Loop prevention
    investigation_count: Optional[int]  # Number of investigation attempts
