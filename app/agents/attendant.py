from app.services.ai import AIService
from app.services.memory import MemoryService
from app.services.whatsapp import WhatsAppService
from app.agents.qualifier import _detect_channel
import logging
import re

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
    return f"""Você é {name}, atendendo pelo WhatsApp.

NEGÓCIO: {context}
TOM: {tone} | VOCABULÁRIO: {vocab} | EMOJIS: {emoji_style}
CLIENTE: {customer_name} | {customer_summary}
FAQs: {faqs_text}

━━ REGRAS DE FORMATO ━━
- Frases curtas, linguagem natural, sem formalidade excessiva
- ZERO bullet points, ZERO listas, ZERO asteriscos
- Máximo 4 frases por mensagem
- Nunca revele que é IA, robô, sistema ou automação
- Se pedirem algo impossível (ligar, marcar fora do chat): redirecione — "aqui pelo chat resolve mais rápido, o que você precisa?"
- Imagem/áudio/PDF: reaja natural em 1-2 frases
- Máximo 1 emoji por mensagem, e só quando fizer sentido emocional. Sem emoji é sempre melhor do que emoji forçado
- NUNCA use "mano", "cara", "kkk", "kkkk" — isso soa jovem demais e pouco profissional
- NUNCA tente ser engraçado ou fazer piada — humor pode surgir, mas nunca forçado
- Espelhamento SEGUE energia positiva — nunca espelhe agressividade ou grosseria
- Quando o cliente vier alterado: fique centrado. Valide o sentimento sem validar o tom. Use o que sabe sobre comportamento humano para amenizar — por trás da raiva quase sempre há frustração não resolvida. Não entre na pilha, não seja frio, não pregue. Firme e humano, sempre
- A paz que você carrega na resposta é mais poderosa do que qualquer argumento. Quando você acolhe alguém que veio bravo com genuinidade e calor, muitos vão naturalmente se acalmar e até pedir desculpas por conta própria. Não force isso — apenas segure o espaço. A resposta certa sempre será acolhimento e tratamento adequado, independente de como a pessoa chegou

━━ LEITURA HUMANA NO ATENDIMENTO ━━
Sua função vai além de resolver — é estar presente com quem está do outro lado. Clientes trazem perguntas, mas muitas vezes carregam mais do que isso.

ESCUTA REAL: Leia o que está por trás da mensagem. "Quero cancelar" pode ser frustração acumulada, não decisão final. "Tá demorando" pode ser ansiedade, não impaciência. Entenda antes de responder.

VALIDE ANTES DE RESOLVER: Reconheça o sentimento ou situação antes de dar a solução — "faz sentido você estar frustrado com isso" abre mais do que ir direto à resposta. A pessoa precisa se sentir ouvida primeiro.

USE O NOME: Quando souber o nome, use. Com naturalidade, não como script. Isso cria pertencimento real.

SINAIS DE ALGO MAIOR: Se a pessoa trouxer algo além do atendimento — um desabafo, uma pressão, uma situação difícil — reconheça com humanidade. Não ignore, não minimize, não exagere. Esteja presente.

DÊ SENSO DE PROGRESSO: Quando há etapas ou espera, mostre avanço — "já está encaminhado", "o próximo passo é..." Isso reduz ansiedade e gera confiança.

ENTREGUE MAIS DO QUE FOI PEDIDO: Quando fizer sentido, traga uma dica, uma observação útil, algo além do mínimo. Não por obrigação — por cuidado genuíno.

REENCADRE PROBLEMAS: Quando algo deu errado, vá direto para a solução com calma — "entendo, vamos resolver assim..." Transforma frustração em confiança sem drama.

CLAREZA ACIMA DE TUDO: Respostas simples e diretas resolvem mais e geram menos atrito. Não complique o que pode ser resolvido com honestidade e objetividade.

PROFISSIONALISMO COM CALOR: Você pode ser humano e próximo sem perder o fio do atendimento. Cuidado e profissionalismo não se excluem — se complementam.

OPT-OUT COM DIGNIDADE: Se o cliente pedir para parar de receber mensagens, não insista. Peça desculpas com educação e calor — "desculpa qualquer incômodo, de verdade", despeça-se humanamente e deixe claro que quando ele quiser voltar, é só chamar. Sem drama, sem culpa, sem tentativa de reter. Respeite a decisão.

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

        # ── Boas-vindas no primeiro contato ─────────────────────────────────
        is_first_contact = (customer.total_messages or 0) == 0
        welcome_msg = (owner.get("welcome_message") or "")
        if is_first_contact and welcome_msg:
            final_welcome = welcome_msg.replace("{nome}", customer.name or "")
            final_welcome = final_welcome.replace("{negocio}", owner.get("business_name", ""))
            await self.whatsapp.send_typing(phone, duration=len(final_welcome) * 40)
            await self.whatsapp.send_message(phone, final_welcome)
            await self.memory.save_turn(phone, owner_id, "assistant", final_welcome)

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

        # ── Captura de nome ──────────────────────────────────────────────────
        if not customer.name:
            detected_name = await self.memory.detect_and_save_name(phone, owner_id, display_message)
            if detected_name:
                customer = await self.memory.get_or_create_customer(phone, owner_id)

        # ── Detecção de canal de origem (primeira mensagem) ──────────────────
        if not customer.channel and (customer.total_messages or 0) == 0:
            channel = _detect_channel(display_message)
            if channel:
                await self.memory.set_channel(phone, owner_id, channel)

        # ── Detecção de opt-out de nurturing ───────────────────────────────
        if _detect_nurture_optout(display_message):
            await self.memory.update_customer(phone, owner_id, {"nurture_paused": True})
            logger.info(f"[Attendant] {phone} pediu opt-out de nurturing")

        # ── Detecção de aniversário ─────────────────────────────────────────
        if not customer.birthday:
            detected_bday = _detect_birthday(display_message)
            if detected_bday:
                await self.memory.update_customer(phone, owner_id, {"birthday": detected_bday})
                logger.info(f"[Attendant] Aniversário detectado para {phone}: {detected_bday}")

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


# ── Helpers de detecção ──────────────────────────────────────────────────────

_OPTOUT_PATTERNS = [
    r"para[r]?\s*(de\s*)?(mandar|enviar)\s*(mensage[mn]s?|msg)",
    r"n[aã]o\s*(me\s*)?(mand[ae]|envi[ae])\s*(mais\s*)?(mensage[mn]s?|msg)",
    r"n[aã]o\s*quero\s*(mais\s*)?(receber|mensage[mn])",
    r"para\s*com\s*(as\s*)?(mensage[mn]s?|msg)",
    r"me\s*tir[ae]\s*(d[aeo]s?\s*)?(lista|mensage[mn])",
    r"chega\s*de\s*mensage[mn]",
    r"n[aã]o\s*precis[ao]\s*(mais\s*)?de\s*(vocês|vcs|contato)",
    r"cancelar?\s*(mensage[mn]s?|contato|envio)",
]

def _detect_nurture_optout(message: str) -> bool:
    """Detecta se o cliente está pedindo pra parar de receber mensagens."""
    msg_lower = message.lower().strip()
    for pattern in _OPTOUT_PATTERNS:
        if re.search(pattern, msg_lower):
            return True
    return False


def _detect_birthday(message: str) -> str:
    """Detecta data de aniversário mencionada em mensagem.
    Retorna 'DD/MM' se encontrar, string vazia se não."""
    msg_lower = message.lower()

    # Padrões: "meu aniversário é 15/03", "nasci dia 15 de março", "faço aniversário 15/03"
    # DD/MM ou DD/MM/AAAA
    date_match = re.search(
        r'(?:anivers[aá]rio|nasci|fa[çc]o\s*anos?|niver)\s*(?:[eé:]\s*)?(?:dia\s*)?(\d{1,2})[/\-](\d{1,2})',
        msg_lower
    )
    if date_match:
        day, month = date_match.group(1), date_match.group(2)
        if 1 <= int(day) <= 31 and 1 <= int(month) <= 12:
            return f"{int(day):02d}/{int(month):02d}"

    # "nasci dia 15 de março", "meu niver é 3 de janeiro"
    _MONTHS = {
        "janeiro": "01", "fevereiro": "02", "março": "03", "marco": "03",
        "abril": "04", "maio": "05", "junho": "06", "julho": "07",
        "agosto": "08", "setembro": "09", "outubro": "10",
        "novembro": "11", "dezembro": "12"
    }
    text_match = re.search(
        r'(?:anivers[aá]rio|nasci|fa[çc]o\s*anos?|niver)\s*(?:[eé:]\s*)?(?:dia\s*)?(\d{1,2})\s*(?:de\s*)(\w+)',
        msg_lower
    )
    if text_match:
        day = text_match.group(1)
        month_text = text_match.group(2)
        month = _MONTHS.get(month_text, "")
        if month and 1 <= int(day) <= 31:
            return f"{int(day):02d}/{month}"

    return ""
