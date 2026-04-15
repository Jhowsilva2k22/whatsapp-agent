from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class CustomerProfile(BaseModel):
    phone: str
    name: Optional[str] = None
    first_contact: Optional[datetime] = None
    last_contact: Optional[datetime] = None
    communication_style: Optional[str] = None
    emoji_usage: Optional[str] = None
    avg_message_length: Optional[str] = None
    lead_score: int = 0
    lead_status: str = "novo"
    intent: Optional[str] = None
    last_intent: Optional[str] = None
    summary: Optional[str] = None
    objections: Optional[list] = None
    interests: Optional[list] = None
    total_messages: int = 0
    owner_id: str = ""
    channel: Optional[str] = None
    follow_up_stage: int = 0
