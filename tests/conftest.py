import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def disable_background_integrations(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "projection_worker_enabled", False)
    monkeypatch.setattr(settings, "gbrain_enabled", False)
