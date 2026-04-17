"""
EcoZap — Closer Agent (Sprint 2)
==================================
Papel: Fechamento de vendas. Recebe leads qualificados (score >= 50) e fecha.
Hierarquia: Especialista → COMMERCIAL → CTO → CEO

Responsabilidades:
- Recebe leads do SDR (score >= 50)
- Apresenta a oferta conectada à dor identificada pelo SDR
- Lida com objeções (preço, tempo, "vou pensar", "não sei se preciso")
- Detecta intenção de compra e confirma a venda
- Envia link de pagamento / redireciona para checkout
- Notifica owner a cada venda confirmada
- Publica evento SALE_CLOSED no message bus
- Passa cliente para o Consultant após fechamento

Opinion bias: "Toda objeção é uma pergunta disfarçada. Fecha com verdade, não com pressão."
"""
import logging
from datetime import datetime, timezone

from app.agents.base import Agent, AgentContext, AgentOpinion, AuthorityLevel
from app.agents.registry import register
from app.agents.message_bus import publish, Events
from app.config import get_settings
from app.services.alerts import notify_owner

logger = logging.getLogger(__name__)
settings = get_settings()


# Objeções conhecidas e como o Closer as reconhece
OBJECTION_SIGNALS = [
    "vou pensar",
    "deixa eu ver",
    "tá caro",
    "não tenho dinheiro",
    "não sei se preciso",
    "preciso falar com",
    "depois",
    "talvez",
    "não sei não",
    "deixa pra depois",
]

PURCHASE_SIGNALS = [
    "quero",
    "vou comprar",
    "me manda o link",
    "como pago",
    "como faço pra contratar",
    "aceita",
    "pix",
    "cartão",
    "boleto",
    "me passa o preço",
    "fechado",
    "pode ser",
    "bora",
    "top",
]


@register
class Closer(Agent):
    role = "closer"
    display_name = "Closer"
    authority_level = AuthorityLevel.SPECIALIST
    department = "commercial"
    opinion_bias = "toda objeção é uma pergunta disfarçada — fecha com verdade, não com pressão"

    autonomous_actions = [
        "send_message",
        "send_payment_link",
        "update_lead_status",
        "update_lead_score",
        "create_customer",
        "schedule_followup",
    ]
    requires_ceo_override = [
        "broadcast_to_all_customers",
        "change_pricing",
        "delete_customer",
    ]

    async def act(self, context: AgentContext) -> dict:
        """
        Processa mensagem de lead qualificado tentando fechar a venda.
        """
        phone = context.payload.get("phone", "")
        owner_id = context.payload.get("owner_id", "")
        message = context.payload.get("message", "")
        current_score = context.payload.get("lead_score", 50)

        logger.info("[Closer] Trabalhando fechamento com %s... score: %d", phone[:5] + "***", current_score)

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phone": phone,
            "owner_id": owner_id,
            "action": "closing",
            "objection_detected": False,
            "purchase_intent": False,
            "sale_closed": False,
            "score_after": current_score,
        }

        # Detecta sinais de objeção ou compra antes de processar
        msg_lower = message.lower()
        result["objection_detected"] = any(s in msg_lower for s in OBJECTION_SIGNALS)
        result["purchase_intent"] = any(s in msg_lower for s in PURCHASE_SIGNALS)

        if result["objection_detected"]:
            logger.info("[Closer] Objeção detectada para %s — aplicando técnica de contorno", phone[:5] + "***")

        # Usa o Qualifier com modo "closer" para manter o tom certo
        try:
            from app.agents.qualifier import QualifierAgent
            qualifier = QualifierAgent()
            process_result = await qualifier.process(
                phone=phone,
                owner_id=owner_id,
                message=message,
                agent_mode="closer",
            )
            new_score = process_result.get("lead_score", current_score)
            new_status = process_result.get("lead_status", "qualificando")
            result["score_after"] = new_score
            result["lead_status"] = new_status

            # Venda confirmada pelo Qualifier
            if new_status == "cliente":
                result["sale_closed"] = True
                result["action"] = "sale_closed"
                await self._handle_sale_closed(phone, owner_id, new_score, context)

        except Exception as e:
            logger.warning("[Closer] Falha ao processar via Qualifier: %s", e)

        return result

    async def _handle_sale_closed(self, phone: str, owner_id: str, score: int, context: AgentContext):
        """Trata fechamento: notifica owner, publica evento, passa para Consultant."""
        logger.info("[Closer] 🎉 VENDA FECHADA — %s", phone[:5] + "***")

        # Publica SALE_CLOSED no message bus
        try:
            import redis as redis_lib
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
            await publish(r, self.role, Events.SALE_CLOSED, {
                "phone": phone,
                "owner_id": owner_id,
                "lead_score": score,
                "tenant_id": context.tenant_id,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.warning("[Closer] Falha ao publicar SALE_CLOSED: %s", e)

        # Notifica owner com mensagem humana
        try:
            notify_owner(
                f"🎉 *Closer — Venda Confirmada!*\n\n"
                f"Um lead acabou de se tornar cliente.\n\n"
                f"*Pontuação final:* {score}/100\n\n"
                f"*O que acontece agora:* O Consultant assumirá o onboarding — "
                f"vai apresentar o produto, tirar dúvidas iniciais e garantir que "
                f"o novo cliente comece bem.",
                level="info",
            )
        except Exception:
            pass

    async def report_status(self) -> dict:
        """Conta leads em fechamento e vendas do dia."""
        try:
            from app.database import get_db
            from datetime import date
            db = get_db()

            # Leads em fechamento
            closing = db.table("customers")\
                .select("id", count="exact")\
                .in_("lead_status", ["qualificando", "em_atendimento_humano"])\
                .gte("lead_score", 50)\
                .execute()

            # Clientes criados hoje
            today = date.today().isoformat()
            sales = db.table("customers")\
                .select("id", count="exact")\
                .eq("lead_status", "cliente")\
                .gte("created_at", today)\
                .execute()

            closing_count = closing.count or 0
            sales_count = sales.count or 0
        except Exception:
            closing_count = 0
            sales_count = 0

        return {
            "role": self.role,
            "status": "operational",
            "leads_in_closing": closing_count,
            "sales_today": sales_count,
            "summary": (
                f"{sales_count} venda(s) hoje. {closing_count} lead(s) em processo de fechamento."
            ),
        }

    def opine(self, question: str, context: AgentContext) -> AgentOpinion:
        sales_keywords = ["oferta", "preço", "venda", "fechar", "pagamento", "plano"]
        if any(kw in question.lower() for kw in sales_keywords):
            return AgentOpinion(
                agent_role=self.role,
                agrees=True,
                reasoning=(
                    f"[{self.display_name}] Qualquer mudança em preço ou oferta precisa "
                    f"ser comunicada para mim antes de ir para os leads — "
                    f"para eu não citar valores desatualizados durante o fechamento."
                ),
            )
        return AgentOpinion(
            agent_role=self.role,
            agrees=True,
            reasoning=f"[{self.display_name}] Sem impacto no processo de fechamento.",
        )
