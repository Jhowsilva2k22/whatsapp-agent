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
        # Usa limit(1) em vez de maybe_single() para evitar 406 quando há duplicatas
        result = self.db.table("customers").select("*").eq("phone", phone).eq("owner_id", owner_id).limit(1).execute()
        if result and result.data:
            return CustomerProfile(**result.data[0])

        # Novo lead — insere no banco e retorna o registro criado
        now = datetime.utcnow().isoformat()
        new_data = {
            "phone": phone,
            "owner_id": owner_id,
            "lead_score": 0,
            "lead_status": "qualificando",
            "total_messages": 0,
            "first_contact": now,
            "last_contact": now,
        }
        insert_result = self.db.table("customers").insert(new_data).execute()
        if insert_result and insert_result.data:
            return CustomerProfile(**insert_result.data[0])

        # Fallback seguro se insert não retornar dados
        return CustomerProfile(phone=phone, owner_id=owner_id)

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

        to_compress = result.data[:total - MAX_RAW_TURNS * 2]
        if not to_compress:
            return

        try:
            from app.services.ai import AIService
            summary_text = await AIService().compress_conversation(to_compress)
            if summary_text:
                customer = self.db.table("customers").select("summary").eq("phone", phone).eq("owner_id", owner_id).limit(1).execute()
                existing = (customer.data[0].get("summary") or "") if customer and customer.data else ""
                notes = "\n".join(line for line in existing.split("\n") if line.strip().startswith("[Nota"))
                new_summary = f"{summary_text}\n{notes}".strip() if notes else summary_text
                self.db.table("customers").update({"summary": new_summary}).eq("phone", phone).eq("owner_id", owner_id).execute()
        except Exception as e:
            logger.error(f"[Memory] Erro ao comprimir histórico: {e}")

        old_ids = [m["id"] for m in to_compress]
        self.db.table("messages").delete().in_("id", old_ids).execute()
        logger.info(f"[Memory] Comprimiu {len(old_ids)} msgs de {phone}")

    async def get_owner_context(self, owner_id: str) -> Optional[dict]:
        result = self.db.table("owners").select("*").eq("id", owner_id).limit(1).execute()
        return result.data[0] if result and result.data else None

    _GREETINGS = {
        "oi", "olá", "ola", "hey", "eae", "eai", "e ai", "e aí",
        "boa noite", "boa tarde", "bom dia", "boa madrugada",
        "oi boa noite", "oi boa tarde", "oi bom dia",
        "olá boa noite", "olá boa tarde", "olá bom dia",
        "ola boa noite", "ola boa tarde", "ola bom dia",
        "oie", "oii", "oiii", "opa", "fala", "salve",
        "obrigado", "obrigada", "vlw", "valeu", "brigado", "brigada",
        "ok", "tá", "ta", "sim", "não", "nao", "beleza", "blz",
        "tudo bem", "tudo bom", "td bem", "td bom",
        "bom", "boa", "show", "massa", "top", "legal",
        "tchau", "até mais", "ate mais", "flw", "falou",
        "oi tudo bem", "oi tudo bom", "olá tudo bem",
        "boas", "noite", "tarde", "dia",
    }

    async def detect_and_save_name(self, phone: str, owner_id: str, message: str):
        """Detecta nome do lead em uma mensagem curta (resposta a 'qual seu nome?')."""
        msg = message.strip()
        clean = msg.replace("!", "").replace("?", "").replace(".", "").replace(",", "").strip()
        clean_lower = clean.lower()

        if clean_lower in self._GREETINGS:
            return None

        words = clean.split()
        if 1 <= len(words) <= 3 and not any(c.isdigit() for c in clean) and "http" not in msg:
            name = clean.title()
            await self.update_customer(phone, owner_id, {"name": name})
            logger.info(f"[Memory] Nome detectado: {name} ({phone})")
            return name
        return None

    async def set_channel(self, phone: str, owner_id: str, channel: str):
        """Salva o canal de origem do lead (reels, anúncio, stories, etc)."""
        await self.update_customer(phone, owner_id, {"channel": channel})
