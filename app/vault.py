from __future__ import annotations

import re
from pathlib import Path


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
    "index",
    "reviews",
    "logs",
]


def ensure_vault(vault_path: Path) -> None:
    for rel in VAULT_DIRS:
        (vault_path / rel).mkdir(parents=True, exist_ok=True)


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
