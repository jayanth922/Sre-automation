import uuid
from typing import Any, Dict, Literal, Optional, List
from datetime import datetime
from pydantic import BaseModel, EmailStr

from backend.models import UserRole, ClusterStatus, IncidentSeverity, IncidentStatus

# ----------------------------------------------------------------------
# Auth Schemas
# ----------------------------------------------------------------------

class Token(BaseModel):
    access_token: str
    token_type: str

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
    id: uuid.UUID
    role: UserRole
    org_id: uuid.UUID
    is_active: bool

    class Config:
        from_attributes = True


class UserProfileResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: Optional[str] = None
    display_name: str
    role: UserRole
    org_id: uuid.UUID
    organization_name: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class PasswordResetRequest(BaseModel):
    current_password: str
    new_password: str

# ----------------------------------------------------------------------
# Organization Schemas
# ----------------------------------------------------------------------

class OrgCreate(BaseModel):
    name: str

class OrgResponse(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime

    class Config:
        from_attributes = True

# ----------------------------------------------------------------------
# Cluster Schemas
# ----------------------------------------------------------------------

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

class ClusterResponse(BaseModel):
    id: uuid.UUID
    name: str
    status: ClusterStatus
    last_heartbeat: Optional[datetime]
    created_at: datetime
    prometheus_url: Optional[str] = None
    loki_url: Optional[str] = None
    k8s_api_server: Optional[str] = None
    github_repo: Optional[str] = None

    class Config:
        from_attributes = True

# ----------------------------------------------------------------------
# Incident Schemas
# ----------------------------------------------------------------------

class IncidentCreate(BaseModel):
    title: str
    description: Optional[str] = None
    severity: IncidentSeverity = IncidentSeverity.MEDIUM

class IncidentResponse(BaseModel):
    id: uuid.UUID
    cluster_id: uuid.UUID
    title: str
    description: Optional[str] = None
    severity: IncidentSeverity
    status: IncidentStatus
    summary: Optional[str] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class IncidentTimelineEventResponse(BaseModel):
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

    class Config:
        from_attributes = True


class IncidentTranscriptResponse(BaseModel):
    incident: IncidentResponse
    conversation_mode: Literal["investigation", "assistant"]
    summary: Optional[str] = None
    events: List[IncidentTimelineEventResponse]


class IncidentMessageRequest(BaseModel):
    message: str

# ----------------------------------------------------------------------
# SLO Schemas
# ----------------------------------------------------------------------

class SLOCreate(BaseModel):
    name: str
    sli_metric: str
    target: float  # e.g., 99.9
    window_days: int = 30

class SLOResponse(BaseModel):
    id: uuid.UUID
    cluster_id: uuid.UUID
    name: str
    sli_metric: str
    target: float
    window_days: int
    current_value: Optional[float] = None
    error_budget_remaining: Optional[float] = None
    last_calculated: Optional[datetime] = None

    class Config:
        from_attributes = True

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

class JobCreate(BaseModel):
    job_type: JobType = JobType.INVESTIGATION
    payload: Optional[str] = None  # JSON string

class JobStatusUpdate(BaseModel):
    status: JobStatus
    result: Optional[str] = None  # JSON string
    logs: Optional[str] = None

class JobResponse(BaseModel):
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

    class Config:
        from_attributes = True
