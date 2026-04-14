from app.database import get_db
from app.services.ai import AIService
from datetime import datetime, timedelta
import logging
import json

logger = logging.getLogger(__name__)

class LearningService:
    def __init__(self):
        self.db = get_db()
        self.ai = AIService()

    async def run_daily_analysis(self, owner_id: str):
        yesterday = datetime.utcnow() - timedelta(days=1)
        messages = self.db.table("messages").select("phone,role,content,created_at").eq("owner_id", owner_id).gte("created_at", yesterday.isoformat()).order("created_at").execute()
        if not messages.data:
            return
        hot_leads = self.db.table("customers").select("phone,lead_score,summary").eq("owner_id", owner_id).gte("lead_score", 70).gte("last_contact", yesterday.isoformat()).execute()
        conversations_text = self._group_by_phone(messages.data)
        hot_count = len(hot_leads.data) if hot_leads.data else 0
        prompt = f"""Analise as conversas de hoje e retorne JSON com:
- winning_patterns, losing_patterns, new_objections
- suggested_qa: lista de {{pergunta, resposta}}
- performance_summary, conversion_rate

Conversas ({hot_count} leads quentes hoje):\n{conversations_text[:6000]}

Responda APENAS o JSON."""
        response = self.ai.claude.messages.create(model="claude-sonnet-4-6", max_tokens=1000, messages=[{"role": "user", "content": prompt}])
        try:
            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("```")[1].replace("json", "").strip()
            learnings = json.loads(text)
        except Exception:
            return
        self.db.table("learnings").insert({"owner_id": owner_id, "date": datetime.utcnow().date().isoformat(), "data": learnings, "hot_leads_count": hot_count, "total_conversations": len(set(m['phone'] for m in messages.data))}).execute()
        if learnings.get("suggested_qa"):
            owner = self.db.table("owners").select("faqs").eq("id", owner_id).maybe_single().execute()
            existing_faqs = (owner.data.get("faqs") if owner and owner.data else None) or []
            new_faqs = [f"{qa['pergunta']} -> {qa['resposta']}" for qa in learnings["suggested_qa"]]
            updated_faqs = list(set(existing_faqs + new_faqs))[:30]
            self.db.table("owners").update({"faqs": updated_faqs}).eq("id", owner_id).execute()

    def _group_by_phone(self, messages: list) -> str:
        groups = {}
        for m in messages:
            phone = m["phone"]
            if phone not in groups:
                groups[phone] = []
            groups[phone].append(f"[{m['role']}]: {m['content']}")
        result = []
        for phone, msgs in list(groups.items())[:10]:
            result.append(f"\n--- Conversa {phone[-4:]} ---")
            result.extend(msgs[:20])
        return "\n".join(result)
