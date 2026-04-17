"""
EcoZap — SDR Agent (Sprint 2)
==============================
Papel: Primeiro contato. Capta, identifica e qualifica leads.
Hierarquia: Especialista → COMMERCIAL → CTO → CEO

Responsabilidades:
- Recebe leads novos (score 0–50)
- Identifica de onde veio o lead (canal: reels, indicação, ads, etc.)
- Faz as perguntas certas para entender o problema real
- Detecta se o lead é qualificado para o Closer (score >= 50)
- Descarta leads fora do perfil com educação e leveza
- Notifica owner e publica evento para o Closer quando lead está maduro

Opinion bias: "Quantidade não vale nada sem qualidade. Prefiro passar 3 leads certos que 30 errados."
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from app.agents.base import Agent, AgentContext, AgentOpinion, AuthorityLevel
from app.agents.registry import register
from app.agents.message_bus import publish, Events
from app.config import get_settings
from app.services.alerts import notify_owner

logger = logging.getLogger(__name__)
settings = get_settings()

SCORE_THRESHOLD_CLOSER = 50   # acima disso → passa para o Closer


@register
class SDR(Agent):
    role = "sdr"
    display_name = "SDR"
    authority_level = AuthorityLevel.SPECIALIST
    department = "commercial"
    opinion_bias = "quantidade não vale nada sem qualidade — prefere 3 leads certos a 30 errados"

    autonomous_actions = [
        "send_message",
        "update_lead_score",
        "update_lead_status",
        "schedule_followup",
        "read_knowledge_base",
        "detect_channel",
    ]
    requires_ceo_override = [
        "broadcast_to_all_customers",
        "delete_customer",
        "change_pricing",
    ]

    async def act(self, context: AgentContext) -> dict:
        """
        Processa mensagem de lead novo ou em qualificação.
        Usa o Qualifier legado e enriquece com lógica de pipeline.
        """
        phone = context.payload.get("phone", "")
        owner_id = context.payload.get("owner_id", "")
        message = context.payload.get("message", "")
        current_score = context.payload.get("lead_score", 0)

        logger.info("[SDR] Qualificando lead %s... score atual: %d", phone[:5] + "***", current_score)

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phone": phone,
            "owner_id": owner_id,
            "action": "qualifying",
            "score_before": current_score,
            "score_after": current_score,
            "ready_for_closer": False,
            "disqualified": False,
        }

        # Usa o Qualifier existente para processar a mensagem
        try:
            from app.agents.qualifier import QualifierAgent
            qualifier = QualifierAgent()
            process_result = await qualifier.process(
                phone=phone,
                owner_id=owner_id,
                message=message,
                agent_mode="qualifier",
            )
            new_score = process_result.get("lead_score", current_score)
            result["score_after"] = new_score
            result["lead_status"] = process_result.get("lead_status", "qualificando")

        except Exception as e:
            logger.warning("[SDR] Falha ao processar via Qualifier: %s", e)
            new_score = current_score

        # Verifica se está pronto para o Closer
        if new_score >= SCORE_THRESHOLD_CLOSER:
            result["ready_for_closer"] = True
            result["action"] = "handoff_to_closer"

            logger.info("[SDR] Lead %s qualificado (score %d) → passando para Closer",
                        phone[:5] + "***", new_score)

            # Publica evento para o Closer
            try:
                import redis as redis_lib
                r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
                await publish(r, self.role, Events.LEAD_QUALIFIED, {
                    "phone": phone,
                    "owner_id": owner_id,
                    "lead_score": new_score,
                    "tenant_id": context.tenant_id,
                })
            except Exception as e:
                logger.warning("[SDR] Falha ao publicar LEAD_QUALIFIED: %s", e)

            # Notifica owner
            try:
                notify_owner(
                    f"🎯 *SDR — Lead Qualificado*\n\n"
                    f"Um lead atingiu a pontuação necessária para ser trabalhado pelo Closer.\n\n"
                    f"*Pontuação:* {new_score}/100\n"
                    f"*O que acontece agora:* O Closer assume a conversa e vai apresentar a oferta.",
                    level="info",
                )
            except Exception:
                pass

        return result

    async def report_status(self) -> dict:
        """Conta leads em qualificação via Supabase."""
        try:
            from app.database import get_db
            db = get_db()
            resp = db.table("customers")\
                .select("id", count="exact")\
                .eq("lead_status", "qualificando")\
                .execute()
            count = resp.count or 0
        except Exception:
            count = 0

        return {
            "role": self.role,
            "status": "operational",
            "leads_qualifying": count,
            "summary": (
                f"{count} lead(s) em processo de qualificação agora."
                if count else
                "Nenhum lead em qualificação no momento."
            ),
        }

    def opine(self, question: str, context: AgentContext) -> AgentOpinion:
        lead_keywords = ["lead", "qualific", "captação", "novo cliente", "funil"]
        if any(kw in question.lower() for kw in lead_keywords):
            return AgentOpinion(
                agent_role=self.role,
                agrees=True,
                reasoning=(
                    f"[{self.display_name}] Qualquer mudança no funil de qualificação "
                    f"deve ser testada com leads de teste antes de ir para produção. "
                    f"O score mínimo para Closer está em {SCORE_THRESHOLD_CLOSER} — "
                    f"posso ajustar se necessário."
                ),
            )
        return AgentOpinion(
            agent_role=self.role,
            agrees=True,
            reasoning=f"[{self.display_name}] Sem impacto no pipeline de qualificação.",
        )
