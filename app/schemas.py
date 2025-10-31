
from pydantic import BaseModel
from typing import Optional, Any, Dict

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

class AgentInvokePayload(BaseModel):
    text: Optional[str] = None
    context: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
