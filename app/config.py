from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "LGDO Knowledge Core"
    app_env: str = "development"
    database_path: Path = Path("data/lgdo.db")
    database_backend: str = "sqlite"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str | None = None
    postgres_database: str = "lgdo"
    rag_store_backend: str = "sqlite"
    vault_path: Path = Path("vault")
    upload_path: Path = Path("uploads")
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str | None = None
    ocr_enabled: bool = False
    rag_chunk_size: int = 1400
    rag_chunk_overlap: int = 250
    rag_embedding_provider: str = "local-hash"
    rag_embedding_model: str = "local-hash-v1"
    rag_embedding_dimension: int = 96
    gbrain_enabled: bool = False
    gbrain_endpoint: str | None = None
    gbrain_api_key: str | None = None
    gbrain_home: Path = Path("data/gbrain")
    gbrain_repo_path: Path = Path("gbrain")
    gbrain_command: str = "bun"
    gbrain_source_id: str | None = None
    gbrain_query_limit: int = 4
    gbrain_candidate_limit: int = 14
    gbrain_query_detail: str = "high"
    gbrain_query_expand: bool = False
    gbrain_query_timeout_seconds: int = 45
    gbrain_query_cache_ttl_seconds: int = 300
    gbrain_import_timeout_seconds: int = 600
    gbrain_import_on_compile: bool = False
    gbrain_import_no_embed: bool = False
    oidc_enabled: bool = False
    oidc_issuer: str | None = None
    oidc_jwks_url: str | None = None
    oidc_client_id: str | None = None
    oidc_audience: str | None = None
    oidc_username_claim: str = "preferred_username"
    oidc_role_claim: str = "role"
    oidc_acl_claim: str = "acl_tags"
    oidc_groups_claim: str = "groups"
    auth_dev_fallback_enabled: bool = True
    auth_dev_user_id: str = "admin"
    auth_dev_username: str = "管理员"
    auth_trust_request_user_context: bool = False
    auth_bootstrap_admin_password: str = "admin"
    auth_session_ttl_hours: int = 12

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def postgres_dsn(self) -> str:
        password = self.postgres_password or ""
        auth = f"{self.postgres_user}:{password}"
        return f"postgresql://{auth}@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
