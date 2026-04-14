from fastapi import APIRouter, Request, HTTPException
from app.services.whatsapp import WhatsAppService
from app.services.memory import MemoryService
from app.queues.tasks import process_message
import logging

logger = logging.getLogger(__name__)
router = APIRouter()
whatsapp = WhatsAppService()
memory = MemoryService()

@router.post("/webhook/whatsapp")
async def receive_whatsapp(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload invalido")
    message = whatsapp.parse_webhook(payload)
    if not message:
        return {"status": "ignored"}
    owner = await _get_owner_by_instance(message.instance)
    if not owner:
        return {"status": "owner_not_found"}
    process_message.apply_async(
        args=[message.phone, owner["id"], message.message, owner.get("agent_mode", "both")],
        queue="messages",
        routing_key=f"phone.{message.phone}"
    )
    return {"status": "queued"}

@router.get("/webhook/health")
async def health():
    return {"status": "ok", "service": "whatsapp-agent"}

async def _get_owner_by_instance(instance: str):
    db = memory.db
    result = db.table("owners").select("*").eq("evolution_instance", instance).maybe_single().execute()
    return result.data if result and result.data else None
