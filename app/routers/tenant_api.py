"""
Router: API do Painel do Cliente (Multi-tenant)
Todos os endpoints exigem JWT do Supabase Auth (Bearer token).
O tenant é identificado automaticamente via auth_user_id.

IMPORTANTE: Este router é ADITIVO — não altera nenhum fluxo existente.
O webhook, onboarding original e painel HTML continuam funcionando igual.
"""

from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel
from typing import Optional, List
from app.config import get_settings
from app.database import get_db
import httpx
import logging
import uuid
import re

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()


# ── Auth: extrair tenant do JWT ──────────────────────────────────────────────

async def get_current_tenant(authorization: str = Header(...)):
    """Valida JWT do Supabase e retorna o tenant vinculado."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token inválido")

    token = authorization.replace("Bearer ", "")
    db = get_db()

    # Valida token via Supabase Auth API
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{settings.supabase_url}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": settings.supabase_anon_key,
                },
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=401, detail="Token expirado ou inválido")
            user = resp.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[TenantAPI] Erro ao validar token: {e}")
        raise HTTPException(status_code=500, detail="Erro de autenticação")

    auth_user_id = user.get("id")
    if not auth_user_id:
        raise HTTPException(status_code=401, detail="Usuário não identificado")

    # Busca tenant vinculado
    result = (
        db.table("tenants")
        .select("*")
        .eq("auth_user_id", auth_user_id)
        .maybe_single()
        .execute()
    )

    if not result or not result.data:
        raise HTTPException(status_code=404, detail="Tenant não encontrado. Faça signup primeiro.")

    return result.data


# ── Models ───────────────────────────────────────────────────────────────────

class UpdateProfileRequest(BaseModel):
    business_name: Optional[str] = None
    business_type: Optional[str] = None
    slug: Optional[str] = None
    owner_name: Optional[str] = None
    owner_phone: Optional[str] = None
    logo_url: Optional[str] = None


class SetupBotRequest(BaseModel):
    bot_name: str
    bot_tone: str = "acolhedor e direto"
    bot_language: str = "pt-BR"
    bot_prompt: Optional[str] = None
    welcome_message: Optional[str] = None


class AddKnowledgeRequest(BaseModel):
    links: List[str]


class ConnectWhatsAppRequest(BaseModel):
    """Dados para criar instância Evolution API."""
    phone_number: Optional[str] = None  # Opcional — pode conectar via QR


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/tenant/me")
async def get_my_profile(tenant: dict = Depends(get_current_tenant)):
    """Retorna perfil completo do tenant logado."""
    # Remove campos sensíveis
    safe = {k: v for k, v in tenant.items() if k not in ("meta_page_token",)}
    return {"status": "ok", "tenant": safe}


@router.get("/tenant/stats")
async def get_my_stats(tenant: dict = Depends(get_current_tenant)):
    """Retorna estatísticas do tenant (leads, mensagens, plano)."""
    db = get_db()
    tid = tenant["id"]

    customers = db.table("customers").select("lead_status,lead_score").eq("tenant_id", tid).execute()
    messages = db.table("messages").select("id", count="exact").eq("tenant_id", tid).execute()

    leads = customers.data or []
    total_leads = len(leads)
    hot = len([l for l in leads if (l.get("lead_score") or 0) >= 70])
    warm = len([l for l in leads if 40 <= (l.get("lead_score") or 0) < 70])
    cold = len([l for l in leads if (l.get("lead_score") or 0) < 40])
    clients = len([l for l in leads if l.get("lead_status") == "cliente"])

    # Se tenant não tem customers com tenant_id ainda, tenta via owner_id (compatibilidade)
    if total_leads == 0 and tenant.get("evolution_instance"):
        owner_result = (
            db.table("owners")
            .select("id")
            .eq("evolution_instance", tenant["evolution_instance"])
            .maybe_single()
            .execute()
        )
        if owner_result and owner_result.data:
            oid = owner_result.data["id"]
            customers = db.table("customers").select("lead_status,lead_score").eq("owner_id", oid).execute()
            leads = customers.data or []
            total_leads = len(leads)
            hot = len([l for l in leads if (l.get("lead_score") or 0) >= 70])
            warm = len([l for l in leads if 40 <= (l.get("lead_score") or 0) < 70])
            cold = len([l for l in leads if (l.get("lead_score") or 0) < 40])
            clients = len([l for l in leads if l.get("lead_status") == "cliente"])

    return {
        "status": "ok",
        "stats": {
            "total_leads": total_leads,
            "hot_leads": hot,
            "warm_leads": warm,
            "cold_leads": cold,
            "clients": clients,
            "total_messages": messages.count if messages else 0,
            "msg_used": tenant.get("msg_used_monthly", 0),
            "msg_limit": tenant.get("msg_limit_monthly", 1000),
            "plan": tenant.get("plan", "starter"),
            "plan_status": tenant.get("plan_status", "trial"),
            "trial_ends_at": tenant.get("trial_ends_at"),
            "whatsapp_connected": tenant.get("whatsapp_connected", False),
        },
    }


@router.put("/tenant/profile")
async def update_profile(
    data: UpdateProfileRequest,
    tenant: dict = Depends(get_current_tenant),
):
    """Atualiza perfil do negócio (tela 2 do onboarding)."""
    db = get_db()
    update = {k: v for k, v in data.model_dump().items() if v is not None}

    if not update:
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")

    # Valida slug único
    if "slug" in update:
        slug = re.sub(r"[^a-z0-9-]", "", update["slug"].lower().strip())
        update["slug"] = slug
        existing = (
            db.table("tenants")
            .select("id")
            .eq("slug", slug)
            .neq("id", tenant["id"])
            .maybe_single()
            .execute()
        )
        if existing and existing.data:
            raise HTTPException(status_code=409, detail="Slug já em uso")

    db.table("tenants").update(update).eq("id", tenant["id"]).execute()
    return {"status": "updated", "fields": list(update.keys())}


@router.put("/tenant/bot")
async def setup_bot(
    data: SetupBotRequest,
    tenant: dict = Depends(get_current_tenant),
):
    """Configura identidade do bot (tela 4 do onboarding)."""
    db = get_db()
    update = {k: v for k, v in data.model_dump().items() if v is not None}

    db.table("tenants").update(update).eq("id", tenant["id"]).execute()

    # Se já tem owner vinculado, sincroniza campos relevantes
    if tenant.get("evolution_instance"):
        owner_update = {}
        if data.bot_name:
            owner_update["business_name"] = data.bot_name
        if data.welcome_message:
            owner_update["welcome_message"] = data.welcome_message

        if owner_update:
            db.table("owners").update(owner_update).eq(
                "evolution_instance", tenant["evolution_instance"]
            ).execute()

    return {"status": "bot_configured", "bot_name": data.bot_name}


@router.post("/tenant/connect-whatsapp")
async def connect_whatsapp(
    data: ConnectWhatsAppRequest,
    tenant: dict = Depends(get_current_tenant),
):
    """
    Cria instância na Evolution API para o tenant.
    Retorna QR code ou status de conexão.

    Fluxo:
    1. Cria instância com nome único (slug ou tenant_id)
    2. Configura webhook apontando pro backend
    3. Retorna QR code para escanear
    4. Cria registro na tabela owners vinculado ao tenant
    """
    db = get_db()
    tid = tenant["id"]

    # Verifica se já tem instância
    if tenant.get("whatsapp_connected") and tenant.get("evolution_instance"):
        return {
            "status": "already_connected",
            "instance": tenant["evolution_instance"],
            "message": "WhatsApp já conectado.",
        }

    instance_name = tenant.get("slug") or f"tenant-{tid[:8]}"

    # 1. Cria instância na Evolution API
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            create_resp = await client.post(
                f"{settings.evolution_api_url}/instance/create",
                json={
                    "instanceName": instance_name,
                    "integration": "WHATSAPP-BAILEYS",
                    "qrcode": True,
                },
                headers={"apikey": settings.evolution_api_key},
            )
            create_data = create_resp.json()

            if create_resp.status_code not in (200, 201):
                logger.error(f"[TenantAPI] Evolution create falhou: {create_data}")
                raise HTTPException(status_code=502, detail="Erro ao criar instância WhatsApp")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[TenantAPI] Evolution API indisponível: {e}")
        raise HTTPException(status_code=502, detail="Evolution API indisponível")

    # 2. Configura webhook da instância
    try:
        webhook_url = f"{settings.app_url}/webhook/whatsapp"
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{settings.evolution_api_url}/webhook/set/{instance_name}",
                json={
                    "webhook": {
                        "enabled": True,
                        "url": webhook_url,
                        "webhookByEvents": False,
                        "events": ["MESSAGES_UPSERT"],
                    }
                },
                headers={"apikey": settings.evolution_api_key},
            )
    except Exception as e:
        logger.warning(f"[TenantAPI] Webhook config falhou (não crítico): {e}")

    # 3. Cria owner vinculado ao tenant (se não existe)
    existing_owner = (
        db.table("owners")
        .select("id")
        .eq("tenant_id", tid)
        .maybe_single()
        .execute()
    )

    owner_id = None
    if not existing_owner or not existing_owner.data:
        owner_id = str(uuid.uuid4())
        owner_data = {
            "id": owner_id,
            "tenant_id": tid,
            "phone": data.phone_number or tenant.get("owner_phone", ""),
            "business_name": tenant.get("business_name", ""),
            "notify_phone": data.phone_number or tenant.get("owner_phone", ""),
            "evolution_instance": instance_name,
            "agent_mode": tenant.get("agent_mode", "both"),
            "handoff_threshold": tenant.get("handoff_threshold", 70),
            "welcome_message": tenant.get("welcome_message", ""),
            "context_summary": tenant.get("context_summary", ""),
            "links_processed": tenant.get("links_processed", []),
            "qualification_questions": tenant.get("qualification_questions", []),
        }
        db.table("owners").insert(owner_data).execute()
    else:
        owner_id = existing_owner.data["id"]
        db.table("owners").update({
            "evolution_instance": instance_name,
        }).eq("id", owner_id).execute()

    # 4. Atualiza tenant
    db.table("tenants").update({
        "evolution_instance": instance_name,
        "whatsapp_number": data.phone_number or "",
    }).eq("id", tid).execute()

    # Extrai QR code da resposta
    qr_code = None
    if isinstance(create_data, dict):
        qr_code = create_data.get("qrcode", {}).get("base64") if isinstance(create_data.get("qrcode"), dict) else create_data.get("qrcode")
        if not qr_code:
            # Tenta buscar QR code via endpoint separado
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    qr_resp = await client.get(
                        f"{settings.evolution_api_url}/instance/connect/{instance_name}",
                        headers={"apikey": settings.evolution_api_key},
                    )
                    qr_data = qr_resp.json()
                    qr_code = qr_data.get("base64") or qr_data.get("qrcode", {}).get("base64")
            except Exception:
                pass

    return {
        "status": "instance_created",
        "instance": instance_name,
        "owner_id": owner_id,
        "qr_code": qr_code,
        "message": "Escaneie o QR code com seu WhatsApp para conectar.",
    }


@router.get("/tenant/whatsapp-status")
async def check_whatsapp_status(tenant: dict = Depends(get_current_tenant)):
    """Verifica se o WhatsApp está conectado na Evolution API."""
    instance = tenant.get("evolution_instance")
    if not instance:
        return {"status": "not_configured", "connected": False}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{settings.evolution_api_url}/instance/connectionState/{instance}",
                headers={"apikey": settings.evolution_api_key},
            )
            data = resp.json()
            state = data.get("state") or data.get("instance", {}).get("state", "unknown")
            connected = state == "open"

            # Atualiza flag no tenant
            db = get_db()
            if connected != tenant.get("whatsapp_connected"):
                db.table("tenants").update(
                    {"whatsapp_connected": connected}
                ).eq("id", tenant["id"]).execute()

            return {
                "status": "ok",
                "connected": connected,
                "state": state,
                "instance": instance,
            }
    except Exception as e:
        logger.error(f"[TenantAPI] Erro ao checar WhatsApp: {e}")
        return {"status": "error", "connected": False, "error": str(e)}


@router.post("/tenant/knowledge")
async def add_knowledge(
    data: AddKnowledgeRequest,
    tenant: dict = Depends(get_current_tenant),
):
    """
    Adiciona links à base de conhecimento do tenant.
    Funciona igual ao /aprender do WhatsApp, mas via API.
    """
    if not data.links:
        raise HTTPException(status_code=400, detail="Nenhum link fornecido")

    db = get_db()
    tid = tenant["id"]

    # Busca owner vinculado
    owner_result = (
        db.table("owners")
        .select("id")
        .eq("tenant_id", tid)
        .maybe_single()
        .execute()
    )

    if not owner_result or not owner_result.data:
        raise HTTPException(
            status_code=400,
            detail="Conecte o WhatsApp primeiro para criar o perfil do bot.",
        )

    owner_id = owner_result.data["id"]

    # Dispara task assíncrona de aprendizado (mesma usada pelo /aprender)
    from app.queues.tasks import learn_from_links
    learn_from_links.apply_async(args=[owner_id, data.links], queue="learning")

    # Atualiza links no tenant também
    existing = tenant.get("links_processed") or []
    new_links = [l for l in data.links if l not in existing]
    if new_links:
        db.table("tenants").update(
            {"links_processed": existing + new_links}
        ).eq("id", tid).execute()

    return {
        "status": "processing",
        "links_queued": len(data.links),
        "new_links": len(new_links),
        "message": "Links sendo processados. Pode levar até 2 minutos.",
    }


@router.get("/tenant/customers")
async def list_customers(
    tenant: dict = Depends(get_current_tenant),
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
):
    """Lista clientes/leads do tenant."""
    db = get_db()
    tid = tenant["id"]

    query = (
        db.table("customers")
        .select("id,phone,name,lead_score,lead_status,channel,total_messages,last_contact,summary")
        .eq("tenant_id", tid)
        .order("last_contact", desc=True)
        .range(offset, offset + limit - 1)
    )

    if status:
        query = query.eq("lead_status", status)

    result = query.execute()

    # Fallback: se nenhum customer tem tenant_id, busca via owner_id
    if not result.data and tenant.get("evolution_instance"):
        owner_result = (
            db.table("owners")
            .select("id")
            .eq("evolution_instance", tenant["evolution_instance"])
            .maybe_single()
            .execute()
        )
        if owner_result and owner_result.data:
            oid = owner_result.data["id"]
            query = (
                db.table("customers")
                .select("id,phone,name,lead_score,lead_status,channel,total_messages,last_contact,summary")
                .eq("owner_id", oid)
                .order("last_contact", desc=True)
                .range(offset, offset + limit - 1)
            )
            if status:
                query = query.eq("lead_status", status)
            result = query.execute()

    return {
        "status": "ok",
        "customers": result.data or [],
        "count": len(result.data or []),
    }
