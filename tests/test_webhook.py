"""
Testes mínimos — webhook + dispatcher.
Cobre: parse_webhook, roteamento de mensagens, deduplicação, comandos do dono.
Nenhuma dependência externa (Redis, Supabase, Evolution mockados).
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from app.models.message import IncomingMessage


# ═══════════════════════════════ parse_webhook ═══════════════════════════════

class TestParseWebhook:
    """Testa WhatsAppService.parse_webhook isolado."""

    def _make_service(self):
        with patch("app.services.whatsapp.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                evolution_api_url="https://fake.com",
                evolution_api_key="key",
                evolution_instance="test-instance",
            )
            from app.services.whatsapp import WhatsAppService
            return WhatsAppService()

    def test_texto_simples(self, sample_whatsapp_payload):
        ws = self._make_service()
        msg = ws.parse_webhook(sample_whatsapp_payload)
        assert msg is not None
        assert isinstance(msg, IncomingMessage)
        assert msg.phone == "5511999999999"
        assert msg.instance == "test-instance"
        assert msg.message == "Oi, quero saber sobre o curso"
        assert msg.message_id == "msg-abc-123"
        assert msg.media_type == "text"

    def test_evento_ignorado(self):
        ws = self._make_service()
        assert ws.parse_webhook({"event": "connection.update", "data": {}}) is None

    def test_from_me_ignorado(self, sample_whatsapp_payload):
        ws = self._make_service()
        sample_whatsapp_payload["data"]["key"]["fromMe"] = True
        assert ws.parse_webhook(sample_whatsapp_payload) is None

    def test_payload_vazio(self):
        ws = self._make_service()
        assert ws.parse_webhook({}) is None

    def test_imagem_com_caption(self):
        ws = self._make_service()
        payload = {
            "event": "messages.upsert",
            "instance": "test-instance",
            "data": {
                "key": {"remoteJid": "5511999999999@s.whatsapp.net", "fromMe": False, "id": "msg-img-001"},
                "message": {"imageMessage": {"caption": "foto do produto"}},
            }
        }
        msg = ws.parse_webhook(payload)
        assert msg is not None
        assert msg.media_type == "image"
        assert "foto do produto" in msg.message

    def test_audio(self):
        ws = self._make_service()
        payload = {
            "event": "messages.upsert",
            "instance": "test-instance",
            "data": {
                "key": {"remoteJid": "5511999999999@s.whatsapp.net", "fromMe": False, "id": "msg-audio-001"},
                "message": {"audioMessage": {"seconds": 10}},
            }
        }
        msg = ws.parse_webhook(payload)
        assert msg is not None
        assert msg.media_type == "audio"


# ═══════════════════════════════ WEBHOOK ENDPOINT ═══════════════════════════

class TestWebhookEndpoint:
    """Testa POST /webhook/whatsapp com mocks."""

    @pytest.fixture(autouse=True)
    def setup_app(self, mock_redis, mock_whatsapp, mock_memory, sample_owner):
        import app.routers.webhook as wh_mod

        # Salva originais
        orig_redis = wh_mod._redis
        orig_wa = wh_mod.whatsapp
        orig_mem = wh_mod.memory

        # Substitui
        wh_mod._redis = mock_redis
        wh_mod.whatsapp = mock_whatsapp
        wh_mod.memory = mock_memory

        with patch.object(wh_mod, "_get_owner_by_instance", new_callable=AsyncMock) as mock_get_owner, \
             patch.object(wh_mod, "process_buffered") as mock_buffered, \
             patch.object(wh_mod, "process_message") as mock_process, \
             patch.object(wh_mod, "follow_up_active") as mock_followup, \
             patch.object(wh_mod, "celery_app") as mock_celery:

            mock_get_owner.return_value = sample_owner
            mock_buffered.apply_async.return_value = MagicMock(id="task-123")
            mock_process.apply_async.return_value = MagicMock(id="task-456")
            mock_followup.apply_async.return_value = MagicMock(id="task-789")

            self.mock_redis = mock_redis
            self.mock_whatsapp = mock_whatsapp
            self.mock_memory = mock_memory
            self.mock_get_owner = mock_get_owner
            self.mock_buffered = mock_buffered

            from app.main import app
            self.client = TestClient(app)
            yield

        # Restaura
        wh_mod._redis = orig_redis
        wh_mod.whatsapp = orig_wa
        wh_mod.memory = orig_mem

    def test_mensagem_lead_vai_pro_buffer(self, sample_whatsapp_payload):
        """Mensagem de lead → buffer → process_buffered agendado."""
        self.mock_whatsapp.parse_webhook.return_value = IncomingMessage(
            instance="test-instance",
            phone="5511999999999",
            message="Oi, quero saber sobre o curso",
            message_id="msg-abc-123",
            media_type="text",
        )
        resp = self.client.post("/webhook/whatsapp", json=sample_whatsapp_payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "buffered"
        self.mock_redis.rpush.assert_called()
        self.mock_buffered.apply_async.assert_called_once()

    def test_payload_invalido_retorna_400(self):
        resp = self.client.post(
            "/webhook/whatsapp",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    def test_evento_ignorado_retorna_ignored(self):
        self.mock_whatsapp.parse_webhook.return_value = None
        resp = self.client.post("/webhook/whatsapp", json={"event": "connection.update"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_owner_nao_encontrado(self, sample_whatsapp_payload):
        self.mock_whatsapp.parse_webhook.return_value = IncomingMessage(
            instance="unknown-instance", phone="5511999999999",
            message="Oi", message_id="msg-xyz", media_type="text",
        )
        self.mock_get_owner.return_value = None
        resp = self.client.post("/webhook/whatsapp", json=sample_whatsapp_payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "owner_not_found"

    def test_duplicata_ignorada(self, sample_whatsapp_payload):
        self.mock_redis.get.return_value = "1"
        self.mock_whatsapp.parse_webhook.return_value = IncomingMessage(
            instance="test-instance", phone="5511999999999",
            message="Oi", message_id="msg-abc-123", media_type="text",
        )
        resp = self.client.post("/webhook/whatsapp", json=sample_whatsapp_payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "duplicate"
        # Reset para não afetar outros testes
        self.mock_redis.get.return_value = None

    def test_comando_dono_painel(self, sample_whatsapp_payload):
        """Dono manda /painel → panel_sent."""
        self.mock_whatsapp.parse_webhook.return_value = IncomingMessage(
            instance="test-instance",
            phone="5511988888888",  # phone do owner
            message="/painel",
            message_id="msg-painel",
            media_type="text",
        )
        resp = self.client.post("/webhook/whatsapp", json=sample_whatsapp_payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "panel_sent"
        self.mock_whatsapp.send_message.assert_called()

    def test_comando_dono_ajuda(self, sample_whatsapp_payload):
        """Dono manda /ajuda → help_sent."""
        self.mock_whatsapp.parse_webhook.return_value = IncomingMessage(
            instance="test-instance",
            phone="5511988888888",
            message="/ajuda",
            message_id="msg-ajuda",
            media_type="text",
        )
        resp = self.client.post("/webhook/whatsapp", json=sample_whatsapp_payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "help_sent"


# ═══════════════════════════════ HEALTH ENDPOINTS ═══════════════════════════

class TestHealthEndpoints:
    """Testa endpoints de health check."""

    @pytest.fixture(autouse=True)
    def setup_app(self):
        from app.main import app
        self.client = TestClient(app)
        yield

    def test_health_live(self):
        resp = self.client.get("/health/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_debug_raise_sem_token(self):
        resp = self.client.get("/debug/raise")
        assert resp.status_code == 401

    def test_admin_backup_sem_token(self):
        resp = self.client.get("/admin/backup")
        assert resp.status_code == 401

    def test_webhook_health(self):
        resp = self.client.get("/webhook/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ═══════════════════════════════ HELPERS ═══════════════════════════════

class TestHelpers:
    """Testa funções auxiliares do webhook."""

    def test_normalize_phone(self):
        from app.routers.webhook import _normalize_phone
        assert _normalize_phone("+55 (11) 99999-9999") == "5511999999999"
        assert _normalize_phone("5511988888888") == "5511988888888"
        assert _normalize_phone("") == ""

    def test_extract_urls(self):
        from app.routers.webhook import _extract_urls
        text = "/aprender https://example.com/page e https://other.com/doc"
        urls = _extract_urls(text)
        assert len(urls) == 2
        assert "https://example.com/page" in urls

    def test_extract_phone(self):
        from app.routers.webhook import _extract_phone
        assert _extract_phone("/assumir 5511999990000 agora") == "5511999990000"
        assert _extract_phone("/assumir sem numero") == ""

    def test_extract_urls_sem_links(self):
        from app.routers.webhook import _extract_urls
        assert _extract_urls("texto sem link nenhum") == []

    def test_extract_after_prefix(self):
        from app.routers.webhook import _extract_after_prefix
        result = _extract_after_prefix("/bemvindo Olá, bem-vindo!", ("/bemvindo ",))
        assert result == "Olá, bem-vindo!"
