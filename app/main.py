"""
Entry point FastAPI.
Fase 1 — Observabilidade: Sentry + alerta Telegram + health checks reais.
Fase 2 — Multi-tenant: tenant_api router para painel do cliente.
Preserva 100% do comportamento original (WhatsApp, Instagram, painel, onboarding).
"""
import os
import logging
import httpx

# ---------------------------------------------------------------------------
# Sentry precisa ser inicializado ANTES de importar a app FastAPI.
# ---------------------------------------------------------------------------
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=os.getenv("APP_ENV", "production"),
            traces_sample_rate=0.1,
            profiles_sample_rate=0.0,
            send_default_pii=False,
            integrations=[StarletteIntegration(), FastApiIntegration()],
        )
    except ImportError:
        logging.warning("[Sentry] sentry-sdk não instalado, pulando init")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from app.routers import webhook, onboarding, panel, instagram_webhook, health, tenant_api
from app.config import get_settings
from app.services.alerts import notify_owner, notify_boot, notify_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(
    title="WhatsApp AI Agent",
    description="Agente de IA para qualificacao de leads e atendimento no WhatsApp",
    version="2.0.0",
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(webhook.router, tags=["WhatsApp"])
app.include_router(instagram_webhook.router, tags=["Instagram"])
app.include_router(onboarding.router, prefix="/api", tags=["Onboarding"])
app.include_router(panel.router, tags=["Panel"])
app.include_router(health.router, tags=["Health"])
app.include_router(tenant_api.router, prefix="/api", tags=["Tenant Panel"])


@app.get("/")
async def root():
    return {"service": "WhatsApp AI Agent", "status": "online", "version": "2.0.0"}


async def _subscribe_instagram_webhook():
    """Garante que a Page está inscrita no webhook de mensagens do Instagram."""
    url = f"https://graph.facebook.com/v21.0/{settings.meta_page_id}/subscribed_apps"
    params = {
        "subscribed_fields": "messages,messaging_postbacks,messaging_optins,message_deliveries,message_reads",
        "access_token": settings.meta_page_token,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, params=params)
            data = resp.json()
            if data.get("success"):
                logger.info(f"[IG Startup] subscribed_apps renovado com sucesso para page {settings.meta_page_id}")
            else:
                logger.warning(f"[IG Startup] subscribed_apps retornou: {data}")
    except Exception as e:
        logger.error(f"[IG Startup] Falha ao renovar subscribed_apps: {e}")


@app.on_event("startup")
async def startup():
    logger.info("WhatsApp AI Agent iniciado")
    logger.info(f"Instancia Evolution: {settings.evolution_instance}")
    if settings.instagram_account_id:
        logger.info(f"Instagram Account ID: {settings.instagram_account_id}")

    # Verifica colunas do banco
    try:
        from app.migrations import run_migrations
        result = await run_migrations()
        if result.get("missing"):
            logger.warning(f"⚠️ COLUNAS FALTANDO NO BANCO: {result['missing']}")
            try:
                notify_owner(
                    f"Colunas faltando no banco: {result['missing']}",
                    level="warn",
                )
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Erro na verificação de migração: {e}")

    # Renova subscription do webhook Instagram automaticamente
    if settings.meta_page_id and settings.meta_page_token:
        await _subscribe_instagram_webhook()

    # Alerta Telegram de boot
    try:
        notify_boot("whatsapp-agent")
    except Exception as e:
        logger.error(f"[Ops] Falha no notify_boot: {e}")


# ---------------------------------------------------------------------------
# Handler global de exceção — Sentry captura automático, Telegram pra ver rápido
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def _ops_global_exc(request: Request, exc: Exception):
    try:
        notify_error(f"{request.method} {request.url.path}", exc)
    except Exception:
        pass
    logger.exception(f"Unhandled exception em {request.method} {request.url.path}")
    return JSONResponse(status_code=500, content={"error": "internal_error"})


# ---------------------------------------------------------------------------
# Rota de DEBUG — só pra testar o alerta end-to-end.
# APAGAR DEPOIS DE CONFIRMAR que o alerta cai no Telegram e no Sentry.
# Protegida por token pra não ficar exposta.
# ---------------------------------------------------------------------------
@app.get("/debug/raise")
async def _debug_raise(token: str = ""):
    if token != settings.app_secret:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Token inválido")
    raise RuntimeError("teste ops — se caiu no Telegram e no Sentry, alerta OK")


# ---------------------------------------------------------------------------
# Admin — backup manual e restore (protegido por token)
# ---------------------------------------------------------------------------
@app.get("/admin/backup")
async def _admin_backup(token: str = ""):
    if token != settings.app_secret:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Token inválido")
    from app.services.backup import run_backup
    return run_backup()


@app.get("/admin/backups")
async def _admin_list_backups(token: str = ""):
    if token != settings.app_secret:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Token inválido")
    from app.services.backup import list_backups
    return {"backups": list_backups()}


@app.get("/admin/restore")
async def _admin_restore(token: str = "", folder: str = "", dry_run: bool = True):
    if token != settings.app_secret:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Token inválido")
    if not folder:
        raise HTTPException(status_code=400, detail="Param 'folder' obrigatório")
    from app.services.backup import run_restore
    return run_restore(folder, dry_run=dry_run)


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    return """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Politica de Privacidade - Joanderson Ecosistema</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;color:#333;line-height:1.6}h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:1.5rem}</style></head>
<body>
<h1>Politica de Privacidade</h1>
<p><strong>Ultima atualizacao:</strong> 15 de abril de 2026</p>
<p>O aplicativo <strong>Joanderson Ecosistema</strong> respeita a sua privacidade. Esta politica descreve como coletamos, usamos e protegemos suas informacoes.</p>

<h2>1. Dados coletados</h2>
<p>Coletamos apenas os dados necessarios para o funcionamento do servico de atendimento automatizado via Instagram e WhatsApp: nome de usuario, identificador de conversa e conteudo das mensagens trocadas com nosso assistente.</p>

<h2>2. Uso dos dados</h2>
<p>Os dados sao utilizados exclusivamente para: responder suas mensagens de forma automatizada, melhorar a qualidade do atendimento e entrar em contato quando solicitado.</p>

<h2>3. Compartilhamento</h2>
<p>Nao vendemos, alugamos ou compartilhamos seus dados pessoais com terceiros, exceto quando exigido por lei.</p>

<h2>4. Armazenamento e seguranca</h2>
<p>Seus dados sao armazenados em servidores seguros com criptografia. Mantemos os dados apenas pelo tempo necessario para a prestacao do servico.</p>

<h2>5. Seus direitos</h2>
<p>Voce pode solicitar a exclusao dos seus dados a qualquer momento entrando em contato pelo e-mail: <a href="mailto:joanderson5@gmail.com">joanderson5@gmail.com</a>.</p>

<h2>6. Contato</h2>
<p>Para duvidas sobre esta politica, entre em contato: <a href="mailto:joanderson5@gmail.com">joanderson5@gmail.com</a></p>
</body></html>"""


@app.get("/terms", response_class=HTMLResponse)
async def terms_of_service():
    return """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Termos de Servico - Joanderson Ecosistema</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;color:#333;line-height:1.6}h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:1.5rem}</style></head>
<body>
<h1>Termos de Servico</h1>
<p><strong>Ultima atualizacao:</strong> 15 de abril de 2026</p>
<p>Ao utilizar os servicos do <strong>Joanderson Ecosistema</strong>, voce concorda com os termos abaixo.</p>

<h2>1. Descricao do servico</h2>
<p>Oferecemos um servico de atendimento automatizado via Instagram e WhatsApp, utilizando inteligencia artificial para responder mensagens, qualificar leads e auxiliar no relacionamento com clientes.</p>

<h2>2. Uso aceitavel</h2>
<p>Voce concorda em utilizar o servico de forma etica e legal. E proibido enviar conteudo ofensivo, spam ou qualquer material que viole leis vigentes.</p>

<h2>3. Disponibilidade</h2>
<p>Nos esforçamos para manter o servico disponivel 24 horas, mas nao garantimos disponibilidade ininterrupta. Manutencoes e atualizacoes podem causar interrupcoes temporarias.</p>

<h2>4. Propriedade intelectual</h2>
<p>Todo o conteudo, codigo e tecnologia do Joanderson Ecosistema sao de propriedade exclusiva do desenvolvedor. E proibida a reproducao sem autorizacao.</p>

<h2>5. Limitacao de responsabilidade</h2>
<p>O servico e fornecido "como esta". Nao nos responsabilizamos por decisoes tomadas com base nas respostas automatizadas ou por eventuais indisponibilidades.</p>

<h2>6. Privacidade</h2>
<p>O tratamento dos seus dados esta descrito na nossa <a href="/privacy">Politica de Privacidade</a>.</p>

<h2>7. Alteracoes</h2>
<p>Estes termos podem ser atualizados a qualquer momento. Recomendamos consultar esta pagina periodicamente.</p>

<h2>8. Contato</h2>
<p>Para duvidas sobre estes termos: <a href="mailto:joanderson5@gmail.com">joanderson5@gmail.com</a></p>
</body></html>"""


@app.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion():
    return """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Exclusao de Dados - Joanderson Ecosistema</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;color:#333;line-height:1.6}h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:1.5rem}</style></head>
<body>
<h1>Instrucoes para Exclusao de Dados</h1>
<p><strong>Ultima atualizacao:</strong> 15 de abril de 2026</p>
<p>O <strong>Joanderson Ecosistema</strong> respeita o seu direito de solicitar a exclusao dos seus dados pessoais armazenados em nossos sistemas.</p>

<h2>Como solicitar a exclusao</h2>
<p>Para solicitar a exclusao dos seus dados, envie um e-mail para <a href="mailto:joanderson5@gmail.com">joanderson5@gmail.com</a> com o assunto <strong>"Exclusao de Dados"</strong> incluindo:</p>
<p>- Seu nome de usuario no Instagram ou numero de WhatsApp<br>
- Uma breve descricao do que deseja excluir (historico de mensagens, dados de cadastro, etc.)</p>

<h2>Prazo</h2>
<p>Sua solicitacao sera processada em ate 15 dias uteis. Voce recebera uma confirmacao por e-mail quando a exclusao for concluida.</p>

<h2>O que sera excluido</h2>
<p>Todos os dados associados ao seu perfil: historico de conversas, dados de qualificacao e informacoes de contato armazenadas em nosso sistema.</p>

<h2>Contato</h2>
<p>Duvidas: <a href="mailto:joanderson5@gmail.com">joanderson5@gmail.com</a></p>
</body></html>"""


@app.get("/api/migrate")
async def migrate(token: str = ""):
    """Verifica colunas e retorna SQL de migração se necessário."""
    if token != settings.app_secret:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Token inválido")
    from app.migrations import run_migrations, get_migration_sql
    result = await run_migrations()
    if result.get("missing"):
        result["migration_sql"] = get_migration_sql()
    return result
