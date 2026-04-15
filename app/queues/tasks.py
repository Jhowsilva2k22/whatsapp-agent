from celery import Celery
from app.config import get_settings
import asyncio
import logging

logger = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery("whatsapp_agent", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    timezone="America/Sao_Paulo", enable_utc=True,
    task_acks_late=True, worker_prefetch_multiplier=1,
    task_routes={
        "app.queues.tasks.process_message": {"queue": "messages"},
        "app.queues.tasks.process_buffered": {"queue": "messages"},
        "app.queues.tasks.follow_up_active": {"queue": "messages"},
        "app.queues.tasks.follow_up_cold_leads": {"queue": "messages"},
        "app.queues.tasks.nurture_customers": {"queue": "messages"},
        "app.queues.tasks.nightly_learning": {"queue": "learning"},
        "app.queues.tasks.nightly_learning_all": {"queue": "learning"},
        "app.queues.tasks.learn_from_links": {"queue": "learning"},
    },
    beat_schedule={
        "nightly-learning-all": {
            "task": "app.queues.tasks.nightly_learning_all",
            "schedule": 86400.0,
            "options": {"queue": "learning"},
        },
        "follow-up-cold-leads": {
            "task": "app.queues.tasks.follow_up_cold_leads",
            "schedule": 3600.0,  # checa a cada 1h
            "options": {"queue": "messages"},
        },
        "nurture-customers": {
            "task": "app.queues.tasks.nurture_customers",
            "schedule": 43200.0,  # checa a cada 12h
            "options": {"queue": "messages"},
        },
    },
)

def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
def process_message(self, phone: str, owner_id: str, message: str, agent_mode: str,
                    message_id: str = "", media_type: str = "text"):
    try:
        kwargs = {"message_id": message_id, "media_type": media_type}
        _dispatch_to_agent(phone, owner_id, message, agent_mode, **kwargs)
    except Exception as exc:
        logger.error(f"Erro ao processar mensagem de {phone}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5, queue="messages")
def process_buffered(self, phone: str, owner_id: str, agent_mode: str):
    """Processa mensagens agrupadas do buffer Redis (rate limiting).
    Mídias são pré-analisadas e tudo vira uma mensagem unificada pro agente."""
    import json as _json
    import redis

    try:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
        buffer_key = f"buffer:{phone}:{owner_id}"
        task_key = f"buffer_task:{phone}:{owner_id}"

        raw_msgs = _redis.lrange(buffer_key, 0, -1)
        _redis.delete(buffer_key)
        _redis.delete(task_key)

        if not raw_msgs:
            logger.info(f"[Buffer] Nenhuma mensagem no buffer para {phone}")
            return

        msgs = [_json.loads(m) for m in raw_msgs]
        logger.info(f"[Buffer] Processando {len(msgs)} mensagem(ns) agrupadas de {phone}")

        # Separa mídias e textos
        media_msgs = [m for m in msgs if m.get("media_type", "text") != "text" and m.get("message_id")]
        text_parts = [m["text"] for m in msgs if m.get("text")]

        if not media_msgs:
            # Só textos — junta tudo e processa como antes
            combined_text = "\n".join(text_parts) if text_parts else ""
            if not combined_text:
                logger.info(f"[Buffer] Mensagens vazias de {phone}, ignorando")
                return
            kwargs = {"message_id": msgs[-1].get("message_id", ""), "media_type": "text"}
            _dispatch_to_agent(phone, owner_id, combined_text, agent_mode, **kwargs)
            return

        # Se tem 1 mídia só, processa normal (agente lida com ela direto)
        if len(media_msgs) == 1 and len(text_parts) <= 1:
            m = media_msgs[0]
            msg_text = text_parts[0] if text_parts else (m.get("text") or "")
            kwargs = {"message_id": m.get("message_id", ""), "media_type": m.get("media_type", "text")}
            _dispatch_to_agent(phone, owner_id, msg_text, agent_mode, **kwargs)
            return

        # Múltiplas mídias: pré-analisa cada uma e manda tudo junto como texto
        from app.services.whatsapp import WhatsAppService
        from app.services.ai import AIService
        wa = WhatsAppService()
        ai = AIService()
        descriptions = []

        for i, media in enumerate(media_msgs, 1):
            mid = media.get("message_id", "")
            mtype = media.get("media_type", "")
            try:
                b64 = run_async(wa.download_media_base64(mid, phone=phone))
                if not b64:
                    descriptions.append(f"[Mídia {i}: não foi possível baixar]")
                    continue

                if mtype == "image":
                    desc = run_async(ai.respond_with_image(
                        system_prompt="Descreva esta imagem em 1-2 frases objetivas: o que é, marca, detalhes visíveis. Só a descrição, sem comentários.",
                        history=[], user_message="Descreva esta imagem.", image_base64=b64
                    ))
                    descriptions.append(f"[Imagem {i}]: {desc}")
                elif mtype == "audio":
                    text = run_async(ai.transcribe_audio(b64))
                    descriptions.append(f"[Áudio {i}]: {text}" if text else f"[Áudio {i}: não transcrito]")
                elif mtype == "document":
                    desc = run_async(ai.respond_with_pdf(
                        system_prompt="Resuma o conteúdo deste documento em 1-2 frases.",
                        history=[], user_message="Resuma este documento.", pdf_base64=b64
                    ))
                    descriptions.append(f"[Documento {i}]: {desc}")
            except Exception as e:
                logger.error(f"[Buffer] Erro pré-analisando mídia {i}: {e}")
                descriptions.append(f"[Mídia {i}: erro ao processar]")

        # Combina descrições das mídias + textos do lead em uma mensagem só
        all_parts = descriptions + (["\n".join(text_parts)] if text_parts else [])
        combined = "\n".join(all_parts)
        logger.info(f"[Buffer] Mídia unificada para {phone}: {len(media_msgs)} mídias + {len(text_parts)} textos")

        # Manda pro agente como texto (todas as mídias já foram descritas)
        kwargs = {"message_id": "", "media_type": "text"}
        _dispatch_to_agent(phone, owner_id, combined, agent_mode, **kwargs)

    except Exception as exc:
        logger.error(f"[Buffer] Erro ao processar buffer de {phone}: {exc}")
        raise self.retry(exc=exc)


def _dispatch_to_agent(phone: str, owner_id: str, message: str, agent_mode: str, **kwargs):
    """Roteia mensagem para o agente correto."""
    if agent_mode == "qualifier":
        from app.agents.qualifier import QualifierAgent
        run_async(QualifierAgent().process(phone, owner_id, message, **kwargs))
    elif agent_mode == "attendant":
        from app.agents.attendant import AttendantAgent
        run_async(AttendantAgent().process(phone, owner_id, message, **kwargs))
    elif agent_mode == "both":
        from app.services.memory import MemoryService
        customer = run_async(MemoryService().get_or_create_customer(phone, owner_id))
        if customer.lead_status in ["cliente"]:
            from app.agents.attendant import AttendantAgent
            run_async(AttendantAgent().process(phone, owner_id, message, **kwargs))
        else:
            from app.agents.qualifier import QualifierAgent
            run_async(QualifierAgent().process(phone, owner_id, message, **kwargs))

@celery_app.task(queue="learning")
def nightly_learning(owner_id: str):
    from app.services.learning import LearningService
    run_async(LearningService().run_daily_analysis(owner_id))


@celery_app.task(queue="learning")
def nightly_learning_all():
    """Roda o aprendizado noturno para todos os donos cadastrados."""
    from app.database import get_db
    from app.services.learning import LearningService
    db = get_db()
    result = db.table("owners").select("id,phone,business_name").execute()
    if not result.data:
        logger.info("[NightlyAll] Nenhum owner encontrado.")
        return
    learning = LearningService()
    success = 0
    errors = 0
    for owner in result.data:
        try:
            run_async(learning.run_daily_analysis(owner["id"]))
            success += 1
            logger.info(f"[NightlyAll] ✅ {owner.get('business_name', owner['id'])}")
        except Exception as e:
            errors += 1
            logger.error(f"[NightlyAll] ❌ {owner['id']}: {e}")
    logger.info(f"[NightlyAll] Concluído: {success} ok, {errors} erros.")


@celery_app.task(bind=True, max_retries=2, default_retry_delay=10, queue="learning")
def learn_from_links(self, owner_id: str, links: list):
    """Processa links enviados pelo dono via WhatsApp e atualiza base de conhecimento."""
    try:
        from app.services.scraper import ScraperService
        from app.services.ai import AIService
        from app.services.memory import MemoryService
        from app.services.whatsapp import WhatsAppService

        db = MemoryService().db
        owner = db.table("owners").select("*").eq("id", owner_id).maybe_single().execute()
        if not owner or not owner.data:
            logger.error(f"[LearnLinks] owner {owner_id} nao encontrado")
            return

        owner_data = owner.data
        existing_links = owner_data.get("links_processed") or []
        new_links = [l for l in links if l not in existing_links]
        if not new_links:
            logger.info(f"[LearnLinks] links já processados: {links}")
            return

        scraped = run_async(ScraperService().read_links(new_links))
        if not scraped:
            logger.warning(f"[LearnLinks] nenhum conteúdo extraído de {new_links}")
            return

        existing_context = owner_data.get("context_summary") or ""
        combined = f"[CONTEXTO ATUAL]\n{existing_context}\n\n[NOVO CONTEÚDO]\n{scraped}"
        analysis = run_async(AIService().analyze_owner_links(combined))
        if not analysis:
            return

        all_links = existing_links + new_links
        db.table("owners").update({**analysis, "links_processed": all_links}).eq("id", owner_id).execute()

        # Notifica o dono via WhatsApp
        owner_phone = owner_data.get("phone", "")
        if owner_phone:
            summary = analysis.get("context_summary", "")[:200]
            msg = (
                f"✅ *Base de conhecimento atualizada!*\n\n"
                f"🔗 {len(new_links)} link(s) processado(s)\n"
                f"🎯 Oferta detectada: {analysis.get('main_offer', '-')}\n"
                f"🗣️ Tom: {analysis.get('tone', '-')}\n\n"
                f"O agente já está usando as novas informações."
            )
            run_async(WhatsAppService().send_message(owner_phone, msg))

        logger.info(f"[LearnLinks] owner {owner_id} aprendeu {len(new_links)} links")

    except Exception as exc:
        logger.error(f"[LearnLinks] erro: {exc}")
        raise self.retry(exc=exc)


# ── Follow-up: conversa ativa (5min → 10-15min) ────────────────────────────

@celery_app.task(bind=True, max_retries=0, queue="messages")
def follow_up_active(self, phone: str, owner_id: str, attempt: int = 1):
    """Follow-up para leads que pararam de responder numa conversa ativa.
    attempt=1 → 5 min depois da última msg
    attempt=2 → 10-15 min depois (agendado pelo attempt 1)
    """
    import redis as _redis_mod
    try:
        r = _redis_mod.from_url(settings.redis_url, decode_responses=True)
        ts_key = f"last_lead_msg:{phone}:{owner_id}"
        fu_key = f"followup_sent:{phone}:{owner_id}"

        # Checa se o lead mandou mensagem nova desde o agendamento
        last_ts = r.get(ts_key)
        if not last_ts:
            return

        import time
        now = time.time()
        elapsed = now - float(last_ts)

        # attempt 1: só envia se passaram >= 4.5 min sem msg nova
        # attempt 2: só envia se passaram >= 9 min sem msg nova
        min_elapsed = 270 if attempt == 1 else 540
        if elapsed < min_elapsed:
            logger.info(f"[FollowUp] {phone} respondeu recentemente ({elapsed:.0f}s), cancelando attempt={attempt}")
            return

        # Não envia se já mandou follow-up nesse nível
        sent = r.get(fu_key)
        if sent and int(sent) >= attempt:
            logger.info(f"[FollowUp] {phone} já recebeu follow-up attempt={sent}, pulando {attempt}")
            return

        from app.services.memory import MemoryService
        from app.services.ai import AIService
        from app.services.whatsapp import WhatsAppService

        memory = MemoryService()
        customer = run_async(memory.get_or_create_customer(phone, owner_id))

        # Não faz follow-up em leads em atendimento humano ou já clientes
        if customer.lead_status in ("em_atendimento_humano", "cliente"):
            return

        owner = run_async(memory.get_owner_context(owner_id))
        if not owner:
            return

        history = run_async(memory.get_conversation_history(phone, owner_id))
        if not history:
            return

        ai = AIService()
        wa = WhatsAppService()

        if attempt == 1:
            instruction = (
                "O lead parou de responder há alguns minutos no meio da conversa. "
                "Envie UMA mensagem curta e natural verificando se ele ainda está aí. "
                "Pode ser algo como retomar o último ponto, uma pergunta leve, ou checar se fez sentido. "
                "NÃO diga 'ainda está aí?'. Seja contextual com base no histórico. "
                "Máximo 2 frases. Tom: genuíno, sem pressão."
            )
        else:
            instruction = (
                "O lead não respondeu mesmo depois de um primeiro check-in. "
                "Envie UMA última mensagem natural mostrando que você está disponível. "
                "Pode retomar com algo útil relacionado ao que conversaram, ou simplesmente "
                "deixar a porta aberta. NÃO insista. NÃO use 'última chance' ou urgência. "
                "Máximo 2 frases. Tom: leve e presente."
            )

        name = owner.get("business_name", "a empresa")
        tone = owner.get("tone", "acolhedor e direto")
        system_prompt = (
            f"Você é {name}, conversando pelo WhatsApp. Tom: {tone}. "
            f"Cliente: {customer.name or 'o lead'}. "
            f"Contexto: {customer.summary or 'conversa em andamento'}. "
            f"Regras: frases curtas, sem bullet points, sem asteriscos, máximo 1 emoji (só se fizer sentido)."
        )

        response = run_async(ai.respond(
            system_prompt=system_prompt,
            history=history[-6:],  # últimas 6 msgs pra contexto
            user_message=instruction,
            use_gemini=False
        ))

        if response:
            run_async(wa.send_typing(phone, duration=len(response) * 40))
            run_async(wa.send_message(phone, response))
            run_async(memory.save_turn(phone, owner_id, "assistant", response))
            r.setex(fu_key, 3600, str(attempt))  # marca follow-up enviado (TTL 1h)
            logger.info(f"[FollowUp] Enviado attempt={attempt} para {phone}")

            # Se foi attempt 1, agenda attempt 2 em 5-10 min
            if attempt == 1:
                import random
                delay = random.randint(300, 600)  # 5-10 min
                follow_up_active.apply_async(
                    args=[phone, owner_id, 2],
                    countdown=delay,
                    queue="messages"
                )
                logger.info(f"[FollowUp] Agendado attempt=2 para {phone} em {delay}s")

    except Exception as exc:
        logger.error(f"[FollowUp] Erro attempt={attempt} para {phone}: {exc}")


# ── Follow-up: leads frios (24h → 3d → 7d) ─────────────────────────────────

@celery_app.task(queue="messages")
def follow_up_cold_leads():
    """Verifica leads frios e envia follow-up escalonado.
    Roda via Celery Beat a cada 1h.
    Cadência: 24h → 3 dias → 7 dias → para permanentemente.
    """
    from datetime import datetime, timedelta, timezone
    from app.database import get_db
    from app.services.memory import MemoryService
    from app.services.ai import AIService
    from app.services.whatsapp import WhatsAppService

    db = get_db()
    memory = MemoryService()
    ai = AIService()
    wa = WhatsAppService()
    now = datetime.now(timezone.utc)

    # Busca todos os owners
    owners_result = db.table("owners").select("id,phone,business_name,tone,context_summary").execute()
    if not owners_result.data:
        return

    total_sent = 0

    for owner in owners_result.data:
        owner_id = owner["id"]

        # Busca leads que não estão em atendimento humano e não são clientes
        result = db.table("customers").select(
            "phone,name,lead_status,lead_score,summary,last_contact,total_messages,follow_up_stage"
        ).eq("owner_id", owner_id).execute()

        if not result.data:
            continue

        for lead in result.data:
            status = lead.get("lead_status", "")
            if status in ("em_atendimento_humano", "cliente", "perdido"):
                continue

            last_contact = lead.get("last_contact")
            if not last_contact:
                continue

            total_msgs = lead.get("total_messages") or 0
            if total_msgs < 1:
                continue  # nunca conversou

            # Parse last_contact
            try:
                if hasattr(last_contact, 'timestamp'):
                    last_dt = last_contact
                else:
                    last_dt = datetime.fromisoformat(str(last_contact).replace("Z", "+00:00"))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            hours_since = (now - last_dt).total_seconds() / 3600
            current_stage = lead.get("follow_up_stage") or 0

            # Determina próximo estágio
            if current_stage == 0 and hours_since >= 24:
                next_stage = 1
            elif current_stage == 1 and hours_since >= 72:  # 3 dias
                next_stage = 2
            elif current_stage == 2 and hours_since >= 168:  # 7 dias
                next_stage = 3
            else:
                continue  # não é hora ainda ou já esgotou

            if next_stage > 3:
                continue  # já fez todos os follow-ups

            phone = lead.get("phone", "")
            if not phone:
                continue

            try:
                history = run_async(memory.get_conversation_history(phone, owner_id))

                if next_stage == 1:
                    instruction = (
                        "O lead não responde há ~24h. Envie UMA mensagem retomando o último assunto "
                        "de forma natural e leve. Conecte com o que conversaram. "
                        "NÃO diga 'sumiu', 'desapareceu', 'faz tempo'. "
                        "Máximo 2 frases. Tom: presente e genuíno, sem pressão."
                    )
                elif next_stage == 2:
                    instruction = (
                        "O lead não responde há ~3 dias. Envie UMA mensagem trazendo algo útil: "
                        "uma dica, um insight, algo de valor conectado ao que conversaram antes. "
                        "NÃO cobre resposta. NÃO use urgência. "
                        "Máximo 3 frases. Tom: generoso e natural."
                    )
                else:  # next_stage == 3
                    instruction = (
                        "O lead não responde há ~7 dias. Envie UMA mensagem final deixando a porta aberta. "
                        "Algo como 'quando fizer sentido pra você, tô por aqui'. "
                        "NÃO use 'última mensagem', 'última chance'. Sem drama. "
                        "Máximo 2 frases. Tom: leve, respeitoso, sem apego."
                    )

                name = owner.get("business_name", "a empresa")
                tone = owner.get("tone", "acolhedor e direto")
                lead_name = lead.get("name") or "o lead"
                summary = lead.get("summary") or ""

                system_prompt = (
                    f"Você é {name}, conversando pelo WhatsApp. Tom: {tone}. "
                    f"Cliente: {lead_name}. "
                    f"Contexto: {summary[:300] if summary else 'conversa anterior'}. "
                    f"Regras: frases curtas, sem bullet points, sem asteriscos, máximo 1 emoji (só se fizer sentido)."
                )

                response = run_async(ai.respond(
                    system_prompt=system_prompt,
                    history=history[-4:] if history else [],
                    user_message=instruction,
                    use_gemini=False
                ))

                if response:
                    run_async(wa.send_typing(phone, duration=len(response) * 40))
                    run_async(wa.send_message(phone, response))
                    run_async(memory.save_turn(phone, owner_id, "assistant", response))

                    # Atualiza estágio
                    db.table("customers").update(
                        {"follow_up_stage": next_stage}
                    ).eq("phone", phone).eq("owner_id", owner_id).execute()

                    total_sent += 1
                    logger.info(f"[ColdFollowUp] stage={next_stage} enviado para {phone} (owner={owner_id})")

            except Exception as e:
                logger.error(f"[ColdFollowUp] Erro {phone}: {e}")
                continue

    logger.info(f"[ColdFollowUp] Ciclo completo: {total_sent} follow-ups enviados")


# ── Nurturing: clientes ativos (semanal + aniversário) ──────────────────────

@celery_app.task(queue="messages")
def nurture_customers():
    """Mantém relacionamento com clientes existentes.
    Roda via Celery Beat a cada 12h.
    - Aniversário: mensagem especial no dia
    - Semanal: check-in leve se não houve contato em 7+ dias
    - Respeita nurture_paused (cliente pediu pra parar)
    """
    from datetime import datetime, timedelta, timezone
    from app.database import get_db
    from app.services.memory import MemoryService
    from app.services.ai import AIService
    from app.services.whatsapp import WhatsAppService

    db = get_db()
    memory = MemoryService()
    ai = AIService()
    wa = WhatsAppService()
    now = datetime.now(timezone.utc)
    today_ddmm = now.strftime("%d/%m")

    owners_result = db.table("owners").select("id,phone,business_name,tone,context_summary,main_offer").execute()
    if not owners_result.data:
        return

    total_sent = 0

    for owner in owners_result.data:
        owner_id = owner["id"]

        result = db.table("customers").select(
            "phone,name,lead_status,summary,last_contact,total_messages,"
            "birthday,nurture_paused,last_nurture"
        ).eq("owner_id", owner_id).eq("lead_status", "cliente").execute()

        if not result.data:
            continue

        for client in result.data:
            phone = client.get("phone", "")
            if not phone:
                continue

            # Respeita opt-out
            if client.get("nurture_paused"):
                continue

            client_name = client.get("name") or ""
            summary = client.get("summary") or ""
            birthday = client.get("birthday") or ""

            # ── Aniversário ─────────────────────────────────────────────
            is_birthday = False
            if birthday:
                # Aceita "DD/MM" ou "DD/MM/AAAA"
                bday_ddmm = birthday[:5] if len(birthday) >= 5 else birthday
                if bday_ddmm == today_ddmm:
                    is_birthday = True

            if is_birthday:
                try:
                    name = owner.get("business_name", "a empresa")
                    tone = owner.get("tone", "acolhedor e direto")
                    display_name = client_name or "o cliente"

                    system_prompt = (
                        f"Você é {name}, conversando pelo WhatsApp. Tom: {tone}. "
                        f"Cliente: {display_name}. Contexto: {summary[:200] if summary else 'cliente ativo'}. "
                        f"Regras: frases curtas, sem bullet points, sem asteriscos, máximo 1 emoji."
                    )
                    instruction = (
                        f"Hoje é aniversário de {display_name}! Envie UMA mensagem de parabéns genuína e calorosa. "
                        f"Pode ser pessoal se souber algo sobre a pessoa pelo histórico. "
                        f"NÃO use 'parabéns' genérico de script. Seja humano e presente. "
                        f"Máximo 3 frases. Pode usar 1 emoji de celebração."
                    )

                    history = run_async(memory.get_conversation_history(phone, owner_id))
                    response = run_async(ai.respond(
                        system_prompt=system_prompt,
                        history=history[-4:] if history else [],
                        user_message=instruction,
                        use_gemini=False
                    ))

                    if response:
                        run_async(wa.send_typing(phone, duration=len(response) * 40))
                        run_async(wa.send_message(phone, response))
                        run_async(memory.save_turn(phone, owner_id, "assistant", response))
                        db.table("customers").update(
                            {"last_nurture": now.isoformat()}
                        ).eq("phone", phone).eq("owner_id", owner_id).execute()
                        total_sent += 1
                        logger.info(f"[Nurture] Aniversário enviado para {phone}")

                except Exception as e:
                    logger.error(f"[Nurture] Erro aniversário {phone}: {e}")

                continue  # Não manda check-in semanal no dia do aniversário

            # ── Check-in semanal ────────────────────────────────────────
            last_contact = client.get("last_contact")
            last_nurture = client.get("last_nurture")

            # Calcula dias desde último contato e último nurture
            try:
                if last_contact:
                    if hasattr(last_contact, 'timestamp'):
                        lc_dt = last_contact
                    else:
                        lc_dt = datetime.fromisoformat(str(last_contact).replace("Z", "+00:00"))
                        if lc_dt.tzinfo is None:
                            lc_dt = lc_dt.replace(tzinfo=timezone.utc)
                    days_since_contact = (now - lc_dt).total_seconds() / 86400
                else:
                    days_since_contact = 999

                if last_nurture:
                    if hasattr(last_nurture, 'timestamp'):
                        ln_dt = last_nurture
                    else:
                        ln_dt = datetime.fromisoformat(str(last_nurture).replace("Z", "+00:00"))
                        if ln_dt.tzinfo is None:
                            ln_dt = ln_dt.replace(tzinfo=timezone.utc)
                    days_since_nurture = (now - ln_dt).total_seconds() / 86400
                else:
                    days_since_nurture = 999
            except Exception:
                days_since_contact = 999
                days_since_nurture = 999

            # Só manda se: 7+ dias sem contato E 6+ dias desde último nurture
            if days_since_contact < 7 or days_since_nurture < 6:
                continue

            try:
                name = owner.get("business_name", "a empresa")
                tone = owner.get("tone", "acolhedor e direto")
                display_name = client_name or "o cliente"
                offer = owner.get("main_offer", "")

                system_prompt = (
                    f"Você é {name}, conversando pelo WhatsApp. Tom: {tone}. "
                    f"Cliente: {display_name}. Contexto: {summary[:200] if summary else 'cliente ativo'}. "
                    f"Oferta principal: {offer}. "
                    f"Regras: frases curtas, sem bullet points, sem asteriscos, máximo 1 emoji."
                )
                instruction = (
                    f"Faz uns dias que não falamos com {display_name}, que já é cliente. "
                    f"Baseado no HISTÓRICO da conversa e no que ele comprou/contratou/demonstrou interesse, "
                    f"escolha UMA dessas abordagens (a que fizer mais sentido com o perfil dele):\n"
                    f"1. Produto/serviço complementar: 'pensei em você, tem algo novo que combina com o que você já usa/contratou'\n"
                    f"2. Novidade relevante: lançamento, atualização, conteúdo novo conectado ao interesse dele\n"
                    f"3. Check-in genuíno: como está indo com o que adquiriu, se teve resultado, se precisa de ajuda\n"
                    f"4. Dica útil: algo prático conectado ao universo dele baseado no que conversaram\n\n"
                    f"A mensagem deve soar como se você LEMBROU da pessoa naturalmente — "
                    f"tipo 'cara, pensei em você quando vi isso' ou 'lembrei que você tinha interesse em X'. "
                    f"Use detalhes REAIS do histórico (produto contratado, dúvida que teve, interesse demonstrado). "
                    f"NÃO pareça script. NÃO pareça cobrança. NÃO use 'sumiu'. NÃO force venda. "
                    f"Se não souber o que a pessoa comprou, vá pelo caminho do check-in genuíno. "
                    f"Máximo 3 frases. Tom: presente, pessoal, como quem se importa de verdade."
                )

                history = run_async(memory.get_conversation_history(phone, owner_id))
                response = run_async(ai.respond(
                    system_prompt=system_prompt,
                    history=history[-4:] if history else [],
                    user_message=instruction,
                    use_gemini=False
                ))

                if response:
                    run_async(wa.send_typing(phone, duration=len(response) * 40))
                    run_async(wa.send_message(phone, response))
                    run_async(memory.save_turn(phone, owner_id, "assistant", response))
                    db.table("customers").update(
                        {"last_nurture": now.isoformat()}
                    ).eq("phone", phone).eq("owner_id", owner_id).execute()
                    total_sent += 1
                    logger.info(f"[Nurture] Check-in semanal para {phone}")

            except Exception as e:
                logger.error(f"[Nurture] Erro check-in {phone}: {e}")

    logger.info(f"[Nurture] Ciclo completo: {total_sent} mensagens enviadas")
