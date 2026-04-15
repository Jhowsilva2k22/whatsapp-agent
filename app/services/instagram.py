import httpx
from app.config import get_settings
from app.models.message import IncomingMessage
import logging

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"


class InstagramService:
    def __init__(self):
        self.settings = get_settings()
        self.page_token = self.settings.meta_page_token
        self.ig_account_id = self.settings.instagram_account_id
        # Para enviar mensagens, a API exige o Facebook Page ID (não o IG Account ID)
        self.page_id = self.settings.meta_page_id or self.ig_account_id
        self.headers = {
            "Authorization": f"Bearer {self.page_token}",
            "Content-Type": "application/json",
        }

    async def send_message(self, recipient_id: str, text: str) -> dict:
        """Envia mensagem de texto via Instagram Messaging API."""
        url = f"{GRAPH_API}/{self.page_id}/messages"
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": text},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(url, json=payload, headers=self.headers)
                response.raise_for_status()
                result = response.json()
                logger.info(f"[IG Send] OK para {recipient_id}: {text[:50]}")
                return result
            except httpx.HTTPError as e:
                logger.error(f"[IG Send] Erro para {recipient_id}: {e}")
                # Log response body for debugging
                if hasattr(e, 'response') and e.response is not None:
                    logger.error(f"[IG Send] Response: {e.response.text[:500]}")
                raise

    async def get_user_profile(self, igsid: str) -> dict:
        """Busca nome e username do usuário pelo IGSID via Graph API."""
        url = f"{GRAPH_API}/{igsid}"
        params = {
            "fields": "name,username",
            "access_token": self.page_token,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                response = await client.get(url, params=params)
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"[IG Profile] {igsid} → name={data.get('name')} username={data.get('username')}")
                    return data
            except Exception as e:
                logger.warning(f"[IG Profile] Falha ao buscar perfil de {igsid}: {e}")
        return {}

    async def send_typing(self, recipient_id: str, duration: int = 3000):
        """Envia indicador de digitando (sender_action)."""
        url = f"{GRAPH_API}/{self.page_id}/messages"
        payload = {
            "recipient": {"id": recipient_id},
            "sender_action": "typing_on",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.post(url, json=payload, headers=self.headers)
            except Exception:
                pass

    def parse_webhook(self, payload: dict) -> list:
        """Parseia webhook do Instagram Messaging. Retorna lista de IncomingMessage."""
        messages = []

        if payload.get("object") != "instagram":
            return messages

        for entry in payload.get("entry", []):
            ig_id = entry.get("id", "")

            for event in entry.get("messaging", []):
                sender_id = event.get("sender", {}).get("id", "")
                recipient_id = event.get("recipient", {}).get("id", "")

                # Ignora mensagens enviadas por nós mesmos
                if sender_id == self.ig_account_id:
                    continue

                msg_data = event.get("message", {})
                if not msg_data:
                    # Pode ser postback, reaction, etc
                    # Tratamos reactions
                    reaction = event.get("reaction")
                    if reaction:
                        emoji = reaction.get("reaction", "👍")
                        messages.append(IncomingMessage(
                            instance=f"ig_{ig_id}",
                            phone=sender_id,
                            message=f"[Reagiu com {emoji}]",
                            message_id=reaction.get("mid", ""),
                            media_type="reaction",
                        ))
                    continue

                message_id = msg_data.get("mid", "")
                text = msg_data.get("text", "")

                # Attachments (imagem, audio, video, arquivo)
                attachments = msg_data.get("attachments", [])
                if attachments:
                    for att in attachments:
                        att_type = att.get("type", "")
                        att_url = att.get("payload", {}).get("url", "")

                        if att_type == "image":
                            caption = text or ""
                            msg_text = f"[Imagem]{': ' + caption if caption else ' recebida'}"
                            messages.append(IncomingMessage(
                                instance=f"ig_{ig_id}",
                                phone=sender_id,
                                message=msg_text,
                                message_id=message_id,
                                media_type="image",
                            ))
                        elif att_type == "audio":
                            messages.append(IncomingMessage(
                                instance=f"ig_{ig_id}",
                                phone=sender_id,
                                message="[Áudio recebido - por favor, escreva sua mensagem em texto]",
                                message_id=message_id,
                                media_type="audio",
                            ))
                        elif att_type == "video":
                            messages.append(IncomingMessage(
                                instance=f"ig_{ig_id}",
                                phone=sender_id,
                                message="[Vídeo recebido]",
                                message_id=message_id,
                                media_type="video",
                            ))
                        elif att_type in ("file", "document"):
                            messages.append(IncomingMessage(
                                instance=f"ig_{ig_id}",
                                phone=sender_id,
                                message="[Documento recebido]",
                                message_id=message_id,
                                media_type="document",
                            ))
                        elif att_type == "share":
                            # Story/post compartilhado
                            share_url = att.get("payload", {}).get("url", "")
                            msg_text = f"[Compartilhou um post]{': ' + text if text else ''}"
                            messages.append(IncomingMessage(
                                instance=f"ig_{ig_id}",
                                phone=sender_id,
                                message=msg_text,
                                message_id=message_id,
                                media_type="text",
                            ))
                        else:
                            messages.append(IncomingMessage(
                                instance=f"ig_{ig_id}",
                                phone=sender_id,
                                message=text or f"[{att_type} recebido]",
                                message_id=message_id,
                                media_type="text",
                            ))
                elif text:
                    # Mensagem de texto puro
                    messages.append(IncomingMessage(
                        instance=f"ig_{ig_id}",
                        phone=sender_id,
                        message=text,
                        message_id=message_id,
                        media_type="text",
                    ))

        return messages
