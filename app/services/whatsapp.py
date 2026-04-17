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

    def _instance(self, instance: str = None) -> str:
        """Retorna a instância correta: a do owner (multi-tenant) ou a padrão do env."""
        return instance or self.settings.evolution_instance

    async def send_message(self, phone: str, text: str, instance: str = None) -> dict:
        inst = self._instance(instance)
        url = f"{self.base_url}/message/sendText/{inst}"
        phone = self._format_phone(phone)
        payload = {"number": phone, "text": text, "delay": 1200}
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(url, json=payload, headers=self.headers)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as e:
                logger.error(f"Erro ao enviar mensagem para {phone} via {inst}: {e}")
                raise

    async def send_typing(self, phone: str, duration: int = 3000, instance: str = None):
        inst = self._instance(instance)
        url = f"{self.base_url}/chat/sendPresence/{inst}"
        phone = self._format_phone(phone)
        payload = {"number": phone, "presence": "composing", "delay": duration}
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.post(url, json=payload, headers=self.headers)
            except Exception:
                pass

    async def download_media_base64(self, message_id: str, phone: str = "", instance: str = None) -> str | None:
        """
        Baixa mídia de uma mensagem via Evolution API e retorna base64.
        Tenta múltiplos formatos de payload para compatibilidade com
        diferentes versões da Evolution API.
        """
        inst = self._instance(instance)
        raw_phone = (
            phone.replace("+", "").replace("-", "").replace(" ", "")
            .replace("@s.whatsapp.net", "")
        ) if phone else ""

        url = f"{self.base_url}/chat/getBase64FromMediaMessage/{inst}"

        # Tentativa 1: com remoteJid (formato padrão)
        # Tentativa 2: sem remoteJid (algumas versões não precisam)
        payloads = [
            {
                "message": {
                    "key": {
                        "id": message_id,
                        "fromMe": False,
                        "remoteJid": f"{raw_phone}@s.whatsapp.net",
                    }
                },
                "convertToMp4": False,
            },
            {
                "message": {
                    "key": {
                        "id": message_id,
                        "fromMe": False,
                    }
                },
                "convertToMp4": False,
            },
        ]

        async with httpx.AsyncClient(timeout=30) as client:
            for attempt, payload in enumerate(payloads, start=1):
                try:
                    response = await client.post(url, json=payload, headers=self.headers)
                    logger.info(
                        f"[Media] attempt={attempt} status={response.status_code} "
                        f"id={message_id} phone={raw_phone} inst={inst}"
                    )
                    if response.status_code in (200, 201):
                        try:
                            data = response.json()
                        except Exception:
                            logger.warning(f"[Media] attempt={attempt} resposta não é JSON: {response.text[:300]}")
                            continue
                        b64 = (
                            data.get("base64")
                            or data.get("data", {}).get("base64")
                        )
                        if b64 and len(b64) > 100:
                            logger.info(f"[Media] OK attempt={attempt} id={message_id} bytes={len(b64)}")
                            return b64
                        logger.warning(
                            f"[Media] attempt={attempt} base64 ausente ou curto. "
                            f"keys={list(data.keys())} body={str(data)[:300]}"
                        )
                    else:
                        logger.error(
                            f"[Media] attempt={attempt} HTTP {response.status_code}: "
                            f"{response.text[:500]}"
                        )
                except Exception as e:
                    logger.error(f"[Media] attempt={attempt} exceção: {e}")

        logger.error(f"[Media] todas as tentativas falharam para id={message_id}")
        return None

    def parse_webhook(self, payload: dict):
        """Parseia qualquer tipo de mensagem do webhook da Evolution API."""
        try:
            event = payload.get("event", "")
            # Evolution API pode enviar "MESSAGES_UPSERT" (uppercase) ou "messages.upsert" (lowercase)
            if event.lower().replace("_", ".") != "messages.upsert":
                return None

            data = payload.get("data", {})
            key = data.get("key", {})

            if key.get("fromMe", False):
                return None

            message_data = data.get("message", {})

            # --- Extrai o número de telefone real ---
            # WhatsApp com Linked Devices envia remoteJid no formato "@lid".
            # Nesse caso, o número real está em remoteJidAlt no formato @s.whatsapp.net.
            remote_jid = key.get("remoteJid", "")
            if "@lid" in remote_jid:
                alt_jid = key.get("remoteJidAlt", "") or ""
                if alt_jid:
                    logger.debug(f"[Webhook] LID detectado: {remote_jid} → usando alt: {alt_jid}")
                    remote_jid = alt_jid
                else:
                    logger.warning(f"[Webhook] LID sem remoteJidAlt: {remote_jid}")

            phone = (
                remote_jid
                .replace("@s.whatsapp.net", "")
                .replace("@g.us", "")
                .replace("@lid", "")
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
                    instance=instance, phone=phone, message=conversation,
                    message_id=message_id, media_type="text",
                )

            # --- IMAGEM ---
            img = message_data.get("imageMessage")
            if img:
                caption = img.get("caption") or ""
                text = f"[Imagem]{': ' + caption if caption else ' recebida'}"
                return IncomingMessage(
                    instance=instance, phone=phone, message=text,
                    message_id=message_id, media_type="image",
                )

            # --- ÁUDIO / PTT ---
            audio = message_data.get("audioMessage") or message_data.get("pttMessage")
            if audio:
                return IncomingMessage(
                    instance=instance, phone=phone,
                    message="[Áudio recebido]",
                    message_id=message_id, media_type="audio",
                )

            # --- VÍDEO ---
            video = message_data.get("videoMessage")
            if video:
                caption = video.get("caption") or ""
                text = f"[Vídeo]{': ' + caption if caption else ' recebido'}"
                return IncomingMessage(
                    instance=instance, phone=phone, message=text,
                    message_id=message_id, media_type="video",
                )

            # --- DOCUMENTO / PDF ---
            doc = message_data.get("documentMessage")
            if not doc:
                dwc = message_data.get("documentWithCaptionMessage", {})
                doc = dwc.get("message", {}).get("documentMessage")
            if doc:
                filename = doc.get("fileName") or "documento"
                caption = doc.get("caption") or ""
                mime = (doc.get("mimetype") or "").lower()

                # JPEG/PNG/WEBP enviados como documento → tratar como imagem
                _image_mimes = ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif")
                if any(mime.startswith(m) for m in _image_mimes):
                    text = f"[Imagem]{': ' + caption if caption else ' recebida'}"
                    return IncomingMessage(
                        instance=instance, phone=phone, message=text,
                        message_id=message_id, media_type="image",
                    )

                tipo = "PDF" if "pdf" in mime else "Documento"
                text = f"[{tipo}: {filename}]"
                if caption:
                    text += f" {caption}"
                return IncomingMessage(
                    instance=instance, phone=phone, message=text,
                    message_id=message_id, media_type="document",
                )

            # --- STICKER ---
            if message_data.get("stickerMessage"):
                return IncomingMessage(
                    instance=instance, phone=phone, message="[Sticker 😄]",
                    message_id=message_id, media_type="sticker",
                )

            # --- LOCALIZAÇÃO ---
            loc = message_data.get("locationMessage")
            if loc:
                name = loc.get("name") or ""
                lat = loc.get("degreesLatitude", "")
                lng = loc.get("degreesLongitude", "")
                text = f"[Localização{': ' + name if name else ''} ({lat},{lng})]"
                return IncomingMessage(
                    instance=instance, phone=phone, message=text,
                    message_id=message_id, media_type="location",
                )

            # --- CONTATO ---
            contact = message_data.get("contactMessage")
            if contact:
                display = contact.get("displayName") or "contato"
                return IncomingMessage(
                    instance=instance, phone=phone,
                    message=f"[Contato compartilhado: {display}]",
                    message_id=message_id, media_type="contact",
                )

            # --- REAÇÃO ---
            reaction = message_data.get("reactionMessage")
            if reaction:
                emoji = reaction.get("text") or "👍"
                return IncomingMessage(
                    instance=instance, phone=phone,
                    message=f"[Reagiu com {emoji}]",
                    message_id=message_id, media_type="reaction",
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
