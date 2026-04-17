"""
Canal de alerta/operação via Telegram.
Uso exclusivo do dono. NÃO usar pra atendimento de cliente.
Falha de alerta nunca pode quebrar a aplicação.
"""
import os
import logging
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_OPS_CHAT_ID", "").strip()
TELEGRAM_API = "https://api.telegram.org"

# Fuso horário de Brasília (UTC-3, sem DST no horário de inverno)
BRT = timezone(timedelta(hours=-3))

ICONS = {
    "info": "🟢",
    "warn": "🟡",
    "error": "🔴",
    "critical": "🚨",
}


def _enabled() -> bool:
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def notify_owner(text: str, level: str = "info") -> bool:
    """
    Manda mensagem pro Telegram do dono.
    level: info | warn | error | critical
    Retorna True se enviou, False caso contrário. Nunca levanta exceção.
    """
    if not _enabled():
        logger.warning("[Ops] Telegram não configurado, alerta perdido: %s", text[:120])
        return False

    now_brt = datetime.now(BRT).strftime("%d/%m %H:%M BRT")
    prefix = ICONS.get(level, "🟢")
    body = f"{prefix} *{level.upper()}*\n🕐 {now_brt}\n\n{text}"[:3800]  # Telegram limita em 4096

    url = f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": body,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.post(url, json=payload)
            if r.status_code == 200:
                return True
            logger.error("[Ops] Telegram falhou %s: %s", r.status_code, r.text[:200])
            return False
    except Exception as e:
        logger.error("[Ops] Telegram exception: %s", e)
        return False


def notify_boot(app_name: str = "whatsapp-agent") -> None:
    """Chamar no startup — confirma que a app subiu."""
    notify_owner(f"{app_name} subiu e está online.", level="info")


def notify_error(context: str, err: Exception) -> None:
    """Chamar em handler de exceção."""
    notify_owner(
        f"Erro em `{context}`\n\n`{type(err).__name__}: {str(err)[:400]}`",
        level="error",
    )


def notify_warn(text: str) -> None:
    notify_owner(text, level="warn")


def notify_critical(text: str) -> None:
    notify_owner(text, level="critical")
