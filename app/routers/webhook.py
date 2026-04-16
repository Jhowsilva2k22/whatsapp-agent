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
LEARN_PREFIXES = ("/aprender ", "/aprender:", "/link ", "/link:", "/base ")
HANDOFF_ASSUME = ("/assumir ",)
HANDOFF_RESUME = ("/retomar ", "/devolver ")
NOTE_PREFIX    = ("/nota ",)
WELCOME_PREFIX = ("/bemvindo ", "/bemvindo:", "/boasvindas ", "/boasvindas:")
CLIENT_PREFIX  = ("/cliente ",)
STATS_CMDS     = ("/stats", "/status", "/resumo")
REPORT_CMDS    = ("/relatorio", "/relatório", "/report")
RECALC_CMDS    = ("/recalcular",)
PANEL_CMDS     = ("/painel", "/panel", "/dashboard")
GOOGLE_CMDS    = ("/conectar_google", "/google")
CAMPAIGN_PREFIX = ("/campanha ", "/campanha:")

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
                    f"📚 Recebi {len(links)} link(s)! Vou processar e aprender. Pode levar até 2 minutos."
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
                await whatsapp.send_message(message.phone, "✅ Anotação salva no perfil do lead.")
                return {"status": "note_saved"}

        # BOAS-VINDAS: dono configura mensagem de boas-vindas
        if any(msg_lower.startswith(p) for p in WELCOME_PREFIX):
            welcome_text = _extract_after_prefix(msg_raw, WELCOME_PREFIX)
            if welcome_text:
                try:
                    memory.db.table("owners").update({"welcome_message": welcome_text}).eq("id", owner["id"]).execute()
                    await whatsapp.send_message(
                        message.phone,
                        f"✅ Mensagem de boas-vindas atualizada!\n\n"
                        f"Preview:\n_{welcome_text}_\n\n"
                        f"Variáveis disponíveis: {{nome}}, {{negocio}}"
                    )
                except Exception as e:
                    logger.error(f"[Webhook] Erro ao salvar welcome: {e}")
                    await whatsapp.send_message(
                        message.phone,
                        "⚠️ Erro ao salvar. Rode no Supabase:\n"
                        "ALTER TABLE owners ADD COLUMN welcome_message TEXT DEFAULT '';"
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
                        f"aniversário e novidades relevantes."
                    )
                    logger.info(f"[Webhook] {lead_phone} marcado como cliente por {owner['id']}")
                except Exception as e:
                    logger.error(f"[Webhook] Erro ao marcar cliente: {e}")
                    await whatsapp.send_message(message.phone, "⚠️ Erro ao marcar como cliente.")
                return {"status": "client_marked"}

        # STATS: resumo rápido do dia
        if msg_lower in STATS_CMDS:
            try:
                stats_msg = await _build_owner_stats(owner["id"])
                await whatsapp.send_message(message.phone, stats_msg)
            except Exception as e:
                logger.error(f"[Webhook] Erro ao gerar stats: {e}")
                await whatsapp.send_message(message.phone, "⚠️ Erro ao gerar relatório.")
            return {"status": "stats_sent"}

        # RELATÓRIO SEMANAL: análise completa com IA
        if msg_lower in REPORT_CMDS:
            try:
                await whatsapp.send_message(message.phone, "📊 Gerando relatório semanal com análise completa... pode levar até 1 minuto.")
                weekly_report.apply_async(queue="learning")
            except Exception as e:
                logger.error(f"[Webhook] Erro ao agendar relatório: {e}")
                await whatsapp.send_message(message.phone, "⚠️ Erro ao gerar relatório.")
            return {"status": "report_queued"}

        # RECALCULAR: recalcula scores de todos os leads
        if msg_lower in RECALC_CMDS:
            try:
                await whatsapp.send_message(message.phone, "🔄 Recalculando scores de todos os leads... pode levar alguns minutos.")
                recalculate_scores.apply_async(args=[owner["id"]], queue="learning")
            except Exception as e:
                logger.error(f"[Webhook] Erro ao agendar recálculo: {e}")
                await whatsapp.send_message(message.phone, "⚠️ Erro ao iniciar recálculo.")
            return {"status": "recalc_queued"}

        # PAINEL: envia link direto pro painel autenticado
        if msg_lower in PANEL_CMDS:
            panel = _panel_url()
            await whatsapp.send_message(message.phone, f"📊 Acesse seu painel:\n👉 {panel}")
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
                f"Após autorizar, você recebe a confirmação aqui."
            )
            return {"status": "google_oauth_sent"}

        # ── CAMPANHA: wizard conversacional ──────────────────────────────
        campaign_key = f"campaign_wizard:{owner['id']}"
        campaign_state = _redis.hgetall(campaign_key)

        # /campanha inicia o wizard
        if msg_lower.strip() in ("/campanha", "/campanha:"):
            _redis.hset(campaign_key, mapping={"step": "publico"})
            _redis.expire(campaign_key, 600)  # 10 min timeout
            await whatsapp.send_message(message.phone,
                "📢 *Vamos criar sua campanha!*\n\n"
                "Primeiro, pra quem é?\n\n"
                "1️⃣ Todos os leads\n"
                "2️⃣ Só mornos pra cima (score 40+)\n"
                "3️⃣ Só quentes (score 70+)\n"
                "4️⃣ Só clientes\n\n"
                "Responda com o número ou descreva (ex: 'mornos e quentes')"
            )
            return {"status": "campaign_step_publico"}

        # Wizard ativo — processa resposta
        if campaign_state and campaign_state.get("step"):
            step = campaign_state["step"]

            # PASSO 1: Público
            if step == "publico":
                # Parseia público
                pub = msg_lower.strip()
                if pub in ("1", "todos", "todos os leads"):
                    target = "todos"
                    target_label = "todos os leads"
                elif pub in ("2", "mornos", "mornos pra cima", "morno"):
                    target = "mornos+"
                    target_label = "mornos pra cima (score 40+)"
                elif pub in ("3", "quentes", "quente", "quentes pra cima"):
                    target = "quentes"
                    target_label = "quentes (score 70+)"
                elif pub in ("4", "clientes", "cliente"):
                    target = "clientes"
                    target_label = "clientes"
                else:
                    target = pub
                    target_label = pub

                _redis.hset(campaign_key, mapping={"step": "descricao", "target": target, "target_label": target_label})
                _redis.expire(campaign_key, 600)
                await whatsapp.send_message(message.phone,
                    f"👥 Público: *{target_label}*\n\n"
                    "Agora me diz: sobre o que é a campanha?\n\n"
                    "Descreva em 1-2 frases (ex: 'lançamento do curso de educação financeira, tom de urgência com exclusividade')"
                )
                return {"status": "campaign_step_descricao"}

            # PASSO 2: Descrição
            if step == "descricao":
                descricao = msg_raw.strip()
                target = campaign_state.get("target", "todos")
                target_label = campaign_state.get("target_label", "todos")

                # Conta leads elegíveis pra dar preview
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
                    f"Responda *sim* pra disparar ou *não* pra cancelar."
                )
                return {"status": "campaign_step_confirmar"}

            # PASSO 3: Confirmação
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
                        "Você recebe o relatório quando terminar."
                    )
                    run_campaign.apply_async(args=[owner["id"], campaign_full], queue="learning")
                    return {"status": "campaign_started"}
                else:
                    _redis.delete(campaign_key)
                    await whatsapp.send_message(message.phone, "❌ Campanha cancelada.")
                    return {"status": "campaign_cancelled"}

        # AJUDA: lista todos os comandos disponíveis
        if msg_lower in ("/help", "/ajuda", "/comandos"):
            help_msg = (
                "📋 *Comandos disponíveis:*\n\n"
                "/stats — resumo rápido do dia\n"
                "/relatorio — relatório semanal completo com IA\n"
                "/recalcular — recalcula scores de todos os leads\n"
                "/campanha — criar campanha ativa (passo a passo)\n"
                "/assumir [telefone] — assumir atendimento de um lead\n"
                "/retomar [telefone] — devolver lead pro bot\n"
                "/cliente [telefone] — marcar como cliente\n"
                "/nota [telefone] [texto] — anotar no perfil do lead\n"
                "/aprender [link] — ensinar o bot com novo conteúdo\n"
                "/bemvindo [texto] — configurar mensagem de boas-vindas\n"
                "/painel — abrir painel de gestão\n"
                "/conectar_google — conectar Google Calendar e Gmail\n"
                "/ajuda — ver esta lista"
            )
            await whatsapp.send_message(message.phone, help_msg)
            return {"status": "help_sent"}

    # ── Bloqueia bot se lead está em atendimento humano ──────────────────────
    if sender_phone != owner_phone:
        customer = await memory.get_or_create_customer(message.phone, owner["id"])
        if customer.lead_status == "em_atendimento_humano":
            # Verifica se é dia seguinte — se sim, retoma o bot automaticamente
            if _is_next_day(customer.last_contact):
                await memory.update_customer(
                    message.phone, owner["id"],
                    {"lead_status": "qualificando"}
                )
                logger.info(f"[Webhook] Bot retomado automaticamente para {message.phone} (dia seguinte)")
                # Deixa cair no fluxo normal abaixo
            else:
                logger.info(f"[Webhook] Ignorado — lead {message.phone} em atendimento humano")
                return {"status": "in_human_handoff"}

    # ── Tracking de follow-up: marca timestamp + reseta estágio frio ────────
    if sender_phone != owner_phone:
        import time as _time
        ts_key = f"last_lead_msg:{message.phone}:{owner['id']}"
        fu_key = f"followup_sent:{message.phone}:{owner['id']}"
        fu_task_key = f"followup_task:{message.phone}:{owner['id']}"
        try:
            _redis.set(ts_key, str(_time.time()))
            _redis.expire(ts_key, 1800)  # TTL 30min
            _redis.delete(fu_key)  # reseta follow-up ativo ao receber msg

            # Reseta follow_up_stage e nurture_paused se lead/cliente voltou a responder
            reset_fields = {}
            if hasattr(customer, 'follow_up_stage') and (customer.follow_up_stage or 0) > 0:
                reset_fields["follow_up_stage"] = 0
            if hasattr(customer, 'nurture_paused') and customer.nurture_paused:
                reset_fields["nurture_paused"] = False
            if reset_fields:
                await memory.update_customer(
                    message.phone, owner["id"], reset_fields
                )

            # Revoga follow-up ativo anterior (se existir)
            old_fu = _redis.get(fu_task_key)
            if old_fu:
                celery_app.control.revoke(old_fu, terminate=False)
                _redis.delete(fu_task_key)
        except Exception as e:
            logger.warning(f"[Webhook] Follow-up tracking falhou (continuando): {e}")

    # ── Rate limiting: agrupa mensagens rápidas ────────────────────────────
    buffer_key = f"buffer:{message.phone}:{owner['id']}"
    task_key = f"buffer_task:{message.phone}:{owner['id']}"

    try:
        import json as _json
        # Adiciona mensagem ao buffer (lista no Redis)
        msg_data = _json.dumps({
            "text": message.message or "",
            "message_id": message.message_id or "",
            "media_type": message.media_type or "text"
        })
        _redis.rpush(buffer_key, msg_data)
        _redis.expire(buffer_key, 30)  # TTL de segurança

        # Revoga task anterior (se existir) e agenda nova com delay
        old_task_id = _redis.get(task_key)
        if old_task_id:
            celery_app.control.revoke(old_task_id, terminate=False)

        result = process_buffered.apply_async(
            args=[message.phone, owner["id"], owner.get("agent_mode", "both")],
            countdown=DEBOUNCE_SECONDS,
            queue="messages"
        )
        _redis.setex(task_key, 30, result.id)

        # ── Agenda follow-up ativo (5 min) para leads ──────────────────────
        if sender_phone != owner_phone:
            fu_task_key = f"followup_task:{message.phone}:{owner['id']}"
            fu_result = follow_up_active.apply_async(
                args=[message.phone, owner["id"], 1],
                countdown=300,  # 5 minutos
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

    # Nome para a despedida — pega primeiro nome do business_name
    first_name = owner_name.split()[0] if owner_name else "eu"

    customer = await memory.get_or_create_customer(lead_phone, owner_id)
    customer_name = customer.name or ""

    # Marca lead como em atendimento humano
    await memory.update_customer(lead_phone, owner_id, {"lead_status": "em_atendimento_humano"})

    # Despedida natural ao lead
    greeting = f"{customer_name}, " if customer_name else ""
    farewell = (
        f"{greeting}o {first_name} vai falar com você agora em instantes. "
        f"Foi ótimo conversar contigo 😊"
    )
    await whatsapp.send_message(lead_phone, farewell)

    # Confirma pro dono com relatório do lead
    report = await _build_lead_report(customer, lead_phone)
    await whatsapp.send_message(
        _normalize_phone(owner.get("phone", "")),
        f"✅ Pronto! Avisei o lead. Aqui está o resumo:\n\n{report}"
    )
    logger.info(f"[Handoff] {lead_phone} assumido pelo dono")


async def _owner_resumes(lead_phone: str, owner: dict):
    """Dono devolve lead pro bot."""
    owner_id = owner["id"]
    await memory.update_customer(lead_phone, owner_id, {"lead_status": "qualificando"})
    await whatsapp.send_message(
        _normalize_phone(owner.get("phone", "")),
        f"🤖 Bot retomado para {lead_phone}. Ele vai cuidar desse lead de agora em diante."
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

    # Top 3 leads por score
    top = sorted(leads, key=lambda x: x.get("lead_score") or 0, reverse=True)[:3]
    top_text = ""
    for i, l in enumerate(top, 1):
        name = l.get("name") or l.get("phone", "?")
        score = l.get("lead_score") or 0
        top_text += f"  {i}. {name} — {score} pts\n"

    # Canais
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

    # Leads ativos hoje
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
    """Retorna True se o último contato foi em um dia diferente do atual."""
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
    db = memory.db
    result = db.table("owners").select("*").eq("evolution_instance", instance).maybe_single().execute()
    return result.data if result and result.data else None


def _normalize_phone(phone: str) -> str:
    return re.sub(r'\D', '', phone or "")


def _extract_phone(text: str) -> str:
    """Extrai número de telefone de um texto como 'assumir 5513999...'"""
    digits = re.findall(r'[\d\s\-\+\(\)]{8,}', text)
    for d in digits:
        clean = re.sub(r'\D', '', d)
        if len(clean) >= 8:
            return clean
    return ""


def _extract_note(text: str) -> str:
    """Remove o prefixo e o número, retorna o texto da nota."""
    # Remove prefixo tipo "/nota 5513999... texto aqui"
    cleaned = re.sub(r'^/?(nota|anotacao|anotação)\s+', '', text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r'^\+?[\d\s\-\(\)]{8,}\s*', '', cleaned).strip()
    return cleaned


def _extract_after_prefix(text: str, prefixes: tuple) -> str:
    """Remove o prefixo do texto e retorna o restante."""
    lower = text.lower().strip()
    for p in prefixes:
        if lower.startswith(p):
            return text.strip()[len(p):].strip()
    return ""


def _extract_urls(text: str) -> list:
    pattern = r'https?://[^\s]+'
    return list(set(re.findall(pattern, text)))
