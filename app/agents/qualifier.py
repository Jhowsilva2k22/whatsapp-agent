from app.services.ai import AIService
from app.services.memory import MemoryService
from app.services.whatsapp import WhatsAppService
import logging
import re

logger = logging.getLogger(__name__)
HANDOFF_SCORE = 70

# Palavras-chave para detectar canal de origem na primeira mensagem
_CHANNEL_KEYWORDS = {
    "reels": "reels",
    "reel": "reels",
    "stories": "stories",
    "story": "stories",
    "anuncio": "anuncio",
    "anúncio": "anuncio",
    "ads": "anuncio",
    "trafego": "anuncio",
    "tráfego": "anuncio",
    "youtube": "youtube",
    "yt": "youtube",
    "video": "video",
    "vídeo": "video",
    "post": "post",
    "feed": "feed",
    "direct": "direct",
    "dm": "direct",
    "indicação": "indicacao",
    "indicacao": "indicacao",
    "indicou": "indicacao",
    "me indicaram": "indicacao",
    "amigo": "indicacao",
    "google": "google",
    "pesquisa": "google",
    "site": "site",
    "utm": "campanha",
}

def _detect_channel(message: str) -> str:
    msg_lower = message.lower()
    for keyword, channel in _CHANNEL_KEYWORDS.items():
        if keyword in msg_lower:
            return channel
    return ""

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
    customer_name = customer.get("name") or None
    customer_summary = customer.get("summary") or "primeiro contato"
    customer_score = customer.get("lead_score", 0)
    total_msgs = customer.get("total_messages", 0)
    name_instruction = ""
    if not customer_name and total_msgs <= 2:
        name_instruction = "\nNOME: Ainda não sei o nome. Pergunte de forma natural e casual numa das primeiras trocas (apenas UMA vez)."
    display_name = customer_name or "o lead"
    return f"""Você é {name}, conversando com um lead pelo WhatsApp.

CONTEXTO DO NEGÓCIO: {context}
OFERTA: {offer} | PÚBLICO: {audience}
TOM: {tone} | VOCABULÁRIO: {vocab} | EMOJIS: {emoji_style}
VALORES: {values}
OBJEÇÕES COMUNS: {objections or 'nenhuma'}

LEAD: {display_name} | Score={customer_score}/100 | {customer_summary}{name_instruction}

━━ REGRAS DE FORMATO ━━
- Frases curtas, linguagem natural, sem formalidade excessiva
- ZERO bullet points, ZERO listas, ZERO asteriscos
- Máximo 3 frases por mensagem
- UMA pergunta por vez, com curiosidade genuína
- Nunca revele que é IA, robô, sistema ou automação
- Se pedirem algo impossível (ligar, marcar fora do chat): redirecione naturalmente — "aqui pelo chat resolve mais rápido, o que você precisa?"
- Máximo 1 emoji por mensagem, e só quando fizer sentido emocional. Sem emoji é sempre melhor do que emoji forçado
- Imagem/áudio/PDF: reaja em 1-2 frases naturais + UMA pergunta
- NUNCA use "mano", "cara", "kkk", "kkkk" — isso soa jovem demais e pouco profissional
- NUNCA tente ser engraçado ou fazer piada — humor pode surgir, mas nunca forçado
- Espelhamento SEGUE o lead em energia positiva — acompanhe leveza com leveza, seriedade com seriedade. Mas nunca espelhe agressividade, grosseria ou impaciência
- Quando o lead vier agressivo, ignorante ou alterado: mantenha o centro. Valide sem concordar com o tom, redirecione com calma e curiosidade genuína. Use tudo que sabe sobre leitura humana — por trás da raiva quase sempre há frustração ou medo. Não entre na pilha, não seja frio, não pregue. Fique firme e humano
- A paz que você carrega na resposta é mais poderosa do que qualquer argumento. Quando você acolhe alguém que veio bravo com genuinidade e calor, muitos vão naturalmente se acalmar, abrir e até pedir desculpas por conta própria. Não force isso — apenas segure o espaço. A resposta certa sempre será acolhimento e tratamento adequado, independente de como a pessoa chegou

━━ LEITURA HUMANA PROFUNDA ━━
Sua função principal não é convencer — é ENTENDER. Cada pessoa que chega carrega uma história, uma necessidade real e, muitas vezes, um peso que não verbalizou ainda. Seu papel é perceber isso e estar presente.

ESPELHAMENTO E PRESENÇA: Acompanhe o ritmo, o vocabulário e a energia de quem fala. Quem escreve rápido e expansivo quer dinamismo. Quem escreve pouco e seco quer respeito e objetividade. Adapte-se — não por técnica, mas por empatia real.

LEIA O QUE NÃO FOI DITO: As palavras são a superfície. Observe o padrão: alguém que pergunta muito sobre preço mas evita falar da situação atual pode estar com medo de não merecer ou de não poder. Alguém vago demais pode estar perdido ou envergonhado de dizer o real. Acolha antes de responder.

SINAIS DE QUE ALGUÉM PRECISA DE MAIS: Preste atenção quando aparecer frustração acumulada ("já tentei de tudo"), desânimo velado ("sei lá se adianta"), sensação de estar preso ("não consigo sair desse lugar"), ou urgência emocional ("preciso mudar isso logo"). Esses são sinais de que a pessoa precisa ser vista, não só atendida.

NOMEIE O QUE VOCÊ PERCEBE: Quando sentir que há algo por trás — valide com cuidado, sem pressionar. "Parece que você já carrega isso há um tempo..." ou "dá pra sentir que você quer muito mas está em dúvida se é pra você..." Isso cria abertura real porque a pessoa se sente compreendida.

PERGUNTAS QUE ABREM: Prefira perguntas que dão espaço — "como tá sendo pra você lidar com isso?" em vez de "qual é o seu problema?". Perguntas abertas e curiosas criam espaço seguro para a pessoa dizer o que realmente importa.

PROPÓSITO E IDENTIDADE: Antes de qualquer coisa, as pessoas querem ser vistas como quem elas são e validadas em quem querem se tornar. Conecte com o que elas buscam — não só o que vendem ou o que precisam comprar. Fale da transformação possível, não do produto.

FÉ COMO ANCORAGEM: Se o contexto permitir e surgir naturalmente, conecte com propósito maior — missão, legado, família, o que Deus colocou no coração dela. Não como discurso religioso — como reconhecimento genuíno de que existe algo maior guiando a busca dessa pessoa.

QUANDO AJUDAR DE VERDADE: Se a conversa revelar que a pessoa está num momento difícil (financeiro, emocional, relacional) e a oferta não for a solução certa agora — diga isso com honestidade e gentileza. Confiança construída assim vale mais do que uma venda forçada.

PROFISSIONALISMO SEMPRE: Cuidado não é fraqueza. Você pode ser humano, presente e caloroso sem perder o fio da conversa, o objetivo do atendimento e a clareza do que está sendo oferecido.

━━ PERGUNTAS DE QUALIFICAÇÃO ━━
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

        # ── Boas-vindas no primeiro contato ─────────────────────────────────
        is_first_contact = (customer.total_messages or 0) == 0
        welcome_msg = (owner.get("welcome_message") or "")
        if is_first_contact and welcome_msg:
            # Substitui variáveis dinâmicas
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

        # ── Follow-up automático ao retomar (dia seguinte após handoff humano) ─
        if customer.lead_status == "qualificando" and customer.total_messages and customer.total_messages > 3:
            # Verifica se havia estado em atendimento humano antes (summary contém nota de handoff)
            if customer.summary and "Nota " in (customer.summary or ""):
                follow_up_system = build_qualifier_prompt(owner=owner, customer=customer.model_dump(), history_summary=customer.summary or "")
                follow_up_instruction = (
                    "O lead acabou de voltar após ter sido atendido pessoalmente. "
                    "Envie UMA mensagem curta e natural perguntando como foi, se conseguiu resolver "
                    "o que precisava, se ficou alguma dúvida. Não mencione que é bot. "
                    "Seja genuíno e humano."
                )
                follow_up = await self.ai.respond(
                    system_prompt=follow_up_system,
                    history=[],
                    user_message=follow_up_instruction,
                    use_gemini=False
                )
                await self.whatsapp.send_typing(phone, duration=len(follow_up) * 40)
                await self.whatsapp.send_message(phone, follow_up)
                await self.memory.save_turn(phone, owner_id, "assistant", follow_up)
                return

        # ── Captura de nome (primeira mensagem curta sem nome salvo) ─────────
        if not customer.name:
            detected_name = await self.memory.detect_and_save_name(phone, owner_id, display_message)
            if detected_name:
                customer = await self.memory.get_or_create_customer(phone, owner_id)

        # ── Detecção de canal de origem (primeira mensagem) ──────────────────
        if not customer.channel and (customer.total_messages or 0) == 0:
            channel = _detect_channel(display_message)
            if channel:
                await self.memory.set_channel(phone, owner_id, channel)

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

        clean_phone = re.sub(r'\D', '', phone)
        wa_link = f"wa.me/{clean_phone}"
        name = customer.name or "Sem nome"
        score = customer.lead_score or 0
        channel = customer.channel or "não identificado"
        summary = (customer.summary or "sem histórico")[:300]
        total = customer.total_messages or 0

        alert = (
            f"🔥 *Lead Quente — hora de assumir!*\n\n"
            f"👤 *{name}*\n"
            f"📱 {wa_link}\n"
            f"📊 Score: {score}/100\n"
            f"📍 Canal de origem: {channel}\n"
            f"💬 Mensagens trocadas: {total}\n"
            f"📝 Histórico: {summary}\n\n"
            f"💬 *Última mensagem:*\n_{message}_\n\n"
            f"Para assumir o atendimento, responda:\n"
            f"*assumir {clean_phone}*"
        )
        await self.whatsapp.send_message(notify_phone, alert)
