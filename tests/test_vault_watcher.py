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


@pytest.mark.asyncio
async def test_adapter_start_marks_ready_after_first_anext_has_started(tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    ready_states = []
    anext_task = None
    adapter = None

    class BlockingWatch:
        def __aiter__(self):
            return self

        async def __anext__(self):
            nonlocal anext_task
            anext_task = asyncio.current_task()
            ready_states.append(adapter._ready.is_set())
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise StopAsyncIteration

    def blocking_watch(*_args, **_kwargs):
        return BlockingWatch()

    async def handler(_events):
        pass

    adapter = VaultWatchAdapter(
        Settings(_env_file=None, vault_path=vault),
        handler,
        watch_factory=blocking_watch,
    )

    await adapter.start()
    assert entered.is_set()
    await adapter.stop()

    assert ready_states == [False]
    assert cancelled.is_set()
    assert anext_task is not None and anext_task.done()
    assert adapter._task is None


@pytest.mark.asyncio
async def test_adapter_start_propagates_failure_from_first_anext(tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    entered = asyncio.Event()

    class FailingWatch:
        def __aiter__(self):
            return self

        async def __anext__(self):
            entered.set()
            raise RuntimeError("first iteration failed")

    def failing_watch(*_args, **_kwargs):
        return FailingWatch()

    async def handler(_events):
        pass

    adapter = VaultWatchAdapter(
        Settings(_env_file=None, vault_path=vault),
        handler,
        watch_factory=failing_watch,
    )

    with pytest.raises(RuntimeError, match="first iteration failed"):
        await adapter.start()

    assert entered.is_set()
    assert adapter._task is None
    assert not adapter._ready.is_set()


@pytest.mark.asyncio
async def test_adapter_start_replaces_watcher_that_ended_after_ready(tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    first_entered = asyncio.Event()
    finish_first = asyncio.Event()
    second_entered = asyncio.Event()
    second_stopped = asyncio.Event()
    factory_calls = 0

    async def first_watch():
        first_entered.set()
        await finish_first.wait()
        return
        yield set()

    async def second_watch():
        try:
            second_entered.set()
            await asyncio.Future()
            yield set()
        finally:
            second_stopped.set()

    def watch_factory(*_args, **_kwargs):
        nonlocal factory_calls
        factory_calls += 1
        return first_watch() if factory_calls == 1 else second_watch()

    async def handler(_events):
        pass

    adapter = VaultWatchAdapter(
        Settings(_env_file=None, vault_path=vault),
        handler,
        watch_factory=watch_factory,
    )

    await adapter.start()
    assert first_entered.is_set()
    first_task = adapter._task
    assert first_task is not None
    finish_first.set()
    await asyncio.wait_for(asyncio.shield(first_task), timeout=1)

    await adapter.start()
    try:
        assert factory_calls == 2
        assert second_entered.is_set()
        assert adapter._task is not None
        assert adapter._task is not first_task
        assert not adapter._task.done()
    finally:
        await adapter.stop()

    assert second_stopped.is_set()


@pytest.mark.asyncio
async def test_adapter_start_rejects_watcher_that_ends_during_startup(tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)

    async def empty_watch():
        return
        yield set()

    def watch_factory(*_args, **_kwargs):
        return empty_watch()

    async def handler(_events):
        pass

    adapter = VaultWatchAdapter(
        Settings(_env_file=None, vault_path=vault),
        handler,
        watch_factory=watch_factory,
    )

    with pytest.raises(RuntimeError, match="watcher stopped during startup"):
        await adapter.start()

    assert adapter._task is None
    assert not adapter._ready.is_set()


@pytest.mark.asyncio
async def test_adapter_accepts_anext_returning_future(tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    loop = asyncio.get_running_loop()
    first_batch = None
    closed = asyncio.Event()

    class FutureWatch:
        def __aiter__(self):
            return self

        def __anext__(self):
            nonlocal first_batch
            first_batch = loop.create_future()
            return first_batch

        async def aclose(self):
            closed.set()

    def future_watch(*_args, **_kwargs):
        return FutureWatch()

    async def handler(_events):
        pass

    adapter = VaultWatchAdapter(
        Settings(_env_file=None, vault_path=vault),
        handler,
        watch_factory=future_watch,
    )

    try:
        await adapter.start()
        assert first_batch is not None
        assert not first_batch.done()
    finally:
        await adapter.stop()

    assert first_batch is not None and first_batch.cancelled()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_adapter_start_recovers_after_immediate_watcher_failure(tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)

    def failing_watch(*_args, **_kwargs):
        raise RuntimeError("watcher startup failed")

    async def handler(_events):
        pass

    adapter = VaultWatchAdapter(
        Settings(_env_file=None, vault_path=vault),
        handler,
        watch_factory=failing_watch,
    )

    with pytest.raises(RuntimeError, match="watcher startup failed"):
        await adapter.start()

    assert adapter._task is None
    assert not adapter._ready.is_set()

    watcher_started = asyncio.Event()
    watcher_stopped = asyncio.Event()

    async def blocking_watch(*_args, **_kwargs):
        try:
            watcher_started.set()
            await asyncio.Future()
            yield set()
        finally:
            watcher_stopped.set()

    adapter.watch_factory = blocking_watch
    await adapter.start()

    assert watcher_started.is_set()
    assert adapter._task is not None
    assert not adapter._task.done()

    await adapter.stop()

    assert watcher_stopped.is_set()
    assert adapter._task is None
    assert not adapter._ready.is_set()


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
