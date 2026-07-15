from __future__ import annotations

import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


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


def _is_reparse_point(path: Path) -> bool:
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return bool(
        getattr(stat_result, "st_file_attributes", 0)
        & FILE_ATTRIBUTE_REPARSE_POINT
    )


def ensure_vault(vault_path: Path) -> None:
    for rel in VAULT_DIRS:
        (vault_path / rel).mkdir(parents=True, exist_ok=True)
    migrate_legacy_indexes(vault_path)


def _new_index_quarantine(vault_path: Path) -> Path:
    quarantine_parent = vault_path / ".lgdo" / "index-migration"
    _safe_mkdirs(vault_path, quarantine_parent)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = quarantine_parent / f"{timestamp}-{uuid4().hex}"
    _require_safe_directory(vault_path, quarantine_parent)
    quarantine.mkdir(exist_ok=False)
    _require_safe_directory(vault_path, quarantine)
    return quarantine


def _require_physical_child(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Legacy index path escapes its physical root: {path}") from exc


def _require_safe_directory(root: Path, directory: Path) -> None:
    directory_stat = directory.lstat()
    if (
        directory.is_symlink()
        or _is_reparse_point(directory)
        or not stat.S_ISDIR(directory_stat.st_mode)
    ):
        raise ValueError(f"Refusing legacy migration through directory reparse point: {directory}")
    _require_physical_child(directory.resolve(strict=True), root.resolve(strict=True))


def _safe_mkdirs(root: Path, directory: Path) -> None:
    current = root
    for component in directory.relative_to(root).parts:
        if current != root:
            _require_safe_directory(root, current)
        current /= component
        try:
            current.mkdir()
        except FileExistsError:
            pass
        _require_safe_directory(root, current)


def _safe_legacy_entries(
    vault_path: Path,
    legacy_root: Path,
) -> tuple[list[Path], dict[Path, tuple[int, int]]]:
    physical_vault = vault_path.resolve(strict=True)
    physical_legacy = legacy_root.resolve(strict=True)
    _require_physical_child(physical_legacy, physical_vault)
    files: list[Path] = []
    root_stat = legacy_root.lstat()
    directories = {legacy_root: (root_stat.st_dev, root_stat.st_ino)}
    pending = [legacy_root]

    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ValueError(f"Unable to safely inspect legacy index directory: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"Unable to safely classify legacy index entry: {path}") from exc
            if entry.is_symlink() or bool(
                getattr(entry_stat, "st_file_attributes", 0)
                & FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError(f"Refusing to migrate legacy index reparse point: {path}")

            physical_path = path.resolve(strict=True)
            _require_physical_child(physical_path, physical_legacy)
            _require_physical_child(physical_path, physical_vault)
            if stat.S_ISDIR(entry_stat.st_mode):
                directory_stat = path.lstat()
                directories[path] = (directory_stat.st_dev, directory_stat.st_ino)
                pending.append(path)
            elif stat.S_ISREG(entry_stat.st_mode):
                files.append(path)
            else:
                raise ValueError(f"Refusing to migrate non-regular legacy index entry: {path}")

    files.sort(key=lambda path: path.relative_to(legacy_root).as_posix())
    return files, directories


def _require_safe_regular_path(path: Path, *physical_roots: Path) -> None:
    path_stat = path.lstat()
    if path.is_symlink() or _is_reparse_point(path) or not stat.S_ISREG(path_stat.st_mode):
        raise ValueError(f"Refusing to migrate unsafe legacy index entry: {path}")
    physical_path = path.resolve(strict=True)
    for root in physical_roots:
        _require_physical_child(physical_path, root)


def _same_directory_identity(path: Path, expected: tuple[int, int]) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    return (
        not path.is_symlink()
        and not _is_reparse_point(path)
        and stat.S_ISDIR(path_stat.st_mode)
        and (path_stat.st_dev, path_stat.st_ino) == expected
    )


def _require_safe_indexes_target(vault_path: Path, indexes_root: Path, target: Path) -> None:
    physical_vault = vault_path.resolve(strict=True)
    if indexes_root.is_symlink() or _is_reparse_point(indexes_root) or not indexes_root.is_dir():
        raise ValueError(f"Refusing legacy migration into indexes reparse point: {indexes_root}")
    physical_indexes = indexes_root.resolve(strict=True)
    _require_physical_child(physical_indexes, physical_vault)

    current = indexes_root
    for component in target.relative_to(indexes_root).parts[:-1]:
        current /= component
        if not current.exists():
            continue
        if current.is_symlink() or _is_reparse_point(current) or not current.is_dir():
            raise ValueError(f"Refusing legacy migration through indexes reparse point: {current}")
        physical_current = current.resolve(strict=True)
        _require_physical_child(physical_current, physical_indexes)
        _require_physical_child(physical_current, physical_vault)
    if target.exists():
        _require_safe_regular_path(target, physical_indexes, physical_vault)


def _publish_claim_no_replace(claim: Path, target: Path) -> str:
    for _attempt in range(2):
        try:
            os.link(claim, target)
            return "published"
        except FileExistsError:
            try:
                return "duplicate" if claim.read_bytes() == target.read_bytes() else "conflict"
            except FileNotFoundError:
                continue
    return "conflict"


def migrate_legacy_indexes(vault_path: Path) -> Path | None:
    legacy_root = vault_path / "index"
    if legacy_root.is_symlink() or _is_reparse_point(legacy_root):
        raise ValueError("Refusing to migrate a legacy index reparse point")
    if not legacy_root.is_dir():
        return None

    indexes_root = vault_path / "indexes"
    indexes_root.mkdir(parents=True, exist_ok=True)
    migration_root: Path | None = None
    physical_vault = vault_path.resolve(strict=True)
    physical_legacy = legacy_root.resolve(strict=True)
    legacy_files, legacy_directories = _safe_legacy_entries(vault_path, legacy_root)
    for source in legacy_files:
        relative_path = source.relative_to(legacy_root)
        _require_safe_indexes_target(vault_path, indexes_root, indexes_root / relative_path)
    for source in legacy_files:
        relative_path = source.relative_to(legacy_root)
        target = indexes_root / relative_path
        _require_safe_regular_path(source, physical_legacy, physical_vault)
        if migration_root is None:
            migration_root = _new_index_quarantine(vault_path)
        claim = migration_root / relative_path
        _safe_mkdirs(vault_path, claim.parent)
        os.replace(source, claim)
        _require_safe_regular_path(
            claim,
            migration_root.resolve(strict=True),
            physical_vault,
        )

        _safe_mkdirs(vault_path, target.parent)
        _require_safe_indexes_target(vault_path, indexes_root, target)
        _publish_claim_no_replace(claim, target)

    directories = sorted(
        legacy_directories.items(),
        key=lambda item: len(item[0].parts),
        reverse=True,
    )
    for directory, expected_identity in directories:
        if not _same_directory_identity(directory, expected_identity):
            continue
        try:
            directory.rmdir()
        except OSError:
            pass
    return migration_root


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
