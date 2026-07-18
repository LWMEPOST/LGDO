from __future__ import annotations

import app.api as api_module
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_upload_rejects_domain_traversal_before_writing_outside_root(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "auth_dev_fallback_enabled", True)
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    escaped_path = tmp_path / "escaped" / "payload.txt"

    response = TestClient(app).post(
        "/api/internal/sources/upload",
        data={"domain": "../escaped"},
        files={"files": ("payload.txt", b"sensitive", "text/plain")},
    )

    assert response.status_code == 400
    assert not escaped_path.exists()


def test_resolved_upload_domain_path_must_remain_within_root(tmp_path):
    resolver = getattr(api_module, "_resolve_upload_domain_path", None)
    assert callable(resolver)

    with pytest.raises(ValueError, match="upload root"):
        resolver(tmp_path / "uploads", "../escaped")


@pytest.mark.parametrize(
    "form_data",
    [
        {"domain": "not-a-supported-domain"},
        {"domain": "product", "metadata_defaults": "[]"},
    ],
)
def test_upload_validates_scan_request_before_writing_files(
    tmp_path,
    monkeypatch,
    form_data,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "auth_dev_fallback_enabled", True)
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")

    response = TestClient(app).post(
        "/api/internal/sources/upload",
        data=form_data,
        files={"files": ("payload.txt", b"sensitive", "text/plain")},
    )

    assert response.status_code == 400
    assert not (tmp_path / "uploads").exists()
