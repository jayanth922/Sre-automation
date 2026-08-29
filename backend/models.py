import uuid
from datetime import datetime
from typing import List, Optional
from enum import Enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from backend.crypto import EncryptedString, current_key_version

# ----------------------------------------------------------------------
# Enum Definitions
# ----------------------------------------------------------------------

class UserRole(str, Enum):
    ADMIN = "admin"
    MEMBER = "member"

class ClusterStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"

class IncidentSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class IncidentStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    INVESTIGATED = "investigated"
    AWAITING_APPROVAL = "awaiting_approval"
    REMEDIATION_IN_PROGRESS = "remediation_in_progress"
    REMEDIATION_FAILED = "remediation_failed"
    VERIFICATION_UNKNOWN = "verification_unknown"
    RESOLVED = "resolved"

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"

class JobType(str, Enum):
    TOOL_CALL = "tool_call"
    INVESTIGATION = "investigation"
    REMEDIATION = "remediation"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"

# ----------------------------------------------------------------------
# Base Model
# ----------------------------------------------------------------------

class Base(DeclarativeBase):
    pass

# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------

class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    api_key: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    users: Mapped[List["User"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    clusters: Mapped[List["Cluster"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    invitations: Mapped[List["OrgInvitation"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    audit_events: Mapped[List["AuditEvent"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Organization(name='{self.name}')>"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String)
    role: Mapped[UserRole] = mapped_column(String, default=UserRole.MEMBER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="users")
    audit_logs: Mapped[List["AuditLog"]] = relationship(back_populates="user")
    sent_invitations: Mapped[List["OrgInvitation"]] = relationship(
        back_populates="invited_by"
    )

    def __repr__(self):
        return f"<User(email='{self.email}', role='{self.role}')>"


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    token: Mapped[str] = mapped_column(EncryptedString(), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    key_version: Mapped[int] = mapped_column(
        Integer, default=current_key_version, nullable=False
    )
    execution_context_version: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    status: Mapped[ClusterStatus] = mapped_column(String, default=ClusterStatus.ONLINE)
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Customer infrastructure connectivity
    prometheus_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    loki_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    k8s_api_server: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    k8s_token: Mapped[Optional[str]] = mapped_column(EncryptedString(), nullable=True)
    github_token: Mapped[Optional[str]] = mapped_column(EncryptedString(), nullable=True)
    github_repo: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # e.g. org/repo
    notion_api_key: Mapped[Optional[str]] = mapped_column(EncryptedString(), nullable=True)
    notion_database_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Observability query conventions (JSON). Null = platform defaults. Lets the
    # platform work against any workload's metric schema, not one demo's.
    metrics_config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Scope: the Kubernetes namespace this cluster represents. Null/empty = the
    # whole cluster (infra-level). When set, metric queries, the service view, and
    # the executor's blast radius are all scoped to it — so one physical cluster
    # can host many apps, each registered as its own scoped Sentinel cluster.
    namespace: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Per-cluster LLM override for the agent's "brain". Null = platform default.
    # Lets each cluster tune the model to its use case (provider/model/endpoint/key).
    llm_provider: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    llm_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    llm_base_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    llm_api_key: Mapped[Optional[str]] = mapped_column(EncryptedString(), nullable=True)

    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="clusters")
    incidents: Mapped[List["Incident"]] = relationship(back_populates="cluster", cascade="all, delete-orphan")
    jobs: Mapped[List["Job"]] = relationship(back_populates="cluster", cascade="all, delete-orphan")
    audit_events: Mapped[List["AuditEvent"]] = relationship(back_populates="cluster", cascade="all, delete-orphan")
    slos: Mapped[List["SLO"]] = relationship(back_populates="cluster", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Cluster(name='{self.name}', status='{self.status}')>"


class Job(Base):
    """Durable investigation/tool job with lease-based ownership."""
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint(
            "cluster_id",
            "idempotency_key",
            name="uq_jobs_cluster_idempotency_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("organizations.id"), nullable=True, index=True
    )
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("incidents.id"), nullable=True, index=True
    )
    job_type: Mapped[JobType] = mapped_column(String, default=JobType.INVESTIGATION)
    status: Mapped[JobStatus] = mapped_column(String, default=JobStatus.PENDING, index=True)
    payload: Mapped[Optional[str]] = mapped_column(Text)  # JSON payload for the job
    result: Mapped[Optional[str]] = mapped_column(Text)   # JSON result from agent
    logs: Mapped[Optional[str]] = mapped_column(Text)     # Accumulated log lines
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Relationships
    cluster: Mapped["Cluster"] = relationship(back_populates="jobs")
    run_manifest: Mapped[Optional["RunManifest"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
        lazy="selectin",
    )

    def __repr__(self):
        return f"<Job(id='{self.id}', status='{self.status}')>"


class RunManifest(Base):
    """Tamper-evident, write-once provenance for an incident job."""

    __tablename__ = "run_manifests"
    __table_args__ = (
        Index("ix_run_manifests_incident_created", "incident_id", "created_at"),
        Index(
            "ix_run_manifests_tenant_created",
            "organization_id",
            "cluster_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    comparable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    non_comparable_reasons: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    root_trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    job: Mapped["Job"] = relationship(back_populates="run_manifest")


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    severity: Mapped[IncidentSeverity] = mapped_column(String, default=IncidentSeverity.MEDIUM)
    status: Mapped[IncidentStatus] = mapped_column(String, default=IncidentStatus.OPEN)
    summary: Mapped[Optional[str]] = mapped_column(Text)  # AI-generated summary
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Relationships
    cluster: Mapped["Cluster"] = relationship(back_populates="incidents")
    timeline_events: Mapped[List["IncidentTimelineEvent"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="IncidentTimelineEvent.sequence",
    )

    def __repr__(self):
        return f"<Incident(title='{self.title}', severity='{self.severity}')>"


class IncidentTimelineEvent(Base):
    __tablename__ = "incident_timeline_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    speaker_role: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[Optional[str]] = mapped_column(Text)
    pending_supervisor: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    handled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    incident: Mapped["Incident"] = relationship(back_populates="timeline_events")

    def __repr__(self):
        return (
            f"<IncidentTimelineEvent(incident='{self.incident_id}', sequence='{self.sequence}', "
            f"event_type='{self.event_type}', speaker_role='{self.speaker_role}')>"
        )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)  # e.g., "created_cluster", "updated_incident"
    target_resource: Mapped[str] = mapped_column(String)  # e.g., "cluster", "incident"
    target_id: Mapped[str] = mapped_column(String)  # UUID as string
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user: Mapped["User"] = relationship(back_populates="audit_logs")

    def __repr__(self):
        return f"<AuditLog(action='{self.action}', user='{self.user_id}')>"


class AuditEvent(Base):
    """
    Enterprise Audit Trail for compliance (SOC2).
    Tracks all remediation actions taken by Agent or User.
    """
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "cluster_id IS NOT NULL OR organization_id IS NOT NULL",
            name="ck_audit_events_has_scope",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cluster_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("clusters.id"), nullable=True
    )
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("organizations.id"), nullable=True, index=True
    )
    
    actor_type: Mapped[str] = mapped_column(String, default="AGENT") # AGENT or USER
    actor_id: Mapped[str] = mapped_column(String) # "sre-agent" or user_uuid
    
    action_type: Mapped[str] = mapped_column(String) # RESTART, SCALE, etc.
    resource_target: Mapped[str] = mapped_column(String) # e.g., "deployment/payment-service"
    outcome: Mapped[str] = mapped_column(String) # SUCCESS, FAILED
    details: Mapped[Optional[str]] = mapped_column(Text) # JSON details or error message
    
    # Relationships
    cluster: Mapped[Optional["Cluster"]] = relationship(back_populates="audit_events")
    organization: Mapped[Optional["Organization"]] = relationship(
        back_populates="audit_events"
    )

    def __repr__(self):
        return f"<AuditEvent(action='{self.action_type}', outcome='{self.outcome}')>"


class AgentAuditLog(Base):
    """Flight recorder: immutable MCP/tool execution log for investigations.

    Lives on the canonical Alembic Base so fresh and upgraded databases create
    the table. Tenant/cluster/incident/run fields make every critical action
    queryable for mission-control and compliance export.
    """

    __tablename__ = "agent_audit_logs"
    __table_args__ = (
        Index("ix_agent_audit_logs_org_timestamp", "organization_id", "timestamp"),
        Index("ix_agent_audit_logs_cluster_timestamp", "cluster_id", "timestamp"),
        Index("ix_agent_audit_logs_incident_timestamp", "incident_id", "timestamp"),
        Index("ix_agent_audit_logs_run_timestamp", "run_id", "timestamp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("organizations.id"), nullable=True, index=True
    )
    cluster_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("clusters.id"), nullable=True, index=True
    )
    incident_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    run_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)

    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    tool_args: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String)  # PENDING, SUCCESS, FAILURE
    result: Mapped[Optional[str]] = mapped_column(Text)
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    def __repr__(self):
        return (
            f"<AgentAuditLog(agent='{self.agent_name}', tool='{self.tool_name}', "
            f"status='{self.status}')>"
        )


class SLO(Base):
    """Service Level Objective definition and tracking."""
    __tablename__ = "slos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)  # e.g., "API Availability"
    sli_metric: Mapped[str] = mapped_column(String(200), nullable=False)  # e.g., "http_requests_total"
    target: Mapped[float] = mapped_column(nullable=False)  # e.g., 99.9 (percentage)
    window_days: Mapped[int] = mapped_column(default=30)  # rolling window
    current_value: Mapped[Optional[float]] = mapped_column(nullable=True)  # latest measured SLI
    error_budget_remaining: Mapped[Optional[float]] = mapped_column(nullable=True)  # percentage remaining
    last_calculated: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    cluster: Mapped["Cluster"] = relationship(back_populates="slos")


class RefreshSession(Base):
    """A rotating refresh-token session. The raw token is never stored — only its
    SHA-256 hash. `family_id` groups a rotation lineage so reuse of a rotated
    token can revoke the whole family (reuse detection)."""
    __tablename__ = "refresh_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<SLO(name='{self.name}', target={self.target}%)>"


class OrgInvitation(Base):
    """Single-use invitation to join an existing organization.

    Only a SHA-256 hash of the opaque invitation token is persisted. The raw
    token is returned once when an administrator creates the invitation.
    """

    __tablename__ = "org_invitations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    role: Mapped[UserRole] = mapped_column(
        String(20), default=UserRole.MEMBER, nullable=False
    )
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    organization: Mapped["Organization"] = relationship(back_populates="invitations")
    invited_by: Mapped["User"] = relationship(back_populates="sent_invitations")


class ApprovalRequest(Base):
    """Durable, single-use authorization for one exact remediation report."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        Index("ix_approval_requests_incident_status", "incident_id", "status"),
        Index("ix_approval_requests_thread_id", "thread_id"),
        Index(
            "uq_approval_requests_pending_action",
            "thread_id",
            "action_hash",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[ApprovalStatus] = mapped_column(
        String(20), default=ApprovalStatus.PENDING, nullable=False
    )
    approver_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
