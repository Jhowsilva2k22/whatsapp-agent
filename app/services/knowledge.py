"""
EcoZap — Knowledge Bank
========================
Banco de conhecimento do atendente por owner.

O que guarda:
- Informações do produto/serviço (preço, detalhes, diferenciais)
- Objeções e como lidar com elas
- Perguntas frequentes e respostas certas
- Extrações de links enviados pelo owner
- Aprendizados do nightly_learning
- Expertise e linguagem do dono

Regra de ouro: o atendente NUNCA inventa.
Se não encontrar no knowledge bank → admite que vai verificar.

Tabela Supabase: knowledge_items
  id, owner_id, category, content, source, confidence, created_at, times_used
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.database import get_db
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Categorias de conhecimento
CATEGORY_PRODUCT     = "produto"       # o que é, como funciona, preços, planos
CATEGORY_OBJECTION   = "objecao"       # como lidar com objeções específicas
CATEGORY_FAQ         = "faq"           # perguntas frequentes + respostas
CATEGORY_STYLE       = "estilo"        # linguagem, tom, vocabulário do dono
CATEGORY_EXPERTISE   = "expertise"     # conhecimento técnico/especialidade do dono
CATEGORY_COMPETITOR  = "concorrente"   # diferencial vs concorrentes
CATEGORY_TESTIMONIAL = "depoimento"    # casos de sucesso, provas sociais
CATEGORY_PROCESS     = "processo"      # como funciona a contratação, onboarding
CATEGORY_LEARNING    = "aprendizado"   # extraído do nightly_learning


class KnowledgeBank:
    """
    Serviço de conhecimento por owner.
    O atendente consulta antes de responder perguntas técnicas.
    """

    def __init__(self):
        self.db = get_db()

    # ──────────────────────── escrita ────────────────────────

    def add_item(
        self,
        owner_id: str,
        category: str,
        content: str,
        source: str = "manual",
        confidence: float = 1.0,
    ) -> dict:
        """Salva um item de conhecimento. Evita duplicatas exatas."""
        content = content.strip()
        if not content or len(content) < 10:
            return {"ok": False, "reason": "content too short"}

        # Verifica duplicata (mesmo owner + conteúdo muito similar)
        try:
            existing = self.db.table("knowledge_items")\
                .select("id")\
                .eq("owner_id", owner_id)\
                .eq("content", content)\
                .limit(1)\
                .execute()
            if existing.data:
                return {"ok": False, "reason": "duplicate", "id": existing.data[0]["id"]}
        except Exception:
            pass

        item = {
            "owner_id": owner_id,
            "category": category,
            "content": content,
            "source": source,
            "confidence": confidence,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "times_used": 0,
        }
        try:
            result = self.db.table("knowledge_items").insert(item).execute()
            return {"ok": True, "id": result.data[0]["id"] if result.data else None}
        except Exception as e:
            logger.warning("[Knowledge] Falha ao salvar item: %s", e)
            return {"ok": False, "reason": str(e)}

    def add_many(self, owner_id: str, items: list[dict]) -> int:
        """Salva múltiplos itens de uma vez. Retorna quantos foram salvos."""
        saved = 0
        for item in items:
            result = self.add_item(
                owner_id=owner_id,
                category=item.get("category", CATEGORY_FAQ),
                content=item.get("content", ""),
                source=item.get("source", "batch"),
                confidence=item.get("confidence", 0.9),
            )
            if result.get("ok"):
                saved += 1
        return saved

    # ──────────────────────── leitura ────────────────────────

    def search(self, owner_id: str, query: str, limit: int = 5) -> list[dict]:
        """
        Busca itens relevantes para uma query.
        Usa ILIKE para busca simples por palavras-chave.
        Em versão futura: embeddings para busca semântica.
        """
        query_clean = query.strip().lower()
        if not query_clean:
            return []

        # Extrai palavras-chave principais (ignora stopwords)
        stopwords = {"o", "a", "os", "as", "de", "da", "do", "que", "e", "em", "para", "com", "um", "uma", "é"}
        keywords = [w for w in re.split(r'\s+', query_clean) if len(w) > 2 and w not in stopwords]

        if not keywords:
            keywords = [query_clean[:30]]

        results = []
        seen_ids = set()

        try:
            for keyword in keywords[:3]:  # máx 3 keywords por busca
                resp = self.db.table("knowledge_items")\
                    .select("id, category, content, confidence, times_used")\
                    .eq("owner_id", owner_id)\
                    .ilike("content", f"%{keyword}%")\
                    .order("confidence", desc=True)\
                    .limit(limit)\
                    .execute()

                for item in (resp.data or []):
                    if item["id"] not in seen_ids:
                        seen_ids.add(item["id"])
                        results.append(item)

            # Ordena por confiança e uso
            results.sort(key=lambda x: (x.get("confidence", 0), x.get("times_used", 0)), reverse=True)

            # Incrementa times_used dos itens retornados
            if results:
                ids = [r["id"] for r in results[:limit]]
                self._increment_usage(ids)

        except Exception as e:
            logger.warning("[Knowledge] Falha na busca: %s", e)

        return results[:limit]

    def get_context_for_prompt(self, owner_id: str, query: str = "", limit: int = 8) -> str:
        """
        Retorna bloco de conhecimento formatado para inserir no prompt.
        Se query vazia, retorna os mais usados/confiáveis.
        """
        if query:
            items = self.search(owner_id, query, limit)
        else:
            items = self._get_top_items(owner_id, limit)

        if not items:
            return ""

        lines = []
        by_category: dict[str, list] = {}
        for item in items:
            cat = item.get("category", "geral")
            by_category.setdefault(cat, []).append(item["content"])

        category_labels = {
            CATEGORY_PRODUCT:    "PRODUTO/SERVIÇO",
            CATEGORY_FAQ:        "PERGUNTAS FREQUENTES",
            CATEGORY_OBJECTION:  "COMO LIDAR COM OBJEÇÕES",
            CATEGORY_STYLE:      "ESTILO DE COMUNICAÇÃO",
            CATEGORY_EXPERTISE:  "CONHECIMENTO ESPECIALIZADO",
            CATEGORY_COMPETITOR: "DIFERENCIAIS",
            CATEGORY_TESTIMONIAL:"PROVAS SOCIAIS",
            CATEGORY_PROCESS:    "PROCESSO DE CONTRATAÇÃO",
            CATEGORY_LEARNING:   "APRENDIZADOS RECENTES",
        }

        for cat, contents in by_category.items():
            label = category_labels.get(cat, cat.upper())
            lines.append(f"\n[{label}]")
            for c in contents[:3]:
                lines.append(f"- {c[:300]}")

        return "\n".join(lines) if lines else ""

    def get_all_faqs(self, owner_id: str, limit: int = 20) -> list[dict]:
        """Retorna todas as FAQs do owner."""
        try:
            resp = self.db.table("knowledge_items")\
                .select("content, confidence")\
                .eq("owner_id", owner_id)\
                .eq("category", CATEGORY_FAQ)\
                .order("confidence", desc=True)\
                .limit(limit)\
                .execute()
            return resp.data or []
        except Exception:
            return []

    # ──────────────────────── ingestão de links ───────────────

    async def ingest_url(self, owner_id: str, url: str) -> dict:
        """
        Fetcha um URL, extrai conhecimento com Claude e salva no banco.
        Usado quando o owner envia um link para treinar o atendente.
        """
        logger.info("[Knowledge] Ingerindo URL: %s", url)

        # Baixa o conteúdo
        raw_content = await self._fetch_url(url)
        if not raw_content:
            return {"ok": False, "reason": "Não foi possível acessar o link."}

        # Extrai conhecimento via Claude
        items = await self._extract_knowledge_from_text(raw_content, source=url, owner_id=owner_id)
        if not items:
            return {"ok": False, "reason": "Não encontrei informações relevantes nesse link."}

        saved = self.add_many(owner_id, items)
        logger.info("[Knowledge] URL %s → %d itens salvos", url, saved)
        return {"ok": True, "items_saved": saved, "total_extracted": len(items)}

    async def ingest_text(self, owner_id: str, text: str, source: str = "owner_input") -> dict:
        """
        Recebe texto direto do owner e extrai conhecimento.
        Usado quando o owner digita um treino no WhatsApp.
        """
        logger.info("[Knowledge] Ingerindo texto direto (%d chars)", len(text))
        items = await self._extract_knowledge_from_text(text, source=source, owner_id=owner_id)
        if not items:
            # Se não conseguiu classificar, salva como FAQ genérico
            self.add_item(owner_id, CATEGORY_FAQ, text, source=source)
            return {"ok": True, "items_saved": 1, "total_extracted": 1}

        saved = self.add_many(owner_id, items)
        return {"ok": True, "items_saved": saved, "total_extracted": len(items)}

    def add_from_learning(self, owner_id: str, learnings: dict) -> int:
        """
        Converte saída do nightly_learning em itens de conhecimento.
        Chamado automaticamente pelo Celery Beat.
        """
        saved = 0

        # suggested_qa → FAQ
        for qa in learnings.get("suggested_qa", []):
            pergunta = qa.get("pergunta", "").strip()
            resposta = qa.get("resposta", "").strip()
            if pergunta and resposta:
                content = f"Pergunta: {pergunta} | Resposta: {resposta}"
                r = self.add_item(owner_id, CATEGORY_FAQ, content, source="nightly_learning", confidence=0.85)
                if r.get("ok"):
                    saved += 1

        # new_objections → objeções
        for obj in learnings.get("new_objections", []):
            if isinstance(obj, str) and len(obj) > 5:
                r = self.add_item(owner_id, CATEGORY_OBJECTION, obj, source="nightly_learning", confidence=0.8)
                if r.get("ok"):
                    saved += 1

        # winning_patterns → aprendizados
        for pattern in learnings.get("winning_patterns", []):
            if isinstance(pattern, str) and len(pattern) > 10:
                r = self.add_item(owner_id, CATEGORY_LEARNING, pattern, source="nightly_learning", confidence=0.75)
                if r.get("ok"):
                    saved += 1

        logger.info("[Knowledge] nightly_learning → %d itens novos para owner %s", saved, owner_id[:8])
        return saved

    # ──────────────────────── privados ───────────────────────

    async def _fetch_url(self, url: str) -> Optional[str]:
        """Baixa conteúdo de uma URL."""
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                headers = {"User-Agent": "Mozilla/5.0 (EcoZap Bot; +https://ecozap.ai)"}
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return None
                # Remove HTML tags de forma simples
                text = re.sub(r'<[^>]+>', ' ', resp.text)
                text = re.sub(r'\s+', ' ', text).strip()
                return text[:8000]  # limita para não estourar contexto
        except Exception as e:
            logger.warning("[Knowledge] Erro ao acessar URL: %s", e)
            return None

    async def _extract_knowledge_from_text(
        self, text: str, source: str, owner_id: str
    ) -> list[dict]:
        """Usa Claude para extrair itens de conhecimento estruturados do texto."""
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

            prompt = (
                f"Você é um especialista em extrair conhecimento útil para treinar um atendente de vendas.\n\n"
                f"Analise o texto abaixo e extraia tudo que um atendente precisaria saber para:\n"
                f"- Responder perguntas sobre o produto/serviço\n"
                f"- Lidar com objeções\n"
                f"- Passar confiança e conhecimento ao cliente\n\n"
                f"Retorne um JSON com lista de itens:\n"
                f'[{{"category": "produto|faq|objecao|estilo|expertise|concorrente|depoimento|processo", '
                f'"content": "informação clara e direta", "confidence": 0.0-1.0}}]\n\n'
                f"TEXTO:\n{text[:5000]}\n\n"
                f"REGRAS:\n"
                f"- Cada item deve ser autocontido (fazer sentido sozinho)\n"
                f"- Máximo 20 itens\n"
                f"- Se o texto não tiver informação útil para vendas, retorne []\n"
                f"- Retorne APENAS o JSON, sem explicação"
            )

            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            text_resp = response.content[0].text.strip()
            # Remove markdown se houver
            text_resp = re.sub(r'^```json\n?', '', text_resp)
            text_resp = re.sub(r'\n?```$', '', text_resp)

            import json
            items = json.loads(text_resp)
            if isinstance(items, list):
                for item in items:
                    item["source"] = source
                return items
        except Exception as e:
            logger.warning("[Knowledge] Erro ao extrair conhecimento: %s", e)
        return []

    def _get_top_items(self, owner_id: str, limit: int) -> list[dict]:
        """Retorna os itens mais usados e mais confiáveis."""
        try:
            resp = self.db.table("knowledge_items")\
                .select("id, category, content, confidence, times_used")\
                .eq("owner_id", owner_id)\
                .order("times_used", desc=True)\
                .limit(limit)\
                .execute()
            return resp.data or []
        except Exception:
            return []

    def _increment_usage(self, ids: list):
        """Incrementa o contador de uso dos itens consultados."""
        try:
            for item_id in ids:
                self.db.rpc("increment_knowledge_usage", {"item_id": item_id}).execute()
        except Exception:
            pass  # não é crítico
