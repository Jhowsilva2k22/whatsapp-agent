"""
EcoZap — SDR Agent (Sprint 2)
==============================
Papel: Primeiro contato. Constrói relacionamento genuíno e qualifica leads.
Hierarquia: Especialista → COMMERCIAL → CTO → CEO

Filosofia do SDR EcoZap:
  Não é um vendedor. É um ser humano que genuinamente quer entender o outro.
  A venda é consequência — nunca o discurso principal.
  Ele lê o momento, identifica a temperatura emocional, cria vínculo real.
  Usa psicologia comportamental avançada: espelhamento, ancoragem emocional,
  reciprocidade, prova social, escassez percebida — sempre de forma orgânica.
  O lead nunca deve sentir que está sendo vendido. Ele deve sentir que encontrou
  alguém que realmente o entende.

Pipeline de temperatura:
  ❄ FRIO  → Curioso. Ainda não reconhece o problema. Modo: conexão, escuta, curiosidade.
  🌡 MORNO → Reconhece o problema. Explorando opções. Modo: educação, construção de valor.
  🔥 QUENTE → Quer resolver. Só precisa de segurança. Modo: prova, urgência suave, facilitação.

Responsabilidades:
  - Captura leads novos e entende de onde vieram
  - Identifica a temperatura emocional antes de qualquer coisa
  - Constrói relacionamento antes de falar de produto
  - Detecta score >= 50 e passa ao Closer de forma natural
  - Descarta leads fora do perfil com respeito e leveza

Opinion bias: "Quem compra de amigo não sente que está comprando."
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

SCORE_THRESHOLD_CLOSER = 50   # acima disso → passa para o Closer

# ── Estágios de temperatura ──────────────────────────────────────────────────
TEMPERATURA_FRIO  = "frio"    # score 0–19
TEMPERATURA_MORNO = "morno"   # score 20–49
TEMPERATURA_QUENTE = "quente" # score >= 50 → Closer

# ── Sinais de desconforto (o lead está incomodado ou com pressa) ─────────────
SINAIS_DESCONFORTO = [
    "para de", "chega", "tô ocupado", "não tenho tempo",
    "não me interessa", "deixa pra lá", "me tira da lista",
    "sai", "para", "não quero"
]

# ── Gatilhos de conexão — tópicos que revelam o que a pessoa valoriza ────────
GATILHOS_CONEXAO = {
    "familia":    ["filh", "esposa", "marido", "família", "pai", "mãe", "minha casa"],
    "dinheiro":   ["fatura", "preço", "caro", "barato", "economiz", "gasto", "conta"],
    "tempo":      ["tempo", "prazo", "urgente", "rápido", "hoje", "agora"],
    "segurança":  ["garantia", "seguro", "confiáv", "certeza", "risco"],
    "status":     ["melhor", "profissional", "qualidade", "premium", "diferente"],
    "dor":        ["problema", "dificuldade", "estresse", "não tô conseguindo", "difícil"],
}


@register
class SDR(Agent):
    role = "sdr"
    display_name = "SDR"
    authority_level = AuthorityLevel.SPECIALIST
    department = "commercial"
    opinion_bias = "quem compra de amigo não sente que está comprando — relacionamento primeiro, venda depois"

    autonomous_actions = [
        "send_message",
        "update_lead_score",
        "update_lead_status",
        "schedule_followup",
        "read_knowledge_base",
        "detect_channel",
        "detect_temperature",
        "build_rapport",
    ]
    requires_ceo_override = [
        "broadcast_to_all_customers",
        "delete_customer",
        "change_pricing",
    ]

    async def act(self, context: AgentContext) -> dict:
        """
        Processa mensagem de lead em qualificação.
        Abordagem: relacionamento genuíno antes de qualquer coisa comercial.
        """
        phone = context.payload.get("phone", "")
        owner_id = context.payload.get("owner_id", "")
        message = context.payload.get("message", "")
        current_score = context.payload.get("lead_score", 0)

        temperatura = self._detectar_temperatura(current_score)
        conexoes = self._detectar_conexoes(message)
        desconforto = self._detectar_desconforto(message)

        logger.info(
            "[SDR] Lead %s | score=%d | temp=%s | conexões=%s",
            phone[:5] + "***", current_score, temperatura, conexoes or "nenhuma"
        )

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phone": phone,
            "owner_id": owner_id,
            "action": "building_relationship",
            "temperatura": temperatura,
            "conexoes_detectadas": conexoes,
            "score_before": current_score,
            "score_after": current_score,
            "ready_for_closer": False,
            "disqualified": False,
        }

        # Lead sinalizou desconforto → pausa, respeita o espaço
        if desconforto:
            result["action"] = "cooling_down"
            logger.info("[SDR] Lead %s sinalizou desconforto — recuando", phone[:5] + "***")
            return result

        # Processa via QualifierAgent (motor de conversa)
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
            new_status = process_result.get("lead_status", "qualificando")
            result["score_after"] = new_score
            result["lead_status"] = new_status
            result["temperatura"] = self._detectar_temperatura(new_score)

        except Exception as e:
            logger.warning("[SDR] Falha ao processar via Qualifier: %s", e)
            new_score = current_score
            new_status = "qualificando"

        # Lead atingiu temperatura quente → passa para o Closer
        if new_score >= SCORE_THRESHOLD_CLOSER:
            result["ready_for_closer"] = True
            result["action"] = "handoff_to_closer"

            logger.info(
                "[SDR] Lead %s amadureceu (score %d) → passando para Closer",
                phone[:5] + "***", new_score
            )

            # Publica evento para o Closer
            try:
                import redis as redis_lib
                r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
                await publish(r, self.role, Events.LEAD_QUALIFIED, {
                    "phone": phone,
                    "owner_id": owner_id,
                    "lead_score": new_score,
                    "temperatura": TEMPERATURA_QUENTE,
                    "conexoes": conexoes,
                    "tenant_id": context.tenant_id,
                })
            except Exception as e:
                logger.warning("[SDR] Falha ao publicar LEAD_QUALIFIED: %s", e)

            # Notifica owner com contexto emocional do lead
            try:
                conexoes_str = ", ".join(conexoes) if conexoes else "—"
                notify_owner(
                    f"🔥 *Lead Amadureceu — Momento de fechar!*\n\n"
                    f"Um lead que começou frio chegou ao ponto de compra naturalmente.\n\n"
                    f"*Pontuação:* {new_score}/100\n"
                    f"*O que ele valoriza:* {conexoes_str}\n"
                    f"*O que acontece agora:* O atendente entra em modo de fechamento — "
                    f"vai apresentar a oferta no momento certo, sem forçar.",
                    level="info",
                )
            except Exception:
                pass

        return result

    # ──────────────────── helpers de psicologia ──────────────────

    def _detectar_temperatura(self, score: int) -> str:
        """Mapeia score numérico para temperatura emocional."""
        if score >= SCORE_THRESHOLD_CLOSER:
            return TEMPERATURA_QUENTE
        if score >= 20:
            return TEMPERATURA_MORNO
        return TEMPERATURA_FRIO

    def _detectar_conexoes(self, message: str) -> list:
        """
        Detecta gatilhos de conexão na mensagem.
        Esses são os valores e dores reais do lead — usados pelo atendente
        para criar ancoragem emocional sem forçar.
        """
        msg_lower = message.lower()
        detectados = []
        for categoria, palavras in GATILHOS_CONEXAO.items():
            if any(p in msg_lower for p in palavras):
                detectados.append(categoria)
        return detectados

    def _detectar_desconforto(self, message: str) -> bool:
        """Detecta se o lead está com desconforto ou querendo sair da conversa."""
        msg_lower = message.lower()
        return any(sinal in msg_lower for sinal in SINAIS_DESCONFORTO)

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
                f"{count} lead(s) em construção de relacionamento agora."
                if count else
                "Nenhum lead em qualificação no momento."
            ),
        }

    def opine(self, question: str, context: AgentContext) -> AgentOpinion:
        lead_keywords = ["lead", "qualific", "captação", "novo cliente", "funil", "abordagem"]
        if any(kw in question.lower() for kw in lead_keywords):
            return AgentOpinion(
                agent_role=self.role,
                agrees=True,
                reasoning=(
                    f"[{self.display_name}] O funil começa com conexão humana. "
                    f"Qualquer mudança na abordagem deve ser testada com leads reais — "
                    f"temperatura emocional não se mede em A/B test, se mede em conversa. "
                    f"Score mínimo para Closer: {SCORE_THRESHOLD_CLOSER}."
                ),
            )
        return AgentOpinion(
            agent_role=self.role,
            agrees=True,
            reasoning=f"[{self.display_name}] Sem impacto no pipeline de qualificação.",
        )
