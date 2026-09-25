import uuid
from typing import Any, Dict, Literal, Optional, List
from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from backend.models import UserRole, ClusterStatus, IncidentSeverity, IncidentStatus

# ----------------------------------------------------------------------
# Auth Schemas
# ----------------------------------------------------------------------

class Token(BaseModel):
    access_token: str
    token_type: str

class SetupStatus(BaseModel):
    """What the login page needs before anyone can hold credentials."""

    needs_setup: bool
    open_registration: bool

class TokenData(BaseModel):
    user_id: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None

# ----------------------------------------------------------------------
# User Schemas
# ----------------------------------------------------------------------

class UserBase(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None

class UserCreate(UserBase):
    password: str
    org_name: str  # Create a new organization with the user

class UserResponse(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: UserRole
    org_id: uuid.UUID
    is_active: bool

class UserProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: Optional[str] = None
    display_name: str
    role: UserRole
    org_id: uuid.UUID
    organization_name: str
    is_active: bool
    created_at: datetime

class PasswordResetRequest(BaseModel):
    current_password: str
    new_password: str


# ----------------------------------------------------------------------
# Organization member management
# ----------------------------------------------------------------------

class OrgMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: Optional[str] = None
    role: UserRole
    is_active: bool
    created_at: datetime

class MemberRoleUpdate(BaseModel):
    role: UserRole


class MemberStatusUpdate(BaseModel):
    is_active: bool


class SlackBotTokenSet(BaseModel):
    # Manual bot-token path for self-hosted, single-org deployments that
    # register their own Slack app directly (Install to Workspace) instead
    # of going through the multi-tenant OAuth "Add to Slack" flow.
    bot_token: str


class LangfuseConfigSet(BaseModel):
    # Per-org Langfuse project (src/sre_agent/tracing.py). host defaults to
    # Langfuse Cloud when omitted — self-hosted Langfuse was removed
    # platform-wide, see docs/ai/PROJECT_STATE.md.
    public_key: str
    secret_key: str
    host: Optional[str] = None


# ----------------------------------------------------------------------
# Organization invitations
# ----------------------------------------------------------------------

class InvitationCreate(BaseModel):
    email: EmailStr
    # No defaults: an admin sending an invite states the role and expiry
    # explicitly rather than silently getting member/72h.
    role: UserRole
    expires_in_hours: int = Field(ge=1, le=720)


class InvitationCreateResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    email: EmailStr
    role: UserRole
    expires_at: datetime
    token: str


class InvitationAccept(BaseModel):
    token: str = Field(min_length=32)
    password: str = Field(min_length=8)
    full_name: Optional[str] = None
    # Accepted for compatibility, but deliberately ignored. Organization and
    # role always come from the server-side invitation record.
    role: Optional[UserRole] = None

# ----------------------------------------------------------------------
# Organization Schemas
# ----------------------------------------------------------------------

class OrgCreate(BaseModel):
    name: str

class OrgResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime
    slack_team_id: Optional[str] = None
    # Public key only — never expose langfuse_secret_key here.
    langfuse_public_key: Optional[str] = None
    langfuse_host: Optional[str] = None

# ----------------------------------------------------------------------
# Cluster Schemas
# ----------------------------------------------------------------------

# The canonical environment labels. Anything else normalises to
# "production" downstream (execution_context._normalized_environment), so
# constraining the input here is what turns a typo into a 422 the admin can
# see instead of a cluster quietly relabelled as production.
ClusterEnvironment = Literal["production", "staging", "development", "testing"]


class ClusterCreate(BaseModel):
    name: str
    # Customer infrastructure endpoints (platform calls these directly)
    prometheus_url: Optional[str] = None
    loki_url: Optional[str] = None
    k8s_api_server: Optional[str] = None
    k8s_token: Optional[str] = None
    github_token: Optional[str] = None
    github_repo: Optional[str] = None
    notion_api_key: Optional[str] = None
    notion_database_id: Optional[str] = None
    jira_url: Optional[str] = None
    jira_email: Optional[str] = None
    jira_api_token: Optional[str] = None
    jira_project_key: Optional[str] = None
    # Observability query conventions (service label, metric names, error selector).
    metrics_config: Optional[Dict[str, str]] = None
    # Scope: namespace this cluster represents. Empty = whole cluster (infra).
    namespace: Optional[str] = None
    # Per-cluster LLM override (null = platform default).
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_router_enabled: Optional[bool] = None
    # Operational policy. Null leaves the cluster on the deployment default.
    environment: Optional[ClusterEnvironment] = None
    approval_ttl_minutes: Optional[int] = Field(default=None, ge=1, le=10080)

class ClusterUpdate(BaseModel):
    name: Optional[str] = None
    prometheus_url: Optional[str] = None
    loki_url: Optional[str] = None
    k8s_api_server: Optional[str] = None
    k8s_token: Optional[str] = None
    github_token: Optional[str] = None
    github_repo: Optional[str] = None
    notion_api_key: Optional[str] = None
    notion_database_id: Optional[str] = None
    jira_url: Optional[str] = None
    jira_email: Optional[str] = None
    jira_api_token: Optional[str] = None
    jira_project_key: Optional[str] = None
    metrics_config: Optional[Dict[str, str]] = None
    namespace: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_router_enabled: Optional[bool] = None
    environment: Optional[ClusterEnvironment] = None
    approval_ttl_minutes: Optional[int] = Field(default=None, ge=1, le=10080)

class ClusterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: ClusterStatus
    last_heartbeat: Optional[datetime]
    heartbeat_source: Optional[str] = None
    heartbeat_reason: Optional[str] = None
    created_at: datetime
    prometheus_url: Optional[str] = None
    loki_url: Optional[str] = None
    k8s_api_server: Optional[str] = None
    github_repo: Optional[str] = None
    notion_database_id: Optional[str] = None
    jira_url: Optional[str] = None
    jira_email: Optional[str] = None
    jira_project_key: Optional[str] = None
    metrics_config: Optional[str] = None
    namespace: Optional[str] = None
    # LLM override — provider/model/base_url are safe to echo; the key is write-only.
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_router_enabled: bool = False
    # Null here means "inheriting the deployment default", which the settings
    # page shows as such rather than inventing a value the row does not hold.
    environment: Optional[str] = None
    approval_ttl_minutes: Optional[int] = None

class LlmModelsRequest(BaseModel):
    # Optional: lets the dashboard list models for a key the admin just typed
    # but hasn't saved yet. Falls back to the cluster's saved llm_api_key.
    api_key: Optional[str] = None

# ----------------------------------------------------------------------
# Incident Schemas
# ----------------------------------------------------------------------

class IncidentCreate(BaseModel):
    title: str
    description: Optional[str] = None
    # No default: a caller must state the severity it actually observed
    # rather than have the platform silently assume MEDIUM.
    severity: IncidentSeverity

class IncidentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cluster_id: uuid.UUID
    title: str
    description: Optional[str] = None
    severity: IncidentSeverity
    status: IncidentStatus
    summary: Optional[str] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None
    jira_issue_key: Optional[str] = None

class IncidentTimelineEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    incident_id: uuid.UUID
    sequence: int
    event_type: str
    speaker_role: str
    title: Optional[str] = None
    content: str
    payload: Optional[Dict[str, Any]] = None
    pending_supervisor: bool = False
    handled_at: Optional[datetime] = None
    created_at: datetime

class IncidentTranscriptResponse(BaseModel):
    incident: IncidentResponse
    conversation_mode: Literal["investigation", "assistant"]
    summary: Optional[str] = None
    events: List[IncidentTimelineEventResponse]


class ApprovalDecisionRequest(BaseModel):
    approval_request_id: uuid.UUID
    action_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class GateApprovalDecisionRequest(BaseModel):
    approved: bool


class GateApprovalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    incident_id: uuid.UUID
    workflow_id: str
    gate: str
    status: str
    approver_user_id: Optional[uuid.UUID] = None
    decided_at: Optional[datetime] = None
    expires_at: datetime
    created_at: datetime

# ----------------------------------------------------------------------
# SLO Schemas
# ----------------------------------------------------------------------

class SLOCreate(BaseModel):
    name: str
    sli_metric: str
    target: float  # e.g., 99.9
    # No default: an SLO's window is a real commitment, not something to
    # silently pick to 30 days on the caller's behalf.
    window_days: int

class SLOUpdate(BaseModel):
    name: Optional[str] = None
    sli_metric: Optional[str] = None
    target: Optional[float] = None
    window_days: Optional[int] = None

class SLOResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cluster_id: uuid.UUID
    name: str
    sli_metric: str
    target: float
    window_days: int
    current_value: Optional[float] = None
    error_budget_remaining: Optional[float] = None
    last_calculated: Optional[datetime] = None

class SLOStatusResponse(BaseModel):
    """Enriched SLO status with burn rate."""
    slo: SLOResponse
    budget_consumed_percent: float
    burn_rate_1h: Optional[float] = None
    burn_rate_6h: Optional[float] = None
    is_breaching: bool

# ----------------------------------------------------------------------
# Job Schemas
# ----------------------------------------------------------------------

from backend.models import JobStatus, JobType

class RunManifestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    incident_id: uuid.UUID
    cluster_id: uuid.UUID
    organization_id: uuid.UUID
    schema_version: int
    manifest: Dict[str, Any]
    manifest_sha256: str
    comparable: bool
    non_comparable_reasons: List[str]
    root_trace_id: str
    created_at: datetime

class RunManifestComparisonResponse(BaseModel):
    left_job_id: str
    right_job_id: str
    comparable: bool
    non_comparable_reasons: List[str]
    configuration_equal: bool
    configuration_differences: List[Dict[str, Any]]
    input_differences: List[Dict[str, Any]]


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cluster_id: uuid.UUID
    job_type: JobType
    status: JobStatus
    payload: Optional[str]
    result: Optional[str]
    logs: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    run_manifest: Optional[RunManifestResponse] = None
    organization_id: Optional[uuid.UUID] = None
    incident_id: Optional[uuid.UUID] = None
    idempotency_key: Optional[str] = None
    attempt_count: Optional[int] = None
    max_attempts: Optional[int] = None
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    cancel_requested_at: Optional[datetime] = None
    last_error: Optional[str] = None
