from fastapi import APIRouter, Request, HTTPException
from app.services.whatsapp import WhatsAppService
from app.services.memory import MemoryService
from app.queues.tasks import process_message, process_buffered, learn_from_links, follow_up_active, weekly_report, recalculate_scores, run_campaign, celery_app, _panel_url
from app.config import get_settings
import logging
import re
import redis

logger = logging.getLogger(__name__)
router = APIRouter()
whatsapp = WhatsAppService()
memory = MemoryService()

# Redis para deduplicação
_settings = get_settings()
_redis = redis.from_url(_settings.redis_url, decode_responses=True)
DEDUP_TTL = 120  # segundos — janela de proteção contra duplicatas
DEBOUNCE_SECONDS = 4  # espera 4s após última mensagem antes de responder

# Comandos do DONO — todos com /prefixo
LEARN_PREFIXES   = ("/aprender ", "/aprender:", "/link ", "/link:", "/base ")
HANDOFF_ASSUME   = ("/assumir ",)
HANDOFF_RESUME   = ("/retomar ", "/devolver ")
NOTE_PREFIX      = ("/nota ",)
WELCOME_PREFIX   = ("/bemvindo ", "/bemvindo:", "/boasvindas ", "/boasvindas:")
CLIENT_PREFIX    = ("/cliente ",)
STATS_CMDS       = ("/stats", "/status", "/resumo")
REPORT_CMDS      = ("/relatorio", "/relatório", "/report")
RECALC_CMDS      = ("/recalcular",)
PANEL_CMDS       = ("/painel", "/panel", "/dashboard")
GOOGLE_CMDS      = ("/conectar_google", "/google")
CAMPAIGN_PREFIX  = ("/campanha ", "/campanha:")
# ── Trainer: treinamento do atendente ──────────────────────────────────────
TRAINER_PREFIXES = ("/treinar ", "/treinar:")
KNOWLEDGE_CMDS   = ("/conhecimento", "/conhecimento:", "/sabe", "/oque sabe")
FORGET_PREFIXES  = ("/esquecer ", "/esquecer:")

@router.post("/webhook/whatsapp")
async def receive_whatsapp(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload invalido")

    message = whatsapp.parse_webhook(payload)
    if not message:
        return {"status": "ignored"}

    # ── Deduplicação por message_id ──────────────────────────────────────────
    if message.message_id:
        dedup_key = f"dedup:{message.message_id}"
        try:
            already_seen = _redis.get(dedup_key)
            if already_seen:
                logger.info(f"[Webhook] Duplicata ignorada: {message.message_id}")
                return {"status": "duplicate"}
            _redis.setex(dedup_key, DEDUP_TTL, "1")
        except Exception as e:
            logger.warning(f"[Webhook] Redis dedup falhou (continuando): {e}")

    owner = await _get_owner_by_instance(message.instance)
    if not owner:
        return {"status": "owner_not_found"}

    # ── Instância Evolution correta para este owner (multi-tenant) ───────────
    evolution_instance = owner.get("evolution_instance") or message.instance

    owner_phone = _normalize_phone(owner.get("phone", ""))
    sender_phone = _normalize_phone(message.phone)
    msg_raw = message.message or ""
    msg_lower = msg_raw.lower().strip()

    # ── Comandos exclusivos do DONO ──────────────────────────────────────────
    if sender_phone == owner_phone and message.media_type == "text":

        # APRENDER: alimentar base de conhecimento
        if any(msg_lower.startswith(p) for p in LEARN_PREFIXES):
            links = _extract_urls(msg_raw)
            if links:
                learn_from_links.apply_async(args=[owner["id"], links], queue="learning")
                await whatsapp.send_message(
                    message.phone,
                    f"📚 Recebi {len(links)} link(s)! Vou processar e aprender. Pode levar até 2 minutos.",
                    instance=evolution_instance
                )
            return {"status": "learning_queued"}

        # ASSUMIR: dono vai atender o lead pessoalmente
        if any(msg_lower.startswith(p) for p in HANDOFF_ASSUME):
            lead_phone = _extract_phone(msg_raw)
            if lead_phone:
                await _owner_assumes(lead_phone, owner, msg_raw)
                return {"status": "handoff_assumed"}

        # RETOMAR: devolver lead pro bot
        if any(msg_lower.startswith(p) for p in HANDOFF_RESUME):
            lead_phone = _extract_phone(msg_raw)
            if lead_phone:
                await _owner_resumes(lead_phone, owner)
                return {"status": "handoff_resumed"}

        # NOTA: dono adiciona anotação pós-conversa
        if any(msg_lower.startswith(p) for p in NOTE_PREFIX):
            lead_phone = _extract_phone(msg_raw)
            note_text = _extract_note(msg_raw)
            if lead_phone and note_text:
                await _save_owner_note(lead_phone, owner, note_text)
                await whatsapp.send_message(message.phone, "✅ Anotação salva no perfil do lead.", instance=evolution_instance)
                return {"status": "note_saved"}

        # BOAS-VINDAS: dono configura mensagem de boas-vindas
        if any(msg_lower.startswith(p) for p in WELCOME_PREFIX):
            welcome_text = _extract_after_prefix(msg_raw, WELCOME_PREFIX)
            if welcome_text:
                try:
                    memory.db.table("tenants").update({"welcome_message": welcome_text}).eq("id", owner["id"]).execute()
                    await whatsapp.send_message(
                        message.phone,
                        f"✅ Mensagem de boas-vindas atualizada!\n\n"
                        f"Preview:\n_{welcome_text}_\n\n"
                        f"Variáveis disponíveis: {{nome}}, {{negocio}}",
                        instance=evolution_instance
                    )
                except Exception as e:
                    logger.error(f"[Webhook] Erro ao salvar welcome: {e}")
                    await whatsapp.send_message(
                        message.phone,
                        "⚠️ Erro ao salvar. Tente novamente.",
                        instance=evolution_instance
                    )
                return {"status": "welcome_updated"}

        # CLIENTE: dono marca lead como cliente
        if any(msg_lower.startswith(p) for p in CLIENT_PREFIX):
            lead_phone = _extract_phone(msg_raw)
            if lead_phone:
                try:
                    customer = await memory.get_or_create_customer(lead_phone, owner["id"])
                    await memory.update_customer(lead_phone, owner["id"], {
                        "lead_status": "cliente",
                        "lead_score": max(customer.lead_score or 0, 100)
                    })
                    lead_name = customer.name or lead_phone
                    await whatsapp.send_message(
                        message.phone,
                        f"✅ *{lead_name}* marcado como cliente!\n\n"
                        f"O agente agora vai cuidar do relacionamento: check-in semanal, "
                        f"aniversário e novidades relevantes.",
                        instance=evolution_instance
                    )
                    logger.info(f"[Webhook] {lead_phone} marcado como cliente por {owner['id']}")
                except Exception as e:
                    logger.error(f"[Webhook] Erro ao marcar cliente: {e}")
                    await whatsapp.send_message(message.phone, "⚠️ Erro ao marcar como cliente.", instance=evolution_instance)
                return {"status": "client_marked"}

        # STATS: resumo rápido do dia
        if msg_lower in STATS_CMDS:
            try:
                stats_msg = await _build_owner_stats(owner["id"])
                await whatsapp.send_message(message.phone, stats_msg, instance=evolution_instance)
            except Exception as e:
                logger.error(f"[Webhook] Erro ao gerar stats: {e}")
                await whatsapp.send_message(message.phone, "⚠️ Erro ao gerar relatório.", instance=evolution_instance)
            return {"status": "stats_sent"}

        # RELATÓRIO SEMANAL: análise completa com IA
        if msg_lower in REPORT_CMDS:
            try:
                await whatsapp.send_message(message.phone, "📊 Gerando relatório semanal com análise completa... pode levar até 1 minuto.", instance=evolution_instance)
                weekly_report.apply_async(queue="learning")
            except Exception as e:
                logger.error(f"[Webhook] Erro ao agendar relatório: {e}")
                await whatsapp.send_message(message.phone, "⚠️ Erro ao gerar relatório.", instance=evolution_instance)
            return {"status": "report_queued"}

        # RECALCULAR: recalcula scores de todos os leads
        if msg_lower in RECALC_CMDS:
            try:
                await whatsapp.send_message(message.phone, "🔄 Recalculando scores de todos os leads... pode levar alguns minutos.", instance=evolution_instance)
                recalculate_scores.apply_async(args=[owner["id"]], queue="learning")
            except Exception as e:
                logger.error(f"[Webhook] Erro ao agendar recálculo: {e}")
                await whatsapp.send_message(message.phone, "⚠️ Erro ao iniciar recálculo.", instance=evolution_instance)
            return {"status": "recalc_queued"}

        # PAINEL: envia link direto pro painel autenticado
        if msg_lower in PANEL_CMDS:
            panel = _panel_url()
            await whatsapp.send_message(message.phone, f"📊 Acesse seu painel:\n👉 {panel}", instance=evolution_instance)
            return {"status": "panel_sent"}

        # GOOGLE CALENDAR: inicia OAuth para conectar Google Calendar + Gmail
        if msg_lower in GOOGLE_CMDS:
            from app.services.calendar import build_oauth_url
            redirect_uri = f"{_settings.app_url}/auth/google/callback"
            oauth_url = build_oauth_url(
                client_id=_settings.google_client_id,
                redirect_uri=redirect_uri,
                state=owner["id"],
            )
            await whatsapp.send_message(
                message.phone,
                f"🔗 *Conectar Google Calendar + Gmail*\n\n"
                f"Clique no link e autorize o EcoZap a acessar sua agenda:\n\n"
                f"{oauth_url}\n\n"
                f"Após autorizar, você recebe a confirmação aqui.",
                instance=evolution_instance
            )
            return {"status": "google_oauth_sent"}

        # ── CAMPANHA: wizard conversacional ──────────────────────────────
        campaign_key = f"campaign_wizard:{owner['id']}"
        campaign_state = _redis.hgetall(campaign_key)

        if msg_lower.strip() in ("/campanha", "/campanha:"):
            _redis.hset(campaign_key, mapping={"step": "publico"})
            _redis.expire(campaign_key, 600)
            await whatsapp.send_message(message.phone,
                "📢 *Vamos criar sua campanha!*\n\n"
                "Primeiro, pra quem é?\n\n"
                "1️⃣ Todos os leads\n"
                "2️⃣ Só mornos pra cima (score 40+)\n"
                "3️⃣ Só quentes (score 70+)\n"
                "4️⃣ Só clientes\n\n"
                "Responda com o número ou descreva (ex: 'mornos e quentes')",
                instance=evolution_instance
            )
            return {"status": "campaign_step_publico"}

        if campaign_state and campaign_state.get("step"):
            step = campaign_state["step"]

            if step == "publico":
                pub = msg_lower.strip()
                if pub in ("1", "todos", "todos os leads"):
                    target = "todos"; target_label = "todos os leads"
                elif pub in ("2", "mornos", "mornos pra cima", "morno"):
                    target = "mornos+"; target_label = "mornos pra cima (score 40+)"
                elif pub in ("3", "quentes", "quente", "quentes pra cima"):
                    target = "quentes"; target_label = "quentes (score 70+)"
                elif pub in ("4", "clientes", "cliente"):
                    target = "clientes"; target_label = "clientes"
                else:
                    target = pub; target_label = pub

                _redis.hset(campaign_key, mapping={"step": "descricao", "target": target, "target_label": target_label})
                _redis.expire(campaign_key, 600)
                await whatsapp.send_message(message.phone,
                    f"👥 Público: *{target_label}*\n\n"
                    "Agora me diz: sobre o que é a campanha?\n\n"
                    "Descreva em 1-2 frases (ex: 'lançamento do curso de educação financeira, tom de urgência com exclusividade')",
                    instance=evolution_instance
                )
                return {"status": "campaign_step_descricao"}

            if step == "descricao":
                descricao = msg_raw.strip()
                target = campaign_state.get("target", "todos")
                target_label = campaign_state.get("target_label", "todos")

                from app.database import get_db
                db = get_db()
                query = db.table("customers").select("phone,nurture_paused,total_messages,lead_status,lead_score").eq("owner_id", owner["id"])
                if target == "clientes":
                    query = query.eq("lead_status", "cliente")
                elif target == "quentes":
                    query = query.gte("lead_score", 70)
                elif target == "mornos+":
                    query = query.gte("lead_score", 40)

                preview = query.execute()
                leads_count = len([l for l in (preview.data or []) if not l.get("nurture_paused") and (l.get("total_messages") or 0) > 0])

                _redis.hset(campaign_key, mapping={"step": "confirmar", "descricao": descricao})
                _redis.expire(campaign_key, 600)
                await whatsapp.send_message(message.phone,
                    f"📢 *Confirma a campanha?*\n\n"
                    f"👥 Público: *{target_label}*\n"
                    f"📝 Tema: {descricao[:150]}\n"
                    f"📊 Leads que vão receber: *{leads_count}*\n\n"
                    f"A IA vai personalizar cada mensagem com nome e histórico do lead.\n\n"
                    f"Responda *sim* pra disparar ou *não* pra cancelar.",
                    instance=evolution_instance
                )
                return {"status": "campaign_step_confirmar"}

            if step == "confirmar":
                if msg_lower.strip() in ("sim", "s", "yes", "bora", "vai", "manda", "confirmar", "ok"):
                    target = campaign_state.get("target", "todos")
                    descricao = campaign_state.get("descricao", "")
                    target_label = campaign_state.get("target_label", "todos")
                    _redis.delete(campaign_key)

                    campaign_full = f"{descricao} | público: {target_label}"
                    await whatsapp.send_message(message.phone,
                        "🚀 *Campanha iniciada!*\n\n"
                        "Gerando mensagens personalizadas e disparando...\n"
                        "Você recebe o relatório quando terminar.",
                        instance=evolution_instance
                    )
                    run_campaign.apply_async(args=[owner["id"], campaign_full], queue="learning")
                    return {"status": "campaign_started"}
                else:
                    _redis.delete(campaign_key)
                    await whatsapp.send_message(message.phone, "❌ Campanha cancelada.", instance=evolution_instance)
                    return {"status": "campaign_cancelled"}

        # ── TRAINER: treinar o atendente pelo WhatsApp ───────────────────────
        is_trainer_cmd = (
            any(msg_lower.startswith(p) for p in TRAINER_PREFIXES)
            or msg_lower.strip() in KNOWLEDGE_CMDS
            or any(msg_lower.startswith(p) for p in FORGET_PREFIXES)
        )
        if is_trainer_cmd:
            try:
                from app.agents.registry import get_agent
                from app.agents.base import AgentContext
                trainer = get_agent("trainer")
                if trainer:
                    ctx = AgentContext(
                        tenant_id=owner["id"],
                        payload={
                            "phone": message.phone,
                            "owner_id": owner["id"],
                            "message": msg_raw,
                        },
                    )
                    result = await trainer.act(ctx)
                    reply = result.get("response") or "✅ Pronto!"
                else:
                    reply = "⚠️ Trainer ainda está iniciando. Tente em instantes."
            except Exception as e:
                logger.error(f"[Webhook] Trainer falhou: {e}")
                reply = "⚠️ Erro ao processar o comando. Tente novamente."
            await whatsapp.send_message(message.phone, reply, instance=evolution_instance)
            return {"status": "trainer_processed"}

        # AJUDA: lista todos os comandos disponíveis
        if msg_lower in ("/help", "/ajuda", "/comandos"):
            help_msg = (
                "📋 *Comandos disponíveis:*\n\n"
                "🧠 *Treinar o atendente:*\n"
                "/treinar [texto] — ensinar algo novo ao atendente\n"
                "/treinar [link] — extrair conhecimento de um site/página\n"
                "/treinar faq: Pergunta → Resposta — adicionar FAQ\n"
                "/treinar produto: X — informação sobre produto/serviço\n"
                "/treinar objecao: X — como lidar com uma objeção\n"
                "/treinar estilo: X — instrução de tom/estilo\n"
                "/conhecimento — ver o que o atendente já sabe\n"
                "/esquecer [trecho] — remover conhecimento\n\n"
                "📊 *Gestão de leads:*\n"
                "/stats — resumo rápido do dia\n"
                "/relatorio — relatório semanal completo com IA\n"
                "/recalcular — recalcula scores de todos os leads\n"
                "/campanha — criar campanha ativa (passo a passo)\n"
                "/assumir [telefone] — assumir atendimento de um lead\n"
                "/retomar [telefone] — devolver lead pro bot\n"
                "/cliente [telefone] — marcar como cliente\n"
                "/nota [telefone] [texto] — anotar no perfil do lead\n\n"
                "⚙️ *Configuração:*\n"
                "/bemvindo [texto] — configurar mensagem de boas-vindas\n"
                "/painel — abrir painel de gestão\n"
                "/conectar_google — conectar Google Calendar e Gmail\n"
                "/ajuda — ver esta lista"
            )
            await whatsapp.send_message(message.phone, help_msg, instance=evolution_instance)
            return {"status": "help_sent"}

    # ── Bloqueia bot se lead está em atendimento humano ──────────────────────
    if sender_phone != owner_phone:
        customer = await memory.get_or_create_customer(message.phone, owner["id"])
        if customer.lead_status == "em_atendimento_humano":
            if _is_next_day(customer.last_contact):
                await memory.update_customer(
                    message.phone, owner["id"],
                    {"lead_status": "qualificando"}
                )
                logger.info(f"[Webhook] Bot retomado automaticamente para {message.phone} (dia seguinte)")
            else:
                logger.info(f"[Webhook] Ignorado — lead {message.phone} em atendimento humano")
                return {"status": "in_human_handoff"}

    # ── Tracking de follow-up ────────────────────────────────────────────────
    if sender_phone != owner_phone:
        import time as _time
        ts_key = f"last_lead_msg:{message.phone}:{owner['id']}"
        fu_key = f"followup_sent:{message.phone}:{owner['id']}"
        fu_task_key = f"followup_task:{message.phone}:{owner['id']}"
        try:
            _redis.set(ts_key, str(_time.time()))
            _redis.expire(ts_key, 1800)
            _redis.delete(fu_key)

            reset_fields = {}
            if hasattr(customer, 'follow_up_stage') and (customer.follow_up_stage or 0) > 0:
                reset_fields["follow_up_stage"] = 0
            if hasattr(customer, 'nurture_paused') and customer.nurture_paused:
                reset_fields["nurture_paused"] = False
            if reset_fields:
                await memory.update_customer(message.phone, owner["id"], reset_fields)

            old_fu = _redis.get(fu_task_key)
            if old_fu:
                celery_app.control.revoke(old_fu, terminate=False)
                _redis.delete(fu_task_key)
        except Exception as e:
            logger.warning(f"[Webhook] Follow-up tracking falhou (continuando): {e}")

    # ── Billing: checa limite de mensagens do plano ───────────────────────────
    if sender_phone != owner_phone:
        try:
            from app.middleware.billing import BillingMiddleware
            billing = BillingMiddleware()
            allowed = await billing.check_and_increment(owner["id"])
            if not allowed:
                logger.warning(f"[Billing] Limite atingido — mensagem de {message.phone} bloqueada para owner {owner['id'][:8]}")
                return {"status": "billing_limit_reached"}
        except Exception as _be:
            logger.warning(f"[Billing] Erro ao verificar limite (permitindo): {_be}")

    # ── Rate limiting: agrupa mensagens rápidas ────────────────────────────
    buffer_key = f"buffer:{message.phone}:{owner['id']}"
    task_key = f"buffer_task:{message.phone}:{owner['id']}"

    try:
        import json as _json
        msg_data = _json.dumps({
            "text": message.message or "",
            "message_id": message.message_id or "",
            "media_type": message.media_type or "text"
        })
        _redis.rpush(buffer_key, msg_data)
        _redis.expire(buffer_key, 30)

        old_task_id = _redis.get(task_key)
        if old_task_id:
            celery_app.control.revoke(old_task_id, terminate=False)

        result = process_buffered.apply_async(
            args=[message.phone, owner["id"], owner.get("agent_mode", "both")],
            countdown=DEBOUNCE_SECONDS,
            queue="messages"
        )
        _redis.setex(task_key, 30, result.id)

        if sender_phone != owner_phone:
            fu_task_key = f"followup_task:{message.phone}:{owner['id']}"
            # BUG CORRIGIDO: follow_up_active aceita apenas phone e owner_id
            fu_result = follow_up_active.apply_async(
                args=[message.phone, owner["id"]],
                countdown=300,
                queue="messages"
            )
            _redis.setex(fu_task_key, 600, fu_result.id)

    except Exception as e:
        logger.warning(f"[Webhook] Buffer falhou, processando direto: {e}")
        process_message.apply_async(
            args=[message.phone, owner["id"], message.message, owner.get("agent_mode", "both"),
                  message.message_id, message.media_type or "text"],
            queue="messages"
        )

    return {"status": "buffered"}


@router.get("/webhook/health")
async def health():
    return {"status": "ok", "service": "whatsapp-agent"}


# ── Helpers de handoff ────────────────────────────────────────────────────────

async def _owner_assumes(lead_phone: str, owner: dict, raw_msg: str):
    """Dono assume atendimento: marca lead, envia despedida natural ao lead."""
    owner_id = owner["id"]
    owner_name = owner.get("business_name", "a equipe")
    evolution_instance = owner.get("evolution_instance") or ""

    first_name = owner_name.split()[0] if owner_name else "eu"

    customer = await memory.get_or_create_customer(lead_phone, owner_id)
    customer_name = customer.name or ""

    await memory.update_customer(lead_phone, owner_id, {"lead_status": "em_atendimento_humano"})

    greeting = f"{customer_name}, " if customer_name else ""
    farewell = (
        f"{greeting}o {first_name} vai falar com você agora em instantes. "
        f"Foi ótimo conversar contigo 😊"
    )
    await whatsapp.send_message(lead_phone, farewell, instance=evolution_instance)

    report = await _build_lead_report(customer, lead_phone)
    await whatsapp.send_message(
        _normalize_phone(owner.get("phone", "")),
        f"✅ Pronto! Avisei o lead. Aqui está o resumo:\n\n{report}",
        instance=evolution_instance
    )
    logger.info(f"[Handoff] {lead_phone} assumido pelo dono")


async def _owner_resumes(lead_phone: str, owner: dict):
    """Dono devolve lead pro bot."""
    owner_id = owner["id"]
    evolution_instance = owner.get("evolution_instance") or ""
    await memory.update_customer(lead_phone, owner_id, {"lead_status": "qualificando"})
    await whatsapp.send_message(
        _normalize_phone(owner.get("phone", "")),
        f"🤖 Bot retomado para {lead_phone}. Ele vai cuidar desse lead de agora em diante.",
        instance=evolution_instance
    )
    logger.info(f"[Handoff] {lead_phone} devolvido ao bot")


async def _save_owner_note(lead_phone: str, owner: dict, note: str):
    """Salva anotação do dono no summary do cliente."""
    from datetime import datetime
    owner_id = owner["id"]
    customer = await memory.get_or_create_customer(lead_phone, owner_id)
    existing = customer.summary or ""
    timestamp = datetime.utcnow().strftime("%d/%m")
    new_summary = f"{existing}\n[Nota {timestamp}]: {note}".strip()
    await memory.update_customer(lead_phone, owner_id, {"summary": new_summary})


async def _build_owner_stats(owner_id: str) -> str:
    """Gera resumo diário do dono: leads, scores, canais, destaques."""
    from datetime import datetime
    db = memory.db
    today = datetime.utcnow().date().isoformat()

    result = db.table("customers").select("name,phone,lead_score,lead_status,channel,total_messages,last_contact").eq("owner_id", owner_id).execute()
    leads = result.data or []
    total = len(leads)
    today_leads = [l for l in leads if (str(l.get("last_contact") or ""))[:10] == today]
    hot = [l for l in leads if (l.get("lead_score") or 0) >= 70]
    human = [l for l in leads if l.get("lead_status") == "em_atendimento_humano"]

    top = sorted(leads, key=lambda x: x.get("lead_score") or 0, reverse=True)[:3]
    top_text = ""
    for i, l in enumerate(top, 1):
        name = l.get("name") or l.get("phone", "?")
        score = l.get("lead_score") or 0
        top_text += f"  {i}. {name} — {score} pts\n"

    channels = {}
    for l in leads:
        c = l.get("channel") or "não identificado"
        channels[c] = channels.get(c, 0) + 1
    ch_text = ", ".join(f"{k} ({v})" for k, v in sorted(channels.items(), key=lambda x: x[1], reverse=True)[:5])

    msg = (
        f"📊 *Resumo do dia*\n\n"
        f"👥 Total de leads: *{total}*\n"
        f"🆕 Contatos hoje: *{len(today_leads)}*\n"
        f"🔥 Leads quentes (70+): *{len(hot)}*\n"
        f"🤝 Em atendimento humano: *{len(human)}*\n\n"
    )

    if top_text:
        msg += f"🏆 *Top leads:*\n{top_text}\n"

    if ch_text:
        msg += f"📍 *Canais:* {ch_text}\n\n"

    if today_leads:
        msg += f"💬 *Ativos hoje:*\n"
        for l in today_leads[:5]:
            name = l.get("name") or l.get("phone", "?")
            msgs = l.get("total_messages") or 0
            msg += f"  • {name} — {msgs} msgs\n"

    if not leads:
        msg = "📊 Nenhum lead registrado ainda."

    return msg


async def _build_lead_report(customer, phone: str) -> str:
    wa_link = f"wa.me/{_normalize_phone(phone)}"
    name = customer.name or "Sem nome"
    score = customer.lead_score or 0
    channel = customer.channel or "não identificado"
    summary = customer.summary or "sem histórico registrado"
    total = customer.total_messages or 0
    return (
        f"👤 *{name}*\n"
        f"📱 {wa_link}\n"
        f"📊 Score: {score}/100\n"
        f"📍 Canal: {channel}\n"
        f"💬 Mensagens: {total}\n"
        f"📝 Resumo: {summary[:300]}"
    )


def _is_next_day(last_contact) -> bool:
    if not last_contact:
        return False
    from datetime import datetime, timezone
    try:
        if hasattr(last_contact, 'date'):
            last_date = last_contact.date()
        else:
            from datetime import date
            last_date = datetime.fromisoformat(str(last_contact)).date()
        return last_date < datetime.utcnow().date()
    except Exception:
        return False


# ── Helpers de parsing ────────────────────────────────────────────────────────

async def _get_owner_by_instance(instance: str):
    """Busca o tenant pelo evolution_instance.
    Lê da tabela 'tenants' (a tabela 'owners' está obsoleta/vazia).
    Normaliza os campos para compatibilidade com o restante do código.
    """
    db = memory.db
    result = db.table("tenants").select("*").eq("evolution_instance", instance).limit(1).execute()
    if not result or not result.data:
        return None
    row = dict(result.data[0])
    # Normaliza campos para compatibilidade com o restante do código
    row.setdefault("phone", row.get("owner_phone", ""))
    row.setdefault("tone", row.get("bot_tone", "amigavel"))
    row.setdefault("notify_phone", row.get("owner_phone", ""))
    return row


def _normalize_phone(phone: str) -> str:
    return re.sub(r'\D', '', phone or "")


def _extract_phone(text: str) -> str:
    digits = re.findall(r'[\d\s\-\+\(\)]{8,}', text)
    for d in digits:
        clean = re.sub(r'\D', '', d)
        if len(clean) >= 8:
            return clean
    return ""


def _extract_note(text: str) -> str:
    cleaned = re.sub(r'^/?(nota|anotacao|anotação)\s+', '', text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r'^\+?[\d\s\-\(\)]{8,}\s*', '', cleaned).strip()
    return cleaned


def _extract_after_prefix(text: str, prefixes: tuple) -> str:
    lower = text.lower().strip()
    for p in prefixes:
        if lower.startswith(p):
            return text.strip()[len(p):].strip()
    return ""


def _extract_urls(text: str) -> list:
    pattern = r'https?://[^\s]+'
    return list(set(re.findall(pattern, text)))
