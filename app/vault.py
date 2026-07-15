from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


VAULT_DIRS = [
    "raw/product",
    "raw/customer_service",
    "raw/administration",
    "normalized/product",
    "normalized/customer_service",
    "normalized/administration",
    "jsonl/product",
    "jsonl/customer_service",
    "jsonl/administration",
    "wiki/product/features",
    "wiki/product/faq",
    "wiki/product/known_issues",
    "wiki/product/policies",
    "wiki/administration/features",
    "wiki/administration/faq",
    "wiki/administration/known_issues",
    "wiki/administration/policies",
    "wiki/customer_service/faq",
    "wiki/customer_service/scripts",
    "wiki/customer_service/policies",
    "wiki/customer_service/cases",
    "indexes",
    "templates",
    "reviews",
    "logs",
]


def ensure_vault(vault_path: Path) -> None:
    for rel in VAULT_DIRS:
        (vault_path / rel).mkdir(parents=True, exist_ok=True)
    migrate_legacy_indexes(vault_path)


def _new_index_quarantine(vault_path: Path) -> Path:
    quarantine_parent = vault_path / ".lgdo" / "index-migration"
    quarantine_parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = quarantine_parent / f"{timestamp}-{uuid4().hex}"
    quarantine.mkdir(exist_ok=False)
    return quarantine


def migrate_legacy_indexes(vault_path: Path) -> Path | None:
    legacy_root = vault_path / "index"
    if not legacy_root.is_dir():
        return None

    indexes_root = vault_path / "indexes"
    indexes_root.mkdir(parents=True, exist_ok=True)
    quarantine: Path | None = None
    legacy_files = sorted(
        (path for path in legacy_root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(legacy_root).as_posix(),
    )
    for source in legacy_files:
        relative_path = source.relative_to(legacy_root)
        target = indexes_root / relative_path
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            continue
        if source.read_bytes() == target.read_bytes():
            source.unlink()
            continue
        if quarantine is None:
            quarantine = _new_index_quarantine(vault_path)
        quarantine_target = quarantine / relative_path
        quarantine_target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, quarantine_target)

    directories = sorted(
        (path for path in legacy_root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        legacy_root.rmdir()
    except OSError:
        pass
    return quarantine


def slugify(value: str, fallback: str = "page") -> str:
    value = value.lower().strip()
    value = re.sub(r"[\\/:*?\"<>|\s]+", "-", value)
    value = re.sub(r"[^a-z0-9._\-\u4e00-\u9fff]+", "", value)
    value = value.strip("-._")
    return value or fallback


def append_log(vault_path: Path, log_name: str, message: str) -> None:
    ensure_vault(vault_path)
    path = vault_path / "logs" / log_name
    with path.open("a", encoding="utf-8") as file:
        file.write(message.rstrip() + "\n")
