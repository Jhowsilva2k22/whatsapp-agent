"""
EcoZap — Trainer Agent (Sprint 2)
===================================
Papel: Recebe treinamento do owner e alimenta o Knowledge Bank.
Hierarquia: Especialista → COMMERCIAL → CTO → CEO

Como usar (pelo WhatsApp do owner):
  /treinar <texto>         → ingere texto direto como conhecimento
  /treinar <url>           → fetcha link e extrai conhecimento
  /treinar faq: P → R     → adiciona FAQ diretamente
  /treinar objecao: X      → adiciona objeção e como lidar
  /treinar produto: X      → adiciona informação de produto
  /treinar estilo: X       → adiciona instrução de estilo/tom
  /conhecimento            → mostra o que o atendente já sabe
  /esquecer <trecho>       → remove item do knowledge bank

O Trainer NUNCA processa mensagens de clientes — só responde ao owner.
"""
import logging
import re
from datetime import datetime, timezone

from app.agents.base import Agent, AgentContext, AgentOpinion, AuthorityLevel
from app.agents.registry import register
from app.config import get_settings
from app.services.knowledge import (
    KnowledgeBank,
    CATEGORY_PRODUCT, CATEGORY_FAQ, CATEGORY_OBJECTION,
    CATEGORY_STYLE, CATEGORY_EXPERTISE, CATEGORY_TESTIMONIAL,
    CATEGORY_PROCESS, CATEGORY_COMPETITOR,
)

logger = logging.getLogger(__name__)
settings = get_settings()

URL_PATTERN = re.compile(r'https?://\S+')

# Prefixos de categoria que o owner pode usar
CATEGORY_PREFIXES = {
    "produto:":    CATEGORY_PRODUCT,
    "serviço:":    CATEGORY_PRODUCT,
    "servico:":    CATEGORY_PRODUCT,
    "faq:":        CATEGORY_FAQ,
    "pergunta:":   CATEGORY_FAQ,
    "objecao:":    CATEGORY_OBJECTION,
    "objeção:":    CATEGORY_OBJECTION,
    "estilo:":     CATEGORY_STYLE,
    "tom:":        CATEGORY_STYLE,
    "expertise:":  CATEGORY_EXPERTISE,
    "conhecimento:": CATEGORY_EXPERTISE,
    "depoimento:": CATEGORY_TESTIMONIAL,
    "testemunho:": CATEGORY_TESTIMONIAL,
    "concorrente:": CATEGORY_COMPETITOR,
    "diferencial:": CATEGORY_COMPETITOR,
    "processo:":   CATEGORY_PROCESS,
    "contratacao:": CATEGORY_PROCESS,
}


@register
class Trainer(Agent):
    role = "trainer"
    display_name = "Trainer"
    authority_level = AuthorityLevel.SPECIALIST
    department = "commercial"
    opinion_bias = "aprendizado contínuo — cada informação nova é uma vantagem competitiva"

    autonomous_actions = [
        "add_knowledge",
        "ingest_url",
        "list_knowledge",
        "remove_knowledge",
    ]
    requires_ceo_override = [
        "clear_all_knowledge",
        "broadcast_to_all_customers",
    ]

    async def act(self, context: AgentContext) -> dict:
        """
        Processa comandos de treinamento vindos do owner.
        Retorna resposta para enviar de volta ao owner pelo WhatsApp.
        """
        command_text = context.payload.get("message", "").strip()
        owner_id = context.payload.get("owner_id", "")
        phone = context.payload.get("phone", "")

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "owner_id": owner_id,
            "command": command_text[:50],
            "response": "",
            "items_saved": 0,
        }

        kb = KnowledgeBank()
        cmd_lower = command_text.lower().strip()

        # ── /conhecimento → lista o que sabe ──────────────────────────────
        if cmd_lower.startswith("/conhecimento") or cmd_lower.startswith("/sabe"):
            response = self._list_knowledge(kb, owner_id)
            result["response"] = response
            return result

        # ── /esquecer → remove item ───────────────────────────────────────
        if cmd_lower.startswith("/esquecer "):
            trecho = command_text[10:].strip()
            removed = self._remove_knowledge(kb, owner_id, trecho)
            result["response"] = (
                f"✓ Removi {removed} item(s) que continham: \"{trecho[:50]}\""
                if removed else
                f"Não encontrei nenhum item com: \"{trecho[:50]}\""
            )
            return result

        # ── /treinar → ingere conhecimento ───────────────────────────────
        if cmd_lower.startswith("/treinar"):
            content = command_text[8:].strip()
            if not content:
                result["response"] = self._help_message()
                return result

            # Link?
            url_match = URL_PATTERN.search(content)
            if url_match:
                url = url_match.group()
                result["response"] = f"⏳ Acessando o link e extraindo o conhecimento...\n`{url[:60]}`"
                # Ingestão assíncrona (responde rápido, processa depois)
                import asyncio
                asyncio.create_task(self._ingest_url_and_notify(kb, owner_id, phone, url))
                return result

            # Verifica prefixo de categoria
            category, clean_content = self._parse_category(content)

            # FAQ no formato "Pergunta → Resposta"
            if "→" in clean_content or "->" in clean_content:
                sep = "→" if "→" in clean_content else "->"
                parts = clean_content.split(sep, 1)
                if len(parts) == 2:
                    pergunta = parts[0].strip()
                    resposta = parts[1].strip()
                    faq_content = f"Pergunta: {pergunta} | Resposta: {resposta}"
                    r = kb.add_item(owner_id, CATEGORY_FAQ, faq_content, source="owner_whatsapp")
                    result["items_saved"] = 1 if r.get("ok") else 0
                    result["response"] = (
                        f"✓ FAQ registrada!\n\n"
                        f"*Pergunta:* {pergunta[:80]}\n"
                        f"*Resposta:* {resposta[:150]}\n\n"
                        f"O atendente já pode usar isso nas conversas."
                        if r.get("ok") else
                        "Essa informação já estava registrada."
                    )
                    return result

            # Texto longo → extrai conhecimento com IA
            if len(clean_content) > 200:
                result["response"] = "⏳ Processando o texto e extraindo conhecimento..."
                asyncio.create_task(self._ingest_text_and_notify(kb, owner_id, phone, clean_content))
                return result

            # Texto curto → salva direto
            r = kb.add_item(owner_id, category, clean_content, source="owner_whatsapp")
            result["items_saved"] = 1 if r.get("ok") else 0
            result["response"] = (
                f"✓ Aprendi: \"{clean_content[:100]}\"\n\n"
                f"Categoria: {category} | O atendente já pode usar isso."
                if r.get("ok") else
                "Essa informação já estava no conhecimento."
            )
            return result

        # Sem comando reconhecido
        result["response"] = self._help_message()
        return result

    # ──────────────────── helpers de comando ─────────────────

    def _parse_category(self, content: str) -> tuple:
        """Detecta prefixo de categoria. Retorna (categoria, conteúdo_limpo)."""
        content_lower = content.lower()
        for prefix, category in CATEGORY_PREFIXES.items():
            if content_lower.startswith(prefix):
                return category, content[len(prefix):].strip()
        return CATEGORY_FAQ, content  # padrão: FAQ

    def _list_knowledge(self, kb: KnowledgeBank, owner_id: str) -> str:
        """Lista resumo do que o atendente sabe."""
        try:
            db = kb.db
            resp = db.table("knowledge_items")\
                .select("category")\
                .eq("owner_id", owner_id)\
                .execute()
            data = resp.data or []
        except Exception:
            data = []

        if not data:
            return (
                "O atendente ainda não tem conhecimento treinado.\n\n"
                "Use /treinar para ensinar:\n"
                "- /treinar produto: descrição do produto\n"
                "- /treinar faq: Pergunta → Resposta\n"
                "- /treinar https://seusite.com\n"
            )

        from collections import Counter
        counts = Counter(item["category"] for item in data)
        labels = {
            "produto": "Produto/Serviço",
            "faq": "Perguntas Frequentes",
            "objecao": "Como lidar com objeções",
            "estilo": "Estilo de comunicação",
            "expertise": "Conhecimento especializado",
            "concorrente": "Diferenciais",
            "depoimento": "Provas sociais",
            "processo": "Processo de contratação",
            "aprendizado": "Aprendizados automáticos",
        }

        lines = [f"*O atendente conhece {len(data)} itens:*\n"]
        for cat, count in sorted(counts.items(), key=lambda x: -x[1]):
            label = labels.get(cat, cat)
            lines.append(f"  • {label}: {count}")
        lines.append("\nUse /treinar para adicionar mais.")
        return "\n".join(lines)

    def _remove_knowledge(self, kb: KnowledgeBank, owner_id: str, trecho: str) -> int:
        """Remove itens que contêm o trecho."""
        try:
            resp = kb.db.table("knowledge_items")\
                .select("id")\
                .eq("owner_id", owner_id)\
                .ilike("content", f"%{trecho}%")\
                .execute()
            ids = [r["id"] for r in (resp.data or [])]
            if ids:
                kb.db.table("knowledge_items").delete().in_("id", ids).execute()
            return len(ids)
        except Exception as e:
            logger.warning("[Trainer] Erro ao remover: %s", e)
            return 0

    async def _ingest_url_and_notify(self, kb: KnowledgeBank, owner_id: str, phone: str, url: str):
        """Ingere URL e envia resultado para o owner."""
        from app.services.sender import send_text_message
        result = await kb.ingest_url(owner_id, url)
        if result.get("ok"):
            msg = (
                f"✓ Link processado com sucesso!\n\n"
                f"Extraí {result['items_saved']} informação(ões) útil(eis).\n"
                f"O atendente já pode usar esse conhecimento nas conversas.\n\n"
                f"Use /conhecimento para ver tudo que ele sabe."
            )
        else:
            msg = (
                f"Não consegui extrair informações desse link.\n\n"
                f"Motivo: {result.get('reason', 'link inacessível')}\n\n"
                f"Tente enviar o texto diretamente com /treinar <texto>."
            )
        try:
            await send_text_message(phone, msg, owner_id)
        except Exception as e:
            logger.warning("[Trainer] Falha ao notificar owner: %s", e)

    async def _ingest_text_and_notify(self, kb: KnowledgeBank, owner_id: str, phone: str, text: str):
        """Ingere texto longo e envia resultado para o owner."""
        from app.services.sender import send_text_message
        result = await kb.ingest_text(owner_id, text, source="owner_whatsapp")
        msg = (
            f"✓ Texto processado!\n\n"
            f"Extraí e organizei {result['items_saved']} informação(ões).\n"
            f"O atendente já aprendeu e vai usar isso nas conversas."
            if result.get("ok") else
            "Não consegui processar esse texto. Tente dividir em partes menores."
        )
        try:
            await send_text_message(phone, msg, owner_id)
        except Exception as e:
            logger.warning("[Trainer] Falha ao notificar owner: %s", e)

    def _help_message(self) -> str:
        return (
            "*Como treinar o atendente:*\n\n"
            "/treinar produto: texto sobre o produto\n"
            "/treinar faq: Pergunta → Resposta\n"
            "/treinar objecao: como lidar com X\n"
            "/treinar estilo: fale sempre de forma Y\n"
            "/treinar https://seusite.com\n\n"
            "Outros comandos:\n"
            "/conhecimento — veja o que ele sabe\n"
            "/esquecer <trecho> — remove um aprendizado"
        )

    async def report_status(self) -> dict:
        try:
            from app.database import get_db
            db = get_db()
            resp = db.table("knowledge_items").select("owner_id", count="exact").execute()
            total = resp.count or 0
        except Exception:
            total = 0
        return {
            "role": self.role,
            "status": "operational",
            "total_knowledge_items": total,
            "summary": f"{total} item(s) de conhecimento no banco.",
        }

    def opine(self, question: str, context: AgentContext) -> AgentOpinion:
        return AgentOpinion(
            agent_role=self.role,
            agrees=True,
            reasoning=(
                f"[{self.display_name}] Qualquer mudança na base de conhecimento "
                f"deve ser testada numa conversa antes de ir para produção. "
                f"Informação errada no banco vale menos que silêncio."
            ),
        )
