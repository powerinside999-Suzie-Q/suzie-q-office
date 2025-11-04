# app/schemas.py
from typing import Optional, Any, Dict, List
from pydantic import BaseModel, Field

# ---------- Slack & Telegram ----------
class SlackEvent(BaseModel):
    token: Optional[str] = None
    team_id: Optional[str] = None
    api_app_id: Optional[str] = None
    type: Optional[str] = None
    challenge: Optional[str] = None
    event: Optional[Dict[str, Any]] = None

class TelegramUpdate(BaseModel):
    update_id: Optional[int] = None
    message: Optional[Dict[str, Any]] = None
    edited_message: Optional[Dict[str, Any]] = None
    channel_post: Optional[Dict[str, Any]] = None
    edited_channel_post: Optional[Dict[str, Any]] = None

# ---------- Agent invoke ----------
class AgentInvokePayload(BaseModel):
    text: Optional[str] = None
    context: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

# ---------- Memory ----------
class RememberPayload(BaseModel):
    content: str
    tags: Optional[List[str]] = None
    importance: Optional[int] = Field(default=1, ge=1, le=5)
    source: Optional[str] = None
    department: Optional[str] = None
    actor: Optional[str] = None

class RecallPayload(BaseModel):
    query: str
    top_k: Optional[int] = Field(default=8, ge=1, le=50)
    min_similarity: Optional[float] = Field(default=0.15, ge=0.0, le=1.0)
    department: Optional[str] = None

# ---------- Staff ----------
class StaffCreatePayload(BaseModel):
    department: str
    employees_count: Optional[int] = Field(default=5, ge=1, le=50)
    employee_names: Optional[List[str]] = None  # if not given, auto-generate
    create_channel: Optional[bool] = False
    slack_channel_id: Optional[str] = None      # if you already made a #dept-... channel

class StaffListQuery(BaseModel):
    department: Optional[str] = None

class StaffDeletePayload(BaseModel):
    staff_id: str  # uuid of the staff member to deactivate/fire

class RnDBootstrapPayload(BaseModel):
    department: str
    researchers: Optional[int] = 5  # Director+N researchers

class RnDProjectCreate(BaseModel):
    department: str
    title: str
    goal: Optional[str] = None

class RnDExperimentCreate(BaseModel):
    project_id: str
    hypothesis: str
    method: Optional[str] = None
    metrics: Optional[List[Dict[str, Any]]] = None  # [{"name":..., "target":...}]

class IngestWebPayload(BaseModel):
    department: str
    url: str
