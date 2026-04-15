from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from app.routers import webhook, onboarding, panel, instagram_webhook
from app.config import get_settings
import logging
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(
    title="WhatsApp AI Agent",
    description="Agente de IA para qualificacao de leads e atendimento no WhatsApp",
    version="1.0.0",
    docs_url="/docs" if settings.debug else None,
    redoc_url=None
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(webhook.router, tags=["WhatsApp"])
app.include_router(instagram_webhook.router, tags=["Instagram"])
app.include_router(onboarding.router, prefix="/api", tags=["Onboarding"])
app.include_router(panel.router, tags=["Panel"])

@app.get("/")
async def root():
    return {"service": "WhatsApp AI Agent", "status": "online", "version": "1.0.0"}


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
    except Exception as e:
        logger.error(f"Erro na verificação de migração: {e}")
    # Renova subscription do webhook Instagram automaticamente
    if settings.meta_page_id and settings.meta_page_token:
        await _subscribe_instagram_webhook()


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
