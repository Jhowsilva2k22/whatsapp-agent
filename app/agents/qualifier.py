from app.services.ai import AIService
from app.services.memory import MemoryService
from app.services.whatsapp import WhatsAppService
from app.services import sender
import logging
import re

logger = logging.getLogger(__name__)
HANDOFF_SCORE = 70

# Palavras-chave para detectar canal de origem na primeira mensagem
_CHANNEL_KEYWORDS = {
    "reels": "reels", "reel": "reels", "stories": "stories", "story": "stories",
    "anuncio": "anuncio", "anúncio": "anuncio", "ads": "anuncio",
    "trafego": "anuncio", "tráfego": "anuncio", "youtube": "youtube",
    "yt": "youtube", "video": "video", "vídeo": "video", "post": "post",
    "feed": "feed", "direct": "direct", "dm": "direct",
    "indicação": "indicacao", "indicacao": "indicacao", "indicou": "indicacao",
    "me indicaram": "indicacao", "amigo": "indicacao", "google": "google",
    "pesquisa": "google", "site": "site", "utm": "campanha",
}

def _detect_channel(message: str) -> str:
    msg_lower = message.lower()
    for keyword, channel in _CHANNEL_KEYWORDS.items():
        if keyword in msg_lower:
            return channel
    return ""


# ─── Busca web em tempo real ───────────────────────────────────────────
_WEB_SEARCH_PAT = re.compile(
    r'@([\w.]{2,30})|https?://\S+',
    re.IGNORECASE,
)


def _detect_web_search_need(message: str):
    """Retorna (precisa_busca: bool, query: str)."""
    m = _WEB_SEARCH_PAT.search(message.strip())
    if not m:
        return False, ""
    full = m.group(0)
    if full.startswith("@"):
        handle = m.group(1)
        return True, f'"{handle}" Instagram perfil bio negócio informações'
    domain = re.sub(r'https?://(www\.)?', '', full).split('/')[0]
    return True, f'site "{domain}" o que é informações sobre'


async def _fetch_web_context(query: str) -> str:
    """Busca web síncrona via asyncio.to_thread. Retorna snippets formatados."""
    import asyncio as _asyncio
    try:
        from app.services.web_search import WebSearchService
        svc = WebSearchService()
        results = await _asyncio.to_thread(svc._search_brave, query, 5)
        if not results:
            return ""
        lines = []
        for r in results[:5]:
            title = r.get("title", "")
            desc = r.get("description", "")
            url = r.get("url", "")
            line = f"• {title}: {desc}"
            if url:
                line += f" ({url})"
            lines.append(line)
        return "\n".join(lines)
    except Exception as _e:
        logger.warning("[Qualifier] Web search falhou: %s", _e)
        return ""


def build_qualifier_prompt(owner: dict, customer: dict, history_summary: str, knowledge_context: str = "") -> str:
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
        name_instruction = "\nNOME: Ainda não sei o nome. Descubra durante a conversa de forma natural — quando houver abertura, pergunte como a pessoa prefere ser chamada (ex: 'como posso te chamar?'). Faça apenas UMA vez. NUNCA comente nem reaja ao nome recebido — use-o direto, como alguém que já conhece."
    display_name = customer_name or "o lead"
    name_usage = f"\nUSO DO NOME: O nome do lead é {customer_name}. Use de forma completamente natural — não em toda mensagem, não como abertura forçada. NUNCA comente, elogie, questione ou reaja ao nome, seja ele qual for. Use-o como qualquer pessoa usaria o nome de um amigo numa conversa." if customer_name else ""

    if customer_score >= 70:
        temperatura = "🔥 QUENTE — está próximo de decidir. Reduza fricção, não desperdice com perguntas desnecessárias."
    elif customer_score >= 40:
        temperatura = "🌡 MORNO — interesse real mas ainda avaliando. Aprofunde o vínculo, explore a dor com mais cuidado."
    else:
        temperatura = "❄ FRIO — chegou agora ou ainda desconfiado. Conquiste antes de qualquer coisa. Não venda — conecte."

    knowledge_block = f"\n━━ CONHECIMENTO TREINADO ━━\nUse isso nas respostas quando relevante. Nunca invente o que não está aqui.\n{knowledge_context}" if knowledge_context else ""

    bot_prompt = owner.get("bot_prompt") or ""
    custom_block = f"\n\n━━ ESTILO PERSONALIZADO ━━\n{bot_prompt}" if bot_prompt else ""

    return f"""Você é um atendente de {name} conversando pelo WhatsApp ou Instagram.

IDENTIDADE: Você representa {name}. Você não é um robô, não é uma IA, não é um sistema. Você é o atendimento de {name}. Ponto. Se alguém perguntar se você é robô ou IA, responda algo como "sou o atendimento da {name}, aqui pra te ajudar" e redirecione com naturalidade. Nunca confirme, nunca negue, nunca entre nessa conversa.

JAMAIS REVELE:
- Que existe um sistema, backend, automação ou plataforma por trás
- Nomes de ferramentas, APIs, bancos de dados ou código
- Que existe um "score", "qualificação" ou processo técnico
- Informações internas de qualquer natureza

CONTEXTO DO NEGÓCIO: {context}
OFERTA: {offer} | PÚBLICO: {audience}
TOM: {tone} | VOCABULÁRIO: {vocab} | EMOJIS: {emoji_style}
VALORES: {values}
OBJEÇÕES CONHECIDAS: {objections or 'nenhuma específica'}

LEAD: {display_name} | Temperatura: {temperatura} | {customer_summary}{name_instruction}{name_usage}
{knowledge_block}

━━ REGRA DE OURO: NUNCA INVENTE ━━
Se você não souber a resposta para algo específico (preço, prazo, detalhe técnico), NUNCA invente. Diga algo como:
"Boa pergunta, deixa eu verificar isso certinho pra você" ou "Vou confirmar esse detalhe" — e então redirecione para o que você sabe com certeza. Informação errada destrói confiança. Silêncio honesto constrói.

━━ PSICOLOGIA DO RELACIONAMENTO ━━
Seu objetivo invisível é a venda. Seu objetivo visível é a pessoa. Esses dois nunca entram em conflito quando você faz o segundo direito.

TEMPERATURA E ABORDAGEM:
- Lead frio: não fale de produto ainda. Ouça, crie vínculo, entenda o mundo dessa pessoa. A venda acontece quando a confiança está alta — não antes.
- Lead morno: aprofunde a dor. Faça ela verbalizar o problema. Quem articula o próprio problema está 80% mais próximo de aceitar a solução.
- Lead quente: reduza fricção. Sem perguntas desnecessárias. Facilite o próximo passo.

CONSTRUÇÃO DE VÍNCULO REAL:
Você não está "usando técnica". Você está genuinamente interessado nessa pessoa. A diferença entre manipulação e influência é a intenção — e a sua é ajudar. Seja curioso de verdade. Faça perguntas porque quer saber, não para marcar caixinhas.

ESPELHAMENTO PROFUNDO:
Acompanhe o ritmo, o vocabulário, a energia. Quem escreve curto e seco quer objetividade. Quem escreve longo e emocional quer ser ouvido. Quem usa gírias quer informalidade. Adapte sem fingir — espelhamento forçado é mais estranho que não espelhar.

LEIA O NÃO DITO:
Alguém que pergunta muito sobre preço mas evita falar da situação atual: pode estar com medo de não merecer. Alguém vago demais: pode estar perdido ou envergonhado do real. Alguém agressivo: quase sempre é frustração acumulada, não raiva de você. Veja a pessoa por trás do comportamento.

ÂNCORAS EMOCIONAIS:
Conecte com o que a pessoa realmente quer (não o produto — o resultado). Família, liberdade, reconhecimento, segurança, propósito. Quando a conversa tocar nisso naturalmente, não desvie. Fique presente. Essas são as âncoras reais de qualquer decisão.

QUANDO NÃO EMPURRAR:
Se perceber que a oferta genuinamente não serve para essa pessoa agora, diga com honestidade. "Não acho que faz sentido pra sua situação agora" constrói mais confiança do que forçar uma venda. Confiança gera indicação. Indicação gera mais venda do que qualquer técnica.

━━ REGRAS DE FORMATO ━━
- Frases curtas. Linguagem de conversa real. Como um amigo que entende do assunto
- ZERO bullet points, ZERO listas, ZERO asteriscos, ZERO formalidade
- Máximo 3 frases por mensagem — menos é mais
- UMA pergunta por vez, feita com curiosidade genuína (nunca pareça um formulário)
- Nunca comece com "Olá!", "Oi!", "Claro!" ou "Com certeza!" — entre direto
- Varie sempre o início das mensagens
- Máximo 1 emoji por mensagem, só quando fizer sentido emocional real
- Adapte o vocabulário ao TOM definido — espelhe a linguagem e energia do negócio naturalmente
- Se pedirem ligar ou encontrar: redirecione com naturalidade
- Imagem/áudio/PDF: reaja em 1-2 frases + UMA pergunta

━━ PERGUNTAS DE QUALIFICAÇÃO ━━
{questions_text}

HISTÓRICO: {history_summary or 'primeiro contato'}{custom_block}"""

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

        evolution_instance = owner.get("evolution_instance") or ""
        ch = customer.channel or "whatsapp"

        history = await self.memory.get_conversation_history(phone, owner_id)

        is_first_contact = (customer.total_messages or 0) == 0 and len(history) == 0
        welcome_msg = (owner.get("welcome_message") or "")
        if is_first_contact and welcome_msg:
            final_welcome = welcome_msg.replace("{nome}", customer.name or "")
            final_welcome = final_welcome.replace("{negocio}", owner.get("business_name", ""))
            await sender.send_typing(phone, channel=ch, duration=len(final_welcome) * 40, instance=evolution_instance)
            await sender.send_message(phone, final_welcome, channel=ch, instance=evolution_instance)
            await self.memory.save_turn(phone, owner_id, "assistant", final_welcome)

        knowledge_context = ""
        try:
            from app.services.knowledge import KnowledgeBank
            kb = KnowledgeBank()
            knowledge_context = kb.get_context_for_prompt(owner_id, query=message, limit=6)
        except Exception as _ke:
            pass

        display_message = message
        media_base64 = None

        if media_type == "image":
            if ": " in message:
                display_message = message.split(": ", 1)[1]
            elif message.startswith("[Imagem"):
                display_message = ""  # sem caption — será tratado abaixo se download falhar

        if media_type in ("image", "audio", "document") and message_id:
            media_base64 = await sender.download_media(message_id, phone=phone, channel=ch, instance=evolution_instance)
            if not media_base64:
                logger.warning(f"[Qualifier] falha ao baixar mídia tipo={media_type} id={message_id}")

        # ── Áudio: transcrever ────────────────────────────────────────────────
        if media_type == "audio" and media_base64:
            transcription = await self.ai.transcribe_audio(media_base64)
            if transcription:
                display_message = f"[Áudio]: {transcription}"
            media_base64 = None
            media_type = "text"
        elif media_type == "audio" and not media_base64:
            # Sem transcrição: pede pra escrever de forma natural
            fallback_audio = "Vi que você mandou um áudio! Aqui fica mais fácil por texto — pode escrever o que precisa? 😊"
            await self.memory.save_turn(phone, owner_id, "assistant", fallback_audio)
            await sender.send_typing(phone, channel=ch, duration=1500, instance=evolution_instance)
            await sender.send_message(phone, fallback_audio, channel=ch, instance=evolution_instance)
            return

        # ── Imagem: fallback quando download falha ────────────────────────────
        if media_type == "image" and not media_base64:
            if not display_message.strip():
                # Sem caption E sem conteúdo: pede pra reenviar
                fallback_img = "Opa, a imagem não carregou aqui 📸 Pode mandar de novo?"
                await self.memory.save_turn(phone, owner_id, "assistant", fallback_img)
                await sender.send_typing(phone, channel=ch, duration=1200, instance=evolution_instance)
                await sender.send_message(phone, fallback_img, channel=ch, instance=evolution_instance)
                return
            # Tem caption: processa como texto normal
            media_type = "text"

        if customer.lead_status == "qualificando" and customer.total_messages and customer.total_messages > 3:
            if customer.summary and "Nota " in (customer.summary or ""):
                follow_up_system = build_qualifier_prompt(owner=owner, customer=customer.model_dump(), history_summary=customer.summary or "", knowledge_context=knowledge_context)
                follow_up_instruction = (
                    "O lead acabou de voltar após ter sido atendido pessoalmente. "
                    "Envie UMA mensagem curta e natural perguntando como foi, se conseguiu resolver "
                    "o que precisava, se ficou alguma dúvida. Não mencione que é bot. "
                    "Seja genuíno e humano."
                )
                follow_up = await self.ai.respond(
                    system_prompt=follow_up_system, history=[],
                    user_message=follow_up_instruction, use_gemini=False
                )
                await sender.send_typing(phone, channel=ch, duration=len(follow_up) * 40, instance=evolution_instance)
                await sender.send_message(phone, follow_up, channel=ch, instance=evolution_instance)
                await self.memory.save_turn(phone, owner_id, "assistant", follow_up)
                return

        if not customer.name:
            detected_name = await self.memory.detect_and_save_name(phone, owner_id, display_message, history=history)
            if detected_name:
                customer = await self.memory.get_or_create_customer(phone, owner_id)

        if not customer.channel and (customer.total_messages or 0) == 0:
            channel = _detect_channel(display_message)
            if channel:
                await self.memory.set_channel(phone, owner_id, channel)

        if not customer.birthday:
            from app.agents.attendant import _detect_birthday
            detected_bday = _detect_birthday(display_message)
            if detected_bday:
                await self.memory.update_customer(phone, owner_id, {"birthday": detected_bday})
                logger.info(f"[Qualifier] Aniversário detectado para {phone}: {detected_bday}")

        classification = await self.ai.classify_intent(display_message, context=customer.summary or "")
        intent = classification.get("intent", "outros")
        score_delta = classification.get("lead_score_delta", 0)
        is_simple = classification.get("is_simple", False)
        new_score = min(100, max(0, (customer.lead_score or 0) + score_delta))

        from app.agents.attendant import _auto_status
        new_status = _auto_status(customer.lead_status, new_score)

        if intent == "compra_confirmada" and new_status != "cliente":
            new_status = "cliente"
            new_score = 100
            await self._notify_sale(phone, owner, customer)
            logger.info(f"[Qualifier] VENDA DETECTADA! {phone} virou cliente automaticamente")

        needs_human = classification.get("needs_human", False)
        human_reason = classification.get("human_reason", "")
        sos_sent = False

        if needs_human and customer.lead_status != "em_atendimento_humano":
            notify_phone = owner.get("notify_phone")
            if notify_phone:
                clean_phone = re.sub(r'\D', '', phone)
                name = customer.name or "Sem nome"
                sentiment = classification.get("sentiment", "neutro")
                urgency = classification.get("urgency", "media")
                urgency_icon = "🔴" if urgency == "alta" else "🟡"

                sos_alert = (
                    f"{urgency_icon} *SOS — Atenção necessária!*\n\n"
                    f"👤 *{name}* | Score: *{new_score}*\n"
                    f"📱 wa.me/{re.sub(r'[^0-9]', '', phone)}\n"
                    f"🎤 Sentimento: *{sentiment}*\n"
                    f"📌 Motivo: {human_reason}\n\n"
                )
                if customer.summary:
                    sos_alert += f"📝 Contexto: {customer.summary[:200]}\n\n"
                sos_alert += "👉 Copie a próxima mensagem e envie pra assumir:"
                await self.whatsapp.send_message(notify_phone, sos_alert, instance=evolution_instance)
                await self.whatsapp.send_message(notify_phone, f"/assumir {phone}", instance=evolution_instance)
                sos_sent = True
                logger.info(f"[SOS] Alerta enviado para dono! {phone} | motivo: {human_reason}")

        handoff_threshold = owner.get("handoff_threshold", HANDOFF_SCORE)
        if new_score >= handoff_threshold and customer.lead_score < handoff_threshold and new_status != "cliente":
            await self._trigger_handoff(phone, owner, customer, display_message)
        await self.memory.save_turn(phone, owner_id, "user", display_message)

        sos_instruction = ""
        if sos_sent:
            sos_instruction = (
                "\n\n━━ ATENÇÃO: MODO CONTENÇÃO ━━\n"
                "O dono foi notificado e vai assumir em breve. "
                "NÃO invente respostas, NÃO prometa nada, NÃO dê informações que você não tem certeza. "
                "Segure a conversa com naturalidade: reconheça o que o cliente disse, "
                "valide o sentimento, e diga que vai verificar/confirmar e já retorna. "
                "Exemplo: 'Entendi perfeitamente. Deixa eu verificar isso com mais cuidado pra te dar a melhor resposta. Já te retorno!'"
            )

        # ── Busca web em tempo real: @handles, URLs ───────────────────────────
        web_context = ""
        _needs_search, _search_query = _detect_web_search_need(display_message)
        if _needs_search and _search_query:
            logger.info("[Qualifier] Busca web: '%s'", _search_query[:60])
            web_context = await _fetch_web_context(_search_query)

        web_block = (
            "\n\n━━ CONTEXTO WEB (AGORA) ━━\n"
            "Busquei na internet agora sobre o que o cliente mencionou:\n"
            f"{web_context}\n"
            "Use essas informações para responder com inteligência e especificidade. "
            "Se não tiver detalhes suficientes, seja honesto mas útil — nunca fique em silêncio."
        ) if web_context else ""

        system_prompt = build_qualifier_prompt(
            owner=owner, customer=customer.model_dump(),
            history_summary=customer.summary or "",
            knowledge_context=knowledge_context,
        ) + sos_instruction + web_block

        if media_type == "image" and media_base64:
            user_msg_for_image = display_message or "O que você acha disso?"
            response = await self.ai.respond_with_image(
                system_prompt=system_prompt, history=history,
                user_message=user_msg_for_image, image_base64=media_base64)
        elif media_type == "document" and media_base64:
            response = await self.ai.respond_with_pdf(
                system_prompt=system_prompt, history=history,
                user_message=message, pdf_base64=media_base64)
        else:
            response = await self.ai.respond(
                system_prompt=system_prompt, history=history,
                user_message=display_message, use_gemini=is_simple)

        if not response:
            logger.warning(f"[Qualifier] Resposta vazia do AI para {phone} | media={media_type} — enviando fallback")
            fallback = "Hmm, tive um problema aqui. Pode repetir o que você disse?"
            await self.memory.save_turn(phone, owner_id, "assistant", fallback)
            await sender.send_typing(phone, channel=ch, duration=1500, instance=evolution_instance)
            await sender.send_message(phone, fallback, channel=ch, instance=evolution_instance)
            return

        await self.memory.save_turn(phone, owner_id, "assistant", response)
        sentiment = classification.get("sentiment", "neutro")
        sent_history = list(customer.sentiment_history or [])[-9:]
        sent_history.append(sentiment)
        await self.memory.update_customer(phone, owner_id, {
            "lead_score": new_score, "lead_status": new_status,
            "last_intent": intent, "total_messages": (customer.total_messages or 0) + 1,
            "last_sentiment": sentiment, "sentiment_history": sent_history
        })
        await sender.send_typing(phone, channel=ch, duration=len(response) * 40, instance=evolution_instance)
        await sender.send_message(phone, response, channel=ch, instance=evolution_instance)
        logger.info(f"[Qualifier] {phone} | intent={intent} | score={new_score} | media={media_type} | ch={ch} | inst={evolution_instance}")

    async def _trigger_handoff(self, phone: str, owner: dict, customer, message: str):
        notify_phone = owner.get("notify_phone")
        if not notify_phone:
            return
        evolution_instance = owner.get("evolution_instance") or ""
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
            f"👉 Copie a próxima mensagem e envie pra assumir:"
        )
        await self.whatsapp.send_message(notify_phone, alert, instance=evolution_instance)
        await self.whatsapp.send_message(notify_phone, f"/assumir {phone}", instance=evolution_instance)

    async def _notify_sale(self, phone: str, owner: dict, customer):
        """Notifica o dono quando uma venda é detectada automaticamente."""
        notify_phone = owner.get("notify_phone")
        if not notify_phone:
            return
        evolution_instance = owner.get("evolution_instance") or ""
        clean_phone = re.sub(r'\D', '', phone)
        wa_link = f"wa.me/{clean_phone}"
        name = customer.name or "Sem nome"
        channel = customer.channel or "não identificado"
        total = customer.total_messages or 0

        alert = (
            f"💰 *Venda Detectada!*\n\n"
            f"👤 *{name}*\n"
            f"📱 {wa_link}\n"
            f"📍 Canal: {channel}\n"
            f"💬 Mensagens: {total}\n\n"
            f"Status atualizado automaticamente pra *cliente*.\n"
            f"O agente agora cuida do pós-venda e relacionamento."
        )
        await self.whatsapp.send_message(notify_phone, alert, instance=evolution_instance)
