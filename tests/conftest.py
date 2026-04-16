"""
Fixtures compartilhados para testes.
Mocka tudo que depende de serviço externo: Supabase, Redis, Evolution API, Celery.
"""

import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Seta envs ANTES de qualquer import da app
# Supabase client valida JWT: precisa de header.payload.signature
_FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.fake_signature_for_tests"
os.environ.update({
    "SUPABASE_URL": "https://fake.supabase.co",
    "SUPABASE_ANON_KEY": _FAKE_JWT,
    "SUPABASE_SERVICE_KEY": _FAKE_JWT,
    "ANTHROPIC_API_KEY": "fake-anthropic-key",
    "EVOLUTION_API_URL": "https://fake-evolution.com",
    "EVOLUTION_API_KEY": "fake-evo-key",
    "EVOLUTION_INSTANCE": "test-instance",
    "REDIS_URL": "redis://localhost:6379/0",
    "APP_SECRET": "test-secret",
    "APP_URL": "https://test.railway.app",
})

# Mock get_db e redis ANTES de importar módulos da app
_mock_db = MagicMock()
_mock_redis_instance = MagicMock()
_mock_redis_instance.get.return_value = None
_mock_redis_instance.setex.return_value = True
_mock_redis_instance.rpush.return_value = 1
_mock_redis_instance.expire.return_value = True
_mock_redis_instance.hgetall.return_value = {}
_mock_redis_instance.delete.return_value = True
_mock_redis_instance.set.return_value = True

# Patch get_db para retornar mock antes de qualquer import
patch("app.database.get_db", return_value=_mock_db).start()
patch("redis.from_url", return_value=_mock_redis_instance).start()


@pytest.fixture
def mock_redis():
    return _mock_redis_instance


@pytest.fixture
def mock_whatsapp():
    """Mock WhatsAppService — send_message retorna sucesso."""
    ws = MagicMock()
    ws.parse_webhook.return_value = None
    ws.send_message = AsyncMock(return_value={"status": "sent"})
    ws.send_typing = AsyncMock()
    return ws


@pytest.fixture
def mock_memory():
    """Mock MemoryService — get_or_create_customer retorna lead básico."""
    from app.models.customer import CustomerProfile
    ms = MagicMock()
    ms.get_or_create_customer = AsyncMock(return_value=CustomerProfile(
        phone="5511999999999",
        owner_id="owner-123",
        name="Lead Teste",
        lead_score=30,
        lead_status="novo",
        total_messages=5,
        follow_up_stage=0,
        nurture_paused=False,
    ))
    ms.update_customer = AsyncMock()
    ms.db = _mock_db
    return ms


@pytest.fixture
def sample_owner():
    """Owner padrão para testes."""
    return {
        "id": "owner-123",
        "evolution_instance": "test-instance",
        "agent_mode": "both",
        "phone": "5511988888888",
        "business_name": "Loja Teste",
        "tone": "acolhedor e direto",
    }


@pytest.fixture
def sample_whatsapp_payload():
    """Payload típico da Evolution API (mensagem de texto)."""
    return {
        "event": "messages.upsert",
        "instance": "test-instance",
        "data": {
            "key": {
                "remoteJid": "5511999999999@s.whatsapp.net",
                "fromMe": False,
                "id": "msg-abc-123"
            },
            "message": {
                "conversation": "Oi, quero saber sobre o curso"
            },
            "messageTimestamp": "1713200000",
            "pushName": "Lead Teste"
        }
    }
