import anthropic
import google.generativeai as genai
from app.config import get_settings
import logging
import base64 as b64lib
import io

logger = logging.getLogger(__name__)
CLAUDE_HAIKU = "claude-haiku-4-5-20251001"
CLAUDE_SONNET = "claude-sonnet-4-6"
GEMINI_FLASH = "gemini-2.0-flash"
MAX_RESPONSE_TOKENS = 300

class AIService:
    def __init__(self):
        settings = get_settings()
        self.claude = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        if settings.google_api_key:
            genai.configure(api_key=settings.google_api_key)
            self.gemini = genai.GenerativeModel(GEMINI_FLASH)
        else:
            self.gemini = None
        # OpenAI para Whisper
        if settings.openai_api_key:
            from openai import OpenAI
            self.openai = OpenAI(api_key=settings.openai_api_key)
        else:
            self.openai = None

    async def respond(self, system_prompt: str, history: list, user_message: str, use_gemini: bool = False) -> str:
        if use_gemini and self.gemini:
            return await self._respond_gemini(system_prompt, history, user_message)
        return await self._respond_claude(system_prompt, history, user_message)

    async def _respond_claude(self, system_prompt: str, history: list, user_message: str) -> str:
        messages = history + [{"role": "user", "content": user_message}]
        response = self.claude.messages.create(model=CLAUDE_SONNET, max_tokens=MAX_RESPONSE_TOKENS, system=system_prompt, messages=messages)
        return response.content[0].text.strip()

    async def _respond_gemini(self, system_prompt: str, history: list, user_message: str) -> str:
        chat_history = [{"role": "user" if m["role"]=="user" else "model", "parts": [m["content"]]} for m in history]
        chat = self.gemini.start_chat(history=chat_history)
        response = chat.send_message(f"{system_prompt}\n\nMensagem: {user_message}")
        return response.text.strip()

    async def classify_intent(self, message: str, context: str = "") -> dict:
        prompt = f"""Analise esta mensagem de WhatsApp e retorne um JSON com:
- intent: compra | suporte | agendamento | informacao | objecao | cancelamento | outros
- lead_score_delta: numero de -10 a +20
- is_simple: true se for mensagem simples (oi, obrigado, ok)
- urgency: alta | media | baixa

Contexto: {context or 'nenhum'}
Mensagem: {message}

Responda APENAS o JSON."""
        response = self.claude.messages.create(model=CLAUDE_HAIKU, max_tokens=100, messages=[{"role": "user", "content": prompt}])
        import json
        try:
            return json.loads(response.content[0].text.strip())
        except Exception:
            return {"intent": "outros", "lead_score_delta": 0, "is_simple": False, "urgency": "media"}

    # ── HELPERS DE MÍDIA ──────────────────────────────────────────────────────

    def _parse_base64(self, b64: str) -> tuple:
        """Remove prefixo data URL se existir. Retorna (base64_limpo, mime_type)."""
        if b64 and b64.startswith("data:") and "," in b64:
            header, data = b64.split(",", 1)
            mime = header.split(":")[1].split(";")[0]
            return data, mime
        return b64, ""

    def _build_openai_history(self, history: list) -> list:
        """Converte histórico para formato OpenAI."""
        return [{"role": m["role"], "content": m["content"]} for m in history]

    async def respond_with_image(self, system_prompt: str, history: list, user_message: str, image_base64: str) -> str:
        """Analisa imagem — GPT-4o Vision (OpenAI) com fallback Claude Sonnet."""
        data, mime = self._parse_base64(image_base64)
        mime = mime or "image/jpeg"
        if user_message and not user_message.startswith("[Imagem"):
            caption_text = user_message
        else:
            caption_text = (
                "Analise esta imagem com TODOS os detalhes: textos visíveis, títulos, "
                "cores, objetos, marcas, números, rostos, cenário. "
                "Depois responda de forma natural conforme seu papel de atendente."
            )

        # ── GPT-4o Vision ─────────────────────────────────────────────────────
        if self.openai:
            try:
                messages = [{"role": "system", "content": system_prompt}]
                messages += self._build_openai_history(history)
                messages.append({"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
                    {"type": "text", "text": caption_text}
                ]})
                resp = self.openai.chat.completions.create(
                    model="gpt-4o", messages=messages, max_tokens=MAX_RESPONSE_TOKENS
                )
                return resp.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"[GPT-4o Vision] erro: {e} — usando Claude como fallback")

        # ── Claude Sonnet fallback ─────────────────────────────────────────────
        try:
            msgs = history + [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
                {"type": "text", "text": caption_text}
            ]}]
            response = self.claude.messages.create(
                model=CLAUDE_SONNET, max_tokens=MAX_RESPONSE_TOKENS,
                system=system_prompt, messages=msgs
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.error(f"[Claude Vision] erro: {e}")
            return "Recebi sua imagem! Pode me descrever o que você precisa sobre ela?"

    async def respond_with_pdf(self, system_prompt: str, history: list, user_message: str, pdf_base64: str) -> str:
        """Lê PDF — GPT-4o (extrai texto) com fallback Claude nativo."""
        data, _ = self._parse_base64(pdf_base64)
        doc_question = user_message if user_message and not user_message.startswith("[PDF") and not user_message.startswith("[Documento") else "Resuma o conteúdo deste documento de forma útil."

        # ── GPT-4o + extração de texto ─────────────────────────────────────────
        if self.openai:
            try:
                import pypdf
                pdf_bytes = b64lib.b64decode(data)
                reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
                text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
                if text:
                    messages = [{"role": "system", "content": system_prompt}]
                    messages += self._build_openai_history(history)
                    messages.append({"role": "user", "content": f"[Documento PDF]:\n{text[:8000]}\n\n{doc_question}"})
                    resp = self.openai.chat.completions.create(
                        model="gpt-4o", messages=messages, max_tokens=MAX_RESPONSE_TOKENS
                    )
                    return resp.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"[GPT-4o PDF] erro: {e} — usando Claude como fallback")

        # ── Claude Sonnet fallback (suporte nativo a PDF) ─────────────────────
        try:
            msgs = history + [{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
                {"type": "text", "text": doc_question}
            ]}]
            response = self.claude.messages.create(
                model=CLAUDE_SONNET, max_tokens=MAX_RESPONSE_TOKENS,
                system=system_prompt, messages=msgs
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.error(f"[Claude PDF] erro: {e}")
            return "Recebi o documento! Pode me dizer o que você quer saber sobre ele?"

    async def transcribe_audio(self, audio_base64: str) -> str:
        """Transcreve áudio — usa Whisper (OpenAI) se disponível, senão Gemini."""
        data, mime = self._parse_base64(audio_base64)
        audio_bytes = b64lib.b64decode(data)

        # ── Whisper (OpenAI) — prioridade ─────────────────────────────────────
        if self.openai:
            try:
                audio_file = io.BytesIO(audio_bytes)
                audio_file.name = "audio.ogg"
                transcript = self.openai.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="pt",
                    response_format="text"
                )
                text = transcript.strip() if isinstance(transcript, str) else transcript.text.strip()
                logger.info(f"[Whisper] transcrição OK: {text[:60]}")
                return text
            except Exception as e:
                logger.error(f"[Whisper] erro: {e} — tentando Gemini como fallback")

        # ── Gemini — fallback ─────────────────────────────────────────────────
        if self.gemini:
            try:
                mime = mime or "audio/ogg"
                response = self.gemini.generate_content(
                    contents=[{
                        "parts": [
                            {"inline_data": {"mime_type": mime, "data": b64lib.b64encode(audio_bytes).decode()}},
                            {"text": "Transcreva este áudio em português. Responda APENAS com a transcrição literal, sem comentários."}
                        ]
                    }]
                )
                return response.text.strip()
            except Exception as e:
                logger.error(f"[Gemini] erro na transcrição: {e}")

        return ""

    # ── FIM HELPERS DE MÍDIA ──────────────────────────────────────────────────

    async def compress_conversation(self, messages: list) -> str:
        text = "\n".join([f"{m['role']}: {m['content']}" for m in messages])
        prompt = f"Resuma esta conversa em maximo 150 palavras. Inclua: pontos discutidos, intencao do cliente, objecoes, onde ficou.\n\n{text}"
        response = self.claude.messages.create(model=CLAUDE_HAIKU, max_tokens=200, messages=[{"role": "user", "content": prompt}])
        return response.content[0].text.strip()

    async def analyze_owner_links(self, scraped_content: str) -> dict:
        prompt = f"""Analise este conteudo e extraia um JSON com:
- tone, vocabulary (lista), emoji_style, avg_response_length, values (lista)
- business_type, main_offer, target_audience
- common_objections (lista), context_summary (max 300 palavras)

Conteudo:
{scraped_content[:8000]}

Responda APENAS o JSON."""
        response = self.claude.messages.create(model="claude-sonnet-4-6", max_tokens=1000, messages=[{"role": "user", "content": prompt}])
        import json
        try:
            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("```")[1].replace("json", "").strip()
            return json.loads(text)
        except Exception as e:
            logger.error(f"Erro ao parsear analise de links: {e}")
            return {}
