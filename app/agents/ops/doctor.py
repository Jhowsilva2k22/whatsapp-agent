"""
EcoZap — Doctor Agent (Sprint 1 — implementação real)
=======================================================
Papel: Diagnóstico de erros e incidentes.
Hierarquia: Especialista → OPS → CTO → CEO

Responsabilidades:
- Recebe anomalia do Sentinel via AgentContext
- Lê últimos erros do Redis (ops.py tracking)
- Classifica por padrão conhecido (banco, rede, código, config, recurso)
- Mapeia arquivo + linha via traceback se disponível
- Gera diagnóstico estruturado para o Surgeon
- Envia resumo Telegram com causa raiz

Opinion bias: "Científico e metódico. Não passa para o Surgeon sem causa raiz confirmada."
"""
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import redis as redis_lib

from app.agents.base import Agent, AgentContext, AgentOpinion, AuthorityLevel
from app.agents.registry import register
from app.agents.message_bus import publish, Events
from app.config import get_settings
from app.services.alerts import notify_owner

logger = logging.getLogger(__name__)
settings = get_settings()

OPS_PREFIX = "ops:"

# ──────────────────────── padrões conhecidos ────────────────────────
# Cada padrão: (regex, causa_raiz, severidade, fix_hint)
ERROR_PATTERNS = [
    (
        r"column .* does not exist|undefined_column|42703",
        "Coluna inexistente no banco (PostgreSQL 42703)",
        "critical",
        "Verificar nome das colunas em tasks.py. Rodar SELECT column_name FROM information_schema.columns WHERE table_name='nome_tabela' no Supabase.",
    ),
    (
        r"OperationalError|could not connect|connection refused|server closed|ECONNREFUSED",
        "Falha de conexão com banco/Redis/serviço externo",
        "critical",
        "Verificar variáveis de ambiente (URLs, portas). Checar status dos serviços no Railway e Supabase dashboard.",
    ),
    (
        r"TimeoutError|ReadTimeout|ConnectTimeout|timed out",
        "Timeout em chamada HTTP/banco",
        "warning",
        "Verificar latência da Evolution API ou Supabase. Considerar aumentar timeout ou adicionar retry.",
    ),
    (
        r"KeyError|AttributeError|NoneType.*has no attribute",
        "Erro de atributo/chave — dado esperado ausente",
        "warning",
        "Verificar se estrutura de dados de resposta da API mudou. Adicionar .get() com fallback.",
    ),
    (
        r"JSONDecodeError|json.decoder|Expecting value",
        "Resposta não-JSON onde se esperava JSON",
        "warning",
        "Verificar resposta raw da API. Possível mudança de contrato ou erro de parsing.",
    ),
    (
        r"rate limit|RateLimitError|429|too many requests",
        "Rate limit atingido em API externa",
        "warning",
        "Implementar exponential backoff. Verificar uso da OpenAI/Evolution API no período.",
    ),
    (
        r"OOM|MemoryError|Cannot allocate|out of memory",
        "Falta de memória",
        "critical",
        "Verificar consumo de memória no Railway. Considerar otimizar queries ou aumentar plano.",
    ),
    (
        r"SyntaxError|IndentationError|NameError",
        "Erro de sintaxe Python — deploy com código inválido",
        "critical",
        "Verificar último commit. Rodar python -m py_compile no arquivo afetado.",
    ),
    (
        r"ImportError|ModuleNotFoundError",
        "Módulo Python não encontrado — dependência faltando",
        "critical",
        "Verificar requirements.txt. Possivelmente nova lib não instalada.",
    ),
    (
        r"duplicate key|UniqueViolation|23505",
        "Violação de chave única no banco",
        "warning",
        "Verificar upsert em vez de insert, ou verificar lógica de deduplicação.",
    ),
]

FILE_PATTERN = re.compile(r'File "([^"]+)", line (\d+)')


@register
class Doctor(Agent):
    role = "doctor"
    display_name = "Doctor"
    authority_level = AuthorityLevel.SPECIALIST
    department = "ops"
    opinion_bias = "científico e metódico — não passa adiante sem causa raiz confirmada"

    autonomous_actions = [
        "read_logs",
        "access_supabase_logs",
        "access_railway_logs",
        "create_incident",
        "generate_diagnosis",
    ]
    requires_ceo_override = [
        "deploy_to_production",
        "merge_to_main",
        "alter_database_schema",
    ]

    async def act(self, context: AgentContext) -> dict:
        """
        Diagnostica um incidente recebido do Sentinel.
        Retorna diagnóstico estruturado para o Surgeon.
        """
        anomaly_payload = context.payload.get("anomaly", context.payload)
        incident_id = context.incident_id or str(uuid.uuid4())[:8]

        logger.info("[Doctor] Iniciando diagnóstico — incidente %s", incident_id)

        diagnosis = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "incident_id": incident_id,
            "anomalies_received": anomaly_payload.get("anomalies", []),
            "root_cause": None,
            "root_cause_type": None,
            "affected_files": [],
            "severity": "unknown",
            "confidence": 0.0,
            "fix_hint": None,
            "raw_errors": [],
            "needs_ceo_override": False,
            "ready_for_surgeon": False,
        }

        # 1. Coleta erros do Redis
        raw_errors = self._collect_errors_from_redis()
        diagnosis["raw_errors"] = raw_errors

        # 2. Adiciona erros dos anomaly events do Sentinel
        sentinel_anomalies = anomaly_payload.get("anomalies", [])
        for a in sentinel_anomalies:
            if a.get("type") in ("circuit_breaker_open", "circuit_near_open", "error_count_high"):
                task = a.get("task", "")
                last_err = self._get_last_error_redis(task)
                if last_err:
                    raw_errors.append(last_err)

        # 3. Analisa padrões de erro
        if raw_errors:
            best_match = self._classify_errors(raw_errors)
            if best_match:
                diagnosis["root_cause"] = best_match["root_cause"]
                diagnosis["root_cause_type"] = best_match["pattern"]
                diagnosis["severity"] = best_match["severity"]
                diagnosis["fix_hint"] = best_match["fix_hint"]
                diagnosis["confidence"] = best_match["confidence"]

        # Se não achou via Redis, usa anomalias do Sentinel diretamente
        if not diagnosis["root_cause"] and sentinel_anomalies:
            worst = sorted(
                sentinel_anomalies,
                key=lambda a: 0 if a.get("severity") == "critical" else 1
            )
            if worst:
                a = worst[0]
                diagnosis["root_cause"] = a.get("message", "Anomalia não classificada")
                diagnosis["severity"] = a.get("severity", "warning")
                diagnosis["confidence"] = 0.5

        # 4. Extrai arquivos/linhas do traceback se disponível
        for err in raw_errors:
            tb = err.get("traceback", "") or err.get("message", "")
            files = self._extract_files_from_traceback(tb)
            for f in files:
                if f not in diagnosis["affected_files"]:
                    diagnosis["affected_files"].append(f)

        # 5. Decide se passa para Surgeon
        if diagnosis["root_cause"] and diagnosis["confidence"] >= 0.6:
            diagnosis["ready_for_surgeon"] = True
            if diagnosis["severity"] == "critical":
                diagnosis["needs_ceo_override"] = True  # cirurgia crítica = CEO aprova

        logger.info(
            "[Doctor] Diagnóstico: '%s' (severidade=%s, confiança=%.0f%%, surgeon=%s)",
            diagnosis["root_cause"],
            diagnosis["severity"],
            diagnosis["confidence"] * 100,
            diagnosis["ready_for_surgeon"],
        )

        # 6. Publica diagnóstico no message bus
        try:
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
            await publish(r, self.role, Events.DIAGNOSIS_READY, {
                "incident_id": incident_id,
                "diagnosis": diagnosis,
                "tenant_id": context.tenant_id,
            })
        except Exception as e:
            logger.warning("[Doctor] Falha ao publicar diagnóstico: %s", e)

        # 7. Alerta Telegram com linguagem natural
        if diagnosis["root_cause"]:
            try:
                icon = "🩺" if diagnosis["severity"] != "critical" else "🚨"
                confianca = int(diagnosis["confidence"] * 100)

                # Tradução humana da causa raiz
                causa_humana = self._humanize_root_cause(
                    diagnosis["root_cause"],
                    diagnosis.get("root_cause_type", ""),
                )
                proximos_passos = self._humanize_next_steps(diagnosis)

                msg_parts = [
                    f"{icon} *Doctor — Diagnóstico #{incident_id}*\n",
                    f"*O que encontrei:*\n{causa_humana}\n",
                ]

                if diagnosis["affected_files"]:
                    files_str = ", ".join(
                        f"`{f['file'].split('/')[-1]}` (linha {f['line']})"
                        for f in diagnosis["affected_files"][:2]
                    )
                    msg_parts.append(f"*Onde está o problema:* {files_str}\n")

                if diagnosis["fix_hint"]:
                    msg_parts.append(f"*Como corrigir:*\n{diagnosis['fix_hint'][:250]}\n")

                msg_parts.append(f"*O que acontece agora:*\n{proximos_passos}")
                msg_parts.append(f"\n_Certeza do diagnóstico: {confianca}%_")

                notify_owner("\n".join(msg_parts),
                             level="error" if diagnosis["severity"] == "critical" else "warn")
            except Exception as e:
                logger.warning("[Doctor] Falha ao notificar Telegram: %s", e)

        return diagnosis

    # ──────────────────── helpers ────────────────────────────

    def _collect_errors_from_redis(self) -> list:
        """Coleta últimos erros de todas as tasks do Redis."""
        errors = []
        try:
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
            keys = r.keys(f"{OPS_PREFIX}last_error:*")
            for key in keys:
                try:
                    data = json.loads(r.get(key) or "{}")
                    if data:
                        data["task"] = key.replace(f"{OPS_PREFIX}last_error:", "")
                        errors.append(data)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("[Doctor] Falha ao ler erros do Redis: %s", e)
        return errors

    def _get_last_error_redis(self, task_name: str) -> Optional[dict]:
        """Lê último erro de uma task específica."""
        try:
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
            data = r.get(f"{OPS_PREFIX}last_error:{task_name}")
            if data:
                return json.loads(data)
        except Exception:
            pass
        return None

    def _classify_errors(self, errors: list) -> Optional[dict]:
        """Classifica erros contra padrões conhecidos. Retorna melhor match."""
        best = None
        for err in errors:
            text = (
                str(err.get("message", "")) + " " +
                str(err.get("traceback", "")) + " " +
                str(err.get("type", ""))
            ).lower()

            for pattern, root_cause, severity, fix_hint in ERROR_PATTERNS:
                if re.search(pattern, text, re.IGNORECASE):
                    confidence = 0.9 if severity == "critical" else 0.75
                    candidate = {
                        "pattern": pattern[:50],
                        "root_cause": root_cause,
                        "severity": severity,
                        "fix_hint": fix_hint,
                        "confidence": confidence,
                        "matched_error": err.get("message", "")[:200],
                    }
                    # Prefere críticos
                    if best is None or (severity == "critical" and best["severity"] != "critical"):
                        best = candidate

        return best

    def _humanize_root_cause(self, root_cause: str, pattern: str) -> str:
        """Traduz causa técnica para linguagem que qualquer pessoa entende."""
        translations = {
            "42703":           "O sistema tentou buscar uma informação que não existe no banco de dados. É como pedir uma coluna de uma tabela que foi renomeada ou removida.",
            "undefined_column":"O sistema tentou buscar uma informação que não existe no banco de dados.",
            "OperationalError":"O sistema perdeu a conexão com o banco de dados ou com um serviço externo. É como a internet caindo no meio de uma ligação.",
            "connection refused":"Um dos serviços internos recusou a conexão — provavelmente está fora do ar ou reiniciando.",
            "TimeoutError":    "Uma parte do sistema demorou demais para responder e o processo foi interrompido. Como ficar esperando uma resposta que não vem.",
            "KeyError":        "O sistema esperava receber uma informação específica, mas ela não veio — pode ser que o formato de resposta de uma API mudou.",
            "JSONDecodeError": "O sistema recebeu uma resposta que não estava no formato esperado — como receber um texto quando esperava uma planilha.",
            "rate limit":      "Atingimos o limite de chamadas permitidas em uma API externa por um período. O sistema está fazendo muitas requisições seguidas.",
            "MemoryError":     "O servidor ficou sem memória para processar as tarefas. É como um computador travando por ter muitos programas abertos.",
            "SyntaxError":     "Um erro de código foi publicado em produção — há um trecho de código com escrita incorreta.",
            "ImportError":     "O sistema não encontrou um módulo ou biblioteca necessária para funcionar.",
            "duplicate key":   "O sistema tentou salvar uma informação que já existe no banco de dados.",
        }

        # Busca match parcial
        root_lower = root_cause.lower()
        pattern_lower = pattern.lower()
        for key, human in translations.items():
            if key.lower() in root_lower or key.lower() in pattern_lower:
                return human

        # Fallback: usa a causa original com uma introdução
        return f"O sistema identificou o seguinte problema: {root_cause}"

    def _humanize_next_steps(self, diagnosis: dict) -> str:
        """Explica o que acontece depois do diagnóstico."""
        if diagnosis["ready_for_surgeon"]:
            if diagnosis["needs_ceo_override"]:
                return (
                    "O Surgeon já está preparando uma correção automática. "
                    "Quando estiver pronta, você receberá um pedido de aprovação no Telegram. "
                    "É só responder APROVADO ou REJEITADO — sem precisar mexer em nenhum código."
                )
            else:
                return (
                    "O Surgeon vai preparar e enviar uma correção. "
                    "Você receberá um aviso quando o Pull Request (proposta de correção) estiver pronto para revisão."
                )
        else:
            return (
                "O problema foi identificado mas não é possível corrigir automaticamente com segurança. "
                "Você precisará verificar manualmente ou acionar um desenvolvedor. "
                "Todas as informações estão registradas para facilitar a análise."
            )

    def _extract_files_from_traceback(self, traceback_text: str) -> list:
        """Extrai pares (arquivo, linha) do traceback Python."""
        files = []
        if not traceback_text:
            return files
        for match in FILE_PATTERN.finditer(traceback_text):
            fp = match.group(1)
            line = int(match.group(2))
            # Só arquivos do projeto (app/)
            if "app/" in fp or "whatsapp" in fp or "ecozap" in fp:
                files.append({"file": fp, "line": line})
        return files[-3:]  # máx 3 arquivos mais próximos do erro

    async def report_status(self) -> dict:
        """Status rápido para reunião."""
        try:
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
            keys = r.keys(f"{OPS_PREFIX}last_error:*")
            tasks_with_errors = [k.replace(f"{OPS_PREFIX}last_error:", "") for k in keys]
        except Exception:
            tasks_with_errors = []

        return {
            "role": self.role,
            "status": "operational",
            "tasks_with_known_errors": tasks_with_errors,
            "summary": (
                f"Diagnóstico sob demanda. {len(tasks_with_errors)} task(s) com histórico de erro."
                if tasks_with_errors else
                "Diagnóstico sob demanda. Sem histórico de erros no Redis."
            ),
        }

    def opine(self, question: str, context: AgentContext) -> AgentOpinion:
        """
        Científico e metódico. Sempre exige causa raiz antes de escalar.
        Quando convocado com tenant_id, verifica aprendizados do KB para
        correlacionar incidentes com contexto de negócio.
        """
        # Consulta KB quando há contexto de tenant (reuniões de conselho)
        kb_insight = ""
        if context.tenant_id:
            try:
                from app.services.knowledge import KnowledgeBank
                kb = KnowledgeBank()
                recent = kb._get_recent_learnings(context.tenant_id, limit=1)
                if recent:
                    kb_insight = (
                        f" KB do tenant consultado — contexto de negócio disponível "
                        f"para correlação com incidentes técnicos."
                    )
            except Exception:
                pass

        return AgentOpinion(
            agent_role=self.role,
            agrees=True,
            reasoning=(
                f"[{self.display_name}] Qualquer mudança crítica deve ter "
                f"estado diagnosticado antes e validação de logs depois. "
                f"Sempre confirmo causa raiz antes de escalar para Surgeon."
                + kb_insight
            ),
        )
