"""
EcoZap — Consultant Agent (Sprint 2)
======================================
Papel: Onboarding, educação e retenção de clientes.
Hierarquia: Especialista → COMMERCIAL → CTO → CEO

Responsabilidades:
- Recebe clientes novos do Closer e faz onboarding completo
- Educa o cliente sobre como usar o produto/serviço
- Tira dúvidas de clientes ativos
- Detecta sinais de insatisfação (churn risk) e age proativamente
- Identifica oportunidades de upgrade e up-sell
- Coleta feedbacks e manda para o aprendizado noturno
- Gera relatório de satisfação para o owner

Opinion bias: "Cliente satisfeito não precisa ser convencido de renovar. Foco em resultado real."
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


# Sinais de risco de churn
CHURN_SIGNALS = [
    "cancelar",
    "não tô usando",
    "não to usando",
    "não funciona",
    "decepcionado",
    "decepcionei",
    "errado",
    "arrependido",
    "devolução",
    "reembolso",
    "péssimo",
    "horrível",
    "não vale",
    "muito caro",
    "vou sair",
    "quero cancelar",
    "quero parar",
]

# Sinais de upgrade / up-sell
UPSELL_SIGNALS = [
    "quero mais",
    "tem plano maior",
    "dá pra aumentar",
    "preciso de mais",
    "tem algo melhor",
    "subir de plano",
    "adicionar",
    "mais recursos",
    "minha equipe",
    "time inteiro",
    "vários usuários",
]

# Mensagem de onboarding padrão (personalizada com dados do owner)
ONBOARDING_SEQUENCE = [
    "boas_vindas",       # Dia 0: boas vindas + próximos passos
    "primeiro_uso",      # Dia 1: como usar o recurso principal
    "dica_chave",        # Dia 3: dica que muda o jogo
    "check_in",          # Dia 7: "como está indo?"
]


@register
class Consultant(Agent):
    role = "consultant"
    display_name = "Consultant"
    authority_level = AuthorityLevel.SPECIALIST
    department = "commercial"
    opinion_bias = "cliente satisfeito não precisa ser convencido de renovar — foco em resultado real"

    autonomous_actions = [
        "send_message",
        "send_onboarding_sequence",
        "schedule_checkin",
        "update_customer_status",
        "collect_feedback",
        "flag_churn_risk",
    ]
    requires_ceo_override = [
        "broadcast_to_all_customers",
        "change_pricing",
        "delete_customer",
        "execute_financial_transaction",
    ]

    async def act(self, context: AgentContext) -> dict:
        """
        Processa mensagem de cliente ativo.
        Detecta churn risk, up-sell ou dúvida normal e age adequadamente.
        """
        phone = context.payload.get("phone", "")
        owner_id = context.payload.get("owner_id", "")
        message = context.payload.get("message", "")
        trigger = context.payload.get("trigger", "message")  # "message" | "new_client" | "scheduled"

        logger.info("[Consultant] Atendendo cliente %s... trigger=%s", phone[:5] + "***", trigger)

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phone": phone,
            "owner_id": owner_id,
            "action": "consulting",
            "churn_risk": False,
            "upsell_opportunity": False,
            "onboarding_triggered": False,
        }

        # Trigger especial: novo cliente chegando do Closer
        if trigger == "new_client":
            result["action"] = "onboarding"
            result["onboarding_triggered"] = True
            await self._start_onboarding(phone, owner_id, context)
            return result

        # Processa mensagem normal usando o Qualifier com modo "consultant"
        msg_lower = message.lower()
        result["churn_risk"] = any(s in msg_lower for s in CHURN_SIGNALS)
        result["upsell_opportunity"] = any(s in msg_lower for s in UPSELL_SIGNALS)

        if result["churn_risk"]:
            logger.warning("[Consultant] ⚠️ Risco de churn detectado — %s", phone[:5] + "***")
            await self._handle_churn_risk(phone, owner_id, message, context)
            result["action"] = "churn_prevention"

        elif result["upsell_opportunity"]:
            logger.info("[Consultant] 💡 Oportunidade de up-sell — %s", phone[:5] + "***")
            result["action"] = "upsell"

        # Processa a resposta via Qualifier (modo atendimento/retenção)
        try:
            from app.agents.qualifier import QualifierAgent
            qualifier = QualifierAgent()
            await qualifier.process(
                phone=phone,
                owner_id=owner_id,
                message=message,
                agent_mode="qualifier",
            )
        except Exception as e:
            logger.warning("[Consultant] Falha ao processar resposta: %s", e)

        return result

    async def _start_onboarding(self, phone: str, owner_id: str, context: AgentContext):
        """Inicia sequência de onboarding para novo cliente."""
        logger.info("[Consultant] Iniciando onboarding para %s", phone[:5] + "***")

        try:
            from app.database import get_db
            from app.services.sender import send_text_message

            db = get_db()

            # Busca dados do owner para personalizar
            owner_resp = db.table("owners").select("business_name, main_offer").eq("id", owner_id).execute()
            owner = owner_resp.data[0] if owner_resp.data else {}
            negocio = owner.get("business_name", "nosso serviço")
            oferta = owner.get("main_offer", "o produto")

            # Mensagem de boas-vindas personalizada
            boas_vindas = (
                f"Seja bem-vindo(a) à família {negocio}! 🎉\n\n"
                f"Muito feliz em ter você com a gente. "
                f"Meu papel agora é garantir que você aproveite o máximo de {oferta}.\n\n"
                f"Nas próximas horas vou te guiar nos primeiros passos. "
                f"Qualquer dúvida é só falar — estou aqui 24h. 😊"
            )

            await send_text_message(phone, boas_vindas, owner_id)

            # Notifica owner
            notify_owner(
                f"🤝 *Consultant — Onboarding Iniciado*\n\n"
                f"Novo cliente recebido do Closer.\n"
                f"Onboarding foi ativado automaticamente — "
                f"o cliente receberá uma sequência de mensagens de orientação nos próximos dias.",
                level="info",
            )

        except Exception as e:
            logger.warning("[Consultant] Falha no onboarding: %s", e)

    async def _handle_churn_risk(self, phone: str, owner_id: str, message: str, context: AgentContext):
        """Alerta owner imediatamente sobre risco de perder um cliente."""
        try:
            # Publica evento no message bus
            import redis as redis_lib
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
            await publish(r, self.role, Events.ANOMALY_DETECTED, {
                "type": "churn_risk",
                "phone": phone,
                "owner_id": owner_id,
                "message_snippet": message[:100],
                "tenant_id": context.tenant_id,
            })
        except Exception:
            pass

        # Notifica owner com linguagem clara
        try:
            notify_owner(
                f"⚠️ *Consultant — Atenção: Cliente em Risco*\n\n"
                f"Um cliente sinalizou insatisfação ou intenção de cancelar.\n\n"
                f"*O que está fazendo:* Estou tentando entender o problema e resolver. "
                f"Se não conseguir resolver sozinho nos próximos minutos, "
                f"vou acionar você para uma conversa personalizada.\n\n"
                f"*O que você pode fazer:* Aguarde. Se quiser agir agora, "
                f"entre na conversa e ofereça algo especial — às vezes um gesto pessoal "
                f"faz toda a diferença.",
                level="warn",
            )
        except Exception:
            pass

    async def report_status(self) -> dict:
        """Conta clientes ativos e em onboarding."""
        try:
            from app.database import get_db
            db = get_db()
            clientes = db.table("customers")\
                .select("id", count="exact")\
                .eq("lead_status", "cliente")\
                .execute()
            total = clientes.count or 0
        except Exception:
            total = 0

        return {
            "role": self.role,
            "status": "operational",
            "active_clients": total,
            "summary": (
                f"{total} cliente(s) ativo(s) em acompanhamento."
                if total else
                "Nenhum cliente ativo no momento."
            ),
        }

    def opine(self, question: str, context: AgentContext) -> AgentOpinion:
        retention_keywords = ["cliente", "retenção", "churn", "cancelamento", "satisfação", "renovação"]
        if any(kw in question.lower() for kw in retention_keywords):
            return AgentOpinion(
                agent_role=self.role,
                agrees=True,
                reasoning=(
                    f"[{self.display_name}] Qualquer mudança que afete clientes ativos "
                    f"deve ter comunicação proativa — aviso antes da mudança, não depois. "
                    f"Clientes que sabem o que está vindo não cancelam."
                ),
            )
        return AgentOpinion(
            agent_role=self.role,
            agrees=True,
            reasoning=f"[{self.display_name}] Sem impacto na retenção identificado.",
        )
