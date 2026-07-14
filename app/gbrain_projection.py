from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import httpx

from app.config import Settings


MCP_PROTOCOL_VERSION = "2025-11-25"
PAGE_STATUSES = {"imported", "skipped", "error", "superseded", "recovery_required"}


class ProjectionConfigurationError(RuntimeError):
    """Projection endpoint, credential, source, or root configuration is invalid."""


class ProjectionNetworkError(RuntimeError):
    """The HTTP MCP exchange failed before a trustworthy result was decoded."""


class ProjectionProtocolError(RuntimeError):
    """The MCP envelope or lgdo_vault_sync payload violated the locked contract."""


class ProjectionToolError(RuntimeError):
    """The MCP server returned a structured tool error."""


def to_gbrain_manifest_path(page_path: str) -> str:
    prefix = "wiki/"
    if "\\" in page_path or not page_path.startswith(prefix):
        raise ProjectionProtocolError("wiki page path must begin with 'wiki/'")
    relative = page_path[len(prefix) :]
    raw_parts = relative.split("/")
    if not relative or relative.startswith("/") or any(part in {"", ".", ".."} for part in raw_parts):
        raise ProjectionProtocolError("wiki page path is not a safe GBrain source path")
    parts = PurePosixPath(relative).parts
    if "/".join(parts) != relative:
        raise ProjectionProtocolError("wiki page path is not a safe GBrain source path")
    return relative


@dataclass(frozen=True)
class ExpectedPage:
    page_id: str
    revision_id: str
    projection_epoch: int
    path: str
    file_hash: str


@dataclass(frozen=True)
class ProtectedMapping:
    source_id: str
    slug: str | None
    source_path: str | None
    reason: str


@dataclass(frozen=True)
class GBrainSyncRequest:
    source_id: str
    root: str
    mode: Literal["incremental", "reconcile"]
    expected_pages: Sequence[ExpectedPage]
    protected_mappings: Sequence[ProtectedMapping]
    no_embed: bool
    idempotency_key: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "root": self.root,
            "mode": self.mode,
            "expected_pages": [asdict(page) for page in self.expected_pages],
            "protected_mappings": [asdict(mapping) for mapping in self.protected_mappings],
            "no_embed": self.no_embed,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class GBrainPageSyncResult:
    page_id: str
    revision_id: str
    projection_epoch: int
    path: str
    file_hash: str
    source_id: str
    slug: str | None
    source_path: str
    raw_file_hash_before: str | None
    raw_file_hash_after: str | None
    content_hash: str | None
    page_generation: int | None
    status: str
    error: str | None
    protected_mappings: Sequence[ProtectedMapping]


@dataclass(frozen=True)
class GBrainDeletedResult:
    source_id: str
    slug: str


@dataclass(frozen=True)
class GBrainSyncResponse:
    source_id: str
    mode: Literal["incremental", "reconcile"]
    idempotency_key: str
    pages: Sequence[GBrainPageSyncResult]
    deleted: Sequence[GBrainDeletedResult]
    protected_mappings: Sequence[ProtectedMapping]
    imported: int
    skipped: int
    errors: int
    chunks: int
    duration_ms: float

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GBrainSyncResponse":
        aggregate_keys = {
            "source_id",
            "mode",
            "idempotency_key",
            "pages",
            "deleted",
            "protected_mappings",
            "imported",
            "skipped",
            "errors",
            "chunks",
            "duration_ms",
        }
        _require_exact_keys(payload, aggregate_keys, "sync result")
        mode = _required_string(payload, "mode", "sync result")
        if mode not in {"incremental", "reconcile"}:
            raise ValueError(f"invalid sync mode: {mode}")

        pages: list[GBrainPageSyncResult] = []
        for index, item in enumerate(_required_list(payload, "pages", "sync result")):
            page = _required_mapping(item, f"pages[{index}]")
            page_keys = {
                "page_id",
                "revision_id",
                "projection_epoch",
                "path",
                "file_hash",
                "source_id",
                "slug",
                "source_path",
                "raw_file_hash_before",
                "raw_file_hash_after",
                "content_hash",
                "page_generation",
                "status",
                "error",
                "protected_mappings",
            }
            _require_exact_keys(page, page_keys, f"pages[{index}]")
            status = _required_string(page, "status", f"pages[{index}]")
            if status not in PAGE_STATUSES:
                raise ValueError(f"invalid page status: {status}")
            pages.append(
                GBrainPageSyncResult(
                    page_id=_required_string(page, "page_id", f"pages[{index}]"),
                    revision_id=_required_string(page, "revision_id", f"pages[{index}]"),
                    projection_epoch=_required_int(page, "projection_epoch", f"pages[{index}]"),
                    path=_required_string(page, "path", f"pages[{index}]"),
                    file_hash=_required_string(page, "file_hash", f"pages[{index}]"),
                    source_id=_required_string(page, "source_id", f"pages[{index}]"),
                    slug=_optional_string(page, "slug", f"pages[{index}]"),
                    source_path=_required_string(page, "source_path", f"pages[{index}]"),
                    raw_file_hash_before=_optional_string(page, "raw_file_hash_before", f"pages[{index}]"),
                    raw_file_hash_after=_optional_string(page, "raw_file_hash_after", f"pages[{index}]"),
                    content_hash=_optional_string(page, "content_hash", f"pages[{index}]"),
                    page_generation=_optional_int(page, "page_generation", f"pages[{index}]"),
                    status=status,
                    error=_optional_string(page, "error", f"pages[{index}]"),
                    protected_mappings=_parse_protected_mappings(
                        page["protected_mappings"],
                        f"pages[{index}].protected_mappings",
                    ),
                )
            )

        deleted: list[GBrainDeletedResult] = []
        for index, item in enumerate(_required_list(payload, "deleted", "sync result")):
            deleted_item = _required_mapping(item, f"deleted[{index}]")
            _require_exact_keys(deleted_item, {"source_id", "slug"}, f"deleted[{index}]")
            deleted.append(
                GBrainDeletedResult(
                    source_id=_required_string(deleted_item, "source_id", f"deleted[{index}]"),
                    slug=_required_string(deleted_item, "slug", f"deleted[{index}]"),
                )
            )

        duration_ms = _required_float(payload, "duration_ms", "sync result")
        if not math.isfinite(duration_ms) or duration_ms < 0:
            raise ValueError("sync result.duration_ms must be a finite non-negative number")
        return cls(
            source_id=_required_string(payload, "source_id", "sync result"),
            mode=mode,
            idempotency_key=_required_string(payload, "idempotency_key", "sync result"),
            pages=tuple(pages),
            deleted=tuple(deleted),
            protected_mappings=_parse_protected_mappings(
                payload["protected_mappings"],
                "sync result.protected_mappings",
            ),
            imported=_required_non_negative_int(payload, "imported", "sync result"),
            skipped=_required_non_negative_int(payload, "skipped", "sync result"),
            errors=_required_non_negative_int(payload, "errors", "sync result"),
            chunks=_required_non_negative_int(payload, "chunks", "sync result"),
            duration_ms=duration_ms,
        )


class GBrainProjectionClient:
    def __init__(self, settings: Settings):
        endpoint = (settings.gbrain_endpoint or "").strip()
        projection_token = (settings.gbrain_projection_api_key or "").strip()
        if not endpoint or not projection_token:
            raise ProjectionConfigurationError("GBrain projection endpoint/token is not configured")
        try:
            parsed_endpoint = httpx.URL(endpoint)
        except (TypeError, ValueError) as exc:
            raise ProjectionConfigurationError("GBrain projection endpoint is invalid") from exc
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.host:
            raise ProjectionConfigurationError("GBrain projection endpoint must be an HTTP(S) URL")

        self.settings = settings
        self.endpoint = endpoint
        self.projection_token = projection_token
        self.root = (settings.vault_path / "wiki").expanduser().resolve()
        if settings.gbrain_import_allowed_root is not None:
            allowed_root = settings.gbrain_import_allowed_root.expanduser().resolve()
            if allowed_root != self.root:
                raise ProjectionConfigurationError("GBrain projection root does not match the configured allowed root")

    async def sync(self, request: GBrainSyncRequest) -> GBrainSyncResponse:
        self._validate_request(request)
        timeout_seconds = (
            self.settings.gbrain_reconcile_timeout_seconds
            if request.mode == "reconcile"
            else self.settings.gbrain_incremental_timeout_seconds
        )
        if timeout_seconds <= 0:
            raise ProjectionConfigurationError("GBrain projection timeout must be positive")

        async with httpx.AsyncClient(timeout=httpx.Timeout(float(timeout_seconds))) as client:
            await self._initialize(client)
            payload = await self._call(client, "lgdo_vault_sync", request.to_payload())
        try:
            response = GBrainSyncResponse.from_payload(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionProtocolError(f"invalid lgdo_vault_sync result: {exc}") from exc
        self._validate_response(request, response)
        return response

    async def _initialize(self, client: httpx.AsyncClient) -> None:
        envelope = await self._post_envelope(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "lgdo-projection", "version": "1"},
                },
            },
            expected_request_id=1,
        )
        self._raise_envelope_error(envelope, "initialize")
        if not isinstance(envelope.get("result"), Mapping):
            raise ProjectionProtocolError("MCP initialize response omitted a result object")

    async def _call(
        self,
        client: httpx.AsyncClient,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        envelope = await self._post_envelope(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
            expected_request_id=2,
        )
        self._raise_envelope_error(envelope, tool_name)
        result = envelope.get("result")
        if not isinstance(result, Mapping):
            raise ProjectionProtocolError(f"MCP tool {tool_name} response omitted a result object")
        is_error = result.get("isError", False)
        if not isinstance(is_error, bool):
            raise ProjectionProtocolError(f"MCP tool {tool_name} returned a non-boolean isError")
        text = _tool_text(result)
        if is_error:
            raise ProjectionToolError(text or f"MCP tool {tool_name} returned isError")

        structured = result.get("structuredContent")
        if structured is not None:
            if not isinstance(structured, Mapping):
                raise ProjectionProtocolError(f"MCP tool {tool_name} returned invalid structuredContent")
            return dict(structured)
        if not text:
            raise ProjectionProtocolError(f"MCP tool {tool_name} returned no JSON result content")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProjectionProtocolError(f"MCP tool {tool_name} returned malformed JSON content") from exc
        if not isinstance(payload, dict):
            raise ProjectionProtocolError(f"MCP tool {tool_name} result must be a JSON object")
        return payload

    async def _post_envelope(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        *,
        expected_request_id: int,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.projection_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        try:
            response = await client.post(self.endpoint, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ProjectionNetworkError("GBrain projection HTTP request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProjectionNetworkError(f"GBrain projection HTTP request failed: {exc}") from exc

        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            envelopes: Any = _parse_sse_envelopes(response.text)
        else:
            try:
                envelopes = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                raise ProjectionProtocolError("GBrain projection returned malformed JSON") from exc
        return _select_response_envelope(envelopes, expected_request_id)

    @staticmethod
    def _raise_envelope_error(envelope: Mapping[str, Any], operation: str) -> None:
        if "error" not in envelope:
            return
        error = envelope["error"]
        if isinstance(error, Mapping):
            code = error.get("code")
            message = error.get("message")
            detail = f"{code}: {message}" if code is not None else str(message or error)
        else:
            detail = str(error)
        raise ProjectionToolError(f"MCP {operation} failed: {detail}")

    def _validate_request(self, request: GBrainSyncRequest) -> None:
        if request.mode not in {"incremental", "reconcile"}:
            raise ProjectionConfigurationError("GBrain sync mode must be incremental or reconcile")
        if not request.source_id or not request.idempotency_key:
            raise ProjectionConfigurationError("GBrain sync source and idempotency key are required")
        managed_source = (self.settings.gbrain_managed_source_id or "").strip()
        if managed_source and request.source_id != managed_source:
            raise ProjectionConfigurationError("GBrain sync source does not match the managed source")
        try:
            request_root = Path(request.root).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise ProjectionConfigurationError("GBrain sync root is invalid") from exc
        if request_root != self.root:
            raise ProjectionConfigurationError("GBrain sync root does not match the trusted Vault root")

    @staticmethod
    def _validate_response(request: GBrainSyncRequest, response: GBrainSyncResponse) -> None:
        if response.source_id != request.source_id:
            raise ProjectionProtocolError("lgdo_vault_sync response source_id mismatch")
        if response.mode != request.mode:
            raise ProjectionProtocolError("lgdo_vault_sync response mode mismatch")
        if response.idempotency_key != request.idempotency_key:
            raise ProjectionProtocolError("lgdo_vault_sync response idempotency_key mismatch")

        expected_by_id = {page.page_id: page for page in request.expected_pages}
        actual_by_id = {page.page_id: page for page in response.pages}
        if len(expected_by_id) != len(request.expected_pages) or len(actual_by_id) != len(response.pages):
            raise ProjectionProtocolError("lgdo_vault_sync returned duplicate page results")
        if set(actual_by_id) != set(expected_by_id):
            raise ProjectionProtocolError("lgdo_vault_sync page results do not match the request")
        for page_id, expected in expected_by_id.items():
            actual = actual_by_id[page_id]
            if actual.revision_id != expected.revision_id:
                raise ProjectionProtocolError(f"page result revision_id mismatch for {page_id}")
            if actual.projection_epoch != expected.projection_epoch:
                raise ProjectionProtocolError(f"page result projection_epoch mismatch for {page_id}")
            if actual.path != expected.path:
                raise ProjectionProtocolError(f"page result path mismatch for {page_id}")
            if actual.file_hash != expected.file_hash:
                raise ProjectionProtocolError(f"page result file_hash mismatch for {page_id}")
            if actual.source_path != expected.path:
                raise ProjectionProtocolError(f"page result source_path mismatch for {page_id}")
            if actual.source_id != request.source_id:
                raise ProjectionProtocolError(f"page result source_id mismatch for {page_id}")
            if any(mapping.source_id != request.source_id for mapping in actual.protected_mappings):
                raise ProjectionProtocolError(f"page result protection source_id mismatch for {page_id}")

        imported = sum(page.status == "imported" for page in response.pages)
        skipped = sum(page.status == "skipped" for page in response.pages)
        errors = len(response.pages) - imported - skipped
        if (response.imported, response.skipped, response.errors) != (imported, skipped, errors):
            raise ProjectionProtocolError("lgdo_vault_sync aggregate page counts do not match page results")
        if any(item.source_id != request.source_id for item in response.deleted):
            raise ProjectionProtocolError("lgdo_vault_sync deletion source_id mismatch")
        if any(mapping.source_id != request.source_id for mapping in response.protected_mappings):
            raise ProjectionProtocolError("lgdo_vault_sync protection source_id mismatch")


def bump_gbrain_projection_generation(conn: Any, timestamp: str) -> int:
    conn.execute(
        "UPDATE projection_state SET value = value + 1, updated_at = ? WHERE key = ?",
        (timestamp, "gbrain_projection_generation"),
    )
    row = conn.execute(
        "SELECT value FROM projection_state WHERE key = ?",
        ("gbrain_projection_generation",),
    ).fetchone()
    if row is None:
        raise RuntimeError("gbrain_projection_generation is not initialized")
    return int(row["value"])


def _parse_sse_envelopes(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif not line.strip() and data_lines:
            events.append(_parse_sse_data(data_lines))
            data_lines = []
    if data_lines:
        events.append(_parse_sse_data(data_lines))
    if not events:
        raise ProjectionProtocolError("GBrain projection returned an empty SSE response")
    return events


def _select_response_envelope(value: Any, expected_request_id: int) -> dict[str, Any]:
    envelopes = value if isinstance(value, list) else [value]
    if not envelopes:
        raise ProjectionProtocolError("GBrain projection returned an empty JSON-RPC batch")
    normalized: list[dict[str, Any]] = []
    for envelope in envelopes:
        if not isinstance(envelope, dict):
            raise ProjectionProtocolError("GBrain projection returned a non-object JSON-RPC envelope")
        if envelope.get("jsonrpc") != "2.0":
            raise ProjectionProtocolError("GBrain projection returned an invalid JSON-RPC version")
        normalized.append(envelope)
    matching = [
        envelope
        for envelope in normalized
        if type(envelope.get("id")) is type(expected_request_id)
        and envelope.get("id") == expected_request_id
    ]
    if not matching:
        raise ProjectionProtocolError(
            f"GBrain projection response id did not match request id {expected_request_id}"
        )
    if len(matching) != 1:
        raise ProjectionProtocolError(
            f"GBrain projection returned duplicate response id {expected_request_id}"
        )
    return matching[0]


def _parse_sse_data(data_lines: Sequence[str]) -> dict[str, Any]:
    try:
        parsed = json.loads("\n".join(data_lines))
    except json.JSONDecodeError as exc:
        raise ProjectionProtocolError("GBrain projection returned malformed SSE JSON") from exc
    if not isinstance(parsed, dict):
        raise ProjectionProtocolError("GBrain projection SSE data must be a JSON object")
    return parsed


def _tool_text(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        item["text"]
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "text" and isinstance(item.get("text"), str)
    ]
    return "\n".join(parts).strip()


def _required_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return dict(value)


def _require_exact_keys(value: Mapping[str, Any], keys: set[str], field: str) -> None:
    missing = keys - set(value)
    extra = set(value) - keys
    if missing:
        raise KeyError(f"{field} missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise TypeError(f"{field} has unexpected fields: {', '.join(sorted(extra))}")


def _required_list(value: Mapping[str, Any], key: str, field: str) -> list[Any]:
    item = value[key]
    if not isinstance(item, list):
        raise TypeError(f"{field}.{key} must be an array")
    return item


def _required_string(value: Mapping[str, Any], key: str, field: str) -> str:
    item = value[key]
    if not isinstance(item, str) or not item:
        raise TypeError(f"{field}.{key} must be a non-empty string")
    return item


def _optional_string(value: Mapping[str, Any], key: str, field: str) -> str | None:
    item = value[key]
    if item is not None and not isinstance(item, str):
        raise TypeError(f"{field}.{key} must be a string or null")
    return item


def _required_int(value: Mapping[str, Any], key: str, field: str) -> int:
    item = value[key]
    if isinstance(item, int) and not isinstance(item, bool):
        return item
    if isinstance(item, str):
        try:
            return int(item)
        except ValueError as exc:
            raise TypeError(f"{field}.{key} must be an integer") from exc
    raise TypeError(f"{field}.{key} must be an integer")


def _optional_int(value: Mapping[str, Any], key: str, field: str) -> int | None:
    if value[key] is None:
        return None
    return _required_int(value, key, field)


def _required_non_negative_int(value: Mapping[str, Any], key: str, field: str) -> int:
    item = _required_int(value, key, field)
    if item < 0:
        raise ValueError(f"{field}.{key} must be non-negative")
    return item


def _required_float(value: Mapping[str, Any], key: str, field: str) -> float:
    item = value[key]
    if isinstance(item, bool):
        raise TypeError(f"{field}.{key} must be a number")
    try:
        return float(item)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field}.{key} must be a number") from exc


def _parse_protected_mappings(value: Any, field: str) -> tuple[ProtectedMapping, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array")
    mappings: list[ProtectedMapping] = []
    for index, item in enumerate(value):
        mapping = _required_mapping(item, f"{field}[{index}]")
        allowed = {"source_id", "slug", "source_path", "reason"}
        required = {"source_id", "reason"}
        missing = required - set(mapping)
        extra = set(mapping) - allowed
        if missing:
            raise KeyError(f"{field}[{index}] missing fields: {', '.join(sorted(missing))}")
        if extra:
            raise TypeError(f"{field}[{index}] has unexpected fields: {', '.join(sorted(extra))}")
        normalized = {**mapping, "slug": mapping.get("slug"), "source_path": mapping.get("source_path")}
        mappings.append(
            ProtectedMapping(
                source_id=_required_string(normalized, "source_id", f"{field}[{index}]"),
                slug=_optional_string(normalized, "slug", f"{field}[{index}]"),
                source_path=_optional_string(normalized, "source_path", f"{field}[{index}]"),
                reason=_required_string(normalized, "reason", f"{field}[{index}]"),
            )
        )
    return tuple(mappings)
