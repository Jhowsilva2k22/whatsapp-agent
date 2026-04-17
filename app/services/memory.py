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
        # Filtra mensagens com conteúdo vazio — evita rejeição 400 da API do Claude/Gemini
        return [{"role": m["role"], "content": m["content"]} for m in messages if m.get("content", "").strip()]

    async def save_turn(self, phone: str, owner_id: str, role: str, content: str):
        # Não salva mensagens com conteúdo vazio (ex: imagem sem legenda → display_message="")
        # Conteúdo vazio no histórico causa rejeição 400 na API do Claude e quebra respostas futuras
        if not content or not content.strip():
            logger.warning(f"[Memory] save_turn ignorado: conteúdo vazio (phone={phone} | role={role})")
            return
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
        # Lê da tabela tenants (tabela owners está obsoleta/vazia)
        result = self.db.table("tenants").select("*").eq("id", owner_id).limit(1).execute()
        if not result or not result.data:
            return None
        row = dict(result.data[0])
        # Normaliza campos para compatibilidade com os agentes
        row.setdefault("phone", row.get("owner_phone", ""))
        row.setdefault("tone", row.get("bot_tone", "amigavel"))
        row.setdefault("notify_phone", row.get("owner_phone", ""))
        return row

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

    # Palavras que indicam que o bot pediu o nome na última mensagem
    _NAME_REQUEST_KEYWORDS = {
        "nome", "como te chama", "como você se chama", "como voce se chama",
        "se apresenta", "quem é você", "quem e voce", "seu nome",
        "me diz seu nome", "qual o seu nome", "qual seu nome",
        "como se chama", "pode se apresentar", "como prefere",
        "como posso te chamar", "como posso chamar",
    }

    @staticmethod
    def _looks_like_real_name(text: str) -> bool:
        """Heurística: verifica se o texto parece um nome próprio real.
        Rejeita: spam de teclado, chars repetidos em excesso, sem vogais, etc.
        """
        import re
        vowels = set("aeiouáéíóúâêîôûãõàèìòùäëïöü")
        for word in text.split():
            w = word.lower()
            # Deve ter pelo menos uma vogal
            if not any(c in vowels for c in w):
                return False
            # Ratio de chars únicos vs total: mínimo 50%
            # Pega "Geueueu" (3 únicos / 7 total = 43%) → rejeita
            if len(w) > 3 and len(set(w)) / len(w) < 0.5:
                return False
            # Não pode ter 3+ chars idênticos consecutivos
            if re.search(r"(.)\1{2,}", w):
                return False
        return True

    async def detect_and_save_name(self, phone: str, owner_id: str, message: str, history: list = None):
        """Detecta nome do lead SOMENTE quando o bot já perguntou pelo nome.
        Também valida se o texto realmente parece um nome próprio.
        """
        # Só captura nome se o bot realmente pediu na mensagem anterior
        if not history:
            return None

        # Verifica se a última mensagem do assistente pede o nome
        last_assistant = ""
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                last_assistant = msg.get("content", "").lower()
                break

        if not last_assistant:
            return None

        if not any(kw in last_assistant for kw in self._NAME_REQUEST_KEYWORDS):
            return None

        msg = message.strip()
        clean = msg.replace("!", "").replace("?", "").replace(".", "").replace(",", "").strip()
        clean_lower = clean.lower()

        if clean_lower in self._GREETINGS:
            return None

        words = clean.split()
        if not (1 <= len(words) <= 3):
            return None
        if any(c.isdigit() for c in clean):
            return None
        if "http" in msg:
            return None

        # Valida se parece nome real (rejeita "Geueueu", spam, etc.)
        if not self._looks_like_real_name(clean):
            logger.info(f"[Memory] Texto rejeitado como nome (não parece nome real): '{clean}' ({phone})")
            return None

        name = clean.title()
        await self.update_customer(phone, owner_id, {"name": name})
        logger.info(f"[Memory] Nome detectado e salvo: {name} ({phone})")
        return name

    async def set_channel(self, phone: str, owner_id: str, channel: str):
        """Salva o canal de origem do lead (reels, anúncio, stories, etc)."""
        await self.update_customer(phone, owner_id, {"channel": channel})
