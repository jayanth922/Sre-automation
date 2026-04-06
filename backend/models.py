import uuid
from datetime import datetime
from typing import List, Optional
from enum import Enum

from sqlalchemy import String, ForeignKey, DateTime, Text, Boolean, Float, Integer, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

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
    RESOLVED = "resolved"

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class JobType(str, Enum):
    TOOL_CALL = "tool_call"
    INVESTIGATION = "investigation"
    REMEDIATION = "remediation"

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

    def __repr__(self):
        return f"<User(email='{self.email}', role='{self.role}')>"


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    token: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    status: Mapped[ClusterStatus] = mapped_column(String, default=ClusterStatus.ONLINE)
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Customer infrastructure connectivity
    prometheus_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    loki_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    k8s_api_server: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    k8s_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # service account token
    github_token: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    github_repo: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # e.g. org/repo
    notion_api_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    notion_database_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="clusters")
    incidents: Mapped[List["Incident"]] = relationship(back_populates="cluster", cascade="all, delete-orphan")
    jobs: Mapped[List["Job"]] = relationship(back_populates="cluster", cascade="all, delete-orphan")
    audit_events: Mapped[List["AuditEvent"]] = relationship(back_populates="cluster", cascade="all, delete-orphan")
    slos: Mapped[List["SLO"]] = relationship(back_populates="cluster", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Cluster(name='{self.name}', status='{self.status}')>"


class Job(Base):
    """Represents a job queued for execution by an Edge Agent."""
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    job_type: Mapped[JobType] = mapped_column(String, default=JobType.INVESTIGATION)
    status: Mapped[JobStatus] = mapped_column(String, default=JobStatus.PENDING)
    payload: Mapped[Optional[str]] = mapped_column(Text)  # JSON payload for the job
    result: Mapped[Optional[str]] = mapped_column(Text)   # JSON result from agent
    logs: Mapped[Optional[str]] = mapped_column(Text)     # Accumulated log lines
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Relationships
    cluster: Mapped["Cluster"] = relationship(back_populates="jobs")

    def __repr__(self):
        return f"<Job(id='{self.id}', status='{self.status}')>"


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

    def __repr__(self):
        return f"<Incident(title='{self.title}', severity='{self.severity}')>"


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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    
    actor_type: Mapped[str] = mapped_column(String, default="AGENT") # AGENT or USER
    actor_id: Mapped[str] = mapped_column(String) # "sre-agent" or user_uuid
    
    action_type: Mapped[str] = mapped_column(String) # RESTART, SCALE, etc.
    resource_target: Mapped[str] = mapped_column(String) # e.g., "deployment/payment-service"
    outcome: Mapped[str] = mapped_column(String) # SUCCESS, FAILED
    details: Mapped[Optional[str]] = mapped_column(Text) # JSON details or error message
    
    # Relationships
    cluster: Mapped["Cluster"] = relationship(back_populates="audit_events")

    def __repr__(self):
        return f"<AuditEvent(action='{self.action_type}', outcome='{self.outcome}')>"


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

    def __repr__(self):
        return f"<SLO(name='{self.name}', target={self.target}%)>"
