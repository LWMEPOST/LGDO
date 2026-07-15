from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from watchfiles import Change, awatch

from app.config import Settings
from app.wiki_markdown import (
    FileObservationInput,
    FrontmatterLimits,
    ObservationChanged as CaptureObservationChanged,
    capture_file_observation,
)


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


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


def _canonical_roots(vault_path: Path | str) -> tuple[Path, Path] | None:
    try:
        vault_root = Path(vault_path).resolve()
        wiki_root = vault_root / "wiki"
        if wiki_root.is_symlink() or _is_reparse_point(wiki_root):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return vault_root, wiki_root


def canonical_wiki_path(
    vault_path: Path | str,
    raw_path: Path | str,
) -> str | None:
    roots = _canonical_roots(vault_path)
    if roots is None:
        return None
    vault_root, wiki_root = roots

    try:
        raw_candidate = Path(raw_path)
        candidate = (
            raw_candidate
            if raw_candidate.is_absolute()
            else vault_root / raw_candidate
        )
        relative = candidate.relative_to(vault_root)
    except (OSError, RuntimeError, ValueError):
        return None

    parts = relative.parts
    if len(parts) < 2 or parts[0] != "wiki":
        return None
    if any(part.startswith(".") for part in parts[1:]):
        return None
    if Path(parts[-1]).suffix.lower() != ".md":
        return None

    current = wiki_root
    try:
        for component in parts[1:]:
            current /= component
            if current.is_symlink() or _is_reparse_point(current):
                return None

        resolved_wiki_root = wiki_root.resolve()
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_wiki_root)

        if candidate.exists() and not candidate.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None

    return "/".join(parts)


def iter_canonical_wiki_files(
    vault_path: Path | str,
    *,
    error_handler: Callable[[Path, Exception], None] | None = None,
) -> Iterator[Path]:
    roots = _canonical_roots(vault_path)
    if roots is None:
        return
    vault_root, wiki_root = roots
    try:
        if not wiki_root.is_dir():
            return
    except OSError as exc:
        if error_handler is not None:
            error_handler(wiki_root, exc)
        return

    stack = [wiki_root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as scanner:
                entries = sorted(
                    scanner,
                    key=lambda entry: entry.name,
                    reverse=True,
                )
        except OSError as exc:
            if error_handler is not None:
                error_handler(directory, exc)
            continue

        for entry in entries:
            if entry.name.startswith("."):
                continue
            path = Path(entry.path)
            try:
                if entry.is_symlink() or _is_reparse_point(path):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif (
                    entry.is_file(follow_symlinks=False)
                    and canonical_wiki_path(vault_root, path) is not None
                ):
                    yield path
            except OSError as exc:
                if error_handler is not None:
                    error_handler(path, exc)


@dataclass(frozen=True, order=True)
class VaultFsEvent:
    kind: str
    page_path: str


def normalize_watchfiles_batch(
    changes: set[tuple[Change, str]],
    vault_path: Path | str,
) -> list[VaultFsEvent]:
    roots = _canonical_roots(vault_path)
    if roots is None:
        return []
    vault_root, _wiki_root = roots
    grouped: dict[str, tuple[set[Change], set[Path]]] = {}

    for change, raw_path in changes:
        page_path = canonical_wiki_path(vault_root, raw_path)
        if page_path is None:
            continue
        raw_candidate = Path(raw_path)
        absolute_path = (
            raw_candidate
            if raw_candidate.is_absolute()
            else vault_root / raw_candidate
        )
        page_changes, paths = grouped.setdefault(page_path, (set(), set()))
        page_changes.add(change)
        paths.add(absolute_path)

    events: list[VaultFsEvent] = []
    for page_path in sorted(grouped):
        page_changes, paths = grouped[page_path]
        selected_path = min(paths, key=lambda path: path.as_posix())
        if not selected_path.exists():
            kind = "delete"
        elif Change.added in page_changes:
            kind = "add"
        else:
            kind = "modify"
        events.append(VaultFsEvent(kind, page_path))
    return events


VaultEventHandler = Callable[[list[VaultFsEvent]], Awaitable[None]]
VaultWatchFactory = Callable[..., AsyncIterator[set[tuple[Change, str]]]]


class VaultWatchAdapter:
    def __init__(
        self,
        settings: Settings,
        handler: VaultEventHandler,
        *,
        watch_factory: VaultWatchFactory = awatch,
        error_handler: Callable[[Exception], None] | None = None,
    ) -> None:
        self.settings = settings
        self.handler = handler
        self.watch_factory = watch_factory
        self.error_handler = error_handler or (lambda _exc: None)
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()

    async def run(self, max_batches: int | None = None) -> None:
        self._ready.set()
        watcher = self.watch_factory(
            self.settings.vault_path / "wiki",
            debounce=self.settings.vault_watch_debounce_ms,
            step=min(250, self.settings.vault_watch_debounce_ms),
            recursive=True,
        )

        if max_batches == 0:
            return

        handled = 0
        async for changes in watcher:
            try:
                events = normalize_watchfiles_batch(
                    changes,
                    self.settings.vault_path,
                )
                if not events:
                    continue
                await self.handler(events)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error_handler(exc)

            handled += 1
            if max_batches is not None and handled >= max_batches:
                return

    async def start(self) -> None:
        if self._task is not None:
            if not self._task.done():
                await self._ready.wait()
                return
            await self._task
            self._task = None

        self._ready.clear()
        self._task = asyncio.create_task(self.run(), name="vault-watchfiles")
        await self._ready.wait()
        await asyncio.sleep(0)
        if self._task.done():
            await self._task

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return

        cancellation_requested = task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not cancellation_requested or not task.cancelled():
                raise
        finally:
            if self._task is task and task.done():
                self._task = None
                self._ready.clear()


class UnstableVaultFile(RuntimeError):
    pass


class ObservationChanged(UnstableVaultFile):
    pass


async def _stat_signature(path: Path) -> tuple[int, int]:
    stat_result = await asyncio.to_thread(path.stat)
    return stat_result.st_size, stat_result.st_mtime_ns


async def wait_for_stable_observation(
    path: Path,
    limits: FrontmatterLimits,
    *,
    timeout_seconds: float,
    poll_interval: float = 0.05,
) -> FileObservationInput:
    deadline = time.monotonic() + timeout_seconds
    previous: tuple[int, int] | None = None

    while True:
        try:
            signature = await _stat_signature(path)
        except OSError as exc:
            raise UnstableVaultFile(
                f"file disappeared while stabilizing: {path}"
            ) from exc

        if signature == previous:
            try:
                observation = await asyncio.to_thread(
                    capture_file_observation,
                    path,
                    max_content_bytes=limits.max_file_bytes,
                    prefix_bytes=limits.max_prefix_bytes,
                )
            except CaptureObservationChanged as exc:
                previous = None
                if time.monotonic() >= deadline:
                    raise ObservationChanged(
                        f"file changed during capture: {path}"
                    ) from exc
                await asyncio.sleep(poll_interval)
                continue

            if (observation.size_bytes, observation.mtime_ns) == signature:
                return observation

            previous = None
            if time.monotonic() >= deadline:
                raise ObservationChanged(f"file changed during capture: {path}")
            await asyncio.sleep(poll_interval)
            continue

        if previous is not None and time.monotonic() >= deadline:
            raise UnstableVaultFile(
                f"file did not stabilize before timeout: {path}"
            )

        previous = signature
        await asyncio.sleep(poll_interval)
