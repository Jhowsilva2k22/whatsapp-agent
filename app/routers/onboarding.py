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

@router.post("/onboarding")
async def create_owner(data: OnboardingRequest):
    logger.info(f"Onboarding: {data.business_name}")
    scraped_content = await scraper.read_links(data.links)
    if not scraped_content:
        raise HTTPException(status_code=400, detail="Nao foi possivel ler os links.")
    analysis = await ai.analyze_owner_links(scraped_content)
    if not analysis:
        raise HTTPException(status_code=500, detail="Erro ao analisar os links.")
    owner_id = str(uuid.uuid4())
    owner_data = {"id": owner_id, "phone": data.phone, "business_name": data.business_name, "notify_phone": data.notify_phone or data.phone, "agent_mode": data.agent_mode, "handoff_threshold": data.handoff_threshold, "links_processed": data.links, "qualification_questions": data.qualification_questions, **analysis}
    memory.db.table("owners").insert(owner_data).execute()
    return {"status": "success", "owner_id": owner_id, "business_name": data.business_name, "persona_detected": {"tone": analysis.get("tone"), "business_type": analysis.get("business_type"), "main_offer": analysis.get("main_offer")}, "next_step": f"Configure o webhook da Evolution API: POST /webhook/whatsapp"}

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
