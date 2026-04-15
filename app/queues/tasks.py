from celery import Celery
from app.config import get_settings
import asyncio
import logging

logger = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery("whatsapp_agent", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    timezone="America/Sao_Paulo", enable_utc=True,
    task_acks_late=True, worker_prefetch_multiplier=1,
    task_routes={
        "app.queues.tasks.process_message": {"queue": "messages"},
        "app.queues.tasks.process_buffered": {"queue": "messages"},
        "app.queues.tasks.nightly_learning": {"queue": "learning"},
        "app.queues.tasks.nightly_learning_all": {"queue": "learning"},
        "app.queues.tasks.learn_from_links": {"queue": "learning"},
    },
    beat_schedule={
        "nightly-learning-all": {
            "task": "app.queues.tasks.nightly_learning_all",
            "schedule": 86400.0,  # 24h em segundos
            "options": {"queue": "learning"},
        }
    },
)

def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
def process_message(self, phone: str, owner_id: str, message: str, agent_mode: str,
                    message_id: str = "", media_type: str = "text"):
    try:
        kwargs = {"message_id": message_id, "media_type": media_type}
        _dispatch_to_agent(phone, owner_id, message, agent_mode, **kwargs)
    except Exception as exc:
        logger.error(f"Erro ao processar mensagem de {phone}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
def process_buffered(self, phone: str, owner_id: str, agent_mode: str):
    """Processa mensagens agrupadas do buffer Redis (rate limiting).
    Mídias são pré-processadas individualmente, textos são agrupados."""
    import json as _json
    import redis

    try:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
        buffer_key = f"buffer:{phone}:{owner_id}"
        task_key = f"buffer_task:{phone}:{owner_id}"

        # Pega todas as mensagens do buffer
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

        # Se tem mídias, processa cada uma individualmente via agente
        if media_msgs:
            # Se tem só mídias (sem texto adicional), processa cada uma
            # Se tem mídias + texto, processa mídias primeiro e texto junto com a última
            for i, media in enumerate(media_msgs):
                is_last_media = (i == len(media_msgs) - 1)
                # Última mídia carrega o texto combinado junto
                if is_last_media and text_parts:
                    msg_text = "\n".join(text_parts)
                else:
                    msg_text = media.get("text", "") or ""

                if not msg_text and media.get("media_type") == "image":
                    msg_text = "[Imagem enviada]"
                elif not msg_text and media.get("media_type") == "audio":
                    msg_text = "[Áudio enviado]"
                elif not msg_text and media.get("media_type") == "document":
                    msg_text = "[Documento enviado]"

                kwargs = {
                    "message_id": media.get("message_id", ""),
                    "media_type": media.get("media_type", "text")
                }
                _dispatch_to_agent(phone, owner_id, msg_text, agent_mode, **kwargs)
        else:
            # Só textos — junta tudo e processa como antes
            combined_text = "\n".join(text_parts) if text_parts else ""
            if not combined_text:
                logger.info(f"[Buffer] Mensagens vazias de {phone}, ignorando")
                return
            kwargs = {"message_id": msgs[-1].get("message_id", ""), "media_type": "text"}
            _dispatch_to_agent(phone, owner_id, combined_text, agent_mode, **kwargs)

    except Exception as exc:
        logger.error(f"[Buffer] Erro ao processar buffer de {phone}: {exc}")
        raise self.retry(exc=exc)


def _dispatch_to_agent(phone: str, owner_id: str, message: str, agent_mode: str, **kwargs):
    """Roteia mensagem para o agente correto."""
    if agent_mode == "qualifier":
        from app.agents.qualifier import QualifierAgent
        run_async(QualifierAgent().process(phone, owner_id, message, **kwargs))
    elif agent_mode == "attendant":
        from app.agents.attendant import AttendantAgent
        run_async(AttendantAgent().process(phone, owner_id, message, **kwargs))
    elif agent_mode == "both":
        from app.services.memory import MemoryService
        customer = run_async(MemoryService().get_or_create_customer(phone, owner_id))
        if customer.lead_status in ["cliente"]:
            from app.agents.attendant import AttendantAgent
            run_async(AttendantAgent().process(phone, owner_id, message, **kwargs))
        else:
            from app.agents.qualifier import QualifierAgent
            run_async(QualifierAgent().process(phone, owner_id, message, **kwargs))

@celery_app.task(queue="learning")
def nightly_learning(owner_id: str):
    from app.services.learning import LearningService
    run_async(LearningService().run_daily_analysis(owner_id))


@celery_app.task(queue="learning")
def nightly_learning_all():
    """Roda o aprendizado noturno para todos os donos cadastrados."""
    from app.database import get_db
    from app.services.learning import LearningService
    db = get_db()
    result = db.table("owners").select("id,phone,business_name").execute()
    if not result.data:
        logger.info("[NightlyAll] Nenhum owner encontrado.")
        return
    learning = LearningService()
    success = 0
    errors = 0
    for owner in result.data:
        try:
            run_async(learning.run_daily_analysis(owner["id"]))
            success += 1
            logger.info(f"[NightlyAll] ✅ {owner.get('business_name', owner['id'])}")
        except Exception as e:
            errors += 1
            logger.error(f"[NightlyAll] ❌ {owner['id']}: {e}")
    logger.info(f"[NightlyAll] Concluído: {success} ok, {errors} erros.")


@celery_app.task(bind=True, max_retries=2, default_retry_delay=10, queue="learning")
def learn_from_links(self, owner_id: str, links: list):
    """Processa links enviados pelo dono via WhatsApp e atualiza base de conhecimento."""
    try:
        from app.services.scraper import ScraperService
        from app.services.ai import AIService
        from app.services.memory import MemoryService
        from app.services.whatsapp import WhatsAppService

        db = MemoryService().db
        owner = db.table("owners").select("*").eq("id", owner_id).maybe_single().execute()
        if not owner or not owner.data:
            logger.error(f"[LearnLinks] owner {owner_id} nao encontrado")
            return

        owner_data = owner.data
        existing_links = owner_data.get("links_processed") or []
        new_links = [l for l in links if l not in existing_links]
        if not new_links:
            logger.info(f"[LearnLinks] links já processados: {links}")
            return

        scraped = run_async(ScraperService().read_links(new_links))
        if not scraped:
            logger.warning(f"[LearnLinks] nenhum conteúdo extraído de {new_links}")
            return

        existing_context = owner_data.get("context_summary") or ""
        combined = f"[CONTEXTO ATUAL]\n{existing_context}\n\n[NOVO CONTEÚDO]\n{scraped}"
        analysis = run_async(AIService().analyze_owner_links(combined))
        if not analysis:
            return

        all_links = existing_links + new_links
        db.table("owners").update({**analysis, "links_processed": all_links}).eq("id", owner_id).execute()

        # Notifica o dono via WhatsApp
        owner_phone = owner_data.get("phone", "")
        if owner_phone:
            summary = analysis.get("context_summary", "")[:200]
            msg = (
                f"✅ *Base de conhecimento atualizada!*\n\n"
                f"🔗 {len(new_links)} link(s) processado(s)\n"
                f"🎯 Oferta detectada: {analysis.get('main_offer', '-')}\n"
                f"🗣️ Tom: {analysis.get('tone', '-')}\n\n"
                f"O agente já está usando as novas informações."
            )
            run_async(WhatsAppService().send_message(owner_phone, msg))

        logger.info(f"[LearnLinks] owner {owner_id} aprendeu {len(new_links)} links")

    except Exception as exc:
        logger.error(f"[LearnLinks] erro: {exc}")
        raise self.retry(exc=exc)
