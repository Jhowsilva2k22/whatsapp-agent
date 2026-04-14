import httpx
import base64
from app.config import get_settings
from app.models.message import IncomingMessage
import logging

logger = logging.getLogger(__name__)


class WhatsAppService:
    def __init__(self):
        self.settings = get_settings()
        self.base_url = self.settings.evolution_api_url.rstrip("/")
        self.headers = {
            "apikey": self.settings.evolution_api_key,
            "Content-Type": "application/json",
        }

    async def send_message(self, phone: str, text: str) -> dict:
        url = f"{self.base_url}/message/sendText/{self.settings.evolution_instance}"
        phone = self._format_phone(phone)
        payload = {"number": phone, "text": text, "delay": 1200}
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(url, json=payload, headers=self.headers)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as e:
                logger.error(f"Erro ao enviar mensagem para {phone}: {e}")
                raise

    async def send_typing(self, phone: str, duration: int = 3000):
        url = f"{self.base_url}/chat/sendPresence/{self.settings.evolution_instance}"
        phone = self._format_phone(phone)
        payload = {"number": phone, "options": {"presence": "composing", "delay": duration}}
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.post(url, json=payload, headers=self.headers)
            except Exception:
                pass

    async def download_media_base64(self, message_id: str) -> str | None:
        """Baixa mídia de uma mensagem via Evolution API e retorna base64."""
        url = f"{self.base_url}/chat/getBase64FromMediaMessage/{self.settings.evolution_instance}"
        payload = {"message": {"key": {"id": message_id}}, "convertToMp4": False}
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(url, json=payload, headers=self.headers)
                if response.status_code == 200:
                    return response.json().get("base64")
            except Exception as e:
                logger.error(f"Erro ao baixar mídia {message_id}: {e}")
        return None

    def parse_webhook(self, payload: dict):
        """Parseia qualquer tipo de mensagem do webhook da Evolution API."""
        try:
            event = payload.get("event", "")
            if event != "messages.upsert":
                return None

            data = payload.get("data", {})
            key = data.get("key", {})

            if key.get("fromMe", False):
                return None

            message_data = data.get("message", {})
            phone = (
                key.get("remoteJid", "")
                .replace("@s.whatsapp.net", "")
                .replace("@g.us", "")
            )
            instance = payload.get("instance", "")
            message_id = key.get("id", "")

            # --- TEXTO ---
            conversation = (
                message_data.get("conversation")
                or message_data.get("extendedTextMessage", {}).get("text")
                or ""
            )
            if conversation:
                return IncomingMessage(
                    instance=instance,
                    phone=phone,
                    message=conversation,
                    message_id=message_id,
                    media_type="text",
                )

            # --- IMAGEM ---
            img = message_data.get("imageMessage")
            if img:
                caption = img.get("caption") or ""
                text = f"[Imagem]{': ' + caption if caption else ' recebida'}"
                return IncomingMessage(
                    instance=instance,
                    phone=phone,
                    message=text,
                    message_id=message_id,
                    media_type="image",
                )

            # --- ÁUDIO / PTT ---
            audio = message_data.get("audioMessage") or message_data.get("pttMessage")
            if audio:
                return IncomingMessage(
                    instance=instance,
                    phone=phone,
                    message="[Áudio recebido - por favor, escreva sua mensagem em texto]",
                    message_id=message_id,
                    media_type="audio",
                )

            # --- VÍDEO ---
            video = message_data.get("videoMessage")
            if video:
                caption = video.get("caption") or ""
                text = f"[Vídeo]{': ' + caption if caption else ' recebido'}"
                return IncomingMessage(
                    instance=instance,
                    phone=phone,
                    message=text,
                    message_id=message_id,
                    media_type="video",
                )

            # --- DOCUMENTO / PDF ---
            doc = message_data.get("documentMessage")
            if not doc:
                dwc = message_data.get("documentWithCaptionMessage", {})
                doc = dwc.get("message", {}).get("documentMessage")
            if doc:
                filename = doc.get("fileName") or "documento"
                caption = doc.get("caption") or ""
                mime = doc.get("mimetype") or ""
                tipo = "PDF" if "pdf" in mime.lower() else "Documento"
                text = f"[{tipo}: {filename}]"
                if caption:
                    text += f" {caption}"
                return IncomingMessage(
                    instance=instance,
                    phone=phone,
                    message=text,
                    message_id=message_id,
                    media_type="document",
                )

            # --- STICKER ---
            if message_data.get("stickerMessage"):
                return IncomingMessage(
                    instance=instance,
                    phone=phone,
                    message="[Sticker 😄]",
                    message_id=message_id,
                    media_type="sticker",
                )

            # --- LOCALIZAÇÃO ---
            loc = message_data.get("locationMessage")
            if loc:
                name = loc.get("name") or ""
                lat = loc.get("degreesLatitude", "")
                lng = loc.get("degreesLongitude", "")
                text = f"[Localização{': ' + name if name else ''} ({lat},{lng})]"
                return IncomingMessage(
                    instance=instance,
                    phone=phone,
                    message=text,
                    message_id=message_id,
                    media_type="location",
                )

            # --- CONTATO ---
            contact = message_data.get("contactMessage")
            if contact:
                display = contact.get("displayName") or "contato"
                return IncomingMessage(
                    instance=instance,
                    phone=phone,
                    message=f"[Contato compartilhado: {display}]",
                    message_id=message_id,
                    media_type="contact",
                )

            # --- REAÇÃO ---
            reaction = message_data.get("reactionMessage")
            if reaction:
                emoji = reaction.get("text") or "👍"
                return IncomingMessage(
                    instance=instance,
                    phone=phone,
                    message=f"[Reagiu com {emoji}]",
                    message_id=message_id,
                    media_type="reaction",
                )

            logger.debug(f"Tipo de mensagem não mapeado: {list(message_data.keys())}")
            return None

        except Exception as e:
            logger.error(f"Erro ao parsear webhook: {e}")
            return None

    def _format_phone(self, phone: str) -> str:
        phone = phone.replace("+", "").replace("-", "").replace(" ", "")
        if not phone.endswith("@s.whatsapp.net"):
            phone = f"{phone}@s.whatsapp.net"
        return phone
