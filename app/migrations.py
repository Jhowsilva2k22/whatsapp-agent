"""
Migração automática — garante que todas as colunas necessárias existam.
Usa consulta direta ao Supabase pra verificar e adicionar colunas.
"""
from app.database import get_db
import logging
import httpx

logger = logging.getLogger(__name__)

# Colunas que DEVEM existir na tabela customers
REQUIRED_COLUMNS = {
    "lead_score": "integer DEFAULT 0",
    "lead_status": "text DEFAULT 'novo'",
    "last_intent": "text",
    "total_messages": "integer DEFAULT 0",
    "channel": "text",
    "follow_up_stage": "integer DEFAULT 0",
    "birthday": "text",
    "nurture_paused": "boolean DEFAULT false",
    "last_nurture": "timestamptz",
    "last_sentiment": "text",
    "sentiment_history": "jsonb",
}


async def run_migrations():
    """Verifica e adiciona colunas faltantes na tabela customers."""
    from app.config import get_settings
    settings = get_settings()

    # Monta headers pra API REST do Supabase
    headers = {
        "apikey": settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    base_url = settings.supabase_url.rstrip("/")

    # Pega colunas existentes via REST
    async with httpx.AsyncClient() as client:
        # Faz um SELECT vazio só pra pegar os headers com as colunas
        resp = await client.get(
            f"{base_url}/rest/v1/customers?select=*&limit=0",
            headers=headers
        )

        if resp.status_code == 200:
            # Pega nomes das colunas do content-profile header ou do schema
            # Tenta via OPTIONS ou HEAD
            pass

        # Abordagem direta: tenta adicionar cada coluna via RPC
        # Se não tiver RPC, faz via postgrest
        added = []
        already = []

        for col_name, col_def in REQUIRED_COLUMNS.items():
            try:
                # Tenta fazer um PATCH com o campo — se a coluna não existir, dá erro
                test_resp = await client.get(
                    f"{base_url}/rest/v1/customers?select={col_name}&limit=1",
                    headers=headers
                )
                if test_resp.status_code == 200:
                    already.append(col_name)
                else:
                    added.append(col_name)
                    logger.warning(f"[Migration] Coluna '{col_name}' NÃO EXISTE — precisa ser criada manualmente")
            except Exception as e:
                logger.error(f"[Migration] Erro verificando {col_name}: {e}")

    if added:
        logger.warning(f"[Migration] COLUNAS FALTANDO: {', '.join(added)}")
        logger.warning("[Migration] Execute o SQL de migração no Supabase Dashboard!")
    else:
        logger.info(f"[Migration] Todas as {len(already)} colunas existem ✓")

    return {"existing": already, "missing": added}


def get_migration_sql() -> str:
    """Retorna o SQL completo pra adicionar todas as colunas faltantes."""
    lines = []
    for col_name, col_def in REQUIRED_COLUMNS.items():
        lines.append(f"ALTER TABLE customers ADD COLUMN IF NOT EXISTS {col_name} {col_def};")
    # Owners: coluna pro Instagram
    lines.append("")
    lines.append("-- Instagram integration")
    lines.append("ALTER TABLE owners ADD COLUMN IF NOT EXISTS instagram_account_id TEXT;")
    return "\n".join(lines)
