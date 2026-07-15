import pytest
from pydantic import ValidationError

from app.config import Settings


def test_vault_watcher_defaults_cover_stability_and_delete_grace():
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
def test_vault_watcher_rejects_unsafe_configuration(updates):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **updates)
