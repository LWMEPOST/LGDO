from __future__ import annotations

import hashlib
import io
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

MANAGED_KEYS = {
    "id",
    "lgdo_page_id",
    "lgdo_revision_id",
    "lgdo_write_token",
    "lgdo_updated_at",
}
ALIAS_RE = re.compile(r"(?:^|\s)[&*][A-Za-z0-9_-]+", re.MULTILINE)
TAG_RE = re.compile(r"(?:^|\s)![!A-Za-z0-9_:./-]+", re.MULTILINE)


class MarkdownParseError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message[:500])
        self.code = code


class ObservationChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class FrontmatterLimits:
    max_frontmatter_bytes: int = 65_536
    max_depth: int = 12
    max_nodes: int = 2_000
    max_aliases: int = 32
    max_file_bytes: int = 5_242_880
    max_prefix_bytes: int = 65_536


@dataclass(frozen=True)
class ParsedWikiDocument:
    frontmatter: CommentedMap
    body: str
    raw_bytes: bytes
    newline: str


@dataclass(frozen=True)
class FileObservationInput:
    file_hash: str
    size_bytes: int
    mtime_ns: int
    content_bytes: bytes | None
    content_prefix: bytes | None
    content_truncated: bool


def compute_file_hash(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _count_nodes(value: Any, depth: int = 0) -> tuple[int, int]:
    if isinstance(value, dict):
        children = [_count_nodes(item, depth + 1) for item in value.values()]
    elif isinstance(value, (list, tuple)):
        children = [_count_nodes(item, depth + 1) for item in value]
    else:
        children = []
    return 1 + sum(count for count, _ in children), max([depth, *(level for _, level in children)])


def parse_wiki_bytes(
    raw_bytes: bytes,
    limits: FrontmatterLimits = FrontmatterLimits(),
) -> ParsedWikiDocument:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MarkdownParseError("invalid_utf8", "Wiki 文件必须是 UTF-8") from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise MarkdownParseError("missing_frontmatter", "Wiki 文件缺少 YAML frontmatter")
    marker = normalized.find("\n---\n", 4)
    if marker < 0:
        raise MarkdownParseError("unterminated_frontmatter", "Wiki frontmatter 未闭合")
    yaml_text = normalized[4:marker]
    if len(yaml_text.encode("utf-8")) > limits.max_frontmatter_bytes:
        raise MarkdownParseError("frontmatter_too_large", "Wiki frontmatter 超过限制")
    if len(ALIAS_RE.findall(yaml_text)) > limits.max_aliases:
        raise MarkdownParseError("yaml_alias_limit", "Wiki frontmatter alias 超过限制")
    if TAG_RE.search(yaml_text):
        raise MarkdownParseError("yaml_tag_forbidden", "Wiki frontmatter 不允许 YAML tag")
    yaml = YAML(typ="rt")
    yaml.allow_duplicate_keys = False
    yaml.preserve_quotes = True
    try:
        loaded = yaml.load(yaml_text) or CommentedMap()
    except YAMLError as exc:
        raise MarkdownParseError("invalid_yaml", "Wiki frontmatter YAML 无效") from exc
    if not isinstance(loaded, CommentedMap):
        raise MarkdownParseError("frontmatter_not_mapping", "Wiki frontmatter 必须是 mapping")
    nodes, depth = _count_nodes(loaded)
    if nodes > limits.max_nodes or depth > limits.max_depth:
        raise MarkdownParseError("yaml_structure_limit", "Wiki frontmatter 结构超过限制")
    body = normalized[marker + 5 :].replace("\n", newline)
    return ParsedWikiDocument(loaded, body, raw_bytes, newline)


def compute_semantic_hash(document: ParsedWikiDocument) -> str:
    metadata = {
        str(key): _plain(value)
        for key, value in document.frontmatter.items()
        if str(key) not in MANAGED_KEYS and not str(key).startswith("lgdo_")
    }
    canonical = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body = document.body.replace("\r\n", "\n")
    return hashlib.sha256((canonical + "\n" + body).encode("utf-8")).hexdigest()


def render_managed_frontmatter(
    document: ParsedWikiDocument,
    *,
    page_id: str,
    revision_id: str,
    write_token: str,
    review_status: str | None = None,
) -> bytes:
    metadata = deepcopy(document.frontmatter)
    existing_page_id = metadata.get("lgdo_page_id") or metadata.get("id")
    if existing_page_id and str(existing_page_id) != page_id:
        raise MarkdownParseError("page_identity_conflict", "frontmatter 页面身份与目标页面不一致")
    metadata["lgdo_page_id"] = page_id
    metadata["lgdo_revision_id"] = revision_id
    metadata["lgdo_write_token"] = write_token
    if review_status is not None:
        metadata["review_status"] = review_status
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4_096
    stream = io.StringIO()
    yaml.dump(metadata, stream)
    header = "---\n" + stream.getvalue() + "---\n"
    rendered = (header + document.body.replace("\r\n", "\n")).replace("\n", document.newline)
    return rendered.encode("utf-8")


def capture_file_observation(
    path: Path,
    *,
    max_content_bytes: int,
    prefix_bytes: int = 65_536,
) -> FileObservationInput:
    before = path.stat()
    digest = hashlib.sha256()
    captured = bytearray()
    prefix = bytearray()
    with path.open("rb", buffering=64 * 1024) as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
            if len(prefix) < prefix_bytes:
                prefix.extend(chunk[: prefix_bytes - len(prefix)])
            if before.st_size <= max_content_bytes and len(captured) < max_content_bytes:
                remaining = max_content_bytes - len(captured)
                captured.extend(chunk[:remaining])
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ObservationChanged(f"file changed while observing: {path}")
    truncated = after.st_size > max_content_bytes
    return FileObservationInput(
        file_hash=digest.hexdigest(),
        size_bytes=after.st_size,
        mtime_ns=after.st_mtime_ns,
        content_bytes=None if truncated else bytes(captured),
        content_prefix=bytes(prefix) if truncated else None,
        content_truncated=truncated,
    )
