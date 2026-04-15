from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.services.scraper import ScraperService
from app.services.ai import AIService
from app.services.memory import MemoryService
import logging
import uuid

logger = logging.getLogger(__name__)
router = APIRouter()
scraper = ScraperService()
ai = AIService()
memory = MemoryService()

class OnboardingRequest(BaseModel):
    business_name: str
    phone: str
    notify_phone: Optional[str] = None
    links: list
    agent_mode: str = "both"
    handoff_threshold: int = 70
    qualification_questions: Optional[list] = None
    welcome_message: Optional[str] = ""

@router.post("/onboarding")
async def create_owner(data: OnboardingRequest):
    logger.info(f"Onboarding: {data.business_name}")

    # Tenta raspar links — se falhar parcialmente, continua com o que tiver
    scraped_content = ""
    if data.links:
        try:
            scraped_content = await scraper.read_links(data.links)
        except Exception as e:
            logger.warning(f"[Onboarding] Scraping parcialmente falhou: {e}")

    # Se não conseguiu raspar nada, cria perfil base sem contexto de links
    if scraped_content:
        analysis = await ai.analyze_owner_links(scraped_content) or {}
    else:
        logger.warning(f"[Onboarding] Sem conteúdo raspado — criando perfil base para {data.business_name}")
        analysis = {
            "tone": "acolhedor e direto",
            "vocabulary": [],
            "emoji_style": "medio",
            "values": [],
            "business_type": "negócio",
            "main_offer": "a ser configurado",
            "target_audience": "a ser configurado",
            "common_objections": [],
            "context_summary": f"Perfil de {data.business_name}. Links fornecidos: {', '.join(data.links)}. Use o comando 'aprender: [link]' via WhatsApp para ensinar o agente.",
        }

    owner_id = str(uuid.uuid4())
    owner_data = {
        "id": owner_id,
        "phone": data.phone,
        "business_name": data.business_name,
        "notify_phone": data.notify_phone or data.phone,
        "agent_mode": data.agent_mode,
        "handoff_threshold": data.handoff_threshold,
        "welcome_message": data.welcome_message or "",
        "links_processed": data.links,
        "qualification_questions": data.qualification_questions,
        **analysis
    }
    memory.db.table("owners").insert(owner_data).execute()
    return {
        "status": "success",
        "owner_id": owner_id,
        "business_name": data.business_name,
        "scraped_links": bool(scraped_content),
        "persona_detected": {
            "tone": analysis.get("tone"),
            "business_type": analysis.get("business_type"),
            "main_offer": analysis.get("main_offer")
        },
        "next_step": "Configure evolution_instance no banco e use 'aprender: [link]' pelo WhatsApp para adicionar mais conhecimento."
    }

@router.put("/onboarding/{owner_id}/refresh-links")
async def refresh_owner_links(owner_id: str):
    result = memory.db.table("owners").select("*").eq("id", owner_id).maybe_single().execute()
    if not (result and result.data):
        raise HTTPException(status_code=404, detail="Dono nao encontrado")
    links = result.data.get("links_processed", [])
    if not links:
        raise HTTPException(status_code=400, detail="Nenhum link salvo")
    scraped_content = await scraper.read_links(links)
    analysis = await ai.analyze_owner_links(scraped_content)
    memory.db.table("owners").update(analysis).eq("id", owner_id).execute()
    return {"status": "updated", "owner_id": owner_id}


class AddLinksRequest(BaseModel):
    links: list


@router.post("/onboarding/{owner_id}/add-links")
async def add_knowledge_links(owner_id: str, data: AddLinksRequest):
    """Adiciona novos links à base de conhecimento do dono sem apagar o que já existe."""
    result = memory.db.table("owners").select("*").eq("id", owner_id).maybe_single().execute()
    if not (result and result.data):
        raise HTTPException(status_code=404, detail="Dono nao encontrado")

    owner = result.data
    existing_links = owner.get("links_processed") or []
    new_links = [l for l in data.links if l not in existing_links]
    if not new_links:
        return {"status": "no_new_links", "message": "Todos os links já foram processados antes."}

    scraped_content = await scraper.read_links(new_links)
    if not scraped_content:
        raise HTTPException(status_code=400, detail="Nao foi possivel ler os links fornecidos.")

    # Combina contexto existente com novo conteúdo
    existing_context = owner.get("context_summary") or ""
    combined_content = f"[CONTEXTO ATUAL]\n{existing_context}\n\n[NOVO CONTEÚDO]\n{scraped_content}"
    analysis = await ai.analyze_owner_links(combined_content)

    all_links = existing_links + new_links
    memory.db.table("owners").update({**analysis, "links_processed": all_links}).eq("id", owner_id).execute()

    logger.info(f"[Onboarding] {owner_id} adicionou {len(new_links)} links: {new_links}")
    return {
        "status": "updated",
        "owner_id": owner_id,
        "new_links_processed": new_links,
        "total_links": len(all_links),
        "detected": {"tone": analysis.get("tone"), "main_offer": analysis.get("main_offer")}
    }
