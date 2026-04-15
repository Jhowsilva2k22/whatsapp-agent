from celery import Celery
from app.config import get_settings
from urllib.parse import quote
import asyncio
import logging

logger = logging.getLogger(__name__)
settings = get_settings()


def _panel_url() -> str:
    """Gera URL do painel já autenticada com token."""
    base = settings.app_url.rstrip("/")
    token = quote(settings.app_secret, safe="")
    return f"{base}/panel?token={token}"

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
        "app.queues.tasks.weekly_report": {"queue": "learning"},
        "app.queues.tasks.recalculate_scores": {"queue": "learning"},
        "app.queues.tasks.nightly_learning": {"queue": "learning"},
        "app.queues.tasks.nightly_learning_all": {"queue": "learning"},
        "app.queues.tasks.learn_from_links": {"queue": "learning"},
        "app.queues.tasks.run_campaign": {"queue": "learning"},
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
        "weekly-report": {
            "task": "app.queues.tasks.weekly_report",
            "schedule": 604800.0,  # 1x por semana
            "options": {"queue": "learning"},
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


# ── Relatório semanal inteligente + sugestões ────────────────────────────────

@celery_app.task(queue="learning")
def weekly_report():
    """Gera e envia relatório semanal inteligente via WhatsApp pro dono.
    Inclui: métricas, sentimento, padrões, sugestões de melhoria.
    Roda via Celery Beat 1x por semana (domingo 20h).
    """
    from datetime import datetime, timedelta, timezone
    from app.database import get_db
    from app.services.ai import AIService
    from app.services.whatsapp import WhatsAppService

    db = get_db()
    ai = AIService()
    wa = WhatsAppService()
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat()

    owners_result = db.table("owners").select("id,phone,business_name,main_offer,target_audience").execute()
    if not owners_result.data:
        return

    for owner in owners_result.data:
        owner_id = owner["id"]
        owner_phone = owner.get("phone", "")
        if not owner_phone:
            continue

        try:
            # ── Coleta dados da semana ──────────────────────────────────
            all_leads = db.table("customers").select(
                "phone,name,lead_score,lead_status,channel,total_messages,"
                "last_contact,first_contact,last_intent,last_sentiment,"
                "sentiment_history,summary"
            ).eq("owner_id", owner_id).execute()

            leads = all_leads.data or []
            if not leads:
                continue

            # Filtra leads ativos na semana
            week_leads = []
            for l in leads:
                lc = l.get("last_contact")
                if lc and str(lc)[:10] >= week_ago[:10]:
                    week_leads.append(l)

            # Novos leads (first_contact na semana)
            new_leads = []
            for l in leads:
                fc = l.get("first_contact")
                if fc and str(fc)[:10] >= week_ago[:10]:
                    new_leads.append(l)

            total = len(leads)
            active_week = len(week_leads)
            new_count = len(new_leads)

            # Status breakdown
            status_count = {}
            for l in leads:
                s = l.get("lead_status", "desconhecido")
                status_count[s] = status_count.get(s, 0) + 1

            # Canais da semana
            channels = {}
            for l in new_leads:
                c = l.get("channel") or "não identificado"
                channels[c] = channels.get(c, 0) + 1

            # Intents da semana
            intents = {}
            for l in week_leads:
                i = l.get("last_intent") or "outros"
                intents[i] = intents.get(i, 0) + 1

            # Sentimento agregado
            sentiments = {"positivo": 0, "neutro": 0, "negativo": 0, "frustrado": 0, "entusiasmado": 0}
            for l in week_leads:
                hist = l.get("sentiment_history") or []
                for s in hist:
                    if s in sentiments:
                        sentiments[s] += 1

            total_sentiments = sum(sentiments.values()) or 1
            sentiment_pct = {k: round(v / total_sentiments * 100) for k, v in sentiments.items() if v > 0}

            # Top leads
            hot_leads = sorted(leads, key=lambda x: x.get("lead_score") or 0, reverse=True)[:5]

            # Leads perdidos / objeções
            objection_leads = [l for l in week_leads if l.get("last_intent") == "objecao"]
            cancel_leads = [l for l in week_leads if l.get("last_intent") == "cancelamento"]

            # ── Monta dados pra IA analisar ─────────────────────────────
            data_summary = (
                f"NEGÓCIO: {owner.get('business_name', '?')} | OFERTA: {owner.get('main_offer', '?')}\n"
                f"PÚBLICO: {owner.get('target_audience', '?')}\n\n"
                f"MÉTRICAS DA SEMANA:\n"
                f"- Total de leads: {total}\n"
                f"- Ativos esta semana: {active_week}\n"
                f"- Novos leads: {new_count}\n"
                f"- Status: {status_count}\n"
                f"- Canais de origem (novos): {channels}\n"
                f"- Intents detectados: {intents}\n"
                f"- Sentimento: {sentiment_pct}\n"
                f"- Objeções na semana: {len(objection_leads)}\n"
                f"- Cancelamentos: {len(cancel_leads)}\n\n"
                f"TOP 5 LEADS:\n"
            )
            for i, l in enumerate(hot_leads, 1):
                data_summary += (
                    f"{i}. {l.get('name') or l.get('phone', '?')} — "
                    f"Score: {l.get('lead_score', 0)} | "
                    f"Status: {l.get('lead_status', '?')} | "
                    f"Sentimento: {l.get('last_sentiment', '?')} | "
                    f"Canal: {l.get('channel', '?')}\n"
                )

            # Resumos dos leads com objeções pra IA entender padrões
            if objection_leads:
                data_summary += "\nOBJEÇÕES DETECTADAS:\n"
                for l in objection_leads[:5]:
                    data_summary += f"- {l.get('name') or l.get('phone', '?')}: {(l.get('summary') or '')[:150]}\n"

            # ── IA gera relatório e sugestões ───────────────────────────
            analysis_prompt = f"""Analise os dados abaixo de um negócio no WhatsApp e gere:

1. RELATÓRIO SEMANAL: resumo executivo em 5-8 linhas, destacando o mais relevante
2. PADRÕES: o que os dados revelam (canais que trazem mais leads, sentimento predominante, onde leads travam)
3. SUGESTÕES: 3 ações práticas e específicas pra próxima semana (baseadas nos dados reais, não genéricas)

DADOS:
{data_summary}

FORMATO: texto direto para WhatsApp, sem markdown pesado. Use emojis com moderação.
Foque no que IMPORTA pro dono tomar decisão."""

            analysis = run_async(ai.respond(
                system_prompt="Você é um analista de dados especializado em vendas pelo WhatsApp no Brasil. Seja direto, prático e orientado a ação.",
                history=[],
                user_message=analysis_prompt,
                use_gemini=False
            ))

            if not analysis:
                continue

            # ── Monta mensagem final ────────────────────────────────────
            # Métricas rápidas no topo + análise da IA
            header = (
                f"📊 *Relatório Semanal*\n"
                f"_{owner.get('business_name', '')}_\n\n"
                f"👥 Total: *{total}* leads | 🆕 Novos: *{new_count}*\n"
                f"💬 Ativos: *{active_week}* | 🔥 Score 70+: *{len([l for l in leads if (l.get('lead_score') or 0) >= 70])}*\n"
            )

            if sentiment_pct:
                sent_line = " | ".join(f"{k}: {v}%" for k, v in sorted(sentiment_pct.items(), key=lambda x: x[1], reverse=True))
                header += f"🎭 Sentimento: {sent_line}\n"

            if channels:
                ch_line = ", ".join(f"{k} ({v})" for k, v in sorted(channels.items(), key=lambda x: x[1], reverse=True)[:4])
                header += f"📍 Canais: {ch_line}\n"

            header += "\n━━━━━━━━━━━━━━━━━━━━━\n\n"

            full_msg = header + analysis

            # Adiciona link do painel
            full_msg += f"\n\n👉 Ver painel: {_panel_url()}"

            # WhatsApp tem limite de ~4096 chars
            if len(full_msg) > 4000:
                full_msg = full_msg[:3900] + f"\n\n_(relatório completo no painel)_\n👉 {_panel_url()}"

            run_async(wa.send_message(owner_phone, full_msg))
            logger.info(f"[WeeklyReport] Enviado para {owner.get('business_name', owner_id)}")

        except Exception as e:
            logger.error(f"[WeeklyReport] Erro owner {owner_id}: {e}")

    logger.info("[WeeklyReport] Ciclo completo")


# ── Recalcular scores de leads existentes ────────────────────────────────────

@celery_app.task(queue="learning")
def recalculate_scores(owner_id: str):
    """Recalcula score e status de todos os leads de um owner
    baseado no histórico real de mensagens. Roda sob demanda."""
    from app.database import get_db
    from app.services.ai import AIService
    from app.services.memory import MemoryService
    from app.services.whatsapp import WhatsAppService
    from app.agents.attendant import _auto_status

    db = get_db()
    ai = AIService()
    memory = MemoryService()
    wa = WhatsAppService()

    owner = db.table("owners").select("*").eq("id", owner_id).maybe_single().execute()
    if not owner or not owner.data:
        logger.error(f"[Recalc] Owner {owner_id} não encontrado")
        return

    owner_data = owner.data
    owner_phone = owner_data.get("phone", "")

    # Busca todos os leads do owner
    result = db.table("customers").select(
        "phone,name,lead_status,lead_score,total_messages"
    ).eq("owner_id", owner_id).execute()

    leads = result.data or []
    if not leads:
        if owner_phone:
            run_async(wa.send_message(owner_phone, "📊 Nenhum lead encontrado pra recalcular."))
        return

    updated = 0
    total = len(leads)

    for lead in leads:
        phone = lead.get("phone", "")
        if not phone:
            continue

        # Pula clientes (status manual)
        if lead.get("lead_status") == "cliente":
            continue

        try:
            # Busca últimas mensagens do lead (só as do usuário)
            msgs_result = db.table("messages").select("content,role").eq(
                "phone", phone
            ).eq("owner_id", owner_id).eq("role", "user").order(
                "created_at", desc=True
            ).limit(15).execute()

            user_msgs = msgs_result.data or []
            if not user_msgs:
                continue

            # Classifica cada mensagem e acumula score
            total_score = 0
            sentiments = []
            last_intent = "outros"
            last_sentiment = "neutro"

            for msg in reversed(user_msgs):  # do mais antigo pro mais novo
                content = msg.get("content", "")
                if not content or len(content) < 2:
                    continue

                try:
                    classification = run_async(ai.classify_intent(content, context=""))
                    delta = classification.get("lead_score_delta", 0)
                    total_score += delta
                    intent = classification.get("intent", "outros")
                    sentiment = classification.get("sentiment", "neutro")
                    if intent != "outros":
                        last_intent = intent
                    last_sentiment = sentiment
                    sentiments.append(sentiment)
                except Exception:
                    continue

            # Normaliza score entre 0-100
            new_score = min(100, max(0, total_score))
            new_status = _auto_status(lead.get("lead_status", "novo"), new_score)

            # Atualiza no banco
            update_data = {
                "lead_score": new_score,
                "lead_status": new_status,
                "last_intent": last_intent,
                "last_sentiment": last_sentiment,
                "sentiment_history": sentiments[-10:]  # últimos 10
            }
            db.table("customers").update(update_data).eq(
                "phone", phone
            ).eq("owner_id", owner_id).execute()

            updated += 1
            name = lead.get("name") or phone
            logger.info(f"[Recalc] {name}: score={new_score} status={new_status}")

        except Exception as e:
            logger.error(f"[Recalc] Erro {phone}: {e}")

    # Notifica o dono
    if owner_phone:
        panel = _panel_url()
        msg = (
            f"✅ *Recálculo completo!*\n\n"
            f"📊 {updated}/{total} leads atualizados\n"
            f"Scores e status recalculados com base no histórico real.\n\n"
            f"👉 Ver painel: {panel}"
        )
        run_async(wa.send_message(owner_phone, msg))

    logger.info(f"[Recalc] Concluído: {updated}/{total} leads atualizados")


# ── Campanhas Ativas ─────────────────────────────────────────────────────────

@celery_app.task(queue="learning")
def run_campaign(owner_id: str, campaign_description: str):
    """Dispara campanha ativa personalizada pra leads segmentados."""
    from app.database import get_db
    from app.services.ai import AIService
    from app.services.whatsapp import WhatsAppService
    import time
    import re as re_mod

    db = get_db()
    ai = AIService()
    wa = WhatsAppService()

    # Busca owner
    owner_result = db.table("owners").select("*").eq("id", owner_id).maybe_single().execute()
    if not owner_result or not owner_result.data:
        logger.error(f"[Campaign] Owner {owner_id} não encontrado")
        return
    owner = owner_result.data
    owner_phone = owner.get("notify_phone") or owner.get("phone")
    business_name = owner.get("business_name", "")

    # ── Parseia filtros do texto da campanha ──────────────────────────
    desc_lower = campaign_description.lower()

    # Filtro de público
    target_statuses = None  # None = todos
    if "clientes" in desc_lower or "cliente" in desc_lower:
        target_statuses = ["cliente"]
    elif "quentes" in desc_lower or "quente" in desc_lower:
        target_statuses = ["quente", "cliente"]
    elif "mornos" in desc_lower or "morno" in desc_lower:
        target_statuses = ["morno", "quente", "cliente"]
    elif "qualificando" in desc_lower:
        target_statuses = ["qualificando", "morno", "quente", "cliente"]
    elif "novos" in desc_lower or "novo" in desc_lower:
        target_statuses = ["novo"]

    # Filtro de score mínimo
    min_score = 0
    score_match = re_mod.search(r'score\s*(?:acima\s*de|maior\s*que|>=?|mínimo)\s*(\d+)', desc_lower)
    if score_match:
        min_score = int(score_match.group(1))

    # ── Busca leads elegíveis ─────────────────────────────────────────
    query = db.table("customers").select(
        "phone,name,lead_score,lead_status,summary,channel,last_sentiment,nurture_paused,total_messages"
    ).eq("owner_id", owner_id)

    if target_statuses:
        query = query.in_("lead_status", target_statuses)
    if min_score > 0:
        query = query.gte("lead_score", min_score)

    result = query.execute()
    leads = result.data if result and result.data else []

    # Filtra: não envia pra quem pediu opt-out ou tem 0 mensagens
    leads = [l for l in leads if not l.get("nurture_paused") and (l.get("total_messages") or 0) > 0]

    if not leads:
        if owner_phone:
            run_async(wa.send_message(owner_phone,
                "📢 Nenhum lead encontrado com os filtros dessa campanha. "
                "Tente ajustar o público ou o score mínimo."
            ))
        return

    # ── Notifica início ───────────────────────────────────────────────
    total = len(leads)
    if owner_phone:
        status_info = f"Status: {', '.join(target_statuses)}" if target_statuses else "Todos os status"
        run_async(wa.send_message(owner_phone,
            f"📢 *Campanha iniciada!*\n\n"
            f"👥 {total} leads selecionados\n"
            f"📊 {status_info}\n"
            f"{'📈 Score mínimo: ' + str(min_score) if min_score > 0 else ''}\n\n"
            f"Gerando mensagens personalizadas e disparando..."
        ))

    # ── Gera e envia mensagens personalizadas ─────────────────────────
    sent = 0
    errors = 0
    responded = []

    for lead in leads:
        phone = lead.get("phone")
        name = lead.get("name") or "amigo"
        summary = lead.get("summary") or "sem histórico"
        score = lead.get("lead_score") or 0
        sentiment = lead.get("last_sentiment") or "neutro"
        status = lead.get("lead_status") or "novo"

        try:
            # IA gera mensagem personalizada
            prompt = f"""Você é {business_name}. Gere UMA mensagem de WhatsApp personalizada pra esta campanha.

CAMPANHA: {campaign_description}

LEAD: {name}
HISTÓRICO: {summary[:300]}
STATUS: {status} | SCORE: {score} | SENTIMENTO: {sentiment}

REGRAS:
- Máximo 3 frases, direto e pessoal
- Use o nome do lead naturalmente
- Conecte com o histórico/interesse dele quando possível
- Tom natural de WhatsApp, como se fosse o dono mandando
- ZERO bullet points, ZERO asteriscos, ZERO formalidade
- Se for cliente: tom de relacionamento e cuidado
- Se for lead: tom de oportunidade e valor
- Não pareça propaganda genérica — tem que parecer que pensou nele
- Máximo 1 emoji, e só se fizer sentido

Responda APENAS a mensagem, nada mais."""

            response = ai.claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )
            msg = response.content[0].text.strip()

            # Envia com delay pra não parecer spam
            run_async(wa.send_typing(phone, duration=len(msg) * 40))
            run_async(wa.send_message(phone, msg))

            # Salva na tabela de mensagens pra manter histórico
            db.table("messages").insert({
                "phone": phone, "owner_id": owner_id,
                "role": "assistant", "content": f"[Campanha] {msg}",
                "created_at": __import__("datetime").datetime.utcnow().isoformat()
            }).execute()

            sent += 1
            logger.info(f"[Campaign] Enviado pra {name} ({phone})")

            # Delay entre mensagens (2-4 segundos) pra não travar API
            time.sleep(3)

        except Exception as e:
            errors += 1
            logger.error(f"[Campaign] Erro {phone}: {e}")

    # ── Relatório final ───────────────────────────────────────────────
    if owner_phone:
        panel = _panel_url()
        report = (
            f"✅ *Campanha finalizada!*\n\n"
            f"📢 {campaign_description[:100]}{'...' if len(campaign_description) > 100 else ''}\n\n"
            f"📊 *Resultado:*\n"
            f"✉️ Enviadas: {sent}/{total}\n"
        )
        if errors:
            report += f"⚠️ Erros: {errors}\n"
        report += (
            f"\nAs respostas dos leads vão chegar normalmente e o bot continua atendendo.\n\n"
            f"👉 Ver painel: {panel}"
        )
        run_async(wa.send_message(owner_phone, report))

    logger.info(f"[Campaign] Concluída: {sent}/{total} enviadas, {errors} erros")
