from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.db import connect_app_write, init_app_db
from app.gbrain import GBrainHit, _query_cache_key, call_gbrain_tool, normalize_gbrain_hits
from app.gbrain_projection import (
    ExpectedPage,
    GBrainProjectionClient,
    GBrainSyncRequest,
    ProjectionConfigurationError,
    ProjectionNetworkError,
    ProjectionProtocolError,
    ProjectionToolError,
    bump_gbrain_projection_generation,
    to_gbrain_manifest_path,
)


ENDPOINT = "https://gbrain.example/mcp"


class ScriptedAsyncClient:
    def __init__(self, outcomes: list[httpx.Response | Exception]):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.timeout: httpx.Timeout | None = None

    async def __aenter__(self) -> "ScriptedAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _response(payload: Any, *, content_type: str = "application/json") -> httpx.Response:
    if content_type == "text/event-stream":
        content = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": content_type},
            request=httpx.Request("POST", ENDPOINT),
        )
    return httpx.Response(
        200,
        json=payload,
        headers={"content-type": content_type},
        request=httpx.Request("POST", ENDPOINT),
    )


def _initialize_response() -> httpx.Response:
    return _response(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"protocolVersion": "2025-11-25", "capabilities": {}},
        }
    )


def _settings(tmp_path, **overrides: Any) -> Settings:
    values = {
        "database_backend": "sqlite",
        "database_path": tmp_path / "lgdo.db",
        "vault_path": tmp_path / "vault",
        "gbrain_endpoint": ENDPOINT,
        "gbrain_api_key": "legacy-query-secret",
        "gbrain_query_api_key": "query-secret",
        "gbrain_projection_api_key": "projection-secret",
        "gbrain_managed_source_id": "lgdo-managed",
    }
    values.update(overrides)
    return Settings(**values)


def _request(settings: Settings, *, mode: str = "incremental") -> GBrainSyncRequest:
    return GBrainSyncRequest(
        source_id="lgdo-managed",
        root=str((settings.vault_path / "wiki").resolve()),
        mode=mode,
        expected_pages=(
            ExpectedPage(
                page_id="page-demo",
                revision_id="wrev-demo-2",
                projection_epoch=2,
                path="product/faq/demo.md",
                file_hash="a" * 64,
            ),
        ),
        protected_mappings=(),
        no_embed=True,
        idempotency_key=f"sync-{mode}",
    )


def _sync_payload(
    request: GBrainSyncRequest,
    *,
    status: str = "imported",
    error: str | None = None,
) -> dict[str, Any]:
    expected = request.expected_pages[0]
    page = {
        "page_id": expected.page_id,
        "revision_id": expected.revision_id,
        "projection_epoch": expected.projection_epoch,
        "path": expected.path,
        "source_id": request.source_id,
        "slug": "product/faq/demo",
        "source_path": expected.path,
        "raw_file_hash_before": expected.file_hash,
        "raw_file_hash_after": expected.file_hash,
        "content_hash": "b" * 64 if status == "imported" else None,
        "page_generation": 3 if status == "imported" else None,
        "status": status,
        "error": error,
        "protected_mappings": [],
    }
    return {
        "source_id": request.source_id,
        "mode": request.mode,
        "idempotency_key": request.idempotency_key,
        "pages": [page],
        "deleted": [],
        "protected_mappings": [],
        "imported": 1 if status == "imported" else 0,
        "skipped": 0,
        "errors": 0 if status in {"imported", "skipped"} else 1,
        "chunks": 1 if status == "imported" else 0,
        "duration_ms": 4.5,
    }


def _tool_response(payload: Any, *, is_error: bool = False, sse: bool = False) -> httpx.Response:
    envelope = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "isError": is_error,
            "content": [{"type": "text", "text": json.dumps(payload)}],
        },
    }
    return _response(envelope, content_type="text/event-stream" if sse else "application/json")


def _install_transport(monkeypatch, scripted: ScriptedAsyncClient) -> None:
    def factory(*args: Any, **kwargs: Any) -> ScriptedAsyncClient:
        scripted.timeout = kwargs["timeout"]
        return scripted

    monkeypatch.setattr("app.gbrain_projection.httpx.AsyncClient", factory)


def test_projection_client_requires_dedicated_projection_credential(tmp_path):
    settings = _settings(
        tmp_path,
        gbrain_projection_api_key=None,
        gbrain_api_key="legacy-only",
        gbrain_query_api_key="query-only",
    )

    with pytest.raises(ProjectionConfigurationError, match="projection"):
        GBrainProjectionClient(settings)


@pytest.mark.parametrize(("mode", "expected_timeout"), [("incremental", 120), ("reconcile", 600)])
def test_projection_client_uses_projection_token_and_mode_timeout(
    tmp_path,
    monkeypatch,
    mode: str,
    expected_timeout: int,
):
    settings = _settings(tmp_path)
    request = _request(settings, mode=mode)
    scripted = ScriptedAsyncClient([_initialize_response(), _tool_response(_sync_payload(request))])
    _install_transport(monkeypatch, scripted)

    result = asyncio.run(GBrainProjectionClient(settings).sync(request))

    assert result.pages[0].status == "imported"
    assert scripted.timeout is not None
    assert scripted.timeout.read == expected_timeout
    assert len(scripted.calls) == 2
    assert all(call["headers"]["Authorization"] == "Bearer projection-secret" for call in scripted.calls)
    assert all("query-secret" not in str(call) and "legacy-query-secret" not in str(call) for call in scripted.calls)


def test_query_transport_uses_query_token_not_projection_token(tmp_path, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_http(endpoint, api_key, tool_name, arguments, timeout):
        captured.update(endpoint=endpoint, api_key=api_key, tool_name=tool_name)
        return []

    monkeypatch.setattr("app.gbrain._call_http_mcp", fake_http)
    settings = _settings(tmp_path)

    call_gbrain_tool(settings, "query", {"query": "demo"})

    assert captured["api_key"] == "query-secret"


def test_projection_client_distinguishes_network_timeout(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    timeout = httpx.ReadTimeout("slow", request=httpx.Request("POST", ENDPOINT))
    scripted = ScriptedAsyncClient([timeout])
    _install_transport(monkeypatch, scripted)

    with pytest.raises(ProjectionNetworkError, match="timed out"):
        asyncio.run(GBrainProjectionClient(settings).sync(_request(settings)))


def test_projection_client_distinguishes_structured_tool_error(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    request = _request(settings)
    scripted = ScriptedAsyncClient(
        [_initialize_response(), _tool_response({"code": "invalid_params", "message": "denied"}, is_error=True)]
    )
    _install_transport(monkeypatch, scripted)

    with pytest.raises(ProjectionToolError, match="denied"):
        asyncio.run(GBrainProjectionClient(settings).sync(request))


def test_projection_client_distinguishes_malformed_json(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    malformed = httpx.Response(
        200,
        content=b"{not-json",
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", ENDPOINT),
    )
    scripted = ScriptedAsyncClient([_initialize_response(), malformed])
    _install_transport(monkeypatch, scripted)

    with pytest.raises(ProjectionProtocolError, match="JSON"):
        asyncio.run(GBrainProjectionClient(settings).sync(_request(settings)))


def test_projection_client_rejects_missing_page_results(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    request = _request(settings)
    payload = _sync_payload(request)
    payload["pages"] = []
    payload["imported"] = 0
    payload["chunks"] = 0
    scripted = ScriptedAsyncClient([_initialize_response(), _tool_response(payload)])
    _install_transport(monkeypatch, scripted)

    with pytest.raises(ProjectionProtocolError, match="page results"):
        asyncio.run(GBrainProjectionClient(settings).sync(request))


def test_projection_client_returns_aggregate_success_with_page_error(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    request = _request(settings)
    payload = _sync_payload(request, status="error", error="frontmatter mismatch")
    scripted = ScriptedAsyncClient([_initialize_response(), _tool_response(payload, sse=True)])
    _install_transport(monkeypatch, scripted)

    result = asyncio.run(GBrainProjectionClient(settings).sync(request))

    assert result.errors == 1
    assert result.pages[0].status == "error"
    assert result.pages[0].error == "frontmatter mismatch"


@pytest.mark.parametrize("field", ["path", "source_path"])
def test_projection_client_rejects_returned_path_identity_mismatch(tmp_path, monkeypatch, field: str):
    settings = _settings(tmp_path)
    request = _request(settings)
    payload = _sync_payload(request)
    payload["pages"][0][field] = "other/demo.md"
    scripted = ScriptedAsyncClient([_initialize_response(), _tool_response(payload)])
    _install_transport(monkeypatch, scripted)

    with pytest.raises(ProjectionProtocolError, match=field):
        asyncio.run(GBrainProjectionClient(settings).sync(request))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["pages"][0].update(unexpected="field"),
        lambda payload: payload["pages"][0].update(page_generation="not-a-number"),
        lambda payload: payload["pages"][0].update(page_generation=1.5),
        lambda payload: payload.update(pages={"wrong": "shape"}),
    ],
)
def test_projection_client_rejects_strict_result_shape(tmp_path, monkeypatch, mutate):
    settings = _settings(tmp_path)
    request = _request(settings)
    payload = _sync_payload(request)
    mutate(payload)
    scripted = ScriptedAsyncClient([_initialize_response(), _tool_response(payload)])
    _install_transport(monkeypatch, scripted)

    with pytest.raises(ProjectionProtocolError, match="invalid lgdo_vault_sync result"):
        asyncio.run(GBrainProjectionClient(settings).sync(request))


def test_manifest_path_conversion_is_strict():
    assert to_gbrain_manifest_path("wiki/product/faq/demo.md") == "product/faq/demo.md"

    invalid = [
        "product/faq/demo.md",
        "wiki/",
        "wiki\\product\\faq\\demo.md",
        "/wiki/product/faq/demo.md",
        "wiki/../demo.md",
        "wiki/product/./demo.md",
        "wiki/product//demo.md",
        "wiki//absolute.md",
    ]
    for path in invalid:
        with pytest.raises(ProjectionProtocolError):
            to_gbrain_manifest_path(path)


def test_persistent_projection_generation_changes_cache_keys_across_instances(tmp_path):
    settings_a = _settings(tmp_path)
    settings_b = _settings(tmp_path)
    init_app_db(settings_a)

    before_a = _query_cache_key(settings_a, "demo", 4)
    before_b = _query_cache_key(settings_b, "demo", 4)
    with connect_app_write(settings_a) as conn:
        generation = bump_gbrain_projection_generation(conn, "2026-07-14T12:00:00+08:00")
    after_a = _query_cache_key(settings_a, "demo", 4)
    after_b = _query_cache_key(settings_b, "demo", 4)

    assert generation == 1
    assert before_a == before_b
    assert after_a == after_b
    assert after_a != before_a


def test_normalized_hit_preserves_gbrain_projection_identity():
    hit = normalize_gbrain_hits(
        [
            {
                "slug": "product/features/kb-0045_points",
                "title": "KB-0045 points",
                "chunk_text": "Daily sign-in grants points.",
                "score": 1.0,
                "source_id": "gbrain-namespace",
                "source_path": "product/features/points.md",
                "content_hash": "c" * 64,
                "page_generation": 9,
            }
        ]
    )[0]

    assert hit.gbrain_source_id == "gbrain-namespace"
    assert hit.source_id == "gbrain-namespace"
    assert hit.source_path == "product/features/points.md"
    assert hit.content_hash == "c" * 64
    assert hit.page_generation == 9


def test_gbrain_hit_source_id_is_a_namespace_alias():
    legacy = GBrainHit(slug="demo", title="Demo", snippet="text", score=1.0, source_id="namespace")
    canonical = GBrainHit(
        slug="demo",
        title="Demo",
        snippet="text",
        score=1.0,
        gbrain_source_id="namespace",
    )

    assert legacy.gbrain_source_id == "namespace"
    assert canonical.source_id == "namespace"
    with pytest.raises(ValueError, match="namespace"):
        GBrainHit(
            slug="demo",
            title="Demo",
            snippet="text",
            score=1.0,
            source_id="one",
            gbrain_source_id="two",
        )
