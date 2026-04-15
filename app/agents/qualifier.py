from app.services.ai import AIService
from app.services.memory import MemoryService
from app.services.whatsapp import WhatsAppService
import logging

logger = logging.getLogger(__name__)
HANDOFF_SCORE = 70

def build_qualifier_prompt(owner: dict, customer: dict, history_summary: str) -> str:
    name = owner.get("business_name", "a empresa")
    tone = owner.get("tone", "acolhedor e direto")
    values = ", ".join(owner.get("values", []) or [])
    vocab = ", ".join(owner.get("vocabulary", []) or [])
    offer = owner.get("main_offer", "nossos servicos")
    audience = owner.get("target_audience", "pessoas interessadas")
    objections = "\n- ".join(owner.get("common_objections", []) or [])
    context = owner.get("context_summary", "")
    emoji_style = owner.get("emoji_style", "medio")
    questions = owner.get("qualification_questions") or ["Voce esta buscando isso pra voce mesmo ou pra sua empresa?", "Ja tentou resolver isso antes?", "Tem disponibilidade para comecar esse mes?"]
    questions_text = "\n- ".join(questions)
    customer_name = customer.get("name") or "o lead"
    customer_summary = customer.get("summary") or "primeiro contato"
    customer_score = customer.get("lead_score", 0)
    return f"""Você é {name}, falando diretamente com um lead pelo WhatsApp.

QUEM VOCÊ É: {context}
OFERTA: {offer} | PÚBLICO: {audience}
TOM: {tone} | VOCABULÁRIO NATURAL: {vocab} | EMOJIS: {emoji_style}
VALORES: {values}
OBJEÇÕES COMUNS: {objections or 'nenhuma'}

LEAD ATUAL: {customer_name} | Score={customer_score}/100 | {customer_summary}

REGRAS DE OURO — SIGA À RISCA:
1. Escreva como humano no WhatsApp: frases curtas, linguagem natural, sem formalidade
2. ZERO bullet points, ZERO listas, ZERO asteriscos pra formatar texto
3. ZERO estrutura de relatório ou resumo técnico
4. Máximo 3 frases por mensagem — seja direto
5. Faça UMA pergunta por vez, de forma casual e curiosa
6. Use o vocabulário natural: {vocab}
7. Nunca revele que é IA, robô, sistema, automação ou assistente virtual
8. Se não puder fazer algo (ex: ligar, mandar mensagem primeiro), responda como humano faria: "não tô conseguindo agora" ou "me chama daqui a pouco" — NUNCA explique limitações técnicas
9. Emojis APENAS quando surgir naturalmente — nunca no final de toda frase, nunca pra enfeitar, nunca como hábito
9. Quando receber imagem/PDF/áudio: comente de forma natural em 1-2 frases e faça UMA pergunta relacionada
9. Perguntas de qualificação (use na ordem certa, sem forçar):
{questions_text}

HISTÓRICO: {history_summary or 'primeiro contato'}"""

class QualifierAgent:
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
            if not media_base64:
                logger.warning(f"[Qualifier] falha ao baixar mídia tipo={media_type} id={message_id}")

        if media_type == "audio" and media_base64:
            transcription = await self.ai.transcribe_audio(media_base64)
            if transcription:
                display_message = f"[Áudio]: {transcription}"
            media_base64 = None
            media_type = "text"
        elif media_type == "audio" and not media_base64:
            display_message = "[Áudio recebido - não foi possível processar]"
        # ────────────────────────────────────────────────────────────────────

        classification = await self.ai.classify_intent(display_message, context=customer.summary or "")
        intent = classification.get("intent", "outros")
        score_delta = classification.get("lead_score_delta", 0)
        is_simple = classification.get("is_simple", False)
        new_score = min(100, max(0, (customer.lead_score or 0) + score_delta))
        handoff_threshold = owner.get("handoff_threshold", HANDOFF_SCORE)
        if new_score >= handoff_threshold and customer.lead_score < handoff_threshold:
            await self._trigger_handoff(phone, owner, customer, display_message)
        await self.memory.save_turn(phone, owner_id, "user", display_message)
        system_prompt = build_qualifier_prompt(owner=owner, customer=customer.model_dump(), history_summary=customer.summary or "")

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
        await self.memory.update_customer(phone, owner_id, {"lead_score": new_score, "last_intent": intent, "total_messages": (customer.total_messages or 0) + 1})
        await self.whatsapp.send_typing(phone, duration=len(response) * 40)
        await self.whatsapp.send_message(phone, response)
        logger.info(f"[Qualifier] {phone} | intent={intent} | score={new_score} | media={media_type}")

    async def _trigger_handoff(self, phone: str, owner: dict, customer, message: str):
        notify_phone = owner.get("notify_phone")
        if not notify_phone:
            return
        customer_name = customer.name or phone
        alert = f"*Lead Quente!*\n\n{customer_name} ({phone})\nScore: {customer.lead_score}/100\nUltima mensagem: {message}\n\nAcesse o painel para ver o historico."
        await self.whatsapp.send_message(notify_phone, alert)
