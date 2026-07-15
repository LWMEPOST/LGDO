import asyncio

import pytest

from app.vault_watcher import (
    ObservationChanged,
    UnstableVaultFile,
    wait_for_stable_observation,
)
from app.wiki_markdown import (
    FrontmatterLimits,
    capture_file_observation,
    parse_wiki_bytes,
    render_managed_frontmatter,
)


def test_stable_observation_preserves_comments_line_endings_and_raw_hash(tmp_path):
    page = tmp_path / "page.md"
    raw = (
        b"---\r\n"
        b"title: Demo\r\n"
        b"owner: Alice # keep owner comment\r\n"
        b"---\r\n\r\n"
        b"# Demo\r\n\r\n"
        b"Human body.\r\n"
    )
    page.write_bytes(raw)
    limits = FrontmatterLimits(
        max_file_bytes=1024 * 1024,
        max_prefix_bytes=64 * 1024,
    )

    observation = asyncio.run(
        wait_for_stable_observation(
            page,
            limits,
            timeout_seconds=0.2,
            poll_interval=0,
        )
    )
    document = parse_wiki_bytes(observation.content_bytes)
    rendered = render_managed_frontmatter(
        document,
        page_id="page_1",
        revision_id="wrev_1",
        write_token="write_1",
    )

    assert observation.content_bytes == raw
    assert observation.content_truncated is False
    assert b"# keep owner comment" in rendered
    assert b"\r\n" in rendered
    assert b"\n" not in rendered.replace(b"\r\n", b"")
    assert len(observation.file_hash) == 64


def test_sparse_oversized_observation_keeps_only_bounded_prefix(tmp_path):
    page = tmp_path / "large.md"
    prefix = b"a" * (64 * 1024)
    with page.open("wb") as handle:
        handle.write(prefix)
        handle.seek((8 * 1024 * 1024) - 1)
        handle.write(b"\0")
    limits = FrontmatterLimits(
        max_file_bytes=1024 * 1024,
        max_prefix_bytes=64 * 1024,
    )

    observation = asyncio.run(
        wait_for_stable_observation(
            page,
            limits,
            timeout_seconds=0.2,
            poll_interval=0,
        )
    )

    assert observation.content_bytes is None
    assert observation.content_truncated is True
    assert observation.content_prefix == prefix
    assert len(observation.file_hash) == 64


def test_unstable_file_times_out_before_capture(tmp_path, monkeypatch):
    page = tmp_path / "page.md"
    page.write_bytes(b"initial")
    calls = 0

    async def changing_signature(_path):
        nonlocal calls
        calls += 1
        return calls, calls

    monkeypatch.setattr("app.vault_watcher._stat_signature", changing_signature)

    with pytest.raises(UnstableVaultFile, match="did not stabilize"):
        asyncio.run(
            wait_for_stable_observation(
                page,
                FrontmatterLimits(),
                timeout_seconds=0,
                poll_interval=0,
            )
        )


def test_file_change_between_stat_and_capture_is_not_returned_as_stable(
    tmp_path, monkeypatch
):
    page = tmp_path / "page.md"
    page.write_bytes(b"initial")

    def rewrite_before_capture(path, **kwargs):
        path.write_bytes(b"changed before capture")
        return capture_file_observation(path, **kwargs)

    monkeypatch.setattr(
        "app.vault_watcher.capture_file_observation",
        rewrite_before_capture,
    )

    with pytest.raises(ObservationChanged, match="changed during capture"):
        asyncio.run(
            wait_for_stable_observation(
                page,
                FrontmatterLimits(),
                timeout_seconds=0,
                poll_interval=0,
            )
        )
