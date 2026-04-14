from app.database import get_db
from app.models.customer import CustomerProfile
from datetime import datetime
from typing import Optional
import logging

logger = logging.getLogger(__name__)
MAX_RAW_TURNS = 10

class MemoryService:
    def __init__(self):
        self.db = get_db()

    async def get_or_create_customer(self, phone: str, owner_id: str) -> CustomerProfile:
        result = self.db.table("customers").select("*").eq("phone", phone).eq("owner_id", owner_id).maybe_single().execute()
        if result and result.data:
            return CustomerProfile(**result.data)
        new_customer = CustomerProfile(phone=phone, owner_id=owner_id, first_contact=datetime.utcnow(), last_contact=datetime.utcnow())
        self.db.table("customers").insert(new_customer.model_dump()).execute()
        return new_customer

    async def update_customer(self, phone: str, owner_id: str, updates: dict):
        updates["last_contact"] = datetime.utcnow().isoformat()
        self.db.table("customers").update(updates).eq("phone", phone).eq("owner_id", owner_id).execute()

    async def get_conversation_history(self, phone: str, owner_id: str) -> list:
        result = self.db.table("messages").select("role,content,created_at").eq("phone", phone).eq("owner_id", owner_id).order("created_at", desc=True).limit(MAX_RAW_TURNS * 2).execute()
        if not result.data:
            return []
        messages = list(reversed(result.data))
        return [{"role": m["role"], "content": m["content"]} for m in messages]

    async def save_turn(self, phone: str, owner_id: str, role: str, content: str):
        self.db.table("messages").insert({"phone": phone, "owner_id": owner_id, "role": role, "content": content, "created_at": datetime.utcnow().isoformat()}).execute()
        await self._maybe_compress(phone, owner_id)

    async def _maybe_compress(self, phone: str, owner_id: str):
        result = self.db.table("messages").select("id,role,content", count="exact").eq("phone", phone).eq("owner_id", owner_id).execute()
        count = result.count or 0
        if count <= MAX_RAW_TURNS * 2:
            return
        old_messages = result.data[:count - MAX_RAW_TURNS * 2]
        if not old_messages:
            return
        old_ids = [m["id"] for m in old_messages]
        self.db.table("messages").delete().in_("id", old_ids).execute()

    async def get_owner_context(self, owner_id: str) -> Optional[dict]:
        result = self.db.table("owners").select("*").eq("id", owner_id).maybe_single().execute()
        return result.data if result and result.data else None
