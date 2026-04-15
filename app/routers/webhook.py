from fastapi import APIRouter, Request, HTTPException
from app.services.whatsapp import WhatsAppService
from app.services.memory import MemoryService
from app.queues.tasks import process_message, learn_from_links
import logging
import re

logger = logging.getLogger(__name__)
router = APIRouter()
whatsapp = WhatsAppService()
memory = MemoryService()

# Prefixos que o DONO usa para ensinar o bot
LEARN_PREFIXES = ("aprender:", "aprender ", "configurar:", "configurar ", "link:", "base:")

# Comandos de controle de handoff
HANDOFF_ASSUME = ("assumir ", "assumindo ", "vou atender ", "atendendo ")
HANDOFF_RESUME = ("retomar ", "retomando ", "devolver ", "bot ")
NOTE_PREFIX    = ("nota ", "anotacao ", "anotação ")

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

    owner_phone = _normalize_phone(owner.get("phone", ""))
    sender_phone = _normalize_phone(message.phone)
    msg_raw = message.message or ""
    msg_lower = msg_raw.lower().strip()

    # ── Comandos exclusivos do DONO ──────────────────────────────────────────
    if sender_phone == owner_phone and message.media_type == "text":

        # APRENDER: alimentar base de conhecimento
        if any(msg_lower.startswith(p) for p in LEARN_PREFIXES):
            links = _extract_urls(msg_raw)
            if links:
                learn_from_links.apply_async(args=[owner["id"], links], queue="learning")
                await whatsapp.send_message(
                    message.phone,
                    f"📚 Recebi {len(links)} link(s)! Vou processar e aprender. Pode levar até 2 minutos."
                )
            return {"status": "learning_queued"}

        # ASSUMIR: dono vai atender o lead pessoalmente
        if any(msg_lower.startswith(p) for p in HANDOFF_ASSUME):
            lead_phone = _extract_phone(msg_raw)
            if lead_phone:
                await _owner_assumes(lead_phone, owner, msg_raw)
                return {"status": "handoff_assumed"}

        # RETOMAR: devolver lead pro bot
        if any(msg_lower.startswith(p) for p in HANDOFF_RESUME):
            lead_phone = _extract_phone(msg_raw)
            if lead_phone:
                await _owner_resumes(lead_phone, owner)
                return {"status": "handoff_resumed"}

        # NOTA: dono adiciona anotação pós-conversa
        if any(msg_lower.startswith(p) for p in NOTE_PREFIX):
            lead_phone = _extract_phone(msg_raw)
            note_text = _extract_note(msg_raw)
            if lead_phone and note_text:
                await _save_owner_note(lead_phone, owner, note_text)
                await whatsapp.send_message(message.phone, "✅ Anotação salva no perfil do lead.")
                return {"status": "note_saved"}

    # ── Bloqueia bot se lead está em atendimento humano ──────────────────────
    if sender_phone != owner_phone:
        customer = await memory.get_or_create_customer(message.phone, owner["id"])
        if customer.lead_status == "em_atendimento_humano":
            # Verifica se é dia seguinte — se sim, retoma o bot automaticamente
            if _is_next_day(customer.last_contact):
                await memory.update_customer(
                    message.phone, owner["id"],
                    {"lead_status": "qualificando"}
                )
                logger.info(f"[Webhook] Bot retomado automaticamente para {message.phone} (dia seguinte)")
                # Deixa cair no fluxo normal abaixo
            else:
                logger.info(f"[Webhook] Ignorado — lead {message.phone} em atendimento humano")
                return {"status": "in_human_handoff"}

    # ── Fluxo normal de atendimento ──────────────────────────────────────────
    process_message.apply_async(
        args=[message.phone, owner["id"], message.message, owner.get("agent_mode", "both"),
              message.message_id, message.media_type or "text"],
        queue="messages",
        routing_key=f"phone.{message.phone}"
    )
    return {"status": "queued"}


@router.get("/webhook/health")
async def health():
    return {"status": "ok", "service": "whatsapp-agent"}


# ── Helpers de handoff ────────────────────────────────────────────────────────

async def _owner_assumes(lead_phone: str, owner: dict, raw_msg: str):
    """Dono assume atendimento: marca lead, envia despedida natural ao lead."""
    owner_id = owner["id"]
    owner_name = owner.get("business_name", "a equipe")

    # Nome para a despedida — pega primeiro nome do business_name
    first_name = owner_name.split()[0] if owner_name else "eu"

    customer = await memory.get_or_create_customer(lead_phone, owner_id)
    customer_name = customer.name or ""

    # Marca lead como em atendimento humano
    await memory.update_customer(lead_phone, owner_id, {"lead_status": "em_atendimento_humano"})

    # Despedida natural ao lead
    greeting = f"{customer_name}, " if customer_name else ""
    farewell = (
        f"{greeting}o {first_name} vai falar com você agora em instantes. "
        f"Foi ótimo conversar contigo 😊"
    )
    await whatsapp.send_message(lead_phone, farewell)

    # Confirma pro dono com relatório do lead
    report = await _build_lead_report(customer, lead_phone)
    await whatsapp.send_message(
        _normalize_phone(owner.get("phone", "")),
        f"✅ Pronto! Avisei o lead. Aqui está o resumo:\n\n{report}"
    )
    logger.info(f"[Handoff] {lead_phone} assumido pelo dono")


async def _owner_resumes(lead_phone: str, owner: dict):
    """Dono devolve lead pro bot."""
    owner_id = owner["id"]
    await memory.update_customer(lead_phone, owner_id, {"lead_status": "qualificando"})
    await whatsapp.send_message(
        _normalize_phone(owner.get("phone", "")),
        f"🤖 Bot retomado para {lead_phone}. Ele vai cuidar desse lead de agora em diante."
    )
    logger.info(f"[Handoff] {lead_phone} devolvido ao bot")


async def _save_owner_note(lead_phone: str, owner: dict, note: str):
    """Salva anotação do dono no summary do cliente."""
    from datetime import datetime
    owner_id = owner["id"]
    customer = await memory.get_or_create_customer(lead_phone, owner_id)
    existing = customer.summary or ""
    timestamp = datetime.utcnow().strftime("%d/%m")
    new_summary = f"{existing}\n[Nota {timestamp}]: {note}".strip()
    await memory.update_customer(lead_phone, owner_id, {"summary": new_summary})


async def _build_lead_report(customer, phone: str) -> str:
    wa_link = f"wa.me/{_normalize_phone(phone)}"
    name = customer.name or "Sem nome"
    score = customer.lead_score or 0
    channel = customer.channel or "não identificado"
    summary = customer.summary or "sem histórico registrado"
    total = customer.total_messages or 0
    return (
        f"👤 *{name}*\n"
        f"📱 {wa_link}\n"
        f"📊 Score: {score}/100\n"
        f"📍 Canal: {channel}\n"
        f"💬 Mensagens: {total}\n"
        f"📝 Resumo: {summary[:300]}"
    )


def _is_next_day(last_contact) -> bool:
    """Retorna True se o último contato foi em um dia diferente do atual."""
    if not last_contact:
        return False
    from datetime import datetime, timezone
    try:
        if hasattr(last_contact, 'date'):
            last_date = last_contact.date()
        else:
            from datetime import date
            last_date = datetime.fromisoformat(str(last_contact)).date()
        return last_date < datetime.utcnow().date()
    except Exception:
        return False


# ── Helpers de parsing ────────────────────────────────────────────────────────

async def _get_owner_by_instance(instance: str):
    db = memory.db
    result = db.table("owners").select("*").eq("evolution_instance", instance).maybe_single().execute()
    return result.data if result and result.data else None


def _normalize_phone(phone: str) -> str:
    return re.sub(r'\D', '', phone or "")


def _extract_phone(text: str) -> str:
    """Extrai número de telefone de um texto como 'assumir 5513999...'"""
    digits = re.findall(r'[\d\s\-\+\(\)]{8,}', text)
    for d in digits:
        clean = re.sub(r'\D', '', d)
        if len(clean) >= 8:
            return clean
    return ""


def _extract_note(text: str) -> str:
    """Remove o prefixo e o número, retorna o texto da nota."""
    # Remove prefixo tipo "nota 5513999... texto aqui"
    cleaned = re.sub(r'^(nota|anotacao|anotação)\s+', '', text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r'^\+?[\d\s\-\(\)]{8,}\s*', '', cleaned).strip()
    return cleaned


def _extract_urls(text: str) -> list:
    pattern = r'https?://[^\s]+'
    return list(set(re.findall(pattern, text)))
