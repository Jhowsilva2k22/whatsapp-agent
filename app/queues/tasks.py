from celery import Celery
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
# Decorator de alerta ops (Telegram + Sentry capture pela CeleryIntegration)
# ---------------------------------------------------------------------------
from app.services.alerts import notify_error  # noqa: E402


def with_ops_alert(context_name: str):
    """Em erro: avisa Telegram + re-levanta pra Celery fazer retry."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
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
    },
    beat_schedule={
        "nightly-learning-all": {
            "task": "app.queues.tasks.nightly_learning_all",
            "schedule": 86400.0,
            "options": {"queue": "learning"},
        },
        "follow-up-cold-leads": {
            "task": "app.queues.tasks.follow_up_cold_leads",
            "schedule": 3600.0,  # checa a cada 1h
            "options": {"queue": "messages"},
        },
        "nurture-customers": {
            "task": "app.queues.tasks.nurture_customers",
            "schedule": 43200.0,  # checa a cada 12h
            "options": {"queue": "messages"},
        },
        "weekly-report": {
            "task": "app.queues.tasks.weekly_report",
            "schedule": 604800.0,  # 1x por semana
            "options": {"queue": "learning"},
        },
        "daily-backup": {
            "task": "app.queues.tasks.daily_backup",
            "schedule": 21600.0,  # 4x por dia (a cada 6h)
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

@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
@with_ops_alert("process_message")
def process_message(self, phone: str, owner_id: str, message: str, agent_mode: str,
                    message_id: str = "", media_type: str = "text"):
    try:
        kwargs = {"message_id": message_id, "media_type": media_type}
        _dispatch_to_agent(phone, owner_id, message, agent_mode, **kwargs)
    except Exception as exc:
        logger.error(f"Erro ao processar mensagem de {phone}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
@with_ops_alert("process_buffered")
def process_buffered(self, phone: str, owner_id: str, agent_mode: str):
    """Processa mensagens agrupadas do buffer Redis (rate limiting).
    Mídias são pré-analisadas e tudo vira uma mensagem unificada pro agente."""
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

        # Separa mídias e textos
        media_msgs = [m for m in msgs if m.get("media_type", "text") != "text" and m.get("message_id")]
        text_parts = [m["text"] for m in msgs if m.get("text")]

        if not media_msgs:
            # Só textos — junta tudo e processa como antes
            combined_text = "\n".join(text_parts) if text_parts else ""
            if not combined_text:
                logger.info(f"[Buffer] Mensagens vazias de {phone}, ignorando")
                return
            kwargs = {"message_id": msgs[-1].get("message_id", ""), "media_type": "text"}
            _dispatch_to_agent(phone, owner_id, combined_text, agent_mode, **kwargs)
            return

        # Se tem 1 mídia só, processa normal (agente lida com ela direto)
        if len(media_msgs) == 1 and len(text_parts) <= 1:
            m = media_msgs[0]
            msg_text = text_parts[0] if text_parts else (m.get("text") or "")
            kwargs = {"message_id": m.get("message_id", ""), "media_type": m.get("media_type", "text")}
            _dispatch_to_agent(phone, owner_id, msg_text, agent_mode, **kwargs)
            return

        # Múltiplas mídias: pré-analisa cada uma e manda tudo junto como texto
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
                    # Tenta descrever o documento (nome, tipo, etc.)
                    descriptions.append(f"[Documento {i}]: {media.get('text', 'documento anexado')}")
                else:
                    descriptions.append(f"[Mídia {i} ({mtype})]: anexada")
            except Exception as e:
                logger.error(f"[Buffer] Erro ao pré-analisar mídia {i} de {phone}: {e}")
                descriptions.append(f"[Mídia {i}: erro ao processar]")

        # Junta textos + descrições de mídias
        combined_text = "\n".join(text_parts + descriptions) if (text_parts or descriptions) else ""
        if not combined_text:
            logger.info(f"[Buffer] Sem conteúdo processável de {phone}")
            return

        # Manda com o message_id do último meio (ou da última mensagem)
        last_msg = msgs[-1]
        kwargs = {
            "message_id": last_msg.get("message_id", ""),
            "media_type": "text"  # pq juntamos tudo em texto
        }
        _dispatch_to_agent(phone, owner_id, combined_text, agent_mode, **kwargs)

    except Exception as exc:
        logger.error(f"Erro ao processar buffer de {phone}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
@with_ops_alert("follow_up_active")
def follow_up_active(self, phone: str, owner_id: str):
    """Envia mensagem de follow up para usuários ativos."""
    try:
        from app.services.contact import ContactService
        from app.services.whatsapp import WhatsAppService

        contact_svc = ContactService()
        wa_svc = WhatsAppService()

        # Busca contatos com status "active" (que interagiram hoje)
        active_contacts = contact_svc.find_active_today(owner_id)
        if not active_contacts:
            logger.info(f"[Follow-up Active] Nenhum contato ativo hoje para {owner_id}")
            return

        for contact in active_contacts:
            try:
                msg = f"Olá {contact.first_name or contact.name}! Tudo bem? Tem algo que eu possa ajudar? 😊"
                run_async(wa_svc.send_message(phone, contact.phone, msg))
                logger.info(f"[Follow-up Active] Enviado para {contact.phone}")
            except Exception as e:
                logger.error(f"[Follow-up Active] Erro ao enviar para {contact.phone}: {e}")

    except Exception as exc:
        logger.error(f"Erro no follow-up ativo para {owner_id}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
@with_ops_alert("follow_up_cold_leads")
def follow_up_cold_leads(self):
    """Envia follow-up para leads frios de TODOS os owners (chamado pelo beat sem args)."""
    try:
        from app.database import get_db
        from app.services.whatsapp import WhatsAppService
        from datetime import datetime, timedelta

        db = get_db()
        wa_svc = WhatsAppService()

        # Busca todos os owners ativos
        owners_resp = db.table("owners").select("id, whatsapp_phone_number_id").execute()
        owners = owners_resp.data or []

        if not owners:
            logger.info("[Follow-up Cold] Nenhum owner encontrado")
            return

        cold_threshold = datetime.utcnow() - timedelta(days=7)

        for owner in owners:
            owner_id = owner["id"]
            phone = owner.get("whatsapp_phone_number_id", "")
            if not phone:
                continue

            try:
                # Busca leads frios (última interação > 7 dias, lead_score != 'client')
                resp = db.table("customers").select("phone, name, first_name").eq(
                    "owner_id", owner_id
                ).lt(
                    "last_contact", cold_threshold.isoformat()
                ).neq(
                    "lead_score", "client"
                ).limit(20).execute()

                cold_leads = resp.data or []
                if not cold_leads:
                    continue

                logger.info(f"[Follow-up Cold] {len(cold_leads)} leads frios para owner {owner_id}")

                for lead in cold_leads:
                    try:
                        name = lead.get("first_name") or lead.get("name") or "você"
                        msg = f"Oi {name}! Faz um tempo que não conversamos. Posso te ajudar com algo? 😊"
                        run_async(wa_svc.send_message(phone, lead["phone"], msg))
                        logger.info(f"[Follow-up Cold] Enviado para {lead['phone']}")
                    except Exception as e:
                        logger.error(f"[Follow-up Cold] Erro ao enviar para {lead.get('phone')}: {e}")

            except Exception as e:
                logger.error(f"[Follow-up Cold] Erro ao processar owner {owner_id}: {e}")

    except Exception as exc:
        logger.error(f"Erro no follow-up de leads frios: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
@with_ops_alert("nurture_customers")
def nurture_customers(self):
    """Envia nurture messages para clientes de TODOS os owners (chamado pelo beat sem args)."""
    try:
        from app.database import get_db
        from app.services.whatsapp import WhatsAppService

        db = get_db()
        wa_svc = WhatsAppService()

        # Busca todos os owners ativos
        owners_resp = db.table("owners").select("id, whatsapp_phone_number_id").execute()
        owners = owners_resp.data or []

        if not owners:
            logger.info("[Nurture] Nenhum owner encontrado")
            return

        for owner in owners:
            owner_id = owner["id"]
            phone = owner.get("whatsapp_phone_number_id", "")
            if not phone:
                continue

            try:
                # Busca clientes (lead_score = 'client')
                resp = db.table("customers").select("phone, name, first_name").eq(
                    "owner_id", owner_id
                ).eq(
                    "lead_score", "client"
                ).limit(20).execute()

                customers = resp.data or []
                if not customers:
                    continue

                logger.info(f"[Nurture] {len(customers)} clientes para owner {owner_id}")

                for customer in customers:
                    try:
                        name = customer.get("first_name") or customer.get("name") or "você"
                        msg = f"Olá {name}! Obrigado por ser cliente! Quer conhecer nossas novidades? ✨"
                        run_async(wa_svc.send_message(phone, customer["phone"], msg))
                        logger.info(f"[Nurture] Enviado para {customer['phone']}")
                    except Exception as e:
                        logger.error(f"[Nurture] Erro ao enviar para {customer.get('phone')}: {e}")

            except Exception as e:
                logger.error(f"[Nurture] Erro ao processar owner {owner_id}: {e}")

    except Exception as exc:
        logger.error(f"Erro no nurture de clientes: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="learning")
@with_ops_alert("weekly_report")
def weekly_report(self, owner_id: str):
    """Gera e envia relatório semanal (estatísticas, insights, etc.)."""
    try:
        from app.services.report import ReportService
        from app.services.alerts import notify_user

        report_svc = ReportService()
        report = report_svc.generate_weekly(owner_id)

        if not report:
            logger.warning(f"[Weekly Report] Nenhum dado para relatório de {owner_id}")
            return

        # Envia via Telegram ou email
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
    """Recalcula scores (relevância, engagement, etc.) dos contatos."""
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
    """Faz aprendizado noturno de um workspace específico."""
    try:
        from app.services.learning import LearningService
        learning_svc = LearningService()
        # Processa este workspace
        updated = learning_svc.learn_from_conversations(owner_id)
        logger.info(f"[Nightly Learning] Aprendeu de {updated} conversas de {owner_id}")

    except Exception as exc:
        logger.error(f"Erro ao fazer nightly learning para {owner_id}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="learning")
@with_ops_alert("nightly_learning_all")
def nightly_learning_all(self):
    """Faz aprendizado noturno para TODOS os workspaces em paralelo."""
    try:
        from app.services.workspace import WorkspaceService
        from app.services.learning import LearningService

        ws_svc = WorkspaceService()
        learning_svc = LearningService()

        workspaces = ws_svc.list_all()
        logger.info(f"[Nightly Learning All] Processando {len(workspaces)} workspace(s)")

        for ws in workspaces:
            try:
                updated = learning_svc.learn_from_conversations(ws.owner_id)
                logger.info(f"[Nightly Learning All] {ws.owner_id}: aprendeu de {updated} conversas")
            except Exception as e:
                logger.error(f"[Nightly Learning All] Erro em {ws.owner_id}: {e}")

    except Exception as exc:
        logger.error(f"Erro no nightly learning all: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="learning")
@with_ops_alert("learn_from_links")
def learn_from_links(self, owner_id: str):
    """Aprende de links compartilhados em conversas (web scraping + análise)."""
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
    """Executa uma campanha (lida + envia mensagens)."""
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
    """Realiza backup diário dos dados via Supabase Storage."""
    try:
        from app.services.backup import run_backup
        result = run_backup()
        logger.info(f"[Daily Backup] Backup realizado com sucesso — {result.get('total_rows', 0)} registros")

    except Exception as exc:
        logger.error(f"Erro ao fazer backup: {exc}")
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Funcs auxiliares (não são tasks, são funcs chamadas pelas tasks)
# ---------------------------------------------------------------------------


async def _dispatch_to_agent(phone: str, owner_id: str, message: str, agent_mode: str, **kwargs):
    """Despacha a mensagem para o agente (determinístico baseado no modo)."""
    from app.services.agent import AgentService

    agent = AgentService(owner_id)
    response = await agent.respond(
        phone=phone,
        message=message,
        agent_mode=agent_mode,
        **kwargs
    )
    return response
