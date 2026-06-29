from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.parsers import ParsedDocument


@dataclass
class NormalizedDocument:
    markdown: str
    chunks: list[dict]
    metadata: dict
    warnings: list[str]


def normalize_document(
    *,
    source_id: str,
    title: str,
    original_path: Path,
    domain: str,
    owner: str | None,
    acl_tags: list[str],
    content_hash: str,
    parsed: ParsedDocument,
    metadata_defaults: dict | None = None,
    chunk_size: int = 1400,
    chunk_overlap: int = 250,
) -> NormalizedDocument:
    warnings = list(parsed.warnings)
    cleaned_owner = clean_owner(owner)
    cleaned_acl = clean_acl_tags(acl_tags)
    defaults = clean_metadata_defaults(metadata_defaults or {})
    body = normalize_text(parsed.text)
    extracted_title = clean_title(extract_title(body) or "")
    cleaned_title = clean_title(title if is_generic_extracted_title(extracted_title) else extracted_title or title)

    if not body.strip():
        warnings.append("empty_body")
        body = "暂无可解析正文。"

    metadata = {
        **defaults,
        "source_id": source_id,
        "title": cleaned_title,
        "domain": domain,
        "owner": cleaned_owner,
        "acl_tags": cleaned_acl,
        "original_path": str(original_path),
        "content_hash": content_hash,
        "parser": parsed.parser,
        "ocr_used": parsed.ocr_used,
        "page_count": parsed.page_count,
        "char_count": len(body),
    }
    markdown = build_markdown(metadata, body)
    chunks = chunk_markdown(source_id, markdown, metadata, max_chars=chunk_size, overlap_chars=chunk_overlap)
    return NormalizedDocument(markdown=markdown, chunks=chunks, metadata=metadata, warnings=warnings)


def clean_title(title: str) -> str:
    title = re.sub(r"[_\-]+", " ", title).strip()
    title = re.sub(r"\s+", " ", title)
    return title or "未命名资料"


def extract_title(text: str) -> str | None:
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if match:
            return re.sub(r"\s+#*$", "", match.group(1).strip())
    return None


def is_generic_extracted_title(title: str) -> bool:
    return bool(re.match(r"^(page|slide)\s+\d+$", title.strip(), flags=re.I))


def clean_owner(owner: str | None) -> str | None:
    if not owner:
        return None
    owner = re.sub(r"\s+", " ", owner).strip()
    return owner or None


def clean_acl_tags(tags: list[str]) -> list[str]:
    cleaned: list[str] = []
    for tag in tags:
        value = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "-", tag.strip()).strip("-").lower()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned or ["internal"]


def clean_metadata_defaults(defaults: dict) -> dict:
    cleaned = {}
    for key, value in defaults.items():
        safe_key = re.sub(r"[^a-zA-Z0-9_]+", "_", str(key).strip()).strip("_").lower()
        if safe_key:
            cleaned[safe_key] = value
    return cleaned


def normalize_text(text: str) -> str:
    text = text.replace("\ufeff", "")
    text = text.replace("\x00", "")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_markdown(metadata: dict, body: str) -> str:
    frontmatter = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            frontmatter.append(f"{key}:")
            for item in value:
                frontmatter.append(f"  - {item}")
        elif value is None:
            frontmatter.append(f"{key}:")
        else:
            frontmatter.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    frontmatter.append("---")
    return "\n".join(frontmatter) + "\n\n" + body + "\n"


def chunk_markdown(
    source_id: str,
    markdown: str,
    metadata: dict,
    max_chars: int = 1400,
    overlap_chars: int = 250,
) -> list[dict]:
    body = re.sub(r"^---\n.*?\n---\n", "", markdown, count=1, flags=re.S).strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    max_chars = max(300, max_chars)
    overlap_chars = min(max(0, overlap_chars), max_chars // 2)
    chunks: list[dict] = []
    buffer: list[str] = []
    size = 0
    index = 0
    for paragraph in paragraphs:
        if buffer and size + len(paragraph) > max_chars:
            chunks.append(make_chunk(source_id, index, "\n\n".join(buffer), metadata))
            index += 1
            buffer = overlap_buffer(buffer, overlap_chars)
            size = sum(len(item) for item in buffer)
        if len(paragraph) > max_chars:
            if buffer:
                chunks.append(make_chunk(source_id, index, "\n\n".join(buffer), metadata))
                index += 1
                buffer = overlap_buffer(buffer, overlap_chars)
                size = sum(len(item) for item in buffer)
            for piece in split_long_paragraph(paragraph, max_chars, overlap_chars):
                if len(piece) >= max_chars:
                    chunks.append(make_chunk(source_id, index, piece, metadata))
                    index += 1
                else:
                    buffer.append(piece)
                    size += len(piece)
            continue
        buffer.append(paragraph)
        size += len(paragraph)
    if buffer:
        chunks.append(make_chunk(source_id, index, "\n\n".join(buffer), metadata))
    return chunks


def overlap_buffer(buffer: list[str], overlap_chars: int) -> list[str]:
    if overlap_chars <= 0:
        return []
    carried: list[str] = []
    size = 0
    for paragraph in reversed(buffer):
        carried.insert(0, paragraph)
        size += len(paragraph)
        if size >= overlap_chars:
            break
    return carried


def split_long_paragraph(paragraph: str, max_chars: int, overlap_chars: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    step = max(1, max_chars - overlap_chars)
    while start < len(paragraph):
        pieces.append(paragraph[start : start + max_chars].strip())
        start += step
    return [piece for piece in pieces if piece]


def make_chunk(source_id: str, index: int, text: str, metadata: dict) -> dict:
    return {
        "id": f"{source_id}_chunk_{index:04d}",
        "source_id": source_id,
        "chunk_index": index,
        "text": text,
        "metadata": {
            "title": metadata.get("title"),
            "domain": metadata.get("domain"),
            "owner": metadata.get("owner"),
            "acl_tags": metadata.get("acl_tags", []),
            "parser": metadata.get("parser"),
            "ocr_used": metadata.get("ocr_used", False),
        },
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
