"""Roteador de mensagens multi-canal.
Envia via WhatsApp ou Instagram conforme o canal do customer."""

from app.services.whatsapp import WhatsAppService
import logging

logger = logging.getLogger(__name__)

_wa = None
_ig = None


def _get_wa():
    global _wa
    if not _wa:
        _wa = WhatsAppService()
    return _wa


def _get_ig():
    global _ig
    if not _ig:
        from app.services.instagram import InstagramService
        _ig = InstagramService()
    return _ig


async def send_message(phone: str, text: str, channel: str = "whatsapp"):
    """Envia mensagem pro lead pelo canal correto."""
    if channel == "instagram":
        await _get_ig().send_message(phone, text)
    else:
        await _get_wa().send_message(phone, text)


async def send_typing(phone: str, channel: str = "whatsapp", duration: int = 3000):
    """Envia indicador de digitação pelo canal correto."""
    if channel == "instagram":
        await _get_ig().send_typing(phone, duration)
    else:
        await _get_wa().send_typing(phone, duration)


async def download_media(message_id: str, phone: str = "", channel: str = "whatsapp"):
    """Baixa mídia. Só WhatsApp tem download via Evolution API."""
    if channel == "whatsapp":
        return await _get_wa().download_media_base64(message_id, phone=phone)
    # Instagram: mídia vem por URL, não suportamos download ainda
    return None
