import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.fixture
def isolated_vault_config_environment(monkeypatch):
    env_keys = (
        "VAULT_WATCH_ENABLED",
        "VAULT_WATCH_DEBOUNCE_MS",
        "VAULT_WATCH_STABILITY_TIMEOUT_SECONDS",
        "VAULT_WATCH_MAX_FILE_BYTES",
        "VAULT_WATCH_MAX_PREFIX_BYTES",
        "VAULT_RENAME_GRACE_MS",
        "VAULT_RENAME_SAFETY_MARGIN_MS",
        "VAULT_WATCH_CONCURRENCY",
        "VAULT_RECONCILE_LEASE_SECONDS",
        "OBSIDIAN_VAULT_NAME",
        "PROJECTION_WORKER_ENABLED",
        "PROJECTION_POLL_SECONDS",
        "PROJECTION_LEASE_SECONDS",
        "PROJECTION_CLAIM_LIMIT",
    )
    for key in env_keys:
        monkeypatch.delenv(key, raising=False)


def test_vault_watcher_defaults_cover_stability_and_delete_grace(
    isolated_vault_config_environment,
):
    settings = Settings(_env_file=None)
    required = max(
        5000,
        settings.vault_watch_debounce_ms
        + int(settings.vault_watch_stability_timeout_seconds * 1000)
        + settings.vault_rename_safety_margin_ms,
    )
    assert settings.vault_watch_enabled is True
    assert settings.vault_watch_max_file_bytes == 5 * 1024 * 1024
    assert settings.vault_watch_max_prefix_bytes == 64 * 1024
    assert settings.vault_rename_grace_ms >= required
    assert settings.vault_watch_concurrency == 4
    assert settings.vault_reconcile_lease_seconds == 30
    assert settings.obsidian_vault_name is None


@pytest.mark.parametrize(
    "updates",
    [
        {"vault_watch_concurrency": 0},
        {"vault_watch_debounce_ms": 0},
        {"vault_watch_stability_timeout_seconds": 0},
        {"vault_rename_safety_margin_ms": -1},
        {"vault_rename_grace_ms": 0},
        {"vault_watch_max_file_bytes": 0},
        {"vault_watch_max_prefix_bytes": 0},
        {"vault_watch_max_file_bytes": 1024, "vault_watch_max_prefix_bytes": 2048},
        {"vault_reconcile_lease_seconds": 0},
        {
            "vault_watch_debounce_ms": 750,
            "vault_watch_stability_timeout_seconds": 3,
            "vault_rename_safety_margin_ms": 1500,
            "vault_rename_grace_ms": 5000,
        },
    ],
)
def test_vault_watcher_rejects_unsafe_configuration(
    updates,
    isolated_vault_config_environment,
):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **updates)
