"""
EcoZap — Agent Service (Router)
================================
Ponto central de roteamento de mensagens para o agente certo.
Chamado por _dispatch_to_agent em tasks.py.

Pipeline de decisão:
  lead_status == "cliente"           → Consultant
  lead_score >= 50                   → Closer
  lead_status == "em_atendimento_humano" → pausa (aguarda owner)
  else                               → SDR (qualificação)

Todos os agentes usam o QualifierAgent como motor de conversa —
a diferença está no contexto (prompt), no que monitoram e nas
ações pós-processamento.
"""
import logging
from datetime import datetime, timezone

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

SCORE_CLOSER_THRESHOLD = 50


class AgentService:
    """
    Roteador central. Decide qual agente trata a mensagem
    e executa as ações pós-processamento do pipeline.
    """

    def __init__(self, owner_id: str):
        self.owner_id = owner_id

    async def respond(
        self,
        phone: str,
        message: str,
        agent_mode: str = "both",
        message_id: str = "",
        media_type: str = "text",
    ) -> dict:
        """
        Roteia mensagem para o agente correto e executa pós-processamento.
        Retorna dict com status, agente usado e resultado.
        """
        # 1. Busca estado atual do cliente/lead
        customer_state = await self._get_customer_state(phone)
        lead_status = customer_state.get("lead_status", "qualificando")
        lead_score = customer_state.get("lead_score", 0)

        logger.info(
            "[AgentService] phone=%s status=%s score=%d mode=%s",
            phone[:5] + "***", lead_status, lead_score, agent_mode,
        )

        # 2. Pausa: lead em atendimento humano → não responde automaticamente
        if lead_status == "em_atendimento_humano":
            logger.info("[AgentService] Lead em atendimento humano — IA pausada.")
            return {"status": "paused", "reason": "human_handoff", "agent": None}

        # 3. Roteamento baseado em status + score
        agent_role = self._route(lead_status, lead_score)
        logger.info("[AgentService] Roteando para agente: %s", agent_role)

        # 4. Processa via QualifierAgent (motor compartilhado)
        try:
            from app.agents.qualifier import QualifierAgent
            qualifier = QualifierAgent()
            await qualifier.process(
                phone=phone,
                owner_id=self.owner_id,
                message=message,
                message_id=message_id,
                media_type=media_type,
            )
        except Exception as e:
            logger.error("[AgentService] QualifierAgent falhou: %s", e, exc_info=True)
            # Nunca deixa o lead no silêncio — envia fallback natural
            try:
                from app.database import get_db
                from app.services.whatsapp import WhatsAppService
                _db = get_db()
                _owner_resp = (
                    _db.table("tenants")
                    .select("evolution_instance")
                    .eq("id", self.owner_id)
                    .limit(1)
                    .execute()
                )
                if _owner_resp.data:
                    _inst = (_owner_resp.data[0].get("evolution_instance") or "").strip()
                    if _inst:
                        _wa = WhatsAppService()
                        await _wa.send_message(
                            phone,
                            "Tive um probleminha aqui, pode repetir? 😊",
                            instance=_inst,
                        )
            except Exception as _fe:
                logger.warning("[AgentService] Fallback não enviado: %s", _fe)
            return {"status": "error", "error": str(e), "agent": agent_role}

        # 5. Pós-processamento — busca estado atualizado do banco
        try:
            updated_state = await self._get_customer_state(phone)
            new_score = updated_state.get("lead_score", lead_score)
            new_status = updated_state.get("lead_status", lead_status)
        except Exception:
            new_score = lead_score
            new_status = lead_status

        await self._post_process(
            agent_role=agent_role,
            phone=phone,
            owner_id=self.owner_id,
            new_score=new_score,
            new_status=new_status,
            old_score=lead_score,
            old_status=lead_status,
        )

        return {
            "status": "ok",
            "agent": agent_role,
            "lead_score": new_score,
            "lead_status": new_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ─────────────────────── roteamento ──────────────────────

    def _route(self, lead_status: str, lead_score: int) -> str:
        """Decide qual agente deve tratar a mensagem."""
        if lead_status == "cliente":
            return "consultant"
        if lead_score >= SCORE_CLOSER_THRESHOLD:
            return "closer"
        return "sdr"

    def _effective_mode(self, agent_role: str, original_mode: str) -> str:
        mode_map = {
            "sdr":        "qualifier",
            "closer":     "closer",
            "consultant": "qualifier",
        }
        return mode_map.get(agent_role, original_mode)

    # ─────────────────────── pós-processamento ───────────────

    async def _post_process(
        self,
        agent_role: str,
        phone: str,
        owner_id: str,
        new_score: int,
        new_status: str,
        old_score: int,
        old_status: str,
    ):
        try:
            import redis as redis_lib
            from app.agents.base import AgentContext
            from app.agents.registry import load_all_agents, get_agent
            from app.agents.message_bus import publish, Events

            load_all_agents()
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
            context = AgentContext(
                tenant_id=owner_id,
                triggered_by=agent_role,
                payload={"phone": phone, "owner_id": owner_id},
            )

            if agent_role == "sdr" and new_score >= SCORE_CLOSER_THRESHOLD and old_score < SCORE_CLOSER_THRESHOLD:
                logger.info("[AgentService] Lead %s passou para Closer (score %d→%d)",
                            phone[:5] + "***", old_score, new_score)
                await publish(r, "sdr", Events.LEAD_QUALIFIED, {
                    "phone": phone,
                    "owner_id": owner_id,
                    "lead_score": new_score,
                })

            if new_status == "cliente" and old_status != "cliente":
                logger.info("[AgentService] Lead %s virou CLIENTE — acionando Consultant",
                            phone[:5] + "***")
                consultant = get_agent("consultant")
                if consultant:
                    onboard_context = AgentContext(
                        tenant_id=owner_id,
                        triggered_by="closer",
                        payload={
                            "phone": phone,
                            "owner_id": owner_id,
                            "trigger": "new_client",
                        },
                    )
                    import asyncio
                    asyncio.create_task(consultant.act(onboard_context))

                await publish(r, "closer", Events.SALE_CLOSED, {
                    "phone": phone,
                    "owner_id": owner_id,
                    "lead_score": new_score,
                })

        except Exception as e:
            logger.warning("[AgentService] Pós-processamento falhou: %s", e)

    # ─────────────────────── helpers ─────────────────────────

    async def _get_customer_state(self, phone: str) -> dict:
        """Lê estado atual do lead/cliente no Supabase."""
        try:
            from app.database import get_db
            db = get_db()
            resp = db.table("customers")\
                .select("lead_status, lead_score")\
                .eq("phone", phone)\
                .eq("owner_id", self.owner_id)\
                .limit(1)\
                .execute()
            if resp.data:
                return resp.data[0]
        except Exception as e:
            logger.warning("[AgentService] Falha ao ler estado do cliente: %s", e)
        return {"lead_status": "qualificando", "lead_score": 0}
