from __future__ import annotations

import pytest

from app.config import get_settings
from network_gate import install_network_gate


install_network_gate()


@pytest.fixture(autouse=True)
def disable_background_integrations(monkeypatch):
    settings = get_settings()
    # Tests that exercise anonymous API flows explicitly opt into the dev identity.
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "auth_dev_fallback_enabled", True)
    monkeypatch.setattr(settings, "projection_worker_enabled", False)
    monkeypatch.setattr(settings, "gbrain_enabled", False)
    monkeypatch.setattr(settings, "vault_watch_enabled", False)
