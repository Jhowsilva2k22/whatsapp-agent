from fastapi import APIRouter, Request, HTTPException, Query
from app.services.instagram import InstagramService
from app.services.memory import MemoryService
from app.queues.tasks import process_buffered, process_message, follow_up_active, celery_app
from app.config import get_settings
import logging
import redis
import json as _json

logger = logging.getLogger(__name__)
router = APIRouter()

_settings = get_settings()
_redis = redis.from_url(_settings.redis_url, decode_responses=True)
DEDUP_TTL = 120
DEBOUNCE_SECONDS = 4


# ── Verificação do Webhook (Meta envia GET) ─────────────────────────────────
@router.get("/webhook/instagram")
async def verify_instagram_webhook(
    request: Request,
):
    """Meta envia GET com hub.mode, hub.verify_token e hub.challenge."""
    params = request.query_params
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    settings = get_settings()
    if mode == "subscribe" and token == settings.meta_verify_token:
        logger.info(f"[IG Webhook] Verificação OK, challenge={challenge}")
        return int(challenge)

    logger.warning(f"[IG Webhook] Verificação FALHOU: mode={mode} token={token}")
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Receber mensagens do Instagram (Meta envia POST) ────────────────────────
@router.post("/webhook/instagram")
async def receive_instagram(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload invalido")

    logger.info(f"[IG Webhook] Recebido: object={payload.get('object')}")

    instagram = InstagramService()
    messages = instagram.parse_webhook(payload)

    if not messages:
        return {"status": "ignored"}

    memory = MemoryService()

    for message in messages:
        # ── Deduplicação ────────────────────────────────────────────────
        if message.message_id:
            dedup_key = f"dedup:ig:{message.message_id}"
            try:
                if _redis.get(dedup_key):
                    logger.info(f"[IG Webhook] Duplicata ignorada: {message.message_id}")
                    continue
                _redis.setex(dedup_key, DEDUP_TTL, "1")
            except Exception as e:
                logger.warning(f"[IG Webhook] Redis dedup falhou: {e}")

        # ── Busca owner pelo instagram_account_id ───────────────────────
        owner = await _get_owner_by_instagram(message.instance)
        if not owner:
            # Fallback: tenta pelo primeiro owner cadastrado
            owner = await _get_first_owner()
            if not owner:
                logger.warning(f"[IG Webhook] Owner não encontrado para {message.instance}")
                continue

        owner_id = owner["id"]
        sender_id = message.phone  # IGSID do remetente

        # ── Garante customer com channel=instagram ──────────────────────
        customer = await memory.get_or_create_customer(sender_id, owner_id)
        if not customer.channel or customer.channel != "instagram":
            await memory.update_customer(sender_id, owner_id, {"channel": "instagram"})

        # ── Bloqueia bot se em atendimento humano ───────────────────────
        if customer.lead_status == "em_atendimento_humano":
            logger.info(f"[IG Webhook] Ignorado — {sender_id} em atendimento humano")
            continue

        # ── Tracking de follow-up ───────────────────────────────────────
        import time as _time
        ts_key = f"last_lead_msg:{sender_id}:{owner_id}"
        fu_key = f"followup_sent:{sender_id}:{owner_id}"
        try:
            _redis.set(ts_key, str(_time.time()))
            _redis.expire(ts_key, 1800)
            _redis.delete(fu_key)
        except Exception:
            pass

        # ── Buffer (debounce) ───────────────────────────────────────────
        buffer_key = f"buffer:{sender_id}:{owner_id}"
        task_key = f"buffer_task:{sender_id}:{owner_id}"

        try:
            msg_data = _json.dumps({
                "text": message.message or "",
                "message_id": message.message_id or "",
                "media_type": message.media_type or "text",
            })
            _redis.rpush(buffer_key, msg_data)
            _redis.expire(buffer_key, 30)

            old_task_id = _redis.get(task_key)
            if old_task_id:
                celery_app.control.revoke(old_task_id, terminate=False)

            result = process_buffered.apply_async(
                args=[sender_id, owner_id, owner.get("agent_mode", "both")],
                countdown=DEBOUNCE_SECONDS,
                queue="messages",
            )
            _redis.setex(task_key, 30, result.id)

            # Follow-up ativo (5 min)
            fu_task_key = f"followup_task:{sender_id}:{owner_id}"
            fu_result = follow_up_active.apply_async(
                args=[sender_id, owner_id, 1],
                countdown=300,
                queue="messages",
            )
            _redis.setex(fu_task_key, 600, fu_result.id)

        except Exception as e:
            logger.warning(f"[IG Webhook] Buffer falhou, processando direto: {e}")
            process_message.apply_async(
                args=[sender_id, owner_id, message.message,
                      owner.get("agent_mode", "both"),
                      message.message_id, message.media_type or "text"],
                queue="messages",
            )

    return {"status": "ok"}


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _get_owner_by_instagram(instance: str):
    """Busca owner pelo instagram_account_id."""
    try:
        db = MemoryService().db
        # instance vem como "ig_17841462631956604"
        ig_id = instance.replace("ig_", "") if instance.startswith("ig_") else instance
        result = db.table("owners").select("*").eq("instagram_account_id", ig_id).maybe_single().execute()
        if result and result.data:
            return result.data
    except Exception as e:
        logger.warning(f"[IG Webhook] Erro ao buscar owner por instagram_account_id: {e}")
    return None


async def _get_first_owner():
    """Fallback: retorna o primeiro owner cadastrado."""
    db = MemoryService().db
    result = db.table("owners").select("*").limit(1).execute()
    if result and result.data:
        return result.data[0]
    return None
