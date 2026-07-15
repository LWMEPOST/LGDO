import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from watchfiles import Change

import app.vault_watcher as vault_watcher
from app.config import Settings
from app.vault_watcher import (
    ObservationChanged,
    UnstableVaultFile,
    VaultFsEvent,
    VaultWatchAdapter,
    canonical_wiki_path,
    iter_canonical_wiki_files,
    normalize_watchfiles_batch,
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


def test_hidden_and_parent_symlink_pages_are_filtered_from_live_and_startup(
    tmp_path,
):
    vault = tmp_path / "vault"
    valid = vault / "wiki" / "product" / "page.md"
    hidden = vault / "wiki" / ".hidden" / "secret.md"
    outside = tmp_path / "outside"
    outside_page = outside / "linked.md"
    valid.parent.mkdir(parents=True)
    hidden.parent.mkdir(parents=True)
    outside.mkdir()
    valid.write_text("valid", encoding="utf-8")
    hidden.write_text("hidden", encoding="utf-8")
    outside_page.write_text("outside", encoding="utf-8")

    changes = {
        (Change.added, str(valid)),
        (Change.added, str(hidden)),
    }
    linked_parent = vault / "wiki" / "linked"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pass
    else:
        changes.add((Change.added, str(linked_parent / outside_page.name)))

    assert normalize_watchfiles_batch(changes, vault) == [
        VaultFsEvent("add", "wiki/product/page.md")
    ]
    assert [path.as_posix() for path in iter_canonical_wiki_files(vault)] == [
        valid.as_posix()
    ]


def test_wiki_root_reparse_and_candidate_resolve_failure_are_closed(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    wiki = vault / "wiki"
    page = wiki / "page.md"
    wiki.mkdir(parents=True)
    page.write_text("page", encoding="utf-8")

    monkeypatch.setattr(
        vault_watcher,
        "_is_reparse_point",
        lambda path: Path(path) == wiki,
    )

    assert canonical_wiki_path(vault, page) is None
    assert list(iter_canonical_wiki_files(vault)) == []

    monkeypatch.setattr(vault_watcher, "_is_reparse_point", lambda _path: False)
    original_resolve = Path.resolve

    def fail_candidate_resolve(self, strict=False):
        if self == page:
            raise OSError("candidate cannot be resolved")
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fail_candidate_resolve)

    assert canonical_wiki_path(vault, page) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_junction_pages_are_filtered_from_live_and_startup(tmp_path):
    vault = tmp_path / "vault"
    wiki = vault / "wiki"
    outside = tmp_path / "outside"
    junction = wiki / "junction"
    outside_page = outside / "page.md"
    wiki.mkdir(parents=True)
    outside.mkdir()
    outside_page.write_text("outside", encoding="utf-8")

    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"mklink /J failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    assert normalize_watchfiles_batch(
        {(Change.added, str(junction / outside_page.name))},
        vault,
    ) == []
    assert list(iter_canonical_wiki_files(vault)) == []


@pytest.mark.asyncio
async def test_adapter_survives_handler_failure_and_processes_next_batch(tmp_path):
    vault = tmp_path / "vault"
    page = vault / "wiki" / "product" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("page", encoding="utf-8")
    handled = []
    errors = []
    calls = 0

    async def fake_awatch(*_args, **_kwargs):
        yield {(Change.added, str(page))}
        yield {(Change.modified, str(page))}

    async def handler(events):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("handler failed")
        handled.append(events)

    adapter = VaultWatchAdapter(
        Settings(_env_file=None, vault_path=vault),
        handler,
        watch_factory=fake_awatch,
        error_handler=errors.append,
    )

    await adapter.run(max_batches=2)

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert handled == [[VaultFsEvent("modify", "wiki/product/page.md")]]


@pytest.mark.asyncio
async def test_adapter_survives_normalization_failure_and_processes_next_batch(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    page = vault / "wiki" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("page", encoding="utf-8")
    handled = []
    errors = []
    real_normalize = normalize_watchfiles_batch
    calls = 0

    async def fake_awatch(*_args, **_kwargs):
        yield {(Change.added, str(page))}
        yield {(Change.modified, str(page))}

    def fail_once(changes, vault_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("normalization failed")
        return real_normalize(changes, vault_path)

    monkeypatch.setattr(vault_watcher, "normalize_watchfiles_batch", fail_once)

    async def handler(events):
        handled.append(events)

    adapter = VaultWatchAdapter(
        Settings(_env_file=None, vault_path=vault),
        handler,
        watch_factory=fake_awatch,
        error_handler=errors.append,
    )

    await adapter.run(max_batches=2)

    assert len(errors) == 1
    assert isinstance(errors[0], PermissionError)
    assert handled == [[VaultFsEvent("modify", "wiki/page.md")]]


def test_unreadable_directory_isolated_during_startup_inventory(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    good = vault / "wiki" / "good.md"
    unreadable = vault / "wiki" / "unreadable"
    bad = unreadable / "bad.md"
    unreadable.mkdir(parents=True)
    good.write_text("good", encoding="utf-8")
    bad.write_text("bad", encoding="utf-8")
    errors = []
    real_scandir = os.scandir

    def guarded_scandir(path):
        if Path(path) == unreadable:
            raise PermissionError("directory is unreadable")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)

    assert [
        path.as_posix()
        for path in iter_canonical_wiki_files(
            vault,
            error_handler=lambda path, exc: errors.append((path, exc)),
        )
    ] == [good.as_posix()]
    assert len(errors) == 1
    assert errors[0][0] == unreadable
    assert isinstance(errors[0][1], PermissionError)
