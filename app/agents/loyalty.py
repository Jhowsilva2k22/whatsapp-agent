"""
EcoZap — Loyalty & Governance
==============================
Implementa a Lei 7: "Sempre leais ao CEO Joanderson."

Componentes:
- CEO_OVERRIDE: invariant de aprovação para ações críticas
- AuditLog: registro imutável de todas as decisões dos agentes
- Whitelist: o que cada agente pode fazer sozinho vs. precisa de aprovação
"""
from datetime import datetime, timezone
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# ─── Whitelist global de ações ────────────────────────────────────────────────

AUTONOMOUS_ACTIONS = {
    # Qualquer agente pode fazer sozinho
    "*": [
        "read_logs",
        "read_metrics",
        "generate_report",
        "send_telegram_alert",
        "health_check",
    ],
    # Por agente específico
    "sentinel": ["restart_service", "trigger_health_check"],
    "doctor":   ["generate_diagnosis", "create_incident"],
    "surgeon":  ["generate_patch", "create_pull_request"],
    "guardian": ["validate_backup", "skip_corrupted_backup"],
    "attendant":["send_message", "update_lead_score", "schedule_followup"],
}

CEO_OVERRIDE_REQUIRED = [
    "merge_to_main",
    "deploy_to_production",
    "alter_database_schema",
    "broadcast_to_all_customers",
    "delete_data",
    "change_pricing",
    "create_new_service",
    "modify_auth_config",
    "spend_above_quota",
    "execute_financial_transaction",
]


def can_act_autonomously(agent_role: str, action: str) -> bool:
    """Verifica se uma ação pode ser executada sem aprovação do CEO."""
    if action in CEO_OVERRIDE_REQUIRED:
        return False
    allowed = AUTONOMOUS_ACTIONS.get("*", []) + AUTONOMOUS_ACTIONS.get(agent_role, [])
    return action in allowed


def requires_override(action: str) -> bool:
    """Verifica se uma ação está na lista de CEO_OVERRIDE."""
    return action in CEO_OVERRIDE_REQUIRED


# ─── Audit Log ────────────────────────────────────────────────────────────────

class AuditLog:
    """
    Registro imutável de todas as decisões dos agentes.
    Salva em Supabase tabela agent_audit_log.
    """

    def __init__(self, db_client=None):
        self.db = db_client

    async def record(
        self,
        agent_role: str,
        action: str,
        context: dict,
        outcome: str,
        approved_by: Optional[str] = None,
        ceo_override: bool = False,
    ) -> dict:
        """Registra uma decisão no audit log."""
        entry = {
            "agent_role": agent_role,
            "action": action,
            "context": context,
            "outcome": outcome,
            "approved_by": approved_by or ("CEO_AUTO" if not ceo_override else "CEO_EXPLICIT"),
            "ceo_override_required": ceo_override,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if self.db:
            try:
                self.db.table("agent_audit_log").insert(entry).execute()
            except Exception as e:
                logger.error(f"[AuditLog] Falha ao salvar entrada: {e}")
                # Log local como fallback
                logger.info(f"[AuditLog] FALLBACK — {entry}")
        else:
            logger.info(f"[AuditLog] {agent_role} → {action} → {outcome}")

        return entry

    async def get_recent(self, limit: int = 20) -> list[dict]:
        """Retorna as entradas mais recentes do audit log."""
        if not self.db:
            return []
        try:
            resp = self.db.table("agent_audit_log")\
                .select("*")\
                .order("timestamp", desc=True)\
                .limit(limit)\
                .execute()
            return resp.data or []
        except Exception as e:
            logger.error(f"[AuditLog] Erro ao buscar entradas: {e}")
            return []


# ─── CEO Override Request ─────────────────────────────────────────────────────

def format_override_request(
    agent_role: str,
    action: str,
    reason: str,
    requested_by: str = None,
    incident_id: str = None,
    extra: dict = None,
    context: dict = None,
) -> str:
    """
    Formata pedido de aprovação do CEO em linguagem natural e clara.
    O CEO responde APROVADO:{incident_id} ou REJEITADO:{incident_id}.
    """
    agente_nome = {
        "surgeon": "Surgeon (agente de correções automáticas)",
        "sentinel": "Sentinel (agente de monitoramento)",
        "doctor": "Doctor (agente de diagnóstico)",
        "guardian": "Guardian (agente de backup)",
    }.get(requested_by or agent_role, agent_role)

    acao_humana = {
        "merge_to_main":            "aplicar a correção ao código principal e fazer o redeploy",
        "deploy_to_production":     "publicar uma nova versão em produção",
        "alter_database_schema":    "alterar a estrutura do banco de dados",
        "broadcast_to_all_customers": "enviar mensagem para todos os clientes",
        "delete_data":              "apagar dados do sistema",
        "change_pricing":           "alterar preços ou planos",
        "create_new_service":       "criar um novo serviço no servidor",
        "modify_auth_config":       "alterar configurações de segurança e autenticação",
        "spend_above_quota":        "realizar um gasto acima do limite configurado",
        "execute_financial_transaction": "executar uma transação financeira",
    }.get(action, action)

    incident_ref = f"#{incident_id}" if incident_id else ""
    codigo_aprovacao = f"APROVADO:{incident_id}" if incident_id else f"APROVADO:{action}"
    codigo_rejeicao = f"REJEITADO:{incident_id}" if incident_id else f"REJEITADO:{action}"

    # Info extra (arquivo corrigido, PR URL, etc.)
    extra_lines = ""
    if extra:
        if extra.get("arquivo_corrigido"):
            extra_lines += f"\n*Arquivo alterado:* `{extra['arquivo_corrigido']}`"
        if extra.get("pr_url"):
            extra_lines += f"\n*Ver a correção completa:* {extra['pr_url']}"

    return (
        f"🔐 *Sua aprovação é necessária {incident_ref}*\n\n"
        f"*Quem está pedindo:* {agente_nome}\n\n"
        f"*O que quer fazer:* {acao_humana}\n\n"
        f"*Por que:*\n{reason}"
        f"{extra_lines}\n\n"
        f"Se você *aprovar,* a ação será executada automaticamente — "
        f"sem precisar mexer em nada.\n"
        f"Se você *rejeitar,* tudo fica como está e o incidente é registrado para análise manual.\n\n"
        f"👉 *Para aprovar, responda:*\n`{codigo_aprovacao}`\n\n"
        f"👉 *Para rejeitar, responda:*\n`{codigo_rejeicao}`"
    )
