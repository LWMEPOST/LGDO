from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote, urlencode
from uuid import uuid4


RESOURCE_ROOT = Path(__file__).resolve().parents[1] / "resources" / "obsidian-vault"
MANAGED_JSON = (
    Path(".obsidian/app.json"),
    Path(".obsidian/core-plugins.json"),
    Path(".obsidian/templates.json"),
)
COPY_IF_MISSING = (
    Path("templates/Wiki Page.md"),
    Path("indexes/Home.md"),
    Path("README.md"),
)


class _CliArgumentError(Exception):
    pass


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliArgumentError(message)


@dataclass(frozen=True)
class ObsidianInstallResult:
    vault_path: Path
    installed: list[str]
    drifted: list[str]
    backup_dir: Path | None


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with temp_path.open("xb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with source.open("rb") as source_file, temp_path.open("xb") as target_file:
            shutil.copyfileobj(source_file, target_file)
            target_file.flush()
            os.fsync(target_file.fileno())
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def _new_backup_dir(vault_path: Path) -> Path:
    backup_root = vault_path / ".lgdo" / "obsidian-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = backup_root / f"{timestamp}-{uuid4().hex}"
    backup_dir.mkdir(exist_ok=False)
    return backup_dir


def build_obsidian_uri(vault_name: str, page_path: str) -> str:
    normalized_path = str(page_path).replace("\\", "/")
    query = urlencode(
        {"vault": str(vault_name), "file": normalized_path},
        quote_via=quote,
    )
    return f"obsidian://open?{query}"


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _merged_json(relative_path: Path, current: Any, managed: Any) -> Any:
    if relative_path.name == "core-plugins.json":
        if not isinstance(current, list) or not isinstance(managed, list):
            raise ValueError("core-plugins.json must contain a JSON array")
        managed_plugins = list(dict.fromkeys(managed))
        managed_set = set(managed_plugins)
        unrelated: list[Any] = []
        seen = set(managed_plugins)
        for plugin in current:
            if plugin in managed_set or plugin in seen:
                continue
            unrelated.append(plugin)
            seen.add(plugin)
        return managed_plugins + unrelated

    if not isinstance(current, dict) or not isinstance(managed, dict):
        raise ValueError(f"{relative_path.name} must contain a JSON object")
    merged = dict(current)
    merged.update(managed)
    return merged


def _backup_file(backup_dir: Path, relative_path: Path, target: Path) -> None:
    backup_path = backup_dir / relative_path
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, backup_path)


def ensure_obsidian_vault(
    vault_path: str | os.PathLike[str],
    refresh: bool = False,
) -> ObsidianInstallResult:
    vault = Path(vault_path).expanduser().resolve()
    vault.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    drifted: list[str] = []
    backup_dir: Path | None = None

    for relative_path in (*MANAGED_JSON, *COPY_IF_MISSING):
        source = RESOURCE_ROOT / relative_path
        target = vault / relative_path
        relative_name = relative_path.as_posix()
        if not source.is_file():
            raise FileNotFoundError(f"Missing Obsidian resource: {source}")

        if not target.exists():
            _atomic_copy(source, target)
            installed.append(relative_name)
            continue

        if relative_path in MANAGED_JSON:
            managed_value = _load_json(source)
            try:
                current_value = _load_json(target)
                desired_value = _merged_json(relative_path, current_value, managed_value)
            except (UnicodeDecodeError, ValueError, TypeError):
                if not refresh:
                    drifted.append(relative_name)
                    continue
                raise
            changed = current_value != desired_value
        else:
            desired_value = None
            changed = target.read_bytes() != source.read_bytes()

        if not changed:
            continue
        if not refresh:
            drifted.append(relative_name)
            continue

        if backup_dir is None:
            backup_dir = _new_backup_dir(vault)
        _backup_file(backup_dir, relative_path, target)
        if relative_path in MANAGED_JSON:
            _atomic_write_json(target, desired_value)
        else:
            _atomic_copy(source, target)
        installed.append(relative_name)

    return ObsidianInstallResult(
        vault_path=vault,
        installed=sorted(installed),
        drifted=sorted(drifted),
        backup_dir=backup_dir,
    )


def _result_json(result: ObsidianInstallResult) -> dict[str, Any]:
    payload = asdict(result)
    payload["vault_path"] = str(result.vault_path)
    payload["backup_dir"] = str(result.backup_dir) if result.backup_dir else None
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(prog="python -m app.obsidian")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--vault", required=True)
    install_parser.add_argument("--refresh", action="store_true")

    link_parser = subparsers.add_parser("link")
    link_parser.add_argument("--vault-name", required=True)
    link_parser.add_argument("--page-path", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        if args.command == "install":
            payload = {
                "operation": "install",
                **_result_json(ensure_obsidian_vault(args.vault, refresh=args.refresh)),
            }
        else:
            payload = {
                "operation": "link",
                "url": build_obsidian_uri(args.vault_name, args.page_path),
            }
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
