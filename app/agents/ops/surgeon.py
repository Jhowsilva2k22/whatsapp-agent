"""
EcoZap — Surgeon Agent (Sprint 1 — implementação real)
========================================================
Papel: Geração e aplicação de patches.
Hierarquia: Especialista → OPS → CTO → CEO

Responsabilidades:
- Recebe diagnóstico do Doctor
- Lê arquivo afetado
- Gera patch via Claude API (fix mínimo e cirúrgico)
- Valida sintaxe do patch gerado
- Cria branch fix/surgeon-{incident_id} no GitHub
- Cria Pull Request com descrição completa
- Solicita CEO_OVERRIDE via Telegram para merge/deploy

CEO_OVERRIDE OBRIGATÓRIO para:
- merge_to_main
- deploy_to_production

Opinion bias: "Cirúrgico. Fix mínimo, sem side effects. Prefere esperar aprovação a errar."

Env vars necessárias:
  ANTHROPIC_API_KEY: para geração do patch
  GITHUB_TOKEN: para criar branch e PR via API REST
  GITHUB_REPO: ex: "Jhowsilva2k22/whatsapp-agent"
"""
import ast
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.agents.base import Agent, AgentContext, AgentOpinion, AuthorityLevel
from app.agents.registry import register
from app.agents.loyalty import format_override_request
from app.agents.message_bus import publish, Events
from app.config import get_settings
from app.services.alerts import notify_owner

logger = logging.getLogger(__name__)
settings = get_settings()

GITHUB_API = "https://api.github.com"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"


@register
class Surgeon(Agent):
    role = "surgeon"
    display_name = "Surgeon"
    authority_level = AuthorityLevel.SPECIALIST
    department = "ops"
    opinion_bias = "cirúrgico — fix mínimo, zero side effects, prefere aguardar aprovação"

    autonomous_actions = [
        "read_codebase",
        "generate_patch",
        "run_local_tests",
        "create_pull_request",
    ]
    requires_ceo_override = [
        "merge_to_main",
        "deploy_to_production",
        "alter_database_schema",
        "delete_data",
    ]

    async def act(self, context: AgentContext) -> dict:
        """
        Gera patch com base no diagnóstico do Doctor.
        NÃO aplica sem CEO_OVERRIDE para merge/deploy.
        """
        diagnosis = context.payload.get("diagnosis", context.payload)
        incident_id = diagnosis.get("incident_id") or str(uuid.uuid4())[:8]

        logger.info("[Surgeon] Iniciando cirurgia — incidente %s", incident_id)

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "incident_id": incident_id,
            "diagnosis_summary": diagnosis.get("root_cause", "?"),
            "patch_generated": False,
            "patch_valid": False,
            "branch": None,
            "pr_url": None,
            "awaiting_ceo_approval": False,
            "deployed": False,
            "error": None,
        }

        # Verifica se há arquivo afetado
        affected_files = diagnosis.get("affected_files", [])
        root_cause = diagnosis.get("root_cause", "")
        fix_hint = diagnosis.get("fix_hint", "")

        if not root_cause:
            result["error"] = "Diagnóstico sem causa raiz — Surgeon aguarda Doctor"
            logger.warning("[Surgeon] Sem causa raiz definida. Abortando.")
            return result

        # 1. Lê arquivo afetado (se identificado pelo Doctor)
        file_content = None
        target_file = None
        if affected_files:
            target_file = affected_files[0].get("file", "")
            file_content = self._read_file_safe(target_file)

        # 2. Gera patch via Claude API
        patch_code = await self._generate_patch(
            root_cause=root_cause,
            fix_hint=fix_hint,
            target_file=target_file,
            file_content=file_content,
            incident_id=incident_id,
        )

        if not patch_code:
            result["error"] = "Claude API não retornou patch utilizável"
            logger.warning("[Surgeon] Nenhum patch gerado.")
            await self._notify_blocked(incident_id, root_cause, "Claude não gerou patch")
            return result

        result["patch_generated"] = True

        # 3. Valida sintaxe do patch (se for Python)
        is_valid, validation_error = self._validate_python_syntax(patch_code)
        result["patch_valid"] = is_valid
        if not is_valid:
            result["error"] = f"Patch com erro de sintaxe: {validation_error}"
            logger.warning("[Surgeon] Patch inválido: %s", validation_error)
            await self._notify_blocked(incident_id, root_cause, f"Sintaxe inválida: {validation_error}")
            return result

        # 4. Cria branch e PR no GitHub
        github_token = os.getenv("GITHUB_TOKEN", "")
        github_repo = os.getenv("GITHUB_REPO", "Jhowsilva2k22/whatsapp-agent")

        if github_token and target_file:
            branch_name = f"fix/surgeon-{incident_id}"
            result["branch"] = branch_name

            pr_url = await self._create_github_pr(
                token=github_token,
                repo=github_repo,
                branch_name=branch_name,
                target_file=target_file,
                patch_content=patch_code,
                incident_id=incident_id,
                root_cause=root_cause,
                fix_hint=fix_hint,
            )

            if pr_url:
                result["pr_url"] = pr_url
                result["awaiting_ceo_approval"] = True

                # 5. Solicita CEO_OVERRIDE via Telegram com linguagem natural
                arquivo_nome = target_file.split("/")[-1] if target_file else "arquivo do sistema"
                override_msg = format_override_request(
                    action="merge_to_main",
                    reason=root_cause,
                    requested_by=self.role,
                    incident_id=incident_id,
                    extra={
                        "arquivo_corrigido": arquivo_nome,
                        "pr_url": pr_url,
                    }
                )
                try:
                    notify_owner(override_msg, level="warn")
                except Exception as e:
                    logger.warning("[Surgeon] Falha ao enviar CEO_OVERRIDE: %s", e)

                logger.info("[Surgeon] PR criado: %s — aguardando CEO.", pr_url)
            else:
                result["error"] = "Falha ao criar PR no GitHub"
        else:
            # Sem GitHub token — reporta patch em texto puro com linguagem natural
            logger.info("[Surgeon] Sem GITHUB_TOKEN. Patch gerado mas não enviado ao repositório.")
            try:
                arquivo_nome = target_file.split("/")[-1] if target_file else "não identificado"
                notify_owner(
                    f"🔧 *Surgeon — Correção Preparada #{incident_id}*\n\n"
                    f"*O problema:*\n{root_cause}\n\n"
                    f"*Arquivo que precisa ser alterado:* `{arquivo_nome}`\n\n"
                    f"*A correção foi escrita,* mas não foi enviada automaticamente ao repositório "
                    f"porque o GITHUB\\_TOKEN ainda não está configurado.\n\n"
                    f"*O que fazer:* Configure a variável `GITHUB\\_TOKEN` no Railway e "
                    f"na próxima ocorrência o Surgeon criará o Pull Request automaticamente, "
                    f"precisando só da sua aprovação.\n\n"
                    f"_Prévia do código corrigido:_\n```python\n{patch_code[:600]}\n```",
                    level="warn",
                )
            except Exception:
                pass

        # 6. Publica evento no message bus
        try:
            import redis as redis_lib
            r = redis_lib.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
            await publish(r, self.role, Events.PATCH_READY, {
                "incident_id": incident_id,
                "result": result,
                "tenant_id": context.tenant_id,
            })
        except Exception as e:
            logger.warning("[Surgeon] Falha ao publicar evento patch_ready: %s", e)

        return result

    # ──────────────────── geração de patch ────────────────────

    async def _generate_patch(
        self,
        root_cause: str,
        fix_hint: str,
        target_file: Optional[str],
        file_content: Optional[str],
        incident_id: str,
    ) -> Optional[str]:
        """Chama a Anthropic API para gerar patch cirúrgico."""
        api_key = settings.anthropic_api_key
        if not api_key:
            logger.warning("[Surgeon] ANTHROPIC_API_KEY não configurado.")
            return None

        # Prepara contexto do arquivo
        file_context = ""
        if target_file and file_content:
            # Limita tamanho para não estourar contexto
            lines = file_content.split("\n")
            file_context = (
                f"\nArquivo afetado: `{target_file}`\n"
                f"Conteúdo atual (primeiras {min(len(lines), 150)} linhas):\n"
                f"```python\n{chr(10).join(lines[:150])}\n```\n"
            )

        prompt = (
            f"Você é um engenheiro sênior Python fazendo uma correção cirúrgica em produção.\n\n"
            f"INCIDENTE #{incident_id}\n"
            f"Causa raiz: {root_cause}\n"
            f"Dica de fix: {fix_hint}\n"
            f"{file_context}\n"
            f"REGRAS ABSOLUTAS:\n"
            f"1. Mude o MÍNIMO de código possível — apenas o necessário para corrigir o bug\n"
            f"2. Não adicione features novas\n"
            f"3. Não reformate código não-relacionado\n"
            f"4. Se não tiver certeza, gere o fix mais conservador\n"
            f"5. Retorne APENAS o código Python corrigido, sem explicações\n"
            f"6. Se o arquivo completo foi fornecido, retorne o arquivo inteiro corrigido\n"
            f"7. Se não conseguir gerar fix seguro, retorne exatamente: SURGEON_CANNOT_FIX\n\n"
            f"Gere o patch agora:"
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    ANTHROPIC_API,
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",  # Haiku: rápido, mecânico
                        "max_tokens": 4096,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                data = resp.json()
                content = data.get("content", [{}])[0].get("text", "").strip()

            if not content or content == "SURGEON_CANNOT_FIX":
                logger.info("[Surgeon] Claude indicou que não consegue gerar fix seguro.")
                return None

            # Remove blocos de markdown se presentes
            content = re.sub(r"^```python\n?", "", content)
            content = re.sub(r"\n?```$", "", content)

            return content.strip()

        except Exception as e:
            logger.error("[Surgeon] Erro ao chamar Claude API: %s", e)
            return None

    # ──────────────────── validação ───────────────────────────

    def _validate_python_syntax(self, code: str) -> tuple:
        """Valida sintaxe Python. Retorna (is_valid, error_message)."""
        try:
            ast.parse(code)
            return True, None
        except SyntaxError as e:
            return False, str(e)

    def _read_file_safe(self, file_path: str) -> Optional[str]:
        """Lê arquivo do projeto com segurança."""
        # Sanitiza path — só arquivos do projeto
        clean_path = file_path.lstrip("/")
        if not (clean_path.startswith("app/") or "whatsapp" in clean_path):
            return None
        try:
            # Tenta path relativo e absoluto
            for base in [".", "/sessions/vigilant-dazzling-ritchie/whatsapp-agent"]:
                full = os.path.join(base, clean_path)
                if os.path.isfile(full):
                    with open(full, "r", encoding="utf-8") as f:
                        return f.read()
        except Exception as e:
            logger.warning("[Surgeon] Falha ao ler arquivo '%s': %s", file_path, e)
        return None

    # ──────────────────── GitHub API ──────────────────────────

    async def _create_github_pr(
        self,
        token: str,
        repo: str,
        branch_name: str,
        target_file: str,
        patch_content: str,
        incident_id: str,
        root_cause: str,
        fix_hint: str,
    ) -> Optional[str]:
        """Cria branch + commit + PR no GitHub via REST API."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                # 1. Busca SHA do commit mais recente da main
                ref_resp = await client.get(
                    f"{GITHUB_API}/repos/{repo}/git/ref/heads/main",
                    headers=headers,
                )
                if ref_resp.status_code != 200:
                    logger.error("[Surgeon] Falha ao obter ref main: %d", ref_resp.status_code)
                    return None
                main_sha = ref_resp.json()["object"]["sha"]

                # 2. Cria nova branch
                branch_resp = await client.post(
                    f"{GITHUB_API}/repos/{repo}/git/refs",
                    headers=headers,
                    json={"ref": f"refs/heads/{branch_name}", "sha": main_sha},
                )
                if branch_resp.status_code not in (200, 201):
                    logger.error("[Surgeon] Falha ao criar branch: %d", branch_resp.status_code)
                    return None

                # 3. Busca SHA do arquivo atual para fazer update
                clean_path = target_file.lstrip("/")
                if not clean_path.startswith("app/"):
                    # Extrai só a parte app/ do path
                    match = re.search(r"(app/.+)", clean_path)
                    clean_path = match.group(1) if match else clean_path

                file_resp = await client.get(
                    f"{GITHUB_API}/repos/{repo}/contents/{clean_path}",
                    headers=headers,
                    params={"ref": branch_name},
                )

                import base64
                content_b64 = base64.b64encode(patch_content.encode("utf-8")).decode("ascii")

                commit_body = {
                    "message": (
                        f"fix(surgeon): patch automático — incidente #{incident_id}\n\n"
                        f"Causa raiz: {root_cause}\n\n"
                        f"Co-Authored-By: Surgeon Agent <noreply@ecozap.ai>"
                    ),
                    "content": content_b64,
                    "branch": branch_name,
                }

                if file_resp.status_code == 200:
                    commit_body["sha"] = file_resp.json()["sha"]

                # 4. Commit o arquivo corrigido
                commit_resp = await client.put(
                    f"{GITHUB_API}/repos/{repo}/contents/{clean_path}",
                    headers=headers,
                    json=commit_body,
                )
                if commit_resp.status_code not in (200, 201):
                    logger.error("[Surgeon] Falha ao commitar patch: %d", commit_resp.status_code)
                    return None

                # 5. Cria Pull Request
                pr_body = (
                    f"## 🤖 Patch Automático — Surgeon Agent\n\n"
                    f"**Incidente:** #{incident_id}\n"
                    f"**Causa raiz:** {root_cause}\n\n"
                    f"### Fix aplicado\n{fix_hint}\n\n"
                    f"### ⚠️ Requer aprovação do CEO antes do merge\n"
                    f"Responda no Telegram: `APROVADO:{incident_id}` ou `REJEITADO:{incident_id}`\n\n"
                    f"---\n_Gerado automaticamente pelo Surgeon Agent · EcoZap_"
                )

                pr_resp = await client.post(
                    f"{GITHUB_API}/repos/{repo}/pulls",
                    headers=headers,
                    json={
                        "title": f"[Surgeon] fix: {root_cause[:80]} (#{incident_id})",
                        "body": pr_body,
                        "head": branch_name,
                        "base": "main",
                        "draft": False,
                    },
                )
                if pr_resp.status_code not in (200, 201):
                    logger.error("[Surgeon] Falha ao criar PR: %d — %s",
                                 pr_resp.status_code, pr_resp.text[:200])
                    return None

                pr_url = pr_resp.json().get("html_url", "")
                logger.info("[Surgeon] PR criado: %s", pr_url)
                return pr_url

        except Exception as e:
            logger.error("[Surgeon] Erro ao criar PR no GitHub: %s", e)
            return None

    async def _notify_blocked(self, incident_id: str, root_cause: str, reason: str):
        """Notifica que a correção automática não foi possível — linguagem natural."""
        try:
            notify_owner(
                f"🛑 *Surgeon — Correção Automática Não Foi Possível #{incident_id}*\n\n"
                f"*Problema identificado:*\n{root_cause}\n\n"
                f"*Por que não corrigiu automaticamente:*\n{reason}\n\n"
                f"*O que você precisa fazer:*\n"
                f"Esse problema precisa de atenção manual. "
                f"Você pode entrar no Railway, verificar os logs do serviço afetado, "
                f"ou acionar um desenvolvedor com o número do incidente `#{incident_id}` — "
                f"todas as informações de diagnóstico foram registradas.",
                level="error",
            )
        except Exception:
            pass

    async def report_status(self) -> dict:
        return {
            "role": self.role,
            "status": "operational",
            "pending_prs": 0,
            "github_configured": bool(os.getenv("GITHUB_TOKEN")),
            "summary": (
                "Pronto para gerar patches. GitHub configurado."
                if os.getenv("GITHUB_TOKEN")
                else "⚠️ GITHUB_TOKEN não configurado — PR automático desabilitado."
            ),
        }

    def opine(self, question: str, context: AgentContext) -> AgentOpinion:
        deploy_keywords = ["deploy", "merge", "production", "release", "apply", "patch"]
        if any(kw in question.lower() for kw in deploy_keywords):
            return AgentOpinion(
                agent_role=self.role,
                agrees=True,
                reasoning=(
                    f"[{self.display_name}] Confirmo que qualquer merge/deploy "
                    f"DEVE ter CEO_OVERRIDE explícito. "
                    f"Minha regra: fix mínimo, PR documentado, CEO aprova."
                ),
            )
        return AgentOpinion(
            agent_role=self.role,
            agrees=True,
            reasoning=f"[{self.display_name}] Sem impacto de patch identificado.",
        )
