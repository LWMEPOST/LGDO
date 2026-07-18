from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
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
    vault_watch_enabled: bool = True
    vault_watch_debounce_ms: int = 750
    vault_watch_stability_timeout_seconds: float = 3.0
    vault_watch_max_file_bytes: int = 5 * 1024 * 1024
    vault_watch_max_prefix_bytes: int = 64 * 1024
    vault_rename_grace_ms: int = 5000
    vault_rename_safety_margin_ms: int = 1000
    vault_watch_concurrency: int = 4
    vault_reconcile_lease_seconds: int = 30
    obsidian_vault_name: str | None = None
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
    dashscope_embedding_enabled: bool = False
    dashscope_api_key: str | None = None
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_embedding_model: str = "text-embedding-v3"
    dashscope_embedding_dimension: int = 1024
    dashscope_embedding_timeout_seconds: float = 3.0
    dashscope_embedding_batch_size: int = 10
    dashscope_embedding_cache_ttl_seconds: int = 600
    dashscope_embedding_cache_max_entries: int = 256
    dashscope_rerank_candidate_limit: int = 20
    gbrain_enabled: bool = False
    gbrain_endpoint: str | None = None
    gbrain_api_key: str | None = None
    gbrain_query_api_key: str | None = None
    gbrain_projection_api_key: str | None = None
    gbrain_managed_source_id: str | None = None
    gbrain_import_allowed_root: Path | None = None
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
    gbrain_circuit_failure_threshold: int = 3
    gbrain_circuit_cooldown_seconds: int = 30
    gbrain_import_timeout_seconds: int = 600
    gbrain_incremental_timeout_seconds: int = 120
    gbrain_reconcile_timeout_seconds: int = 600
    gbrain_import_on_compile: bool = False
    gbrain_import_no_embed: bool = False
    projection_worker_enabled: bool = True
    projection_poll_seconds: float = 1.0
    projection_lease_seconds: int = 180
    projection_claim_limit: int = 500
    oidc_enabled: bool = False
    oidc_issuer: str | None = None
    oidc_jwks_url: str | None = None
    oidc_client_id: str | None = None
    oidc_audience: str | None = None
    oidc_username_claim: str = "preferred_username"
    oidc_role_claim: str = "role"
    oidc_acl_claim: str = "acl_tags"
    oidc_groups_claim: str = "groups"
    auth_dev_fallback_enabled: bool = False
    auth_dev_user_id: str = "admin"
    auth_dev_username: str = "管理员"
    auth_trust_request_user_context: bool = False
    auth_bootstrap_admin_password: str | None = None
    auth_session_ttl_hours: int = 12

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def gbrain_query_token(self) -> str | None:
        return self.gbrain_query_api_key or self.gbrain_api_key

    @model_validator(mode="after")
    def validate_vault_runtime(self) -> "Settings":
        if self.vault_watch_debounce_ms < 1:
            raise ValueError("vault_watch_debounce_ms must be at least 1")
        if self.vault_watch_stability_timeout_seconds <= 0:
            raise ValueError("vault_watch_stability_timeout_seconds must be greater than 0")
        if self.vault_rename_safety_margin_ms < 0:
            raise ValueError("vault_rename_safety_margin_ms must be at least 0")
        if self.vault_rename_grace_ms < 1:
            raise ValueError("vault_rename_grace_ms must be at least 1")

        required_rename_grace_ms = max(
            5000,
            self.vault_watch_debounce_ms
            + int(self.vault_watch_stability_timeout_seconds * 1000)
            + self.vault_rename_safety_margin_ms,
        )
        if self.vault_rename_grace_ms < required_rename_grace_ms:
            raise ValueError(
                "vault_rename_grace_ms must cover debounce, stability timeout, "
                "and rename safety margin"
            )
        if self.vault_watch_concurrency < 1:
            raise ValueError("vault_watch_concurrency must be at least 1")
        if self.vault_watch_max_file_bytes < 1:
            raise ValueError("vault_watch_max_file_bytes must be at least 1")
        if self.vault_watch_max_prefix_bytes < 1:
            raise ValueError("vault_watch_max_prefix_bytes must be at least 1")
        if self.vault_watch_max_prefix_bytes > self.vault_watch_max_file_bytes:
            raise ValueError("vault_watch_max_prefix_bytes must not exceed vault_watch_max_file_bytes")
        if self.vault_reconcile_lease_seconds < 3:
            raise ValueError("vault_reconcile_lease_seconds must be at least 3")
        return self

    @property
    def effective_obsidian_vault_name(self) -> str:
        return self.obsidian_vault_name or self.vault_path.resolve().name

    @property
    def postgres_dsn(self) -> str:
        password = self.postgres_password or ""
        auth = f"{self.postgres_user}:{password}"
        return f"postgresql://{auth}@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
