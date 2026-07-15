from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.wiki_markdown import (
    FileObservationInput,
    FrontmatterLimits,
    ObservationChanged as CaptureObservationChanged,
    capture_file_observation,
)


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
