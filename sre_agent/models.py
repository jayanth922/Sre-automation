import uuid
from datetime import datetime
from typing import List, Optional
from enum import Enum

from sqlalchemy import String, ForeignKey, DateTime, Text, Boolean, func
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
    DEGRADED = "degraded"
    STALE = "stale"
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
    token: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)  # Agent authentication token
    status: Mapped[ClusterStatus] = mapped_column(String, default=ClusterStatus.OFFLINE)
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="clusters")
    incidents: Mapped[List["Incident"]] = relationship(back_populates="cluster", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Cluster(name='{self.name}', status='{self.status}')>"


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


class AgentAuditLog(Base):
    """
    Flight Recorder: Immutable log of all MCP tool executions by Agents.
    """
    __tablename__ = "agent_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    
    # Context
    incident_id: Mapped[str] = mapped_column(String, index=True, nullable=True) # ID or "general"
    agent_name: Mapped[str] = mapped_column(String, nullable=False) # e.g. "KubernetesAgent"
    
    # Action
    tool_name: Mapped[str] = mapped_column(String, nullable=False) # e.g. "mcp-k8s-real.get_pod"
    tool_args: Mapped[str] = mapped_column(Text) # JSON string of arguments
    
    # Outcome
    status: Mapped[str] = mapped_column(String) # PENDING, SUCCESS, FAILURE
    result: Mapped[Optional[str]] = mapped_column(Text) # JSON string of result (truncated if too large)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    
    def __repr__(self):
        return f"<AgentAuditLog(agent='{self.agent_name}', tool='{self.tool_name}', status='{self.status}')>"
