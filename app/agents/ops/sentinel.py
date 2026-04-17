"""
EcoZap — Sentinel Agent (Sprint 1 — implementação real)
=========================================================
Papel: Monitoramento 24/7 da infraestrutura.
Hierarquia: Especialista → OPS → CTO → CEO

Responsabilidades:
- Verifica /health endpoint (tempo de resposta + status)
- Lê contadores de erro do Redis (ops.py tracking)
- Verifica backlog de filas Celery no Redis
- Verifica circuit breakers abertos
- Publica eventos no message bus
- Alerta Telegram em caso de anomalia

Opinion bias: "Paranoico com estabilidade. Prefiro falso alarme a susto real."

Env vars opcionais:
  RAILWAY_API_TOKEN: para enriquecer logs com dados do Railway
  APP_URL: URL base da aplicação (para health check HTTP)
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import redis as redis_lib

from app.agents.base import Agent, AgentContext, AgentOpinion, AuthorityLevel
from app.agents.registry import register
from app.agents.loyalty import can_act_autonomously
from app.agents.message_bus import publish, Events
from app.config import get_settings
from app.services.alerts import notify_owner

logger = logging.getLogger(__name__)
settings = get_settings()

# ──────────────────────── constantes ────────────────────────
HEALTH_TIMEOUT_MS = 3000          # > 3s = degradado
HEALTH_CRITICAL_MS = 8000         # > 8s = crítico
CELERY_QUEUE_WARN = 50            # > 50 jobs = alerta
CELERY_QUEUE_CRITICAL = 200       # > 200 = crítico
ERROR_COUNT_WARN = 3              # 3 erros consecutivos = alerta
ERROR_COUNT_CRITICAL = 5          # 5 erros = circuit aberto
OPS_PREFIX = "ops:"               # prefixo Redis do ops.py


# ────────────────────────────── agente ──────────────────────
@register
class Sentinel(Agent):
    role = "sentinel"
    display_name = "Sentinel"
    authority_level = AuthorityLevel.SPECIALIST
    department = "ops"
    opinion_bias = "paranoico com estabilidade — prefere falso alarme a susto real"

    autonomous_actions = [
        "read_logs",
        "trigger_health_check",
        "send_telegram_alert",
        "publish_anomaly_event",
    ]
    requires_ceo_override = [
        "restart_service",
        "deploy_to_production",
        "merge_to_main",
    ]

    # ─────────────────────────── act ──────────────────────────
    async def act(self, context: AgentContext) -> dict:
        """
        Executa ciclo de monitoramento.
        Chamado pelo Celery Beat a cada 5 minutos.
        """
        ts = datetime.now(timezone.utc).isoformat()
        logger.info("[Sentinel] Iniciando ciclo de monitoramento...")

        findings = {
            "timestamp": ts,
            "anomalies": [],
            "checks": {},
            "status": "healthy",
        }

        # Roda todos os checks em paralelo
        results = await asyncio.gather(
            self._check_health_endpoint(),
            self._check_redis_errors(),
            self._check_celery_queues(),
            self._check_circuit_breakers(),
            return_exceptions=True,
        )

        check_names = ["health_endpoint", "redis_errors", "celery_queues", "circuit_breakers"]
        for name, result in zip(check_names, results):
            if isinstance(result, Exception):
                logger.warning("[Sentinel] Check '%s' falhou com exceção: %s", name, result)
                findings["checks"][name] = {"status": "check_failed", "error": str(result)}
            else:
                findings["checks"][name] = result
                if result.get("anomalies"):
                    findings["anomalies"].extend(result["anomalies"])

        # Classifica severidade geral
        critical = [a for a in findings["anomalies"] if a.get("severity") == "critical"]
        warnings = [a for a in findings["anomalies"] if a.get("severity") == "warning"]

        if critical:
            findings["status"] = "critical"
        elif warnings:
            findings["status"] = "degraded"
        else:
            findings["status"] = "healthy"

        # Publica no message bus e alerta Telegram se necessário
        if findings["anomalies"]:
            await self._handle_anomalies(findings, context)
        else:
            logger.info("[Sentinel] ✅ Sistema saudável.")

        return findings

    # ─────────────────────── checks ───────────────────────────

    async def _check_health_endpoint(self) -> dict:
        """Verifica /health: tempo de resposta + componentes."""
        app_url = settings.app_url.rstrip("/")
        check = {"status": "ok", "anomalies": [], "response_ms": None}

        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{app_url}/health")
            elapsed_ms = int((time.monotonic() - start) * 1000)
            check["response_ms"] = elapsed_ms
            check["http_status"] = resp.status_code

            if resp.status_code >= 500:
                check["anomalies"].append({
                    "type": "health_endpoint_error",
                    "severity": "critical",
                    "message": f"/health retornou HTTP {resp.status_code}",
                    "value": resp.status_code,
                })
            elif elapsed_ms > HEALTH_CRITICAL_MS:
                check["anomalies"].append({
                    "type": "response_time_critical",
                    "severity": "critical",
                    "message": f"Tempo de resposta crítico: {elapsed_ms}ms (limite: {HEALTH_CRITICAL_MS}ms)",
                    "value": elapsed_ms,
                })
            elif elapsed_ms > HEALTH_TIMEOUT_MS:
                check["anomalies"].append({
                    "type": "response_time_slow",
                    "severity": "warning",
                    "message": f"Resposta lenta: {elapsed_ms}ms (limite: {HEALTH_TIMEOUT_MS}ms)",
                    "value": elapsed_ms,
                })

            # Inspeciona componentes se a resposta for JSON
            try:
                data = resp.json()
                overall = data.get("status", "").lower()
                if overall not in ("healthy", "ok", ""):
                    check["anomalies"].append({
                        "type": "health_degraded",
                        "severity": "warning",
                        "message": f"Health reportou status: {overall}",
                        "components": data.get("components", {}),
                    })
            except Exception:
                pass

        except httpx.TimeoutException:
            check["anomalies"].append({
                "type": "health_timeout",
                "severity": "critical",
                "message": f"/health não respondeu em 10s — possível crash",
            })
        except Exception as e:
            check["anomalies"].append({
                "type": "health_unreachable",
                "severity": "critical",
                "message": f"/health inacessível: {e}",
            })

        return check

    async def _check_redis_errors(self) -> dict:
        """Lê contadores de erro do Redis (rastreados pelo ops.py)."""
        check = {"status": "ok", "anomalies": [], "error_counts": {}}
        try:
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)

            # Busca todos os contadores de erros consecutivos
            keys = r.keys(f"{OPS_PREFIX}err_count:*")
            for key in keys:
                task_name = key.replace(f"{OPS_PREFIX}err_count:", "")
                try:
                    count = int(r.get(key) or 0)
                    check["error_counts"][task_name] = count

                    if count >= ERROR_COUNT_CRITICAL:
                        check["anomalies"].append({
                            "type": "circuit_near_open",
                            "severity": "critical",
                            "message": f"Task '{task_name}' com {count} erros consecutivos — circuit breaker iminente",
                            "task": task_name,
                            "count": count,
                        })
                    elif count >= ERROR_COUNT_WARN:
                        check["anomalies"].append({
                            "type": "error_count_high",
                            "severity": "warning",
                            "message": f"Task '{task_name}' com {count} erros consecutivos",
                            "task": task_name,
                            "count": count,
                        })
                except (ValueError, TypeError):
                    pass

            # Verifica último erro de cada task para detalhes
            last_error_keys = r.keys(f"{OPS_PREFIX}last_error:*")
            for key in last_error_keys:
                try:
                    err_data = json.loads(r.get(key) or "{}")
                    task_name = key.replace(f"{OPS_PREFIX}last_error:", "")
                    if task_name not in check["error_counts"]:
                        check["error_counts"][f"{task_name}_last"] = err_data
                except Exception:
                    pass

        except Exception as e:
            check["anomalies"].append({
                "type": "redis_check_failed",
                "severity": "warning",
                "message": f"Não foi possível ler Redis: {e}",
            })

        return check

    async def _check_celery_queues(self) -> dict:
        """Verifica tamanho das filas Celery no Redis."""
        check = {"status": "ok", "anomalies": [], "queue_lengths": {}}
        try:
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)

            celery_queues = ["celery", "celery.default", "ecozap"]
            for queue in celery_queues:
                length = r.llen(queue)
                if length > 0:
                    check["queue_lengths"][queue] = length

                    if length >= CELERY_QUEUE_CRITICAL:
                        check["anomalies"].append({
                            "type": "celery_queue_critical",
                            "severity": "critical",
                            "message": f"Fila '{queue}' com {length} jobs pendentes — possível worker travado",
                            "queue": queue,
                            "length": length,
                        })
                    elif length >= CELERY_QUEUE_WARN:
                        check["anomalies"].append({
                            "type": "celery_queue_high",
                            "severity": "warning",
                            "message": f"Fila '{queue}' com {length} jobs — monitorar",
                            "queue": queue,
                            "length": length,
                        })

        except Exception as e:
            check["anomalies"].append({
                "type": "celery_check_failed",
                "severity": "warning",
                "message": f"Não foi possível checar filas Celery: {e}",
            })

        return check

    async def _check_circuit_breakers(self) -> dict:
        """Verifica quais circuit breakers estão abertos."""
        check = {"status": "ok", "anomalies": [], "open_circuits": []}
        try:
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
            circuit_keys = r.keys(f"{OPS_PREFIX}circuit:*")

            for key in circuit_keys:
                task_name = key.replace(f"{OPS_PREFIX}circuit:", "")
                try:
                    err_data = json.loads(r.get(key) or "{}")
                    ttl = r.ttl(key)
                    check["open_circuits"].append({
                        "task": task_name,
                        "ttl_seconds": ttl,
                        "error": err_data.get("message", "?")[:200],
                    })
                    check["anomalies"].append({
                        "type": "circuit_breaker_open",
                        "severity": "critical",
                        "message": (
                            f"Circuit breaker ABERTO: '{task_name}' — "
                            f"reativa em {ttl // 60}min"
                        ),
                        "task": task_name,
                        "ttl": ttl,
                    })
                except Exception:
                    pass

        except Exception as e:
            check["anomalies"].append({
                "type": "circuit_check_failed",
                "severity": "warning",
                "message": f"Não foi possível checar circuit breakers: {e}",
            })

        return check

    # ──────────────────── handle anomalias ────────────────────

    # ────────────── tradução humana dos tipos de anomalia ─────────────
    _HUMAN_MESSAGES = {
        "health_timeout":        ("o sistema não respondeu em 10 segundos — pode ter travado ou caído",
                                  "O Doctor já foi acionado para investigar. Se confirmar, o Surgeon vai preparar uma correção."),
        "health_endpoint_error": ("o sistema retornou um erro grave quando testamos se estava funcionando",
                                  "O Doctor vai analisar os logs agora. Você receberá o diagnóstico em breve."),
        "health_unreachable":    ("não conseguimos nem acessar o sistema — ele pode estar fora do ar",
                                  "O Doctor foi acionado. Se o problema persistir, você receberá um alerta para verificar o Railway."),
        "response_time_critical":("o sistema está respondendo muito devagar — clientes podem estar esperando mais de 8 segundos",
                                  "O Sentinel continuará monitorando. Se piorar, o Doctor entra em ação."),
        "response_time_slow":    ("o sistema está um pouco lento — tempo de resposta acima do normal",
                                  "Monitorando. Por enquanto não requer ação."),
        "health_degraded":       ("o sistema sinalizou que algum componente interno não está 100%",
                                  "O Doctor vai verificar qual parte específica está com problema."),
        "circuit_near_open":     ("uma tarefa automática falhou várias vezes seguidas e está prestes a ser pausada",
                                  "O Doctor vai identificar a causa. Se houver correção automática possível, o Surgeon prepara um PR."),
        "circuit_breaker_open":  ("uma tarefa automática falhou tantas vezes que o sistema a pausou por segurança",
                                  "O sistema está protegido, mas essa tarefa não está rodando. O Doctor já foi acionado para diagnosticar."),
        "error_count_high":      ("uma tarefa está tendo erros repetidos — ainda não foi pausada, mas está no limite",
                                  "Monitorando de perto. Se continuar, o Doctor entra em ação automaticamente."),
        "celery_queue_critical": ("há muitas tarefas acumuladas esperando para executar — parece que o worker travou",
                                  "O Doctor foi acionado para investigar se o worker precisa ser reiniciado."),
        "celery_queue_high":     ("as filas de tarefas automáticas estão com mais trabalho do que o normal",
                                  "Monitorando. Pode ser pico de uso. Se continuar crescendo, receberá outro alerta."),
        "redis_check_failed":    ("não conseguimos acessar a memória interna do sistema (Redis)",
                                  "O Doctor foi acionado. Redis é crítico — sem ele várias funções param."),
        "celery_check_failed":   ("não conseguimos verificar as filas de tarefas automáticas",
                                  "Verificação incompleta. Monitorando na próxima rodada."),
    }

    def _humanize_anomaly(self, anomaly: dict) -> tuple:
        """Retorna (problema_humano, solucao_humana) para um tipo de anomalia."""
        tipo = anomaly.get("type", "")
        default_problema = anomaly.get("message", "algo inesperado aconteceu no sistema")
        default_solucao = "O Doctor foi acionado para investigar."
        return self._HUMAN_MESSAGES.get(tipo, (default_problema, default_solucao))

    async def _handle_anomalies(self, findings: dict, context: AgentContext):
        """Publica evento e envia alerta Telegram com linguagem natural."""
        anomalies = findings["anomalies"]
        critical = [a for a in anomalies if a.get("severity") == "critical"]
        warnings = [a for a in anomalies if a.get("severity") == "warning"]

        logger.warning("[Sentinel] %d anomalia(s) detectada(s) (%d crítica(s))",
                       len(anomalies), len(critical))

        # Publica no message bus para Doctor
        try:
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
            await publish(r, self.role, Events.ANOMALY_DETECTED, {
                "anomalies": anomalies,
                "status": findings["status"],
                "timestamp": findings["timestamp"],
                "tenant_id": context.tenant_id,
            })
        except Exception as e:
            logger.warning("[Sentinel] Falha ao publicar no message bus: %s", e)

        # Alerta Telegram com linguagem natural
        try:
            if critical:
                icone = "🚨"
                titulo = "PROBLEMA CRÍTICO detectado"
            else:
                icone = "⚠️"
                titulo = "Aviso — algo precisa de atenção"

            linhas = [f"{icone} *Sentinel — {titulo}*\n"]

            # Mostra até 2 críticos e 2 avisos com linguagem humana
            mostrados = (critical[:2] if critical else []) + (warnings[:2] if not critical else [])
            for i, a in enumerate(mostrados, 1):
                problema, solucao = self._humanize_anomaly(a)
                linhas.append(f"*{i}. O que aconteceu:*\n{problema}")
                linhas.append(f"*O que vem a seguir:*\n{solucao}\n")

            if len(anomalies) > 2:
                linhas.append(f"_(+{len(anomalies) - 2} ocorrência(s) adicional(is) registrada(s))_\n")

            linhas.append(f"`Detectado às {findings['timestamp'][11:19]} UTC`")

            notify_owner("\n".join(linhas), level="error" if critical else "warn")
        except Exception as e:
            logger.warning("[Sentinel] Falha ao enviar Telegram: %s", e)

    # ──────────────────── report_status ───────────────────────

    async def report_status(self) -> dict:
        """Status rápido para reunião de conselho."""
        try:
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
            circuit_keys = r.keys(f"{OPS_PREFIX}circuit:*")
            open_circuits = [k.replace(f"{OPS_PREFIX}circuit:", "") for k in circuit_keys]
            err_keys = r.keys(f"{OPS_PREFIX}err_count:*")
            tasks_with_errors = [k.replace(f"{OPS_PREFIX}err_count:", "") for k in err_keys]
        except Exception:
            open_circuits = []
            tasks_with_errors = []

        summary = "Sistema saudável." if not open_circuits and not tasks_with_errors else (
            f"⚠️ {len(open_circuits)} circuit(s) aberto(s), "
            f"{len(tasks_with_errors)} task(s) com erros."
        )

        return {
            "role": self.role,
            "status": "critical" if open_circuits else "healthy",
            "open_circuits": open_circuits,
            "tasks_with_errors": tasks_with_errors,
            "summary": summary,
        }

    def opine(self, question: str, context: AgentContext) -> AgentOpinion:
        """Sentinel sempre pergunta sobre estabilidade primeiro."""
        stability_keywords = ["deploy", "rename", "schema", "migration", "restart", "update"]
        if any(kw in question.lower() for kw in stability_keywords):
            return AgentOpinion(
                agent_role=self.role,
                agrees=True,
                reasoning=(
                    f"[{self.display_name}] Vou monitorar ativamente por 30 minutos "
                    f"após a mudança. Qualquer anomalia reporto imediatamente. "
                    f"Recomendo janela de manutenção com backup confirmado."
                ),
            )
        return AgentOpinion(
            agent_role=self.role,
            agrees=True,
            reasoning=f"[{self.display_name}] Sem impacto de monitoramento identificado.",
        )
