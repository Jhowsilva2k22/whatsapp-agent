from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    supabase_url: str
    supabase_anon_key: str
    supabase_service_key: str
    anthropic_api_key: str
    google_api_key: str = ""
    evolution_api_url: str
    evolution_api_key: str
    evolution_instance: str
    redis_url: str = "redis://localhost:6379/0"
    openai_api_key: str = ""
    firecrawl_api_key: str = ""
    brave_api_key: str = ""          # Brave Search API — web search autônomo
    # Meta / Instagram
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_page_token: str = ""
    meta_page_id: str = ""
    instagram_account_id: str = ""
    meta_verify_token: str = "ig_verify_joa2024"
    app_secret: str = "secret"
    app_url: str = "http://localhost:8000"
    debug: bool = False
    # Google OAuth (Calendar + Gmail)
    google_client_id: str = ""
    google_client_secret: str = ""
    # Asaas — Billing (PIX + Boleto + Cartão, SaaS BR)
    asaas_api_key: str = ""              # $aact_... (produção) ou $aasp_... (sandbox)
    asaas_environment: str = "production"  # 'sandbox' ou 'production'
    asaas_webhook_token: str = ""        # token para validar eventos do Asaas

    class Config:
        env_file = ".env"
        case_sensitive = False

@lru_cache()
def get_settings() -> Settings:
    return Settings()
