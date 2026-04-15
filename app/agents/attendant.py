from app.services.ai import AIService
from app.services.memory import MemoryService
from app.services.whatsapp import WhatsAppService
import logging

logger = logging.getLogger(__name__)

def build_attendant_prompt(owner: dict, customer: dict, history_summary: str) -> str:
    name = owner.get("business_name", "a empresa")
    tone = owner.get("tone", "acolhedor e direto")
    vocab = ", ".join(owner.get("vocabulary", []) or [])
    context = owner.get("context_summary", "")
    emoji_style = owner.get("emoji_style", "medio")
    faqs = owner.get("faqs") or []
    faqs_text = "\n- ".join(faqs) if faqs else "nenhuma FAQ cadastrada"
    customer_name = customer.get("name") or "o cliente"
    customer_summary = customer.get("summary") or "primeiro contato"
    return f"""Você é {name}, atendendo um cliente pelo WhatsApp.

NEGÓCIO: {context}
TOM: {tone} | VOCABULÁRIO: {vocab} | EMOJIS: {emoji_style}
CLIENTE: {customer_name} | {customer_summary}
FAQs: {faqs_text}

REGRAS DE OURO — SIGA À RISCA:
1. Escreva como humano no WhatsApp: frases curtas, linguagem natural, sem formalidade
2. ZERO bullet points, ZERO listas, ZERO asteriscos pra formatar texto
3. ZERO estrutura de relatório — responda como numa conversa real
4. Máximo 4 frases por mensagem
5. Nunca revele que é IA, robô, sistema, automação ou assistente virtual
6. Se não puder fazer algo, responda como humano: "agora não dá" ou "me chama mais tarde" — NUNCA explique limitações técnicas
7. Quando receber imagem/PDF/áudio: reaja de forma natural e humana em 1-2 frases
7. Use o vocabulário natural: {vocab}
8. Emojis APENAS quando surgir naturalmente — nunca no final de toda frase, nunca pra enfeitar, nunca como hábito

HISTÓRICO: {history_summary or 'primeiro contato'}"""

class AttendantAgent:
    def __init__(self):
        self.ai = AIService()
        self.memory = MemoryService()
        self.whatsapp = WhatsAppService()

    async def process(self, phone: str, owner_id: str, message: str,
                      message_id: str = "", media_type: str = "text"):
        customer = await self.memory.get_or_create_customer(phone, owner_id)
        owner = await self.memory.get_owner_context(owner_id)
        if not owner:
            return
        history = await self.memory.get_conversation_history(phone, owner_id)

        # ── Processa mídia (mantém fluxo de texto intacto) ──────────────────
        display_message = message
        media_base64 = None

        if media_type in ("image", "audio", "document") and message_id:
            media_base64 = await self.whatsapp.download_media_base64(message_id, phone=phone)

        if media_type == "audio" and media_base64:
            transcription = await self.ai.transcribe_audio(media_base64)
            if transcription:
                display_message = f"[Áudio]: {transcription}"
            media_base64 = None
            media_type = "text"
        # ────────────────────────────────────────────────────────────────────

        classification = await self.ai.classify_intent(display_message, context=customer.summary or "")
        is_simple = classification.get("is_simple", False)
        intent = classification.get("intent", "outros")
        await self.memory.save_turn(phone, owner_id, "user", display_message)
        system_prompt = build_attendant_prompt(owner=owner, customer=customer.model_dump(), history_summary=customer.summary or "")

        if media_type == "image" and media_base64:
            response = await self.ai.respond_with_image(
                system_prompt=system_prompt, history=history,
                user_message=message, image_base64=media_base64)
        elif media_type == "document" and media_base64:
            response = await self.ai.respond_with_pdf(
                system_prompt=system_prompt, history=history,
                user_message=message, pdf_base64=media_base64)
        else:
            response = await self.ai.respond(
                system_prompt=system_prompt, history=history,
                user_message=display_message, use_gemini=is_simple)

        await self.memory.save_turn(phone, owner_id, "assistant", response)
        await self.memory.update_customer(phone, owner_id, {"last_intent": intent, "total_messages": (customer.total_messages or 0) + 1})
        await self.whatsapp.send_typing(phone, duration=len(response) * 40)
        await self.whatsapp.send_message(phone, response)
        logger.info(f"[Attendant] {phone} | intent={intent} | media={media_type}")
