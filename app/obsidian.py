from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote, urlencode
from uuid import uuid4


RESOURCE_ROOT = Path(__file__).resolve().parents[1] / "resources" / "obsidian-vault"
PROJECT_ROOT = RESOURCE_ROOT.parents[1]
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


class ObsidianConcurrencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObsidianInstallResult:
    vault_path: Path
    installed: list[str]
    drifted: list[str]
    backup_dir: Path | None


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _stage_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temp_path.open("xb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, _json_bytes(value))


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    temp_path = _stage_bytes(path, payload)
    try:
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _publish_staged_no_replace(staged: Path, target: Path) -> bool:
    try:
        os.link(staged, target)
        return True
    except FileExistsError:
        return False


def _copy_if_missing(source: Path, target: Path) -> bool:
    staged = _stage_bytes(target, source.read_bytes())
    try:
        return _publish_staged_no_replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _new_backup_dir(vault_path: Path) -> Path:
    backup_root = vault_path / ".lgdo" / "obsidian-backups"
    _safe_mkdirs(vault_path, backup_root)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = backup_root / f"{timestamp}-{uuid4().hex}"
    _require_safe_directory(vault_path, backup_root)
    backup_dir.mkdir(exist_ok=False)
    _require_safe_directory(vault_path, backup_dir)
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
    _safe_mkdirs(backup_dir, backup_path.parent)
    shutil.copy2(target, backup_path)


def _require_nonblank_vault_path(vault_path: str | os.PathLike[str]) -> None:
    raw_path = os.fspath(vault_path)
    if isinstance(raw_path, str) and not raw_path.strip():
        raise ValueError("Vault path must not be empty or whitespace")


def _resolve_safe_vault_path(vault_path: str | os.PathLike[str]) -> Path:
    _require_nonblank_vault_path(vault_path)
    vault = Path(vault_path).expanduser().resolve()
    dangerous_roots = {
        Path.cwd().resolve(),
        PROJECT_ROOT.resolve(),
        Path(vault.anchor).resolve(),
    }
    if vault in dangerous_roots:
        raise ValueError(f"Vault path is unsafe for asset installation: {vault}")
    return vault


def _is_reparse_point(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return bool(
        getattr(path_stat, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _require_safe_directory(root: Path, directory: Path) -> None:
    directory_stat = directory.lstat()
    if (
        directory.is_symlink()
        or _is_reparse_point(directory)
        or not stat.S_ISDIR(directory_stat.st_mode)
    ):
        raise ValueError(f"Refusing Obsidian asset directory reparse point: {directory}")
    try:
        directory.resolve(strict=True).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ValueError(f"Obsidian asset directory escapes the Vault: {directory}") from exc


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


def _require_safe_regular_file(path: Path, physical_root: Path) -> None:
    path_stat = path.lstat()
    if path.is_symlink() or _is_reparse_point(path) or not stat.S_ISREG(path_stat.st_mode):
        raise ValueError(f"Refusing unsafe Obsidian target: {path}")
    try:
        path.resolve(strict=True).relative_to(physical_root)
    except ValueError as exc:
        raise ValueError(f"Obsidian target escapes its physical root: {path}") from exc


def _require_safe_target(vault: Path, target: Path) -> None:
    physical_vault = vault.resolve(strict=True)
    current = vault
    for component in target.relative_to(vault).parts[:-1]:
        current /= component
        if not current.exists():
            continue
        if current.is_symlink() or _is_reparse_point(current) or not current.is_dir():
            raise ValueError(f"Refusing unsafe Obsidian target parent: {current}")
        try:
            current.resolve(strict=True).relative_to(physical_vault)
        except ValueError as exc:
            raise ValueError(f"Obsidian target parent escapes the Vault: {current}") from exc
    if target.exists():
        _require_safe_regular_file(target, physical_vault)


def _refresh_target(
    *,
    vault: Path,
    backup_dir: Path,
    relative_path: Path,
    target: Path,
    current_bytes: bytes,
    desired_bytes: bytes,
) -> None:
    staged = _stage_bytes(target, desired_bytes)
    backup_path = backup_dir / relative_path
    claim = backup_dir / ".claimed" / relative_path
    try:
        _require_safe_target(vault, target)
        _backup_file(backup_dir, relative_path, target)
        _atomic_write_bytes(backup_path, current_bytes)
        _safe_mkdirs(backup_dir, claim.parent)
        _require_safe_target(vault, target)
        os.replace(target, claim)
        _require_safe_regular_file(claim, backup_dir.resolve(strict=True))
        claimed_bytes = claim.read_bytes()
        if claimed_bytes != current_bytes:
            _copy_if_missing(claim, target)
            raise ObsidianConcurrencyError(
                f"Obsidian target changed during refresh: {relative_path.as_posix()}"
            )

        try:
            published = _publish_staged_no_replace(staged, target)
        except OSError:
            _copy_if_missing(claim, target)
            raise
        if not published:
            raise ObsidianConcurrencyError(
                f"Obsidian target changed during refresh: {relative_path.as_posix()}"
            )
    finally:
        staged.unlink(missing_ok=True)


def ensure_obsidian_vault(
    vault_path: str | os.PathLike[str],
    refresh: bool = False,
) -> ObsidianInstallResult:
    vault = _resolve_safe_vault_path(vault_path)
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

        _require_safe_target(vault, target)
        _safe_mkdirs(vault, target.parent)
        if not target.exists() and _copy_if_missing(source, target):
            installed.append(relative_name)
            continue

        _require_safe_target(vault, target)
        current_bytes = target.read_bytes()

        if relative_path in MANAGED_JSON:
            managed_value = _load_json(source)
            try:
                current_value = json.loads(current_bytes.decode("utf-8"))
                desired_value = _merged_json(relative_path, current_value, managed_value)
            except (UnicodeDecodeError, ValueError, TypeError):
                if not refresh:
                    drifted.append(relative_name)
                    continue
                raise
            changed = current_value != desired_value
            desired_bytes = _json_bytes(desired_value)
        else:
            desired_value = None
            desired_bytes = source.read_bytes()
            changed = current_bytes != desired_bytes

        if not changed:
            continue
        if not refresh:
            drifted.append(relative_name)
            continue

        if backup_dir is None:
            backup_dir = _new_backup_dir(vault)
        _refresh_target(
            vault=vault,
            backup_dir=backup_dir,
            relative_path=relative_path,
            target=target,
            current_bytes=current_bytes,
            desired_bytes=desired_bytes,
        )
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
            _require_nonblank_vault_path(args.vault)
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
