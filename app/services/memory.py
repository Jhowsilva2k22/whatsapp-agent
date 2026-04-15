from app.database import get_db
from app.models.customer import CustomerProfile
from datetime import datetime
from typing import Optional
import logging

logger = logging.getLogger(__name__)
MAX_RAW_TURNS = 10  # pares de mensagens antes de comprimir

class MemoryService:
    def __init__(self):
        self.db = get_db()

    async def get_or_create_customer(self, phone: str, owner_id: str) -> CustomerProfile:
        result = self.db.table("customers").select("*").eq("phone", phone).eq("owner_id", owner_id).maybe_single().execute()
        if result and result.data:
            return CustomerProfile(**result.data)
        new_customer = CustomerProfile(phone=phone, owner_id=owner_id, first_contact=datetime.utcnow(), last_contact=datetime.utcnow())
        self.db.table("customers").insert(new_customer.model_dump(mode='json')).execute()
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
        self.db.table("messages").insert({
            "phone": phone, "owner_id": owner_id,
            "role": role, "content": content,
            "created_at": datetime.utcnow().isoformat()
        }).execute()
        await self._maybe_compress(phone, owner_id)

    async def _maybe_compress(self, phone: str, owner_id: str):
        """Comprime histórico antigo em resumo para economizar tokens."""
        result = self.db.table("messages").select("id,role,content").eq("phone", phone).eq("owner_id", owner_id).order("created_at").execute()
        if not result.data:
            return
        total = len(result.data)
        if total <= MAX_RAW_TURNS * 2:
            return

        # Mensagens antigas que serão comprimidas
        to_compress = result.data[:total - MAX_RAW_TURNS * 2]
        if not to_compress:
            return

        try:
            from app.services.ai import AIService
            summary_text = await AIService().compress_conversation(to_compress)
            if summary_text:
                # Pega notas do dono (preserva) e substitui o resumo antigo
                customer = self.db.table("customers").select("summary").eq("phone", phone).eq("owner_id", owner_id).maybe_single().execute()
                existing = (customer.data.get("summary") or "") if customer and customer.data else ""
                # Preserva apenas notas do dono
                notes = "\n".join(line for line in existing.split("\n") if line.strip().startswith("[Nota"))
                new_summary = f"{summary_text}\n{notes}".strip() if notes else summary_text
                self.db.table("customers").update({"summary": new_summary}).eq("phone", phone).eq("owner_id", owner_id).execute()
        except Exception as e:
            logger.error(f"[Memory] Erro ao comprimir histórico: {e}")

        # Deleta mensagens antigas independente de compressão ter funcionado
        old_ids = [m["id"] for m in to_compress]
        self.db.table("messages").delete().in_("id", old_ids).execute()
        logger.info(f"[Memory] Comprimiu {len(old_ids)} msgs de {phone}")

    async def get_owner_context(self, owner_id: str) -> Optional[dict]:
        result = self.db.table("owners").select("*").eq("id", owner_id).maybe_single().execute()
        return result.data if result and result.data else None

    async def detect_and_save_name(self, phone: str, owner_id: str, message: str):
        """Detecta nome do lead em uma mensagem curta (resposta a 'qual seu nome?')."""
        msg = message.strip()
        # Heurística: mensagem curta (1-3 palavras), sem URL, sem número
        words = msg.split()
        if 1 <= len(words) <= 3 and not any(c.isdigit() for c in msg) and "http" not in msg:
            name = msg.title()
            await self.update_customer(phone, owner_id, {"name": name})
            logger.info(f"[Memory] Nome detectado: {name} ({phone})")
            return name
        return None

    async def set_channel(self, phone: str, owner_id: str, channel: str):
        """Salva o canal de origem do lead (reels, anúncio, stories, etc)."""
        await self.update_customer(phone, owner_id, {"channel": channel})
