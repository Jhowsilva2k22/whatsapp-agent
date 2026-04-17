from pydantic import BaseModel
from typing import Optional


class CustomerProfile(BaseModel):
    """Customer profile — espelha a tabela 'customers' do Supabase."""
    id: Optional[str] = None
    owner_id: str
    phone: str
    name: Optional[str] = None
    first_name: Optional[str] = None
    email: Optional[str] = None
    lead_score: Optional[int] = 0
    lead_status: Optional[str] = "qualificando"
    channel: Optional[str] = None
    summary: Optional[str] = None
    total_messages: Optional[int] = 0
    last_contact: Optional[str] = None
    first_contact: Optional[str] = None
    follow_up_stage: Optional[int] = 0
    nurture_paused: Optional[bool] = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True
