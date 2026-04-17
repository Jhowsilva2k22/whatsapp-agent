from celery import Celery
from celery.schedules import crontab
from app.config import get_settings
from urllib.parse import quote
import asyncio
import logging
import os
from functools import wraps

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Sentry init pro worker Celery (Fase 1 — Observabilidade)
# ---------------------------------------------------------------------------
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=os.getenv("APP_ENV", "production"),
            traces_sample_rate=0.1,
            profiles_sample_rate=0.0,
            send_default_pii=False,
            integrations=[CeleryIntegration()],
        )
    except ImportError:
        logger.warning("[Sentry] sentry-sdk não instalado no worker")


# ---------------------------------------------------------------------------
# Decorator de alerta ops — agora com tracking, circuit breaker e auto-fix
# ---------------------------------------------------------------------------
from app.services.alerts import notify_error  # noqa: E402


def with_ops_alert(context_name: str):
    """Decorator que:
    1. Checa circuit breaker antes de executar
    2. Rastreia sucesso/erro em Redis
    3. Abre circuit breaker após 5 erros consecutivos
    4. Tenta auto-fix para padrões conhecidos
    5. Alerta via Telegram
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            # ── Circuit breaker check ──
            try:
                from app.services.ops import is_circuit_open
                if is_circuit_open(context_name):
                    logger.warning("[Ops] Circuit aberto para %s — pulando execução", context_name)
                    return None
            except Exception:
                pass  # se ops falhar, roda a task normalmente

            # ── Execução ──
            try:
                result = fn(*args, **kwargs)
                # Sucesso → reseta contador de erros
                try:
                    from app.services.ops import track_success
                    track_success(context_name)
                except Exception:
                    pass
                return result
            except Exception as e:
                # Erro → rastreia + alerta + re-raise pro Celery retry
                try:
                    from app.services.ops import track_error
                    track_error(context_name, e)
                except Exception:
                    pass
                try:
                    notify_error(f"celery.{context_name}", e)
                except Exception:
                    pass
                raise
        return wrapper
    return decorator


def _panel_url() -> str:
    """Gera URL do painel já autenticada com token."""
    base = settings.app_url.rstrip("/")
    token = quote(settings.app_secret, safe="")
    return f"{base}/panel?token={token}"

celery_app = Celery("whatsapp_agent", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    timezone="America/Sao_Paulo", enable_utc=True,
    task_acks_late=True, worker_prefetch_multiplier=1,
    task_routes={
        "app.queues.tasks.process_message": {"queue": "messages"},
        "app.queues.tasks.process_buffered": {"queue": "messages"},
        "app.queues.tasks.follow_up_active": {"queue": "messages"},
        "app.queues.tasks.follow_up_cold_leads": {"queue": "messages"},
        "app.queues.tasks.nurture_customers": {"queue": "messages"},
        "app.queues.tasks.weekly_report": {"queue": "learning"},
        "app.queues.tasks.recalculate_scores": {"queue": "learning"},
        "app.queues.tasks.nightly_learning": {"queue": "learning"},
        "app.queues.tasks.nightly_learning_all": {"queue": "learning"},
        "app.queues.tasks.learn_from_links": {"queue": "learning"},
        "app.queues.tasks.run_campaign": {"queue": "learning"},
        "app.queues.tasks.daily_backup": {"queue": "learning"},
        "app.queues.tasks.health_check": {"queue": "learning"},
        "app.queues.tasks.daily_ops_report": {"queue": "learning"},
        "app.queues.tasks.daily_web_search": {"queue": "learning"},  # busca autônoma
    },
    beat_schedule={
        # ── LEARNING: horário fixo, não reseta com deploy ──
        "nightly-learning-all": {
            "task": "app.queues.tasks.nightly_learning_all",
            "schedule": crontab(hour=3, minute=0),  # 3:00 AM BRT diário
            "options": {"queue": "learning"},
        },
        "daily-web-search": {
            "task": "app.queues.tasks.daily_web_search",
            "schedule": crontab(hour=6, minute=0),  # 6:00 AM BRT diário — após o learning noturno
            "options": {"queue": "learning"},
        },
        # ── MENSAGENS: intervalos curtos (ok resetar) ──
        "follow-up-cold-leads": {
            "task": "app.queues.tasks.follow_up_cold_leads",
            "schedule": 3600.0,  # a cada 1h
            "options": {"queue": "messages"},
        },
        "nurture-customers": {
            "task": "app.queues.tasks.nurture_customers",
            "schedule": crontab(hour="8,20", minute=0),  # 8h e 20h BRT
            "options": {"queue": "messages"},
        },
        # ── RELATÓRIOS: horários fixos ──
        "weekly-report": {
            "task": "app.queues.tasks.weekly_report",
            "schedule": crontab(hour=8, minute=0, day_of_week=1),  # segunda 8h BRT
            "options": {"queue": "learning"},
        },
        # ── BACKUP: 4x por dia em horários fixos ──
        "daily-backup": {
            "task": "app.queues.tasks.daily_backup",
            "schedule": crontab(hour="0,6,12,18", minute=0),  # 0h, 6h, 12h, 18h BRT
            "options": {"queue": "learning"},
        },
        # ── OPS: monitoramento autônomo ──
        "health-check": {
            "task": "app.queues.tasks.health_check",
            "schedule": 1800.0,  # a cada 30 min (intervalo curto, ok)
            "options": {"queue": "learning"},
        },
        "daily-ops-report": {
            "task": "app.queues.tasks.daily_ops_report",
            "schedule": crontab(hour="1,7,13,19", minute=0),  # 4x ao dia em horários fixos
            "options": {"queue": "learning"},
        },
        # ── AGENTES AUTÔNOMOS: Sentinel a cada 5 min ──
        "sentinel-monitor": {
            "task": "app.queues.tasks.sentinel_monitor",
            "schedule": 300.0,  # a cada 5 minutos
            "options": {"queue": "learning"},
        },
    },
)

def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════════════════════
#  TASKS DE MENSAGEM
# ═══════════════════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
@with_ops_alert("process_message")
def process_message(self, phone: str, owner_id: str, message: str, agent_mode: str,
                    message_id: str = "", media_type: str = "text"):
    try:
        kwargs = {"message_id": message_id, "media_type": media_type}
        # _dispatch_to_agent é async — DEVE ser envolvida com run_async()
        run_async(_dispatch_to_agent(phone, owner_id, message, agent_mode, **kwargs))
    except Exception as exc:
        logger.error(f"Erro ao processar mensagem de {phone}: {exc}", exc_info=True)
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
@with_ops_alert("process_buffered")
def process_buffered(self, phone: str, owner_id: str, agent_mode: str):
    """Processa mensagens agrupadas do buffer Redis (rate limiting)."""
    import json as _json
    import redis

    try:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
        buffer_key = f"buffer:{phone}:{owner_id}"
        task_key = f"buffer_task:{phone}:{owner_id}"

        raw_msgs = _redis.lrange(buffer_key, 0, -1)
        _redis.delete(buffer_key)
        _redis.delete(task_key)

        if not raw_msgs:
            logger.info(f"[Buffer] Nenhuma mensagem no buffer para {phone}")
            return

        msgs = [_json.loads(m) for m in raw_msgs]
        logger.info(f"[Buffer] Processando {len(msgs)} mensagem(ns) agrupadas de {phone}")

        media_msgs = [m for m in msgs if m.get("media_type", "text") != "text" and m.get("message_id")]
        text_parts = [m["text"] for m in msgs if m.get("text")]

        if not media_msgs:
            combined_text = "\n".join(text_parts) if text_parts else ""
            if not combined_text:
                logger.info(f"[Buffer] Mensagens vazias de {phone}, ignorando")
                return
            kwargs = {"message_id": msgs[-1].get("message_id", ""), "media_type": "text"}
            # _dispatch_to_agent é async — DEVE ser envolvida com run_async()
            run_async(_dispatch_to_agent(phone, owner_id, combined_text, agent_mode, **kwargs))
            return

        if len(media_msgs) == 1 and len(text_parts) <= 1:
            m = media_msgs[0]
            msg_text = text_parts[0] if text_parts else (m.get("text") or "")
            kwargs = {"message_id": m.get("message_id", ""), "media_type": m.get("media_type", "text")}
            # _dispatch_to_agent é async — DEVE ser envolvida com run_async()
            run_async(_dispatch_to_agent(phone, owner_id, msg_text, agent_mode, **kwargs))
            return

        from app.services.whatsapp import WhatsAppService
        from app.services.ai import AIService
        wa = WhatsAppService()
        ai = AIService()
        descriptions = []

        for i, media in enumerate(media_msgs, 1):
            mid = media.get("message_id", "")
            mtype = media.get("media_type", "")
            try:
                b64 = run_async(wa.download_media_base64(mid, phone=phone))
                if not b64:
                    descriptions.append(f"[Mídia {i}: não foi possível baixar]")
                    continue

                if mtype == "image":
                    desc = run_async(ai.respond_with_image(
                        system_prompt="Descreva esta imagem em 1-2 frases objetivas: o que é, marca, detalhes visíveis. Só a descrição, sem comentários.",
                        history=[], user_message="Descreva esta imagem.", image_base64=b64
                    ))
                    descriptions.append(f"[Imagem {i}]: {desc}")
                elif mtype == "audio":
                    text = run_async(ai.transcribe_audio(b64))
                    descriptions.append(f"[Áudio {i}]: {text}" if text else f"[Áudio {i}: não transcrito]")
                elif mtype == "document":
                    descriptions.append(f"[Documento {i}]: {media.get('text', 'documento anexado')}")
                else:
                    descriptions.append(f"[Mídia {i} ({mtype})]: anexada")
            except Exception as e:
                logger.error(f"[Buffer] Erro ao pré-analisar mídia {i} de {phone}: {e}")
                descriptions.append(f"[Mídia {i}: erro ao processar]")

        combined_text = "\n".join(text_parts + descriptions) if (text_parts or descriptions) else ""
        if not combined_text:
            logger.info(f"[Buffer] Sem conteúdo processável de {phone}")
            return

        last_msg = msgs[-1]
        kwargs = {
            "message_id": last_msg.get("message_id", ""),
            "media_type": "text"
        }
        # _dispatch_to_agent é async — DEVE ser envolvida com run_async()
        run_async(_dispatch_to_agent(phone, owner_id, combined_text, agent_mode, **kwargs))

    except Exception as exc:
        logger.error(f"Erro ao processar buffer de {phone}: {exc}", exc_info=True)
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
@with_ops_alert("follow_up_active")
def follow_up_active(self, phone: str, owner_id: str):
    """Envia mensagem de follow up para usuários ativos."""
    try:
        from app.services.contact import ContactService
        from app.services.whatsapp import WhatsAppService
        from app.database import get_db

        contact_svc = ContactService()
        wa_svc = WhatsAppService()

        active_contacts = contact_svc.find_active_today(owner_id)
        if not active_contacts:
            logger.info(f"[Follow-up Active] Nenhum contato ativo hoje para {owner_id}")
            return

        # Busca evolution_instance do tenant para envio correto (multi-tenant)
        db = get_db()
        owner_resp = db.table("tenants").select("evolution_instance").eq("id", owner_id).limit(1).execute()
        evolution_instance = (owner_resp.data[0] if owner_resp and owner_resp.data else {}).get("evolution_instance", "")

        for contact in active_contacts:
            try:
                msg = f"Olá {contact.first_name or contact.name}! Tudo bem? Tem algo que eu possa ajudar? 😊"
                run_async(wa_svc.send_message(contact.phone, msg, instance=evolution_instance))
                logger.info(f"[Follow-up Active] Enviado para {contact.phone}")
            except Exception as e:
                logger.error(f"[Follow-up Active] Erro ao enviar para {contact.phone}: {e}")

    except Exception as exc:
        logger.error(f"Erro no follow-up ativo para {owner_id}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
@with_ops_alert("follow_up_cold_leads")
def follow_up_cold_leads(self):
    """Follow-up de leads frios — itera todos os tenants (beat sem args)."""
    try:
        from app.database import get_db
        from app.services.whatsapp import WhatsAppService
        from app.services.ops import save_progress, get_progress, clear_progress
        from datetime import datetime, timedelta

        db = get_db()
        wa_svc = WhatsAppService()

        # Retomada: checa se tem progresso salvo
        progress = get_progress("follow_up_cold_leads")
        processed_owners = set(progress.get("done", [])) if progress else set()

        # Lê da tabela tenants (owners está vazia/obsoleta)
        owners_resp = db.table("tenants").select("id, owner_phone, evolution_instance").execute()
        owners = []
        for row in (owners_resp.data or []):
            r = dict(row)
            r.setdefault("phone", r.get("owner_phone", ""))
            owners.append(r)

        if not owners:
            logger.info("[Follow-up Cold] Nenhum tenant encontrado")
            return

        cold_threshold = datetime.utcnow() - timedelta(days=7)

        for owner in owners:
            owner_id = owner["id"]
            if owner_id in processed_owners:
                continue

            evolution_instance = owner.get("evolution_instance", "")
            if not evolution_instance:
                logger.warning(f"[Follow-up Cold] Tenant {owner_id} sem evolution_instance — pulando")
                processed_owners.add(owner_id)
                save_progress("follow_up_cold_leads", {"done": list(processed_owners)})
                continue

            try:
                resp = db.table("customers").select("phone, name, first_name").eq(
                    "owner_id", owner_id
                ).lt(
                    "last_contact", cold_threshold.isoformat()
                ).neq(
                    "lead_score", "client"
                ).limit(20).execute()

                cold_leads = resp.data or []
                if not cold_leads:
                    processed_owners.add(owner_id)
                    save_progress("follow_up_cold_leads", {"done": list(processed_owners)})
                    continue

                logger.info(f"[Follow-up Cold] {len(cold_leads)} leads frios para tenant {owner_id}")

                for lead in cold_leads:
                    try:
                        name = lead.get("first_name") or lead.get("name") or "você"
                        msg = f"Oi {name}! Faz um tempo que não conversamos. Posso te ajudar com algo? 😊"
                        run_async(wa_svc.send_message(lead["phone"], msg, instance=evolution_instance))
                    except Exception as e:
                        logger.error(f"[Follow-up Cold] Erro ao enviar para {lead.get('phone')}: {e}")

                processed_owners.add(owner_id)
                save_progress("follow_up_cold_leads", {"done": list(processed_owners)})

            except Exception as e:
                logger.error(f"[Follow-up Cold] Erro ao processar tenant {owner_id}: {e}")

        clear_progress("follow_up_cold_leads")

    except Exception as exc:
        logger.error(f"Erro no follow-up de leads frios: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
@with_ops_alert("nurture_customers")
def nurture_customers(self):
    """Nurture de clientes — itera todos os tenants (beat sem args)."""
    try:
        from app.database import get_db
        from app.services.whatsapp import WhatsAppService
        from app.services.ops import save_progress, get_progress, clear_progress

        db = get_db()
        wa_svc = WhatsAppService()

        progress = get_progress("nurture_customers")
        processed_owners = set(progress.get("done", [])) if progress else set()

        # Lê da tabela tenants (owners está vazia/obsoleta)
        owners_resp = db.table("tenants").select("id, owner_phone, evolution_instance").execute()
        owners = []
        for row in (owners_resp.data or []):
            r = dict(row)
            r.setdefault("phone", r.get("owner_phone", ""))
            owners.append(r)

        if not owners:
            logger.info("[Nurture] Nenhum tenant encontrado")
            return

        for owner in owners:
            owner_id = owner["id"]
            if owner_id in processed_owners:
                continue

            evolution_instance = owner.get("evolution_instance", "")
            if not evolution_instance:
                logger.warning(f"[Nurture] Tenant {owner_id} sem evolution_instance — pulando")
                processed_owners.add(owner_id)
                save_progress("nurture_customers", {"done": list(processed_owners)})
                continue

            try:
                resp = db.table("customers").select("phone, name, first_name").eq(
                    "owner_id", owner_id
                ).eq(
                    "lead_status", "cliente"
                ).limit(20).execute()

                customers = resp.data or []
                if not customers:
                    processed_owners.add(owner_id)
                    save_progress("nurture_customers", {"done": list(processed_owners)})
                    continue

                logger.info(f"[Nurture] {len(customers)} clientes para tenant {owner_id}")

                for customer in customers:
                    try:
                        name = customer.get("first_name") or customer.get("name") or "você"
                        msg = f"Olá {name}! Obrigado por ser cliente! Quer conhecer nossas novidades? ✨"
                        run_async(wa_svc.send_message(customer["phone"], msg, instance=evolution_instance))
                    except Exception as e:
                        logger.error(f"[Nurture] Erro ao enviar para {customer.get('phone')}: {e}")

                processed_owners.add(owner_id)
                save_progress("nurture_customers", {"done": list(processed_owners)})

            except Exception as e:
                logger.error(f"[Nurture] Erro ao processar tenant {owner_id}: {e}")

        clear_progress("nurture_customers")

    except Exception as exc:
        logger.error(f"Erro no nurture de clientes: {exc}")
        raise self.retry(exc=exc)


# ═══════════════════════════════════════════════════════════════════════════
#  TASKS DE LEARNING
# ═══════════════════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="learning")
@with_ops_alert("weekly_report")
def weekly_report(self, owner_id: str):
    try:
        from app.services.report import ReportService
        from app.services.alerts import notify_user

        report_svc = ReportService()
        report = report_svc.generate_weekly(owner_id)

        if not report:
            logger.warning(f"[Weekly Report] Nenhum dado para relatório de {owner_id}")
            return

        notify_user(
            owner_id=owner_id,
            title="Relatório Semanal",
            message=report,
            panel_url=_panel_url()
        )
        logger.info(f"[Weekly Report] Enviado para {owner_id}")

    except Exception as exc:
        logger.error(f"Erro ao gerar relatório semanal para {owner_id}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="learning")
@with_ops_alert("recalculate_scores")
def recalculate_scores(self, owner_id: str):
    try:
        from app.services.scoring import ScoringService
        scoring_svc = ScoringService()
        updated = scoring_svc.recalculate_all(owner_id)
        logger.info(f"[Recalc Scores] Atualizou {updated} contatos para {owner_id}")

    except Exception as exc:
        logger.error(f"Erro ao recalcular scores para {owner_id}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="learning")
@with_ops_alert("nightly_learning")
def nightly_learning(self, owner_id: str):
    try:
        from app.services.learning import LearningService
        learning_svc = LearningService()
        updated = learning_svc.learn_from_conversations(owner_id)
        logger.info(f"[Nightly Learning] Aprendeu de {updated} conversas de {owner_id}")

    except Exception as exc:
        logger.error(f"Erro ao fazer nightly learning para {owner_id}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="learning")
@with_ops_alert("nightly_learning_all")
def nightly_learning_all(self):
    """Aprendizado noturno para TODOS os workspaces. Salva progresso."""
    try:
        from app.services.ops import save_progress, get_progress, clear_progress

        progress = get_progress("nightly_learning_all")
        done_ids = set(progress.get("done", [])) if progress else set()

        # Lê da tabela tenants (owners está vazia/obsoleta)
        from app.database import get_db
        db = get_db()
        resp = db.table("tenants").select("id").execute()
        all_owners = [row["id"] for row in (resp.data or [])]

        from app.services.learning import LearningService
        learning_svc = LearningService()

        logger.info(f"[Nightly Learning All] Processando {len(all_owners)} tenant(s), {len(done_ids)} já feitos")

        for oid in all_owners:
            if oid in done_ids:
                continue
            try:
                updated = learning_svc.learn_from_conversations(oid)
                logger.info(f"[Nightly Learning All] {oid}: aprendeu de {updated} conversas")
            except Exception as e:
                logger.error(f"[Nightly Learning All] Erro em {oid}: {e}")

            done_ids.add(oid)
            save_progress("nightly_learning_all", {"done": list(done_ids)})

        clear_progress("nightly_learning_all")

    except Exception as exc:
        logger.error(f"Erro no nightly learning all: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=60, queue="learning")
@with_ops_alert("daily_web_search")
def daily_web_search(self):
    """
    Busca autônoma diária — cada agente aprende sua especialidade.
    Roda todo dia às 6h BRT, logo após o nightly_learning_all das 3h.

    Fluxo:
      1. Lê todos os tenants ativos
      2. Para cada tenant × cada role: chama WebSearchService.search_and_learn(role=role)
      3. Resultados salvos no knowledge_items com tag do role:
         [SDR: prospecção ativa WhatsApp...]
         [Closer: técnicas fechamento vendas...]
         [Ops/Infra: Evolution API instabilidades...]

    Capacidade Brave Free (2.000 buscas/mês ≈ 66/dia):
      7 roles × ~3 tópicos = ~21 calls/tenant/dia
      Comporta até 3 tenants simultâneos com folga.

    Requisito: BRAVE_API_KEY configurada no Railway env.
    Sem a chave, a task executa mas não faz buscas (aviso no log).
    """
    try:
        from app.database import get_db
        from app.services.web_search import WebSearchService, TOPICS_BY_ROLE

        db = get_db()
        resp = db.table("tenants").select("id").execute()
        all_owners = [row["id"] for row in (resp.data or [])]

        if not all_owners:
            logger.info("[WebSearch] Nenhum tenant encontrado — pulando")
            return

        svc = WebSearchService()
        roles = list(TOPICS_BY_ROLE.keys())
        total_saved = 0

        logger.info(
            "[WebSearch] Iniciando ciclo diário — %d tenant(s) × %d roles",
            len(all_owners),
            len(roles),
        )

        for owner_id in all_owners:
            tenant_saved = 0
            for role in roles:
                try:
                    saved = svc.search_and_learn(owner_id, role=role)
                    tenant_saved += saved
                    total_saved += saved
                except Exception as e:
                    logger.error(
                        "[WebSearch] Erro no tenant %s role=%s: %s",
                        owner_id[:8],
                        role,
                        e,
                    )
            logger.info(
                "[WebSearch] Tenant %s concluído — %d insights salvos (%d roles)",
                owner_id[:8],
                tenant_saved,
                len(roles),
            )

        logger.info(
            "[WebSearch] Ciclo diário concluído — %d insights totais em %d tenant(s)",
            total_saved,
            len(all_owners),
        )

    except Exception as exc:
        logger.error(f"Erro no daily_web_search: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="learning")
@with_ops_alert("learn_from_links")
def learn_from_links(self, owner_id: str):
    try:
        from app.services.link_learning import LinkLearningService
        link_svc = LinkLearningService()
        updated = link_svc.process_all_pending_links(owner_id)
        logger.info(f"[Learn from Links] Processou {updated} links para {owner_id}")

    except Exception as exc:
        logger.error(f"Erro ao processar links para {owner_id}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="learning")
@with_ops_alert("run_campaign")
def run_campaign(self, campaign_id: str):
    try:
        from app.services.campaign import CampaignService
        campaign_svc = CampaignService()
        campaign_svc.execute(campaign_id)
        logger.info(f"[Campaign] Executada campanha {campaign_id}")

    except Exception as exc:
        logger.error(f"Erro ao executar campanha {campaign_id}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="learning")
@with_ops_alert("daily_backup")
def daily_backup(self):
    """Backup diário via Supabase Storage."""
    try:
        from app.services.backup import run_backup
        result = run_backup()
        logger.info(f"[Daily Backup] OK — {result.get('total_rows', 0)} registros")

    except Exception as exc:
        logger.error(f"Erro ao fazer backup: {exc}")
        raise self.retry(exc=exc)


# ═══════════════════════════════════════════════════════════════════════════
#  TASKS DE OPS (monitoramento autônomo)
# ═══════════════════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=1, default_retry_delay=30, queue="learning")
def health_check(self):
    """Roda a cada 30 min. Verifica componentes e alerta se algo está degradado."""
    try:
        from app.services.ops import run_health_check
        from app.services.alerts import notify_warn

        report = run_health_check()

        if report["overall"] != "healthy":
            # Monta resumo dos problemas
            problems = []
            for comp, info in report.get("components", {}).items():
                if info["status"] != "ok":
                    problems.append(f"`{comp}`: {info['status']}")
            for task, info in report.get("circuits", {}).items():
                ttl = info.get("ttl_seconds", 0)
                problems.append(f"Circuit `{task}` aberto ({ttl // 60}min restantes)")

            if problems:
                notify_warn(
                    f"Health Check — DEGRADADO\n\n"
                    + "\n".join(problems)
                )

        logger.info(f"[Health Check] Status: {report['overall']}")

    except Exception as exc:
        logger.error(f"Erro no health check: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=1, default_retry_delay=30, queue="learning")
def daily_ops_report(self):
    """Relatório de ops a cada 6h no Telegram."""
    try:
        from app.services.ops import generate_ops_report
        from app.services.alerts import notify_owner

        report = generate_ops_report()
        notify_owner(report, level="info")
        logger.info("[Ops Report] Enviado")

    except Exception as exc:
        logger.error(f"Erro ao gerar ops report: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=1, default_retry_delay=60, queue="learning")
def sentinel_monitor(self):
    """
    Ciclo de monitoramento autônomo do Sentinel.
    Roda a cada 5 minutos via Celery Beat.
    Detecta anomalias, aciona Doctor se necessário.
    """
    try:
        from app.agents.registry import load_all_agents, get_agent
        from app.agents.base import AgentContext

        load_all_agents()
        sentinel = get_agent("sentinel")
        if not sentinel:
            logger.warning("[sentinel_monitor] Sentinel não encontrado no registry")
            return

        context = AgentContext(
            tenant_id="system",
            triggered_by="celery_beat",
            payload={"source": "scheduled"},
        )

        findings = run_async(sentinel.act(context))
        status = findings.get("status", "unknown")
        anomaly_count = len(findings.get("anomalies", []))

        logger.info("[sentinel_monitor] Status=%s, anomalias=%d", status, anomaly_count)

        # Se houver anomalias críticas, aciona o Doctor imediatamente
        if anomaly_count > 0:
            doctor = get_agent("doctor")
            if doctor:
                import uuid
                incident_id = str(uuid.uuid4())[:8]
                doctor_context = AgentContext(
                    tenant_id="system",
                    triggered_by="sentinel",
                    incident_id=incident_id,
                    payload={
                        "anomaly": findings,
                        "anomalies": findings.get("anomalies", []),
                        "triggered_by_sentinel": True,
                    },
                )
                diagnosis = run_async(doctor.act(doctor_context))

                # Se diagnóstico pronto para Surgeon, aciona
                if diagnosis.get("ready_for_surgeon"):
                    surgeon = get_agent("surgeon")
                    if surgeon:
                        surgeon_context = AgentContext(
                            tenant_id="system",
                            triggered_by="doctor",
                            incident_id=incident_id,
                            payload={"diagnosis": diagnosis},
                        )
                        run_async(surgeon.act(surgeon_context))

    except Exception as exc:
        logger.error(f"[sentinel_monitor] Erro: {exc}")
        raise self.retry(exc=exc)


# ═══════════════════════════════════════════════════════════════════════════
#  FUNCS AUXILIARES
# ═══════════════════════════════════════════════════════════════════════════

async def _dispatch_to_agent(phone: str, owner_id: str, message: str, agent_mode: str, **kwargs):
    """Despacha a mensagem para o agente."""
    from app.services.agent import AgentService

    agent = AgentService(owner_id)
    response = await agent.respond(
        phone=phone,
        message=message,
        agent_mode=agent_mode,
        **kwargs
    )
    return response
