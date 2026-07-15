from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from app import vault_writer as vault_writer_module
from app import wiki_revisions as wiki_revisions_module
from app.config import Settings, get_settings
from app.db import connect_app, init_app_db
from app.vault_writer import (
    AtomicVaultWriter,
    IntentExecutor,
    TargetChanged,
    VaultWriteError,
)
from app.wiki_markdown import capture_file_observation, compute_file_hash
from app.wiki_revisions import (
    CompileCandidateCommand,
    ManualSaveCommand,
    ResolveConflictCommand,
    RevisionConflict,
    WikiRevisionService,
)


def _claim_intent_process(
    settings_data,
    intent_id: str,
    owner: str,
    events,
    start_event,
) -> None:
    try:
        settings = Settings.model_validate(settings_data)
        events.put((owner, "ready"))
        if not start_event.wait(15):
            raise TimeoutError("claim start event timed out")
        claimed = IntentExecutor(settings, owner=owner).claim(
            intent_id,
            lease_seconds=120,
        )
        events.put((owner, "claim", claimed))
    except BaseException as exc:
        events.put((owner, "error", type(exc).__name__, str(exc)))
        raise


def _hold_intent_os_lock(
    settings_data,
    intent_id: str,
    owner: str,
    events,
    held_event,
    release_event,
) -> None:
    try:
        settings = Settings.model_validate(settings_data)
        executor = IntentExecutor(settings, owner=owner)
        claimed = executor.claim(intent_id, lease_seconds=120)
        events.put(("holder", "claim", claimed))
        if not claimed:
            raise RuntimeError("holder failed to claim intent")
        lock_path = executor.writer.lock_path(intent_id)
        with vault_writer_module.portalocker.Lock(
            str(lock_path),
            mode="a+",
            timeout=0,
        ):
            events.put(("holder", "locked", str(lock_path)))
            held_event.set()
            if not release_event.wait(30):
                raise TimeoutError("holder release event timed out")
    except BaseException as exc:
        events.put(("holder", "error", type(exc).__name__, str(exc)))
        raise


def _execute_with_os_lock_probe(
    settings_data,
    intent_id: str,
    owner: str,
    events,
    held_event,
) -> None:
    real_lock = vault_writer_module.portalocker.Lock
    try:
        settings = Settings.model_validate(settings_data)
        if not held_event.wait(15):
            raise TimeoutError("holder did not acquire OS lock")

        class ProbedLock:
            def __init__(self, *args, **kwargs):
                self.inner = real_lock(*args, **kwargs)
                self.path = str(args[0])

            def __enter__(self):
                events.put(("contender", "lock_attempt", self.path))
                value = self.inner.__enter__()
                events.put(("contender", "lock_entered", self.path))
                return value

            def __exit__(self, *exc):
                return self.inner.__exit__(*exc)

        vault_writer_module.portalocker.Lock = ProbedLock
        result = IntentExecutor(settings, owner=owner).execute(
            intent_id,
            lease_seconds=120,
        )
        events.put(("contender", "result", result))
    except BaseException as exc:
        events.put(("contender", "error", type(exc).__name__, str(exc)))
        raise
    finally:
        vault_writer_module.portalocker.Lock = real_lock


def _join_or_terminate(process, timeout: float = 10) -> None:
    if process.pid is None and process.exitcode is None:
        return

    errors = []

    def attempt(action) -> None:
        try:
            action()
        except Exception as exc:
            errors.append(exc)

    def is_alive() -> bool:
        try:
            return process.is_alive()
        except Exception as exc:
            errors.append(exc)
            return True

    attempt(lambda: process.join(timeout))
    if not is_alive():
        if errors:
            raise errors[0]
        return

    attempt(process.terminate)
    attempt(lambda: process.join(timeout))
    if is_alive():
        kill = getattr(process, "kill", None)
        if callable(kill):
            attempt(kill)
            attempt(lambda: process.join(timeout))
    if is_alive():
        survivor = AssertionError(
            f"process {process.pid} is still alive after terminate/kill"
        )
        if errors:
            raise survivor from errors[0]
        raise survivor
    if errors:
        raise errors[0]


@pytest.mark.parametrize("kill_succeeds", [True, False])
def test_join_or_terminate_escalates_and_reports_survivor(kill_succeeds):
    class FakeProcess:
        pid = 42
        exitcode = None

        def __init__(self):
            self.alive = True
            self.calls = []

        def join(self, timeout):
            self.calls.append(("join", timeout))

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.calls.append(("terminate", None))

        def kill(self):
            self.calls.append(("kill", None))
            if kill_succeeds:
                self.alive = False

    process = FakeProcess()
    if kill_succeeds:
        _join_or_terminate(process, timeout=3)
    else:
        with pytest.raises(AssertionError, match="process 42 is still alive"):
            _join_or_terminate(process, timeout=3)

    assert process.calls == [
        ("join", 3),
        ("terminate", None),
        ("join", 3),
        ("kill", None),
        ("join", 3),
    ]


def test_writer_captures_old_inode_installs_without_replace_and_retains_backup(tmp_path: Path):
    vault = tmp_path / "vault"
    target = vault / "wiki/product/faq/demo.md"
    target.parent.mkdir(parents=True)
    old = b"old human bytes"
    new = b"new managed bytes"
    target.write_bytes(old)
    writer = AtomicVaultWriter(vault)

    captured = writer.capture(
        intent_id="wint_1", target_path="wiki/product/faq/demo.md",
        expected_file_hash=compute_file_hash(old),
    )
    installed = writer.install(
        intent_id="wint_1", target_path="wiki/product/faq/demo.md", content=new,
    )

    assert captured.backup_path.read_bytes() == old
    assert installed.target_hash == compute_file_hash(new)
    assert target.read_bytes() == new
    assert captured.backup_path.exists()


def test_capture_durably_syncs_backup_and_source_destination_directories(
    tmp_path: Path,
    monkeypatch,
):
    vault = tmp_path / "vault"
    target_path = "wiki/product/faq/demo.md"
    target = vault / target_path
    target.parent.mkdir(parents=True)
    original = b"old human bytes"
    target.write_bytes(original)
    writer = AtomicVaultWriter(vault)
    events: list[tuple[str, Path, bool | None]] = []
    original_link = os.link
    original_rename = os.rename

    def tracked_link(source, destination):
        events.append(("link", Path(destination), None))
        return original_link(source, destination)

    def tracked_rename(source, destination):
        events.append(("rename", Path(destination), None))
        return original_rename(source, destination)

    monkeypatch.setattr("app.vault_writer.os.link", tracked_link)
    monkeypatch.setattr("app.vault_writer.os.rename", tracked_rename)
    monkeypatch.setattr(
        writer,
        "_fsync_existing_file",
        lambda path: events.append(("file", Path(path), None)),
        raising=False,
    )
    monkeypatch.setattr(
        writer,
        "_fsync_directory",
        lambda path, *, strict=False: events.append(
            ("directory", Path(path), strict)
        ),
    )

    captured = writer.capture(
        intent_id="wint_durable_capture",
        target_path=target_path,
        expected_file_hash=compute_file_hash(original),
    )

    claim = writer.capture_claim_path("wint_durable_capture")
    link_index = events.index(("link", captured.backup_path, None))
    assert events[link_index:] == [
        ("link", captured.backup_path, None),
        ("file", captured.backup_path, None),
        ("directory", captured.backup_path.parent, True),
        ("rename", claim, None),
        ("file", claim, None),
        ("directory", claim.parent, True),
        ("directory", target.parent, True),
        ("directory", claim.parent, True),
    ]


def test_capture_existing_backup_is_durable_before_return(
    tmp_path: Path,
    monkeypatch,
):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = AtomicVaultWriter(vault)
    backup = writer.backup_path("wint_existing_backup")
    original = b"already captured bytes"
    backup.write_bytes(original)
    events: list[tuple[str, Path, bool | None]] = []
    monkeypatch.setattr(
        writer,
        "_fsync_existing_file",
        lambda path: events.append(("file", Path(path), None)),
    )
    monkeypatch.setattr(
        writer,
        "_fsync_directory",
        lambda path, *, strict=False: events.append(
            ("directory", Path(path), strict)
        ),
    )

    captured = writer.capture(
        intent_id="wint_existing_backup",
        target_path="wiki/product/faq/demo.md",
        expected_file_hash=compute_file_hash(original),
    )

    assert captured.backup_path == backup
    assert events == [
        ("file", backup, None),
        ("directory", backup.parent, True),
    ]


def test_install_durably_creates_parent_chains_and_syncs_link(
    tmp_path: Path,
    monkeypatch,
):
    vault = tmp_path / "vault"
    vault.mkdir()
    target_path = "wiki/product/faq/demo.md"
    target = vault / target_path
    writer = AtomicVaultWriter(vault)
    events: list[tuple[str, Path, bool | None]] = []
    original_link = os.link

    def tracked_link(source, destination):
        events.append(("link", Path(destination), None))
        return original_link(source, destination)

    monkeypatch.setattr("app.vault_writer.os.link", tracked_link)
    monkeypatch.setattr(
        writer,
        "_fsync_directory",
        lambda path, *, strict=False: events.append(
            ("directory", Path(path), strict)
        ),
    )

    writer.install(
        intent_id="wint_durable_install",
        target_path=target_path,
        content=b"new managed bytes",
    )

    link_index = events.index(("link", target, None))
    required_before_link = [
        ("directory", vault, True),
        ("directory", vault / ".lgdo", True),
        ("directory", writer.pending_root, True),
        (
            "directory",
            writer.staged_path("wint_durable_install").parent,
            True,
        ),
        ("directory", vault / "wiki", True),
        ("directory", vault / "wiki/product", True),
    ]
    for event in required_before_link:
        assert event in events[:link_index]
    assert events[link_index + 1] == ("directory", target.parent, True)


def test_install_existing_staged_file_is_durable_before_link(
    tmp_path: Path,
    monkeypatch,
):
    vault = tmp_path / "vault"
    target = vault / "wiki/product/faq/demo.md"
    target.parent.mkdir(parents=True)
    writer = AtomicVaultWriter(vault)
    staged = writer.staged_path("wint_existing_staged")
    content = b"already staged bytes"
    staged.write_bytes(content)
    events: list[tuple[str, Path, bool | None]] = []
    original_link = os.link

    def tracked_link(source, destination):
        events.append(("link", Path(destination), None))
        return original_link(source, destination)

    monkeypatch.setattr("app.vault_writer.os.link", tracked_link)
    monkeypatch.setattr(
        writer,
        "_fsync_existing_file",
        lambda path: events.append(("file", Path(path), None)),
    )
    monkeypatch.setattr(
        writer,
        "_fsync_directory",
        lambda path, *, strict=False: events.append(
            ("directory", Path(path), strict)
        ),
    )

    writer.install(
        intent_id="wint_existing_staged",
        target_path="wiki/product/faq/demo.md",
        content=content,
    )

    link_index = events.index(("link", target, None))
    assert events[:link_index] == [
        ("file", staged, None),
        ("directory", staged.parent, True),
    ]


@pytest.mark.parametrize("operation", ["capture", "install"])
def test_writer_mutation_directory_fsync_is_strict(
    tmp_path: Path,
    monkeypatch,
    operation: str,
):
    vault = tmp_path / "vault"
    target_path = "wiki/product/faq/demo.md"
    target = vault / target_path
    target.parent.mkdir(parents=True)
    writer = AtomicVaultWriter(vault)

    def fail_directory_sync(*args, **kwargs):
        raise OSError("directory fsync unsupported")

    monkeypatch.setattr(
        vault_writer_module,
        "_sync_directory",
        fail_directory_sync,
    )
    if operation == "capture":
        old = b"old human bytes"
        target.write_bytes(old)
        with pytest.raises(OSError, match="directory fsync unsupported"):
            writer.capture(
                intent_id="wint_strict_capture",
                target_path=target_path,
                expected_file_hash=compute_file_hash(old),
            )
        assert target.read_bytes() == old
    else:
        new = b"new managed bytes"
        with pytest.raises(OSError, match="directory fsync unsupported"):
            writer.install(
                intent_id="wint_strict_install",
                target_path=target_path,
                content=new,
            )
        assert not target.exists()


def test_writer_directory_fsync_strict_mode_propagates_errors(
    tmp_path: Path,
    monkeypatch,
):
    writer = AtomicVaultWriter(tmp_path / "vault")

    def fail_directory_sync(*args, **kwargs):
        raise OSError("strict directory sync failure")

    monkeypatch.setattr(
        vault_writer_module,
        "_sync_directory",
        fail_directory_sync,
    )

    with pytest.raises(OSError, match="strict directory sync failure"):
        writer._fsync_directory(tmp_path, strict=True)


@pytest.mark.parametrize("failure_point", ["open", "fsync"])
def test_posix_directory_sync_propagates_os_errors(
    tmp_path: Path,
    monkeypatch,
    failure_point: str,
):
    closed: list[int] = []

    def fail(*args, **kwargs):
        raise OSError(f"posix directory {failure_point} failure")

    if failure_point == "open":
        monkeypatch.setattr("app.vault_writer.os.open", fail)
    else:
        monkeypatch.setattr("app.vault_writer.os.open", lambda *args, **kwargs: 42)
        monkeypatch.setattr("app.vault_writer.os.fsync", fail)
        monkeypatch.setattr(
            "app.vault_writer.os.close",
            lambda descriptor: closed.append(descriptor),
        )

    with pytest.raises(OSError, match=f"posix directory {failure_point} failure"):
        vault_writer_module._sync_directory_posix(tmp_path)
    assert closed == ([42] if failure_point == "fsync" else [])


@pytest.mark.skipif(os.name != "nt", reason="Windows directory flush smoke test")
def test_writer_directory_fsync_strict_mode_syncs_real_windows_directory(
    tmp_path: Path,
):
    AtomicVaultWriter._fsync_directory(tmp_path, strict=True)


def test_writer_never_overwrites_target_created_after_capture(tmp_path: Path):
    vault = tmp_path / "vault"
    target = vault / "wiki/product/faq/demo.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    writer = AtomicVaultWriter(vault)
    writer.capture("wint_2", "wiki/product/faq/demo.md", compute_file_hash(b"old"))
    target.write_bytes(b"obsidian wins")

    with pytest.raises(TargetChanged):
        writer.install("wint_2", "wiki/product/faq/demo.md", b"managed")
    assert target.read_bytes() == b"obsidian wins"
    assert writer.backup_path("wint_2").read_bytes() == b"old"


def test_capture_backup_destination_race_preserves_both_paths(
    tmp_path: Path,
    monkeypatch,
):
    vault = tmp_path / "vault"
    target_path = "wiki/product/faq/demo.md"
    target = vault / target_path
    target.parent.mkdir(parents=True)
    original = b"original target bytes"
    raced_backup = b"late backup bytes"
    target.write_bytes(original)
    writer = AtomicVaultWriter(vault)
    backup = writer.backup_path("wint_backup_race")
    original_link = os.link
    original_rename = os.rename

    def create_raced_backup():
        if not backup.exists():
            backup.write_bytes(raced_backup)

    def race_link(source, destination, *args, **kwargs):
        if Path(destination) == backup:
            create_raced_backup()
        return original_link(source, destination, *args, **kwargs)

    def race_rename(source, destination, *args, **kwargs):
        if Path(destination) == backup:
            create_raced_backup()
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr("app.vault_writer.os.link", race_link)
    monkeypatch.setattr("app.vault_writer.os.rename", race_rename)

    with pytest.raises(TargetChanged):
        writer.capture(
            "wint_backup_race",
            target_path,
            compute_file_hash(original),
        )

    assert target.read_bytes() == original
    assert backup.read_bytes() == raced_backup


def test_capture_source_replacement_preserves_original_and_late_bytes(
    tmp_path: Path,
    monkeypatch,
):
    vault = tmp_path / "vault"
    target_path = "wiki/product/faq/demo.md"
    target = vault / target_path
    target.parent.mkdir(parents=True)
    original = b"original target bytes"
    late = b"late replacement bytes"
    displaced = target.with_name("displaced-before-capture.md")
    target.write_bytes(original)
    writer = AtomicVaultWriter(vault)
    backup = writer.backup_path("wint_source_race")
    original_rename = os.rename
    replaced = False

    def replace_source_before_claim(source, destination, *args, **kwargs):
        nonlocal replaced
        source = Path(source)
        if source == target and not replaced:
            replaced = True
            target.replace(displaced)
            target.write_bytes(late)
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        "app.vault_writer.os.rename",
        replace_source_before_claim,
    )

    with pytest.raises(TargetChanged):
        writer.capture(
            "wint_source_race",
            target_path,
            compute_file_hash(original),
        )

    assert target.read_bytes() == late
    assert backup.read_bytes() == original
    assert displaced.read_bytes() == original
    claim = backup.parent / "capture-claim.md"
    assert claim.read_bytes() == late


def test_capture_source_replacement_second_collision_is_observed_fail_closed(
    wiki_intent_fixture,
    monkeypatch,
):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="capture-source-double-race")
    with connect_app(settings) as conn:
        intent = conn.execute(
            "SELECT target_path,page_id FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
    target = settings.vault_path / intent["target_path"]
    original = target.read_bytes()
    late = _valid_wiki_variant(original, "Late claimed replacement.")
    second = _valid_wiki_variant(original, "Second target collision.")
    displaced = target.with_name("displaced-before-double-race.md")
    backup = executor.writer.backup_path(intent_id)
    claim = backup.parent / "capture-claim.md"
    original_rename = os.rename
    original_link = os.link
    replaced = False

    def replace_source_before_claim(source, destination, *args, **kwargs):
        nonlocal replaced
        source = Path(source)
        if source == target and not replaced:
            replaced = True
            target.replace(displaced)
            target.write_bytes(late)
        return original_rename(source, destination, *args, **kwargs)

    def collide_during_restore(source, destination, *args, **kwargs):
        if Path(source) == claim and Path(destination) == target:
            target.write_bytes(second)
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        "app.vault_writer.os.rename",
        replace_source_before_claim,
    )
    monkeypatch.setattr(
        "app.vault_writer.os.link",
        collide_during_restore,
    )

    first = executor.execute(intent_id)
    assert first is not None
    assert first.intent_status == "recovery_required"
    assert target.read_bytes() == second
    assert backup.read_bytes() == original
    assert claim.read_bytes() == late

    recovered = executor.reconcile_one(intent_id)

    with connect_app(settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (intent["page_id"],),
        ).fetchone()
        stored_intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
        observations = conn.execute(
            """
            SELECT file_hash,content_bytes FROM wiki_file_observations
            WHERE page_id=? AND file_hash IN (?,?) ORDER BY file_hash
            """,
            (
                intent["page_id"],
                compute_file_hash(late),
                compute_file_hash(second),
            ),
        ).fetchall()
    assert recovered.intent_status == "recovery_required"
    assert recovered.successor_intent_id is None
    assert len(recovered.observation_ids) == 2
    assert stored_intent["status"] == "recovery_required"
    assert page["pending_write_intent_id"] == intent_id
    assert page["lifecycle_status"] == "invalid"
    assert {bytes(row["content_bytes"]) for row in observations} == {
        late,
        second,
    }


def test_capture_claim_samefile_target_is_deduplicated_during_recovery(
    wiki_intent_fixture,
    monkeypatch,
):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="capture-claim-dedup")
    with connect_app(settings) as conn:
        intent = conn.execute(
            "SELECT target_path,page_id FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
    target = settings.vault_path / intent["target_path"]
    original = target.read_bytes()
    late = _valid_wiki_variant(original, "Late replacement for dedup.")
    displaced = target.with_name("displaced-before-dedup.md")
    backup = executor.writer.backup_path(intent_id)
    claim = backup.parent / "capture-claim.md"
    original_rename = os.rename
    replaced = False

    def replace_source_before_claim(source, destination, *args, **kwargs):
        nonlocal replaced
        source = Path(source)
        if source == target and not replaced:
            replaced = True
            target.replace(displaced)
            target.write_bytes(late)
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        "app.vault_writer.os.rename",
        replace_source_before_claim,
    )

    first = executor.execute(intent_id)
    assert first is not None
    assert first.intent_status == "recovery_required"
    assert os.path.samefile(claim, target)

    recovered = executor.reconcile_one(intent_id)

    with connect_app(settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (intent["page_id"],),
        ).fetchone()
    assert recovered.intent_status == "superseded"
    assert recovered.current_kind == "external_target"
    assert recovered.successor_intent_id is not None
    assert len(recovered.observation_ids) == 1
    assert page["pending_write_intent_id"] == recovered.successor_intent_id
    assert claim.read_bytes() == late


def test_capture_claim_only_unknown_is_observed_and_kept_fail_closed(
    wiki_intent_fixture,
):
    settings, intent_id = wiki_intent_fixture
    writer = AtomicVaultWriter(settings.vault_path)
    with connect_app(settings) as conn:
        intent = conn.execute(
            "SELECT target_path,page_id FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
    target = settings.vault_path / intent["target_path"]
    expected = target.read_bytes()
    backup = writer.backup_path(intent_id)
    claim = writer.capture_claim_path(intent_id)
    backup.write_bytes(expected)
    claim_bytes = _valid_wiki_variant(
        expected,
        "Distinct hidden capture claim.",
    )
    claim.write_bytes(claim_bytes)
    with connect_app(settings) as conn:
        conn.execute(
            """
            UPDATE vault_write_intents
            SET status='recovery_required',executor_owner=NULL,
                lease_expires_at='t0'
            WHERE id=?
            """,
            (intent_id,),
        )

    recovered = IntentExecutor(
        settings,
        owner="capture-claim-only-recovery",
    ).reconcile_one(
        intent_id,
        now=datetime.now(timezone.utc) + timedelta(seconds=1),
    )

    with connect_app(settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (intent["page_id"],),
        ).fetchone()
        stored_intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
        observation = conn.execute(
            """
            SELECT * FROM wiki_file_observations
            WHERE page_id=? AND file_hash=?
            """,
            (intent["page_id"], compute_file_hash(claim_bytes)),
        ).fetchone()
    assert recovered.intent_status == "recovery_required"
    assert recovered.current_kind == "unchanged"
    assert recovered.successor_intent_id is None
    assert recovered.observation_ids == (observation["id"],)
    assert stored_intent["status"] == "recovery_required"
    assert page["pending_write_intent_id"] == intent_id
    assert page["lifecycle_status"] == "invalid"
    assert bytes(observation["content_bytes"]) == claim_bytes
    assert claim.read_bytes() == claim_bytes


def test_capture_replay_removes_claim_that_is_samefile_as_backup(
    tmp_path: Path,
    monkeypatch,
):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "wiki/product/faq/missing.md"
    target.parent.mkdir(parents=True)
    writer = AtomicVaultWriter(vault)
    backup = writer.backup_path("wint_duplicate_claim")
    claim = writer.capture_claim_path("wint_duplicate_claim")
    original = b"durable captured bytes"
    backup.write_bytes(original)
    os.link(backup, claim)
    events: list[tuple[str, Path, bool | None]] = []
    original_unlink = Path.unlink
    monkeypatch.setattr(
        writer,
        "_fsync_existing_file",
        lambda path: events.append(("file", Path(path), None)),
    )
    monkeypatch.setattr(
        writer,
        "_fsync_directory",
        lambda path, *, strict=False: events.append(
            ("directory", Path(path), strict)
        ),
    )

    def tracked_unlink(path, *args, **kwargs):
        if path == claim:
            events.append(("unlink", Path(path), None))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", tracked_unlink)

    captured = writer.capture(
        "wint_duplicate_claim",
        "wiki/product/faq/missing.md",
        compute_file_hash(original),
    )

    assert captured.backup_path == backup
    assert backup.read_bytes() == original
    assert not claim.exists()
    assert events == [
        ("file", backup, None),
        ("directory", backup.parent, True),
        ("directory", target.parent, True),
        ("unlink", claim, None),
        ("directory", claim.parent, True),
    ]


@pytest.mark.parametrize("collision", [False, True])
def test_capture_replay_relinks_distinct_claim_before_recovery(
    wiki_intent_fixture,
    monkeypatch,
    collision,
):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner=f"stale-claim-{collision}")
    with connect_app(settings) as conn:
        intent = conn.execute(
            "SELECT target_path,page_id FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
    target = settings.vault_path / intent["target_path"]
    expected = target.read_bytes()
    late = _valid_wiki_variant(expected, "Stale distinct claim.")
    second = _valid_wiki_variant(expected, "Stale claim target collision.")
    backup = executor.writer.backup_path(intent_id)
    claim = executor.writer.capture_claim_path(intent_id)
    backup.write_bytes(expected)
    claim.write_bytes(late)
    target.unlink()
    original_link = os.link

    def maybe_collide(source, destination, *args, **kwargs):
        if (
            collision
            and Path(source) == claim
            and Path(destination) == target
        ):
            target.write_bytes(second)
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr("app.vault_writer.os.link", maybe_collide)

    first = executor.execute(intent_id)
    assert first is not None
    assert first.intent_status == "recovery_required"
    assert target.read_bytes() == (second if collision else late)
    assert claim.read_bytes() == late
    if not collision:
        assert os.path.samefile(claim, target)

    recovered = executor.reconcile_one(intent_id)

    with connect_app(settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (intent["page_id"],),
        ).fetchone()
    if collision:
        assert recovered.intent_status == "recovery_required"
        assert recovered.successor_intent_id is None
        assert len(recovered.observation_ids) == 2
        assert page["pending_write_intent_id"] == intent_id
        assert page["lifecycle_status"] == "invalid"
    else:
        assert recovered.intent_status == "superseded"
        assert recovered.current_kind == "external_target"
        assert recovered.successor_intent_id is not None
        assert len(recovered.observation_ids) == 1
        assert page["pending_write_intent_id"] == recovered.successor_intent_id


def test_capture_hash_mismatch_keeps_unknown_bytes_in_backup(tmp_path: Path):
    vault = tmp_path / "vault"
    target = vault / "wiki/product/faq/demo.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"changed before capture")
    writer = AtomicVaultWriter(vault)

    with pytest.raises(TargetChanged) as exc_info:
        writer.capture("wint_3", "wiki/product/faq/demo.md", compute_file_hash(b"expected"))
    assert exc_info.value.observed_hash == compute_file_hash(b"changed before capture")
    assert writer.backup_path("wint_3").read_bytes() == b"changed before capture"
    assert not target.exists()


@pytest.fixture
def wiki_intent_fixture(tmp_path):
    settings = get_settings().model_copy()
    settings.database_backend = "sqlite"
    settings.database_path = tmp_path / "intent.db"
    settings.vault_path = tmp_path / "vault"
    page_path = "wiki/product/faq/intent.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\ntitle: Intent\nsource_ids: [src_intent]\nreview_status: draft\n---\n# Intent\n",
        encoding="utf-8",
    )
    init_app_db(settings)
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id,domain,title,source_type,original_path,raw_path,content_hash,
              size_bytes,status,metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "src_intent",
                "product",
                "Intent source",
                "markdown",
                "intent-source.md",
                "raw/product/src_intent.md",
                "a" * 64,
                1,
                "active",
                "{}",
                "t0",
                "t0",
            ),
        )
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,domain,page_type,title,source_ids_json,review_status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (page_path,"product","faq","Intent",'["src_intent"]',"draft","t0","t0"),
        )
    service = WikiRevisionService(settings)
    current = service.get_page(page_path)
    prepared = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page_path,
            content=current.content + "\nPrepared manual change.\n",
            expected_revision_id=current.current_revision_id,
            request_id="fixture-pending-intent",
            actor="test",
            owner=None,
            note=None,
            review_status="draft",
        ),
        execute_intent=False,
    )
    assert prepared.status == "prepared"
    assert prepared.write_intent_id is not None
    return settings, prepared.write_intent_id


def test_only_lease_and_os_lock_owner_can_advance_intent(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    now = datetime.now(timezone.utc)
    first = IntentExecutor(settings, owner="executor_a")
    second = IntentExecutor(settings, owner="executor_b")

    assert first.claim(intent_id, now=now, lease_seconds=30) is True
    assert second.claim(intent_id, now=now, lease_seconds=30) is False
    assert second.advance_phase(intent_id, expected_status="pending", status="captured") is False
    assert first.advance_phase(intent_id, expected_status="pending", status="captured") is True


@pytest.mark.parametrize(
    "crash_point",
    ["intent_created", "captured", "installed"],
)
def test_reconcile_recovers_each_crash_point(
    wiki_intent_fixture,
    crash_point,
):
    settings, intent_id = wiki_intent_fixture
    crashed = IntentExecutor(settings, owner=f"crash-{crash_point}")

    stopped = crashed.execute(
        intent_id,
        stop_after=crash_point,
        lease_seconds=1,
    )

    assert stopped is not None
    assert stopped.intent_status == (
        "pending" if crash_point == "intent_created" else crash_point
    )
    recovered = IntentExecutor(
        settings,
        owner=f"recover-{crash_point}",
    ).reconcile_all(
        now=datetime.now(timezone.utc) + timedelta(seconds=2),
    )
    with connect_app(settings) as conn:
        page = conn.execute(
            "SELECT pending_write_intent_id,current_revision_id FROM wiki_pages"
        ).fetchone()
        intent = conn.execute(
            """
            SELECT status,backup_retention_status
            FROM vault_write_intents WHERE id=?
            """,
            (intent_id,),
        ).fetchone()

    assert recovered == [intent_id]
    assert page["pending_write_intent_id"] is None
    assert page["current_revision_id"] is not None
    assert tuple(intent) == ("applied", "retained")


@pytest.mark.skipif(os.name != "nt", reason="Windows spawn lease test")
def test_spawn_processes_allow_exactly_one_database_lease_owner(
    wiki_intent_fixture,
):
    settings, intent_id = wiki_intent_fixture
    ctx = multiprocessing.get_context("spawn")
    events = ctx.Queue()
    start_event = ctx.Event()
    owners = ("spawn-lease-a", "spawn-lease-b")
    processes = [
        ctx.Process(
            target=_claim_intent_process,
            args=(
                settings.model_dump(mode="json"),
                intent_id,
                owner,
                events,
                start_event,
            ),
        )
        for owner in owners
    ]
    messages = []
    try:
        for process in processes:
            process.start()
        messages.extend(events.get(timeout=15) for _ in processes)
        start_event.set()
        messages.extend(events.get(timeout=15) for _ in processes)
    finally:
        cleanup_errors = []
        start_event.set()
        for process in processes:
            try:
                _join_or_terminate(process)
            except BaseException as exc:
                cleanup_errors.append(exc)
        try:
            events.close()
            events.join_thread()
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            raise cleanup_errors[0]

    assert all(process.exitcode == 0 for process in processes)
    ready_messages = [message for message in messages if message[1] == "ready"]
    claim_messages = [message for message in messages if message[1] == "claim"]
    assert {message[0] for message in ready_messages} == set(owners)
    assert len(claim_messages) == 2
    assert sorted(message[2] for message in claim_messages) == [False, True]
    winner = next(message[0] for message in claim_messages if message[2])
    with connect_app(settings) as conn:
        intent = conn.execute(
            """
            SELECT executor_owner,attempts,status,lease_expires_at
            FROM vault_write_intents WHERE id=?
            """,
            (intent_id,),
        ).fetchone()
    assert intent["executor_owner"] == winner
    assert intent["attempts"] == 1
    assert intent["status"] == "pending"
    assert intent["lease_expires_at"] is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows spawn OS lock test")
def test_spawn_process_contender_reaches_and_loses_real_intent_os_lock(
    wiki_intent_fixture,
):
    settings, intent_id = wiki_intent_fixture
    owner = "shared-os-lock-owner"
    settings_data = settings.model_dump(mode="json")
    ctx = multiprocessing.get_context("spawn")
    events = ctx.Queue()
    held_event = ctx.Event()
    release_event = ctx.Event()
    holder = ctx.Process(
        target=_hold_intent_os_lock,
        args=(
            settings_data,
            intent_id,
            owner,
            events,
            held_event,
            release_event,
        ),
    )
    contender = ctx.Process(
        target=_execute_with_os_lock_probe,
        args=(settings_data, intent_id, owner, events, held_event),
    )
    messages = []
    contender_alive_before_release = True
    contender_exitcode_before_release = None
    writer = AtomicVaultWriter(settings.vault_path)
    with connect_app(settings) as conn:
        intent = conn.execute(
            "SELECT target_path FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
    target = settings.vault_path / intent["target_path"]
    original_bytes = target.read_bytes()
    database_state = None
    try:
        holder.start()
        messages.append(events.get(timeout=15))
        messages.append(events.get(timeout=15))
        contender.start()
        while not any(
            message[0] == "contender"
            and message[1] in {"result", "error"}
            for message in messages
        ):
            messages.append(events.get(timeout=15))
        contender.join(10)
        contender_alive_before_release = contender.is_alive()
        contender_exitcode_before_release = contender.exitcode
        with connect_app(settings) as conn:
            database_state = conn.execute(
                """
                SELECT executor_owner,attempts,status,lease_expires_at
                FROM vault_write_intents WHERE id=?
                """,
                (intent_id,),
            ).fetchone()
    finally:
        cleanup_errors = []
        try:
            _join_or_terminate(contender)
        except BaseException as exc:
            cleanup_errors.append(exc)
        release_event.set()
        try:
            _join_or_terminate(holder)
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            events.close()
            events.join_thread()
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            raise cleanup_errors[0]

    holder_claims = [
        message
        for message in messages
        if message[:2] == ("holder", "claim")
    ]
    holder_locks = [
        message
        for message in messages
        if message[:2] == ("holder", "locked")
    ]
    lock_attempts = [
        message
        for message in messages
        if message[:2] == ("contender", "lock_attempt")
    ]
    lock_entries = [
        message
        for message in messages
        if message[:2] == ("contender", "lock_entered")
    ]
    results = [
        message
        for message in messages
        if message[:2] == ("contender", "result")
    ]
    errors = [message for message in messages if message[1] == "error"]
    assert holder_claims == [("holder", "claim", True)]
    assert len(holder_locks) == 1
    assert len(lock_attempts) == 1
    assert lock_attempts[0][2] == holder_locks[0][2]
    assert lock_entries == []
    assert results == [("contender", "result", None)]
    assert errors == []
    assert contender_alive_before_release is False
    assert contender_exitcode_before_release == 0
    assert contender.exitcode == holder.exitcode == 0
    assert database_state["executor_owner"] == owner
    assert database_state["attempts"] == 2
    assert database_state["status"] == "pending"
    assert database_state["lease_expires_at"] is not None
    assert target.read_bytes() == original_bytes
    assert not writer.backup_path(intent_id).exists()
    assert not writer.capture_claim_path(intent_id).exists()
    assert not writer.staged_path(intent_id).exists()


def test_reconcile_finalizes_installed_intent_after_process_crash(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="crashed")
    assert executor.claim(intent_id, lease_seconds=1)
    executor.capture_and_install(intent_id, stop_after="installed")

    with connect_app(settings) as conn:
        before = conn.execute("SELECT current_revision_id,pending_write_intent_id FROM wiki_pages").fetchone()
    assert before[1] == intent_id

    recovered = IntentExecutor(settings, owner="recovery").reconcile_all(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )

    with connect_app(settings) as conn:
        page = conn.execute("SELECT current_revision_id,pending_write_intent_id FROM wiki_pages").fetchone()
        intent = conn.execute("SELECT status,backup_retention_status FROM vault_write_intents WHERE id=?", (intent_id,)).fetchone()
    assert recovered == [intent_id]
    assert page[0] is not None and page[1] is None
    assert tuple(intent) == ("applied", "retained")


def test_reconcile_finishes_install_crash_before_phase_was_recorded(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    crashed = IntentExecutor(settings, owner="crashed-before-installed-phase")
    assert crashed.claim(intent_id, lease_seconds=1)
    crashed.capture_and_install(intent_id, stop_after="captured")

    with connect_app(settings) as conn:
        intent = conn.execute(
            "SELECT target_path,revision_id,status FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
        revision = conn.execute(
            "SELECT content FROM wiki_page_revisions WHERE id=?",
            (intent["revision_id"],),
        ).fetchone()
    assert intent["status"] == "captured"
    crashed.writer.install(intent_id, intent["target_path"], revision["content"].encode("utf-8"))

    recovered = IntentExecutor(settings, owner="recovery").reconcile_all(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )

    with connect_app(settings) as conn:
        page = conn.execute(
            "SELECT current_revision_id,pending_write_intent_id FROM wiki_pages"
        ).fetchone()
        status = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    assert recovered == [intent_id]
    assert page[0] is not None and page[1] is None
    assert status == "applied"


@pytest.mark.parametrize("intent_status", ["captured", "installed", "recovery_required"])
def test_existing_intended_target_is_durable_before_database_advance(
    wiki_intent_fixture,
    monkeypatch,
    intent_status,
):
    settings, intent_id = wiki_intent_fixture
    setup = IntentExecutor(settings, owner=f"durable-{intent_status}")
    assert setup.claim(intent_id, lease_seconds=30)
    setup.capture_and_install(
        intent_id,
        stop_after="captured" if intent_status == "captured" else "installed",
    )
    with connect_app(settings) as conn:
        intent = conn.execute(
            "SELECT target_path,revision_id FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
        revision = conn.execute(
            "SELECT content FROM wiki_page_revisions WHERE id=?",
            (intent["revision_id"],),
        ).fetchone()
    target = settings.vault_path / intent["target_path"]
    if intent_status == "captured":
        setup.writer.install(
            intent_id,
            intent["target_path"],
            revision["content"].encode("utf-8"),
        )
        executor = setup
    elif intent_status == "recovery_required":
        with connect_app(settings) as conn:
            conn.execute(
                """
                UPDATE vault_write_intents
                SET status='recovery_required',executor_owner=NULL,
                    lease_expires_at='t0'
                WHERE id=?
                """,
                (intent_id,),
            )
        executor = IntentExecutor(settings, owner="durable-recovery")
    else:
        executor = setup

    events: list[tuple[str, Path, bool | None]] = []
    monkeypatch.setattr(
        executor.writer,
        "_fsync_existing_file",
        lambda path: events.append(("file", Path(path), None)),
    )
    monkeypatch.setattr(
        executor.writer,
        "_fsync_directory",
        lambda path, *, strict=False: events.append(
            ("directory", Path(path), strict)
        ),
    )
    original_finalize = executor.revisions.finalize_intent

    def tracked_finalize(*args, **kwargs):
        events.append(("finalize", target, None))
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(executor.revisions, "finalize_intent", tracked_finalize)
    if intent_status == "captured":
        original_advance = executor.advance_phase

        def tracked_advance(*args, **kwargs):
            events.append(("advance", target, None))
            return original_advance(*args, **kwargs)

        monkeypatch.setattr(executor, "advance_phase", tracked_advance)
        result = executor.capture_and_install(intent_id)
        boundary = ("advance", target, None)
    elif intent_status == "recovery_required":
        result = executor.reconcile_one(
            intent_id,
            now=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        boundary = ("finalize", target, None)
    else:
        result = executor.capture_and_install(intent_id)
        boundary = ("finalize", target, None)

    boundary_index = events.index(boundary)
    assert ("file", target, None) in events[:boundary_index]
    assert ("directory", target.parent, True) in events[:boundary_index]
    assert result.intent_status == "applied"


def test_reconcile_keeps_unknown_installed_target_pending_for_recovery(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    crashed = IntentExecutor(settings, owner="crashed-with-external-edit")
    assert crashed.claim(intent_id, lease_seconds=1)
    crashed.capture_and_install(intent_id, stop_after="installed")

    with connect_app(settings) as conn:
        target_path = conn.execute(
            "SELECT target_path FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    target = settings.vault_path / target_path
    target.write_bytes(b"unknown obsidian bytes")

    recovered = IntentExecutor(settings, owner="recovery").reconcile_all(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )

    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
        intent = conn.execute(
            "SELECT status,backup_retention_status FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
    assert recovered == []
    assert page[0] == intent_id
    assert tuple(intent) == ("recovery_required", "retained")
    assert crashed.writer.backup_path(intent_id).exists()


def test_finalize_hash_race_becomes_recovery_required(wiki_intent_fixture, monkeypatch):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="finalize-race")
    assert executor.claim(intent_id, lease_seconds=30)
    executor.capture_and_install(intent_id, stop_after="installed")
    with connect_app(settings) as conn:
        target_path = conn.execute(
            "SELECT target_path FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    target = settings.vault_path / target_path
    original_finalize = executor.revisions.finalize_intent

    def edit_then_finalize(*args, **kwargs):
        target.write_bytes(b"obsidian raced finalize")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(executor.revisions, "finalize_intent", edit_then_finalize)

    result = executor.capture_and_install(intent_id)

    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
        status = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    assert result.intent_status == "recovery_required"
    assert page[0] == intent_id
    assert status == "recovery_required"


def test_finalize_delete_race_becomes_recovery_required(wiki_intent_fixture, monkeypatch):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="finalize-delete-race")
    assert executor.claim(intent_id, lease_seconds=30)
    executor.capture_and_install(intent_id, stop_after="installed")
    with connect_app(settings) as conn:
        target_path = conn.execute(
            "SELECT target_path FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    target = settings.vault_path / target_path
    original_finalize = executor.revisions.finalize_intent

    def delete_then_finalize(*args, **kwargs):
        target.unlink()
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(executor.revisions, "finalize_intent", delete_then_finalize)

    result = executor.capture_and_install(intent_id)

    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
        status = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    assert result.intent_status == "recovery_required"
    assert page[0] == intent_id
    assert status == "recovery_required"


def test_preflight_hash_delete_race_becomes_recovery_required(
    wiki_intent_fixture, monkeypatch
):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="preflight-delete-race")
    assert executor.claim(intent_id, lease_seconds=30)
    executor.capture_and_install(intent_id, stop_after="installed")
    with connect_app(settings) as conn:
        target_path = conn.execute(
            "SELECT target_path FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    target = settings.vault_path / target_path
    original_stream_hash = executor.writer._stream_hash
    removed = False

    def delete_then_hash(path):
        nonlocal removed
        if path == target and not removed:
            removed = True
            target.unlink()
        return original_stream_hash(path)

    monkeypatch.setattr(executor.writer, "_stream_hash", delete_then_hash)

    result = executor.capture_and_install(intent_id)

    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
        status = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    assert result.intent_status == "recovery_required"
    assert page[0] == intent_id
    assert status == "recovery_required"


def test_failed_lease_renewal_stops_before_file_mutation(wiki_intent_fixture, monkeypatch):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="expired-owner")
    assert executor.claim(intent_id, lease_seconds=1)
    with connect_app(settings) as conn:
        target_path = conn.execute(
            "SELECT target_path FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    target = settings.vault_path / target_path
    before = target.read_bytes()
    monkeypatch.setattr(executor, "renew_lease", lambda *args, **kwargs: False)

    with pytest.raises(VaultWriteError, match="lease ownership"):
        executor.capture_and_install(intent_id)

    assert target.read_bytes() == before
    assert not executor.writer.backup_path(intent_id).exists()


def test_get_page_reads_current_revision_during_capture_window(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="captured-reader")
    assert executor.claim(intent_id, lease_seconds=30)
    executor.capture_and_install(intent_id, stop_after="captured")
    with connect_app(settings) as conn:
        page = conn.execute(
            "SELECT path,current_revision_id FROM wiki_pages"
        ).fetchone()
        current_content = conn.execute(
            "SELECT content FROM wiki_page_revisions WHERE id=?",
            (page["current_revision_id"],),
        ).fetchone()[0]

    observed = WikiRevisionService(settings).get_page(page["path"])

    assert observed.current_revision_id == page["current_revision_id"]
    assert observed.content == current_content
    assert observed.write_in_progress is True
    assert observed.write_intent_id == intent_id


def test_terminal_clear_requires_matching_pending_intent(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="executor_a")
    assert executor.claim(intent_id, lease_seconds=30)
    with connect_app(settings) as conn:
        conn.execute("UPDATE wiki_pages SET pending_write_intent_id='wint_successor'")
    assert executor.clear_terminal(intent_id, "failed", "simulated") is False
    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
    assert page[0] == "wint_successor"


def test_finalize_hashes_installed_target_while_page_lock_is_held(
    wiki_intent_fixture, monkeypatch
):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="finalizer")
    assert executor.claim(intent_id, lease_seconds=30)
    executor.capture_and_install(intent_id, stop_after="installed")

    service = executor.revisions
    original_lock_page = service.coordinator.lock_page
    original_stream_hash = service.writer._stream_hash
    page_lock_held = False

    @contextmanager
    def tracked_lock_page(*args, **kwargs):
        nonlocal page_lock_held
        with original_lock_page(*args, **kwargs) as locked:
            page_lock_held = True
            try:
                yield locked
            finally:
                page_lock_held = False

    def checked_stream_hash(path):
        assert page_lock_held, "installed target hash was read outside the page lock"
        return original_stream_hash(path)

    monkeypatch.setattr(service.coordinator, "lock_page", tracked_lock_page)
    monkeypatch.setattr(service.writer, "_stream_hash", checked_stream_hash)

    result = service.finalize_intent(intent_id, executor.owner)

    assert result.status == "applied"


def test_terminal_clear_owner_mismatch_rolls_back_pending_pointer(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="stale-terminal-owner")
    assert executor.claim(intent_id, lease_seconds=30)
    with connect_app(settings) as conn:
        conn.execute(
            "UPDATE vault_write_intents SET executor_owner='successor' WHERE id=?",
            (intent_id,),
        )

    assert executor.clear_terminal(intent_id, "failed", "simulated") is False

    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
        intent = conn.execute(
            "SELECT status,executor_owner FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
    assert page[0] == intent_id
    assert tuple(intent) == ("pending", "successor")


def _valid_wiki_variant(raw: bytes, label: str) -> bytes:
    return raw + f"\n\n{label}\n".encode("utf-8")


@dataclass(frozen=True)
class RecoveryScenario:
    settings: object
    intent_id: str
    page_id: str
    page_path: str
    current_revision_id: str
    intended_revision_id: str
    after_lease: datetime
    target_path: Path
    backup_path: Path
    expected_bytes: bytes
    intended_bytes: bytes
    target_bytes: bytes | None
    backup_bytes: bytes | None
    baseline_epoch: int

    def observations_for(self, raw: bytes) -> list[object]:
        with connect_app(self.settings) as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM wiki_file_observations
                    WHERE page_id=? AND file_hash=? ORDER BY id
                    """,
                    (self.page_id, compute_file_hash(raw)),
                ).fetchall()
            )


class RecoveryIntentFactory:
    def __init__(self, settings, intent_id: str):
        self.settings = settings
        self.intent_id = intent_id

    def materialize(
        self,
        target_kind: str,
        backup_kind: str,
    ) -> RecoveryScenario:
        with connect_app(self.settings) as conn:
            intent = conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?",
                (self.intent_id,),
            ).fetchone()
            page = conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?",
                (intent["page_id"],),
            ).fetchone()
            current = conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (page["current_revision_id"],),
            ).fetchone()
            intended = conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (intent["revision_id"],),
            ).fetchone()
        expected_bytes = current["content"].encode("utf-8")
        intended_bytes = intended["content"].encode("utf-8")
        target_unknown = _valid_wiki_variant(
            expected_bytes,
            "External target recovery bytes.",
        )
        backup_unknown = _valid_wiki_variant(
            expected_bytes,
            "External backup recovery bytes.",
        )
        truncated = (
            b"---\ntitle: Large recovery\nsource_ids: [src_intent]\n"
            b"review_status: draft\n---\n# Large\n"
            + (b"x" * 5_300_000)
        )
        payloads = {
            "expected": expected_bytes,
            "intended": intended_bytes,
            "unknown": target_unknown,
            "invalid": b"invalid recovery bytes",
            "truncated": truncated,
            "missing": None,
        }
        target_bytes = payloads[target_kind]
        backup_bytes = (
            backup_unknown if backup_kind == "unknown" else payloads[backup_kind]
        )
        writer = AtomicVaultWriter(self.settings.vault_path)
        target_path = self.settings.vault_path / intent["target_path"]
        backup_path = writer.backup_path(self.intent_id)
        target_path.unlink(missing_ok=True)
        backup_path.unlink(missing_ok=True)
        if target_bytes is not None:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(target_bytes)
        if backup_bytes is not None:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_bytes(backup_bytes)
        with connect_app(self.settings) as conn:
            conn.execute(
                """
                UPDATE vault_write_intents
                SET status='recovery_required',executor_owner=NULL,
                    lease_expires_at='t0',backup_path=?,captured_file_hash=?,
                    backup_last_observed_hash=NULL,backup_retention_status=?,
                    last_error='fixture recovery',updated_at='t0'
                WHERE id=?
                """,
                (
                    str(backup_path),
                    compute_file_hash(expected_bytes),
                    "retained" if backup_bytes is not None else "none",
                    self.intent_id,
                ),
            )
        return RecoveryScenario(
            settings=self.settings,
            intent_id=self.intent_id,
            page_id=page["page_id"],
            page_path=page["path"],
            current_revision_id=page["current_revision_id"],
            intended_revision_id=intent["revision_id"],
            after_lease=datetime.now(timezone.utc) + timedelta(seconds=1),
            target_path=target_path,
            backup_path=backup_path,
            expected_bytes=expected_bytes,
            intended_bytes=intended_bytes,
            target_bytes=target_bytes,
            backup_bytes=backup_bytes,
            baseline_epoch=int(page["projection_epoch"]),
        )


@pytest.fixture
def recovery_intent_fixture(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    return RecoveryIntentFactory(settings, intent_id)


@dataclass(frozen=True)
class AppliedIntentScenario:
    settings: object
    intent_id: str
    page_id: str
    page_path: str
    current_revision_id: str
    backup_path: Path
    backup_hash: str
    backup_bytes: bytes


@pytest.fixture
def applied_intent_fixture(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    result = IntentExecutor(settings, owner="fixture-apply").execute(intent_id)
    assert result is not None and result.intent_status == "applied"
    writer = AtomicVaultWriter(settings.vault_path)
    backup_path = writer.backup_path(intent_id)
    backup_bytes = backup_path.read_bytes()
    backup_hash = compute_file_hash(backup_bytes)
    with connect_app(settings) as conn:
        intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (intent["page_id"],),
        ).fetchone()
    return AppliedIntentScenario(
        settings=settings,
        intent_id=intent_id,
        page_id=page["page_id"],
        page_path=page["path"],
        current_revision_id=page["current_revision_id"],
        backup_path=backup_path,
        backup_hash=backup_hash,
        backup_bytes=backup_bytes,
    )


@pytest.mark.parametrize(
    (
        "target_kind",
        "backup_kind",
        "expected_status",
        "expected_kind",
    ),
    [
        ("expected", "missing", "applied", "intended"),
        ("intended", "expected", "applied", "intended"),
        ("intended", "unknown", "superseded", "external_backup"),
        ("unknown", "expected", "superseded", "external_target"),
        ("unknown", "unknown", "recovery_required", "unchanged"),
        ("missing", "expected", "applied", "intended"),
        ("missing", "unknown", "superseded", "external_backup"),
        ("missing", "missing", "failed", "unchanged"),
    ],
)
def test_recovery_matrix_preserves_unknown_bytes_and_pointer_invariants(
    recovery_intent_fixture,
    target_kind,
    backup_kind,
    expected_status,
    expected_kind,
):
    scenario = recovery_intent_fixture.materialize(target_kind, backup_kind)

    result = IntentExecutor(
        scenario.settings,
        owner="matrix-recovery",
    ).reconcile_one(scenario.intent_id, now=scenario.after_lease)

    with connect_app(scenario.settings) as conn:
        old_intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        successor = (
            conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?",
                (result.successor_intent_id,),
            ).fetchone()
            if result.successor_intent_id is not None
            else None
        )
        jobs = conn.execute(
            """
            SELECT target,operation,revision_id FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch=? ORDER BY target
            """,
            (scenario.page_id, page["projection_epoch"]),
        ).fetchall()
    assert result.intent_status == expected_status
    assert result.current_kind == expected_kind
    assert old_intent["status"] == expected_status
    unknown_bytes = [
        raw
        for raw in (scenario.target_bytes, scenario.backup_bytes)
        if raw is not None
        and raw not in {scenario.expected_bytes, scenario.intended_bytes}
    ]
    for raw in unknown_bytes:
        assert len(scenario.observations_for(raw)) == 1

    if expected_status == "applied":
        assert page["current_revision_id"] == scenario.intended_revision_id
        assert page["pending_write_intent_id"] is None
        assert scenario.target_path.read_bytes() == scenario.intended_bytes
        assert scenario.backup_path.exists()
        assert scenario.backup_path.read_bytes() == scenario.expected_bytes
        assert result.successor_intent_id is None
    elif expected_status == "superseded":
        assert page["current_revision_id"] == scenario.current_revision_id
        assert successor is not None
        assert successor["status"] == "pending"
        assert page["pending_write_intent_id"] == successor["id"]
        assert successor["expected_revision_id"] == scenario.current_revision_id
        revision = None
        with connect_app(scenario.settings) as conn:
            revision = conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (successor["revision_id"],),
            ).fetchone()
            metadata = json.loads(revision["metadata_json"])
            event = conn.execute(
                "SELECT * FROM vault_change_events WHERE id=?",
                (metadata["vault_change_event_id"],),
            ).fetchone()
        assert revision["origin"] == "external"
        assert metadata["recovery_intent_id"] == scenario.intent_id
        assert successor["revision_id"] == revision["id"]
        assert event["status"] == "prepared"
        assert event["observation_id"] == metadata["observation_id"]
        assert event["result_revision_id"] is None
        assert event["result_payload_json"] is None
        assert old_intent["executor_owner"] is None

        finalized = IntentExecutor(
            scenario.settings,
            owner="matrix-successor-finalize",
        ).execute(successor["id"])
        with connect_app(scenario.settings) as conn:
            terminal_event = conn.execute(
                "SELECT * FROM vault_change_events WHERE id=?",
                (metadata["vault_change_event_id"],),
            ).fetchone()
        terminal_payload = json.loads(terminal_event["result_payload_json"])
        assert finalized is not None
        assert finalized.intent_status == "applied"
        assert terminal_event["status"] == "applied"
        assert terminal_event["result_revision_id"] == revision["id"]
        assert terminal_payload["revision_id"] == revision["id"]
        assert terminal_payload["current_revision_id"] == revision["id"]
        assert terminal_event["result_payload_json"] == json.dumps(
            terminal_payload,
            sort_keys=True,
            separators=(",", ":"),
        )
    elif target_kind == "unknown" and backup_kind == "unknown":
        assert page["current_revision_id"] == scenario.current_revision_id
        assert page["pending_write_intent_id"] == scenario.intent_id
        assert page["lifecycle_status"] == "invalid"
        assert page["projection_epoch"] == scenario.baseline_epoch + 1
        assert {(row["target"], row["operation"], row["revision_id"]) for row in jobs} == {
            ("rag", "delete", None),
            ("gbrain", "delete", None),
        }
        assert scenario.target_path.read_bytes() == scenario.target_bytes
        assert scenario.backup_path.read_bytes() == scenario.backup_bytes
    else:
        assert target_kind == backup_kind == "missing"
        assert page["current_revision_id"] == scenario.current_revision_id
        assert page["pending_write_intent_id"] is None
        assert page["projection_epoch"] == scenario.baseline_epoch + 1
        assert not scenario.target_path.exists()
        assert not scenario.backup_path.exists()
        assert {(row["target"], row["operation"], row["revision_id"]) for row in jobs} == {
            ("rag", "delete", None),
            ("gbrain", "delete", None),
        }


@pytest.mark.parametrize(
    ("target_kind", "backup_kind"),
    [("intended", "missing"), ("missing", "intended")],
)
def test_recovery_finalizes_known_intended_bytes_without_capture_mismatch(
    recovery_intent_fixture,
    target_kind,
    backup_kind,
):
    scenario = recovery_intent_fixture.materialize(target_kind, backup_kind)

    result = IntentExecutor(
        scenario.settings,
        owner=f"known-intended-{target_kind}-{backup_kind}",
    ).reconcile_one(scenario.intent_id, now=scenario.after_lease)

    with connect_app(scenario.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
    assert result.intent_status == "applied"
    assert result.current_kind == "intended"
    assert intent["status"] == "applied"
    assert page["current_revision_id"] == scenario.intended_revision_id
    assert page["pending_write_intent_id"] is None
    assert scenario.target_path.read_bytes() == scenario.intended_bytes
    if backup_kind == "missing":
        assert not scenario.backup_path.exists()
        assert intent["backup_retention_status"] == "none"
    else:
        assert scenario.backup_path.read_bytes() == scenario.intended_bytes
        assert intent["backup_retention_status"] == "retained"


def test_known_intended_recovery_lease_loss_stops_before_status_cas(
    recovery_intent_fixture,
    monkeypatch,
):
    scenario = recovery_intent_fixture.materialize("missing", "intended")
    executor = IntentExecutor(
        scenario.settings,
        owner="known-intended-lease-loss",
    )
    renewals = 0

    def lose_after_install(*args, **kwargs):
        nonlocal renewals
        renewals += 1
        return renewals < 3

    monkeypatch.setattr(executor, "renew_lease", lose_after_install)

    with pytest.raises(VaultWriteError, match="lease ownership"):
        executor.reconcile_one(
            scenario.intent_id,
            now=scenario.after_lease,
        )

    with connect_app(scenario.settings) as conn:
        intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
    assert renewals == 3
    assert intent["status"] == "recovery_required"
    assert page["pending_write_intent_id"] == scenario.intent_id
    assert scenario.target_path.read_bytes() == scenario.intended_bytes


def test_recovery_handoff_directly_cas_replaces_old_pointer(
    recovery_intent_fixture,
):
    scenario = recovery_intent_fixture.materialize("unknown", "expected")
    trigger_name = "forbid_recovery_pointer_null"
    with connect_app(scenario.settings) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE OF pending_write_intent_id ON wiki_pages
            WHEN OLD.pending_write_intent_id='{scenario.intent_id}'
              AND NEW.pending_write_intent_id IS NULL
            BEGIN
              SELECT RAISE(ABORT, 'recovery pointer became null');
            END
            """
        )

    result = IntentExecutor(
        scenario.settings,
        owner="direct-cas-recovery",
    ).reconcile_one(scenario.intent_id, now=scenario.after_lease)

    with connect_app(scenario.settings) as conn:
        page = conn.execute(
            "SELECT pending_write_intent_id FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        old_status = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()[0]
    assert result.intent_status == "superseded"
    assert result.successor_intent_id is not None
    assert page["pending_write_intent_id"] == result.successor_intent_id
    assert old_status == "superseded"


def test_recovery_handoff_replay_and_startup_finish_same_successor(
    recovery_intent_fixture,
):
    scenario = recovery_intent_fixture.materialize("unknown", "expected")
    executor = IntentExecutor(scenario.settings, owner="handoff-crash")
    handed_off = executor.reconcile_one(
        scenario.intent_id,
        now=scenario.after_lease,
    )
    assert handed_off.intent_status == "superseded"
    assert handed_off.successor_intent_id is not None

    with connect_app(scenario.settings) as conn:
        candidate = conn.execute(
            """
            SELECT revision.* FROM vault_write_intents AS intent
            JOIN wiki_page_revisions AS revision ON revision.id=intent.revision_id
            WHERE intent.id=?
            """,
            (handed_off.successor_intent_id,),
        ).fetchone()
        metadata = json.loads(candidate["metadata_json"])
        event_id = metadata["vault_change_event_id"]
        counts_after_handoff = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "wiki_file_observations",
                "vault_change_events",
                "wiki_page_revisions",
                "vault_write_intents",
            )
        }

    replay = executor.reconcile_one(
        scenario.intent_id,
        now=scenario.after_lease + timedelta(seconds=1),
    )
    completed = IntentExecutor(
        scenario.settings,
        owner="startup-after-handoff",
    ).reconcile_all(now=scenario.after_lease + timedelta(seconds=2))

    with connect_app(scenario.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        current = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (page["current_revision_id"],),
        ).fetchone()
        old_intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        successor = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (handed_off.successor_intent_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        displaced_reviews = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchall()
        counts_after_finish = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counts_after_handoff
        }

    assert replay.intent_status == "superseded"
    assert replay.successor_intent_id == handed_off.successor_intent_id
    assert completed == [handed_off.successor_intent_id]
    assert counts_after_finish == counts_after_handoff
    assert old_intent["status"] == "superseded"
    assert successor["status"] == "applied"
    assert page["pending_write_intent_id"] is None
    assert page["current_revision_id"] == candidate["id"]
    assert page["file_hash"] == current["file_hash"]
    assert scenario.target_path.read_bytes() == current["content"].encode("utf-8")
    assert event["status"] == "applied"
    assert event["result_revision_id"] == current["id"]
    assert len(displaced_reviews) == 1
    assert displaced_reviews[0]["base_revision_id"] == current["id"]
    assert (
        displaced_reviews[0]["candidate_revision_id"]
        == scenario.intended_revision_id
    )


def test_recovery_displaced_merge_is_the_only_resolvable_merge_candidate(
    recovery_intent_fixture,
):
    scenario = recovery_intent_fixture.materialize("unknown", "expected")
    with connect_app(scenario.settings) as conn:
        conn.execute(
            "UPDATE wiki_page_revisions SET origin='merge' WHERE id=?",
            (scenario.intended_revision_id,),
        )
    handed_off = IntentExecutor(
        scenario.settings,
        owner="handoff-displaced-merge",
    ).reconcile_one(scenario.intent_id, now=scenario.after_lease)
    assert handed_off.successor_intent_id is not None
    applied = IntentExecutor(
        scenario.settings,
        owner="apply-displaced-merge",
    ).execute(handed_off.successor_intent_id)
    assert applied is not None and applied.intent_status == "applied"

    with connect_app(scenario.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        current = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (page["current_revision_id"],),
        ).fetchone()
        old_intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
    current_metadata = json.loads(current["metadata_json"])
    assert current["origin"] == "external"
    assert current_metadata["recovery_intent_id"] == scenario.intent_id
    assert (
        current_metadata["recovery_displaced_revision_id"]
        == scenario.intended_revision_id
    )
    assert old_intent["status"] == "superseded"
    assert old_intent["revision_id"] == scenario.intended_revision_id
    assert review is not None
    assert review["base_revision_id"] == page["current_revision_id"]
    assert review["candidate_revision_id"] == scenario.intended_revision_id
    assert (
        json.loads(review["expected_state_json"])[
            "recovery_displaced_intent_id"
        ]
        == scenario.intent_id
    )

    resolved = WikiRevisionService(scenario.settings).resolve_conflict(
        ResolveConflictCommand(
            review_id=review["id"],
            resolution="keep_current",
            merged_content=None,
            expected_current_revision_id=page["current_revision_id"],
            expected_generated_revision_id=page["generated_revision_id"],
            request_id="keep-external-over-displaced-merge",
            actor="recovery-admin",
            note=None,
        )
    )
    assert resolved.status == "resolved"


def test_ordinary_external_metadata_cannot_forge_a_displaced_candidate(
    recovery_intent_fixture,
):
    scenario = recovery_intent_fixture.materialize("unknown", "expected")
    handed_off = IntentExecutor(
        scenario.settings,
        owner="handoff-before-forged-recovery-metadata",
    ).reconcile_one(scenario.intent_id, now=scenario.after_lease)
    assert handed_off.successor_intent_id is not None
    applied_recovery = IntentExecutor(
        scenario.settings,
        owner="apply-before-forged-recovery-metadata",
    ).execute(handed_off.successor_intent_id)
    assert applied_recovery is not None
    assert applied_recovery.intent_status == "applied"
    service = WikiRevisionService(scenario.settings)
    with connect_app(scenario.settings) as conn:
        recovered_page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        displaced_review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
    service.resolve_conflict(
        ResolveConflictCommand(
            review_id=displaced_review["id"],
            resolution="keep_current",
            merged_content=None,
            expected_current_revision_id=recovered_page["current_revision_id"],
            expected_generated_revision_id=recovered_page["generated_revision_id"],
            request_id="close-real-recovery-before-forgery",
            actor="recovery-admin",
            note=None,
        )
    )

    target = scenario.settings.vault_path / scenario.page_path
    current_bytes = target.read_bytes()
    newline = b"\r\n" if b"\r\n" in current_bytes else b"\n"
    closing_marker = newline + b"---" + newline
    closing_index = current_bytes.find(closing_marker, 4)
    assert closing_index >= 0
    forged_bytes = (
        current_bytes[:closing_index]
        + newline
        + (
            "recovery_displaced_revision_id: "
            f"{scenario.intended_revision_id}"
        ).encode("utf-8")
        + newline
        + (
            "recovery_intent_id: "
            f"{scenario.intent_id}"
        ).encode("utf-8")
        + current_bytes[closing_index:]
        + newline
        + b"Ordinary external edit with forged recovery metadata."
        + newline
    )
    target.write_bytes(forged_bytes)

    applied = service.ingest_external_change(
        "ordinary-external-forged-displacement",
        scenario.page_path,
        capture_file_observation(
            target,
            max_content_bytes=1_000_000,
        ),
    )

    with connect_app(scenario.settings) as conn:
        current = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (applied.current_revision_id,),
        ).fetchone()
        pending = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchall()
    metadata = json.loads(current["metadata_json"])
    assert current["origin"] == "external"
    assert (
        metadata["recovery_displaced_revision_id"]
        == scenario.intended_revision_id
    )
    assert metadata["recovery_intent_id"] == scenario.intent_id
    assert pending == []


def test_recovery_handoff_failure_rolls_back_every_successor_artifact(
    recovery_intent_fixture,
    monkeypatch,
):
    scenario = recovery_intent_fixture.materialize("unknown", "expected")
    executor = IntentExecutor(scenario.settings, owner="rollback-recovery")
    with connect_app(scenario.settings) as conn:
        counts_before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "wiki_file_observations",
                "vault_change_events",
                "wiki_page_revisions",
                "vault_write_intents",
            )
        }

    revision_creation_attempted = False

    def fail_revision_creation(*args, **kwargs):
        nonlocal revision_creation_attempted
        revision_creation_attempted = True
        raise VaultWriteError("simulated successor creation failure")

    monkeypatch.setattr(
        executor.revisions,
        "_create_revision_locked",
        fail_revision_creation,
    )

    result = executor.reconcile_one(
        scenario.intent_id,
        now=scenario.after_lease,
    )

    with connect_app(scenario.settings) as conn:
        counts_after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counts_before
        }
        page = conn.execute(
            "SELECT pending_write_intent_id FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        intent = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
    assert result.intent_status == "recovery_required"
    assert revision_creation_attempted is True
    assert counts_after == counts_before
    assert page["pending_write_intent_id"] == scenario.intent_id
    assert intent["status"] == "recovery_required"


@pytest.mark.parametrize(
    ("target_kind", "backup_kind", "expected_status"),
    [
        ("unknown", "expected", "superseded"),
        ("missing", "missing", "failed"),
    ],
)
def test_recovery_cas_is_null_safe_before_first_current_revision(
    recovery_intent_fixture,
    target_kind,
    backup_kind,
    expected_status,
):
    scenario = recovery_intent_fixture.materialize(target_kind, backup_kind)
    with connect_app(scenario.settings) as conn:
        conn.execute(
            """
            UPDATE wiki_pages
            SET current_revision_id=NULL
            WHERE page_id=? AND pending_write_intent_id=?
            """,
            (scenario.page_id, scenario.intent_id),
        )

    result = IntentExecutor(
        scenario.settings,
        owner=f"null-current-{expected_status}",
    ).reconcile_one(scenario.intent_id, now=scenario.after_lease)

    with connect_app(scenario.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
    assert result.intent_status == expected_status
    assert page["current_revision_id"] is None
    if expected_status == "superseded":
        assert result.successor_intent_id is not None
        assert page["pending_write_intent_id"] == result.successor_intent_id
    else:
        assert page["pending_write_intent_id"] is None


@pytest.mark.parametrize("resolution", ["keep_current", "accept_candidate"])
def test_ambiguous_recovery_conflict_can_resolve_through_successor_intent(
    recovery_intent_fixture,
    resolution,
):
    scenario = recovery_intent_fixture.materialize("unknown", "unknown")
    executor = IntentExecutor(scenario.settings, owner="ambiguous-recovery")
    recovery = executor.reconcile_one(
        scenario.intent_id,
        now=scenario.after_lease,
    )
    assert recovery.intent_status == "recovery_required"

    with connect_app(scenario.settings) as conn:
        review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
        candidate = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (review["candidate_revision_id"],),
        ).fetchone()
        metadata = json.loads(candidate["metadata_json"])
        expected_state = json.loads(review["expected_state_json"])
        audit_payload = json.loads(
            conn.execute(
                """
                SELECT payload_json FROM audit_logs
                WHERE event_type='wiki_recovery_ambiguous'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()[0]
        )
        page_before = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        candidate_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (metadata["vault_change_event_id"],),
        ).fetchone()

    secondary_ids = expected_state["recovery_secondary_observation_ids"]
    assert len(secondary_ids) == 1
    assert metadata["recovery_secondary_observation_ids"] == secondary_ids
    assert audit_payload["secondary_observation_ids"] == secondary_ids
    assert audit_payload["primary_observation_id"] == metadata["observation_id"]
    assert metadata["recovery_intent_id"] == scenario.intent_id
    assert candidate_event["status"] == "prepared"
    assert candidate_event["result_revision_id"] is None
    assert candidate_event["result_payload_json"] is None

    result = WikiRevisionService(scenario.settings).resolve_conflict(
        ResolveConflictCommand(
            review_id=review["id"],
            resolution=resolution,
            merged_content=None,
            expected_current_revision_id=scenario.current_revision_id,
            expected_generated_revision_id=page_before["generated_revision_id"],
            request_id=f"resolve-ambiguous-{resolution}",
            actor="recovery-admin",
            note=None,
        )
    )

    with connect_app(scenario.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        old_intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        resolved_review = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (review["id"],),
        ).fetchone()
        current = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (page["current_revision_id"],),
        ).fetchone()
        resolution_intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (result.write_intent_id,),
        ).fetchone()
        resolved_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (metadata["vault_change_event_id"],),
        ).fetchone()
    assert result.status == "resolved"
    assert old_intent["status"] == "superseded"
    assert resolution_intent["status"] == "applied"
    assert page["pending_write_intent_id"] is None
    assert page["lifecycle_status"] == "active"
    assert resolved_review["status"] == "resolved"
    assert resolved_event["status"] == (
        "superseded" if resolution == "keep_current" else "applied"
    )
    assert scenario.target_path.read_bytes() == current["content"].encode("utf-8")
    if resolution == "keep_current":
        assert page["current_revision_id"] == scenario.current_revision_id
        assert resolved_event["result_revision_id"] is None
        assert resolved_event["result_payload_json"] is None
    else:
        assert page["current_revision_id"] == review["candidate_revision_id"]
        terminal_payload = json.loads(resolved_event["result_payload_json"])
        assert resolved_event["result_revision_id"] == candidate["id"]
        assert terminal_payload["revision_id"] == candidate["id"]
        assert terminal_payload["current_revision_id"] == candidate["id"]
        assert resolved_event["result_payload_json"] == json.dumps(
            terminal_payload,
            sort_keys=True,
            separators=(",", ":"),
        )
    replay = IntentExecutor(
        scenario.settings,
        owner=f"replay-resolved-{resolution}",
    ).reconcile_one(scenario.intent_id)
    assert replay.intent_status == "superseded"
    assert replay.successor_intent_id == result.write_intent_id


def test_recovery_keep_current_event_stays_prepared_until_successor_finalize(
    recovery_intent_fixture,
    monkeypatch,
):
    scenario = recovery_intent_fixture.materialize("unknown", "unknown")
    IntentExecutor(
        scenario.settings,
        owner="ambiguous-before-resolution-crash",
    ).reconcile_one(scenario.intent_id, now=scenario.after_lease)
    with connect_app(scenario.settings) as conn:
        review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
        candidate = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (review["candidate_revision_id"],),
        ).fetchone()
        event_id = json.loads(candidate["metadata_json"])[
            "vault_change_event_id"
        ]
        page_before = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()

    monkeypatch.setattr(IntentExecutor, "execute", lambda self, intent_id: None)
    with pytest.raises(RevisionConflict):
        WikiRevisionService(scenario.settings).resolve_conflict(
            ResolveConflictCommand(
                review_id=review["id"],
                resolution="keep_current",
                merged_content=None,
                expected_current_revision_id=scenario.current_revision_id,
                expected_generated_revision_id=page_before["generated_revision_id"],
                request_id="keep-current-crash-before-execute",
                actor="recovery-admin",
                note=None,
            )
        )

    with connect_app(scenario.settings) as conn:
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        pending_review = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (review["id"],),
        ).fetchone()
        old_intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
    assert event["status"] == "prepared"
    assert pending_review["status"] == "pending"
    assert old_intent["status"] == "superseded"
    assert page["pending_write_intent_id"] != scenario.intent_id


def test_ambiguous_recovery_resolution_does_not_steal_reclaimer_lease(
    recovery_intent_fixture,
):
    scenario = recovery_intent_fixture.materialize("unknown", "unknown")
    IntentExecutor(
        scenario.settings,
        owner="ambiguous-before-reclaimer",
    ).reconcile_one(scenario.intent_id, now=scenario.after_lease)
    with connect_app(scenario.settings) as conn:
        review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
        page_before = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE vault_write_intents
            SET executor_owner='active-reclaimer',lease_expires_at='9999-01-01T00:00:00+00:00'
            WHERE id=? AND status='recovery_required' AND executor_owner IS NULL
            """,
            (scenario.intent_id,),
        )

    with pytest.raises(RevisionConflict):
        WikiRevisionService(scenario.settings).resolve_conflict(
            ResolveConflictCommand(
                review_id=review["id"],
                resolution="accept_candidate",
                merged_content=None,
                expected_current_revision_id=scenario.current_revision_id,
                expected_generated_revision_id=page_before["generated_revision_id"],
                request_id="resolve-while-reclaimer-owns",
                actor="recovery-admin",
                note=None,
            )
        )

    with connect_app(scenario.settings) as conn:
        intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
    assert intent["status"] == "recovery_required"
    assert intent["executor_owner"] == "active-reclaimer"
    assert page["pending_write_intent_id"] == scenario.intent_id


def test_ambiguous_recovery_a_to_b_to_a_creates_distinct_event_occurrences(
    recovery_intent_fixture,
):
    scenario = recovery_intent_fixture.materialize("unknown", "unknown")
    executor = IntentExecutor(scenario.settings, owner="recovery-occurrences")
    executor.reconcile_one(scenario.intent_id, now=scenario.after_lease)

    def pending_candidate_event():
        with connect_app(scenario.settings) as conn:
            review = conn.execute(
                """
                SELECT * FROM review_items
                WHERE page_id=? AND issue_type='concurrent_write_conflict'
                  AND status='pending'
                """,
                (scenario.page_id,),
            ).fetchone()
            candidate = conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (review["candidate_revision_id"],),
            ).fetchone()
            metadata = json.loads(candidate["metadata_json"])
            event = conn.execute(
                "SELECT * FROM vault_change_events WHERE id=?",
                (metadata["vault_change_event_id"],),
            ).fetchone()
            revision_count = conn.execute(
                "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
                (scenario.page_id,),
            ).fetchone()[0]
        return candidate, event, revision_count

    first_candidate, first_event, _ = pending_candidate_event()
    second_bytes = _valid_wiki_variant(
        scenario.expected_bytes,
        "External target recovery occurrence B.",
    )
    scenario.target_path.write_bytes(second_bytes)
    executor.reconcile_one(
        scenario.intent_id,
        now=scenario.after_lease + timedelta(seconds=1),
    )
    second_candidate, second_event, _ = pending_candidate_event()

    scenario.target_path.write_bytes(scenario.target_bytes)
    executor.reconcile_one(
        scenario.intent_id,
        now=scenario.after_lease + timedelta(seconds=2),
    )
    cycled_candidate, cycled_event, cycled_revision_count = (
        pending_candidate_event()
    )
    replay = executor.reconcile_one(
        scenario.intent_id,
        now=scenario.after_lease + timedelta(seconds=3),
    )
    replay_candidate, replay_event, replay_revision_count = (
        pending_candidate_event()
    )

    with connect_app(scenario.settings) as conn:
        stored_first = conn.execute(
            "SELECT status FROM vault_change_events WHERE id=?",
            (first_event["id"],),
        ).fetchone()[0]
        stored_second = conn.execute(
            "SELECT status FROM vault_change_events WHERE id=?",
            (second_event["id"],),
        ).fetchone()[0]
    assert replay.intent_status == "recovery_required"
    assert stored_first == "superseded"
    assert stored_second == "superseded"
    assert len(
        {
            first_event["id"],
            second_event["id"],
            cycled_event["id"],
        }
    ) == 3
    assert json.loads(first_candidate["metadata_json"])[
        "observed_file_hash"
    ] != json.loads(second_candidate["metadata_json"])["observed_file_hash"]
    assert json.loads(cycled_candidate["metadata_json"])[
        "observed_file_hash"
    ] == json.loads(first_candidate["metadata_json"])["observed_file_hash"]
    assert cycled_event["status"] == "prepared"
    assert replay_event["id"] == cycled_event["id"]
    assert replay_candidate["id"] == cycled_candidate["id"]
    assert replay_revision_count == cycled_revision_count


@pytest.mark.parametrize("target_kind", ["invalid", "truncated"])
def test_invalid_or_truncated_recovery_unknown_is_observed_fail_closed(
    recovery_intent_fixture,
    target_kind,
):
    scenario = recovery_intent_fixture.materialize(target_kind, "expected")
    with connect_app(scenario.settings) as conn:
        revision_count_before = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()[0]

    result = IntentExecutor(
        scenario.settings,
        owner=f"{target_kind}-recovery",
    ).reconcile_one(scenario.intent_id, now=scenario.after_lease)

    observations = scenario.observations_for(scenario.target_bytes)
    with connect_app(scenario.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        revision_count_after = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()[0]
        invalid_reviews = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='invalid_frontmatter'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchall()
        jobs = conn.execute(
            """
            SELECT target,operation,revision_id FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch=? ORDER BY target
            """,
            (scenario.page_id, page["projection_epoch"]),
        ).fetchall()
    assert result.intent_status == "recovery_required"
    assert intent["status"] == "recovery_required"
    assert page["pending_write_intent_id"] == scenario.intent_id
    assert page["current_revision_id"] == scenario.current_revision_id
    assert page["lifecycle_status"] == "invalid"
    assert page["projection_epoch"] == scenario.baseline_epoch + 1
    assert revision_count_after == revision_count_before
    assert len(observations) == 1
    assert observations[0]["parse_status"] == "invalid"
    if target_kind == "truncated":
        assert observations[0]["content_bytes"] is None
        assert observations[0]["content_prefix"] is not None
        assert observations[0]["content_truncated"] == 1
        assert observations[0]["error_code"] == "file_too_large"
    else:
        assert observations[0]["content_bytes"] == scenario.target_bytes
        assert observations[0]["content_truncated"] == 0
    assert len(invalid_reviews) == 1
    assert invalid_reviews[0]["base_revision_id"] == scenario.current_revision_id
    assert invalid_reviews[0]["candidate_revision_id"] is None
    assert json.loads(invalid_reviews[0]["expected_state_json"])[
        "recovery_observation_ids"
    ] == [observations[0]["id"]]
    assert {(row["target"], row["operation"], row["revision_id"]) for row in jobs} == {
        ("rag", "delete", None),
        ("gbrain", "delete", None),
    }


def test_unknown_staged_recovery_bytes_are_observed_without_moving_target(
    recovery_intent_fixture,
):
    scenario = recovery_intent_fixture.materialize("expected", "missing")
    writer = AtomicVaultWriter(scenario.settings.vault_path)
    staged_path = writer.staged_path(scenario.intent_id)
    staged_bytes = _valid_wiki_variant(
        scenario.expected_bytes,
        "Unexpected staged recovery bytes.",
    )
    staged_path.write_bytes(staged_bytes)
    with connect_app(scenario.settings) as conn:
        revisions_before = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()[0]

    result = IntentExecutor(
        scenario.settings,
        owner="unknown-staged-recovery",
    ).reconcile_one(scenario.intent_id, now=scenario.after_lease)

    with connect_app(scenario.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        observation = conn.execute(
            """
            SELECT * FROM wiki_file_observations
            WHERE page_id=? AND file_hash=?
            """,
            (scenario.page_id, compute_file_hash(staged_bytes)),
        ).fetchone()
        revisions_after = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()[0]
    assert result.intent_status == "recovery_required"
    assert page["pending_write_intent_id"] == scenario.intent_id
    assert page["lifecycle_status"] == "invalid"
    assert intent["status"] == "recovery_required"
    assert observation is not None
    assert observation["parse_status"] == "valid"
    assert revisions_after == revisions_before
    assert scenario.target_path.read_bytes() == scenario.expected_bytes
    assert staged_path.read_bytes() == staged_bytes


def test_fail_closed_recovery_supersedes_stale_invalid_frontmatter_review(
    recovery_intent_fixture,
):
    scenario = recovery_intent_fixture.materialize("invalid", "expected")
    review_id = "review_stale_invalid_before_recovery"
    with connect_app(scenario.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO review_items(
              id,page_path,page_id,issue_type,status,owner,source_ids_json,
              created_at,updated_at,base_revision_id,candidate_revision_id,
              expected_state_json
            ) VALUES (?,?,?,'invalid_frontmatter','pending',?,?,?,?,?,NULL,'{}')
            """,
            (
                review_id,
                scenario.page_path,
                scenario.page_id,
                page["owner"],
                page["source_ids_json"],
                "t-stale-invalid",
                "t-stale-invalid",
                scenario.current_revision_id,
            ),
        )

    IntentExecutor(
        scenario.settings,
        owner="fail-closed-review-cleanup",
    ).reconcile_one(scenario.intent_id, now=scenario.after_lease)

    with connect_app(scenario.settings) as conn:
        review = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (review_id,),
        ).fetchone()
        pending = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='invalid_frontmatter'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchall()
    assert review["status"] == "superseded"
    assert review["resolved_at"] is not None
    assert len(pending) == 1
    assert pending[0]["id"] != review_id


def test_finalize_initializes_backup_monitor_baseline_from_captured_hash(
    applied_intent_fixture,
):
    scenario = applied_intent_fixture
    with connect_app(scenario.settings) as conn:
        intent = conn.execute(
            """
            SELECT captured_file_hash,backup_last_observed_hash,
                   backup_retention_status
            FROM vault_write_intents WHERE id=?
            """,
            (scenario.intent_id,),
        ).fetchone()
    assert intent["captured_file_hash"] == scenario.backup_hash
    assert intent["backup_last_observed_hash"] == scenario.backup_hash
    assert intent["backup_retention_status"] == "retained"


def test_retained_backup_each_hash_change_reconciles_one_concurrent_conflict(
    applied_intent_fixture,
):
    scenario = applied_intent_fixture
    first_bytes = _valid_wiki_variant(
        scenario.backup_bytes,
        "Late backup writer one.",
    )
    scenario.backup_path.write_bytes(first_bytes)
    monitor = IntentExecutor(scenario.settings, owner="backup-monitor")

    first = monitor.reconcile_retained_backups()
    replay = monitor.reconcile_retained_backups()

    with connect_app(scenario.settings) as conn:
        first_intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        first_observation = conn.execute(
            """
            SELECT * FROM wiki_file_observations
            WHERE page_id=? AND file_hash=?
            """,
            (scenario.page_id, compute_file_hash(first_bytes)),
        ).fetchone()
        first_review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
        first_candidate = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (first_review["candidate_revision_id"],),
        ).fetchone()
        first_metadata = json.loads(first_candidate["metadata_json"])
        first_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (first_metadata["vault_change_event_id"],),
        ).fetchone()
        counts_after_first = (
            conn.execute(
                "SELECT COUNT(*) FROM wiki_file_observations WHERE page_id=?",
                (scenario.page_id,),
            ).fetchone()[0],
            conn.execute(
                """
                SELECT COUNT(*) FROM wiki_page_revisions
                WHERE page_id=? AND origin='external'
                """,
                (scenario.page_id,),
            ).fetchone()[0],
        )
    assert first == [scenario.intent_id]
    assert replay == []
    assert first_intent["backup_retention_status"] == "change_detected"
    assert first_intent["backup_last_observed_hash"] == compute_file_hash(first_bytes)
    assert first_observation["content_bytes"] == first_bytes
    assert first_observation["parse_status"] == "valid"
    assert first_candidate["origin"] == "external"
    assert first_candidate["base_revision_id"] == scenario.current_revision_id
    assert first_event["status"] == "prepared"
    assert first_event["observation_id"] == first_observation["id"]
    assert first_event["result_revision_id"] is None
    assert first_event["result_payload_json"] is None

    second_bytes = _valid_wiki_variant(
        scenario.backup_bytes,
        "Late backup writer two.",
    )
    scenario.backup_path.write_bytes(second_bytes)
    second = monitor.reconcile_retained_backups()

    with connect_app(scenario.settings) as conn:
        pending = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchall()
        old_review = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (first_review["id"],),
        ).fetchone()
        old_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (first_metadata["vault_change_event_id"],),
        ).fetchone()
        new_candidate = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (pending[0]["candidate_revision_id"],),
        ).fetchone()
        new_metadata = json.loads(new_candidate["metadata_json"])
        new_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (new_metadata["vault_change_event_id"],),
        ).fetchone()
        counts_after_second = (
            conn.execute(
                "SELECT COUNT(*) FROM wiki_file_observations WHERE page_id=?",
                (scenario.page_id,),
            ).fetchone()[0],
            conn.execute(
                """
                SELECT COUNT(*) FROM wiki_page_revisions
                WHERE page_id=? AND origin='external'
                """,
                (scenario.page_id,),
            ).fetchone()[0],
        )
    assert second == [scenario.intent_id]
    assert len(pending) == 1
    assert pending[0]["candidate_revision_id"] != first_review["candidate_revision_id"]
    assert old_review["status"] == "superseded"
    assert old_event["status"] == "superseded"
    assert old_event["result_revision_id"] is None
    assert old_event["result_payload_json"] is None
    assert new_event["status"] == "prepared"
    assert new_event["result_revision_id"] is None
    assert new_event["result_payload_json"] is None
    assert counts_after_second == (
        counts_after_first[0] + 1,
        counts_after_first[1] + 1,
    )
    assert scenario.backup_path.exists()
    assert scenario.backup_path.read_bytes() == second_bytes

    scenario.backup_path.write_bytes(first_bytes)
    cycled = monitor.reconcile_retained_backups()
    with connect_app(scenario.settings) as conn:
        cycled_pending = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchall()
        second_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (new_metadata["vault_change_event_id"],),
        ).fetchone()
        cycled_candidate = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (cycled_pending[0]["candidate_revision_id"],),
        ).fetchone()
        cycled_metadata = json.loads(cycled_candidate["metadata_json"])
        cycled_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (cycled_metadata["vault_change_event_id"],),
        ).fetchone()
        counts_after_cycle = (
            conn.execute(
                "SELECT COUNT(*) FROM wiki_file_observations WHERE page_id=?",
                (scenario.page_id,),
            ).fetchone()[0],
            conn.execute(
                """
                SELECT COUNT(*) FROM wiki_page_revisions
                WHERE page_id=? AND origin='external'
                """,
                (scenario.page_id,),
            ).fetchone()[0],
        )
    assert cycled == [scenario.intent_id]
    assert len(cycled_pending) == 1
    assert second_event["status"] == "superseded"
    assert second_event["result_revision_id"] is None
    assert second_event["result_payload_json"] is None
    assert cycled_event["status"] == "prepared"
    assert cycled_event["result_revision_id"] is None
    assert cycled_event["result_payload_json"] is None
    assert cycled_event["id"] != first_event["id"]
    assert counts_after_cycle == (
        counts_after_second[0],
        counts_after_second[1] + 1,
    )


def test_retained_backup_monitor_continues_after_one_intent_fails(
    applied_intent_fixture,
    monkeypatch,
):
    scenario = applied_intent_fixture
    second_intent_id = "wint_monitor_after_failure"
    writer = AtomicVaultWriter(scenario.settings.vault_path)
    second_backup = writer.backup_path(second_intent_id)
    second_backup.write_bytes(scenario.backup_bytes)
    with connect_app(scenario.settings) as conn:
        conn.execute(
            """
            INSERT INTO vault_write_intents(
              id,page_id,revision_id,expected_revision_id,expected_file_hash,
              target_path,write_token,backup_path,captured_file_hash,
              backup_last_observed_hash,backup_retention_status,status,
              executor_owner,lease_expires_at,attempts,last_error,created_at,updated_at
            )
            SELECT ?,page_id,revision_id,expected_revision_id,expected_file_hash,
                   target_path,write_token,?,captured_file_hash,
                   backup_last_observed_hash,'retained','applied',
                   NULL,NULL,attempts,NULL,'9999-01-01','9999-01-01'
            FROM vault_write_intents WHERE id=?
            """,
            (second_intent_id, str(second_backup), scenario.intent_id),
        )
    scenario.backup_path.write_bytes(
        _valid_wiki_variant(scenario.backup_bytes, "Failing monitor intent.")
    )
    second_backup.write_bytes(
        _valid_wiki_variant(scenario.backup_bytes, "Later monitor intent.")
    )
    monitor = IntentExecutor(scenario.settings, owner="isolated-backup-monitor")
    calls: list[str] = []

    def reconcile_one(intent_id, observation):
        calls.append(intent_id)
        if len(calls) == 1:
            raise RevisionConflict(
                "simulated per-intent monitor failure",
                current_revision_id=scenario.current_revision_id,
            )
        return intent_id == second_intent_id

    monkeypatch.setattr(
        monitor.revisions,
        "reconcile_retained_backup",
        reconcile_one,
    )

    assert monitor.reconcile_retained_backups() == [second_intent_id]
    assert calls[-1] == second_intent_id
    assert len(calls) >= 2


@pytest.mark.parametrize("resolution", ["keep_current", "accept_candidate"])
def test_retained_backup_resolution_finishes_candidate_event(
    applied_intent_fixture,
    resolution,
):
    scenario = applied_intent_fixture
    changed_bytes = _valid_wiki_variant(
        scenario.backup_bytes,
        f"Late backup resolution: {resolution}.",
    )
    scenario.backup_path.write_bytes(changed_bytes)
    assert IntentExecutor(
        scenario.settings,
        owner=f"backup-resolution-{resolution}",
    ).reconcile_retained_backups() == [scenario.intent_id]

    with connect_app(scenario.settings) as conn:
        page_before = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
        candidate = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (review["candidate_revision_id"],),
        ).fetchone()
        metadata = json.loads(candidate["metadata_json"])
        event_id = metadata["vault_change_event_id"]

    result = WikiRevisionService(scenario.settings).resolve_conflict(
        ResolveConflictCommand(
            review_id=review["id"],
            resolution=resolution,
            merged_content=None,
            expected_current_revision_id=scenario.current_revision_id,
            expected_generated_revision_id=page_before["generated_revision_id"],
            request_id=f"resolve-backup-{resolution}",
            actor="backup-admin",
            note=None,
        )
    )

    with connect_app(scenario.settings) as conn:
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
    assert result.status == "resolved"
    assert event["status"] == (
        "superseded" if resolution == "keep_current" else "applied"
    )
    assert page["current_revision_id"] == (
        scenario.current_revision_id
        if resolution == "keep_current"
        else candidate["id"]
    )


def test_retained_backup_event_survives_generated_conflict_reconciliation(
    applied_intent_fixture,
):
    scenario = applied_intent_fixture
    scenario.backup_path.write_bytes(
        _valid_wiki_variant(
            scenario.backup_bytes,
            "Retained candidate before generated conflict.",
        )
    )
    assert IntentExecutor(
        scenario.settings,
        owner="backup-before-generated-conflict",
    ).reconcile_retained_backups() == [scenario.intent_id]
    service = WikiRevisionService(scenario.settings)
    before = service.get_page(scenario.page_path)
    with connect_app(scenario.settings) as conn:
        page_before = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        original_review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
        candidate = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (original_review["candidate_revision_id"],),
        ).fetchone()
    event_id = json.loads(candidate["metadata_json"])["vault_change_event_id"]

    service.apply_generated_candidate(
        CompileCandidateCommand(
            page_path=before.page_path,
            content=before.content + "\nGenerated content conflict.\n",
            domain=page_before["domain"],
            page_type=page_before["page_type"],
            title=page_before["title"],
            source_ids=json.loads(page_before["source_ids_json"]),
            owner=page_before["owner"],
            source_hash="retained-event-generated-conflict",
            compiler_version="wiki-revision-v1",
            compile_job_id="retained-event-generated-conflict",
        )
    )

    with connect_app(scenario.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()
        concurrent_review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
        content_review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='content_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
    assert content_review is not None
    assert concurrent_review["id"] == original_review["id"]
    assert concurrent_review["candidate_revision_id"] == candidate["id"]
    assert event["status"] == "prepared"
    assert event["expected_state_json"] == concurrent_review["expected_state_json"]

    resolved = service.resolve_conflict(
        ResolveConflictCommand(
            review_id=concurrent_review["id"],
            resolution="accept_candidate",
            merged_content=None,
            expected_current_revision_id=page["current_revision_id"],
            expected_generated_revision_id=page["generated_revision_id"],
            request_id="accept-retained-after-generated-conflict",
            actor="backup-admin",
            note=None,
        )
    )

    with connect_app(scenario.settings) as conn:
        final_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
    assert resolved.status == "resolved"
    assert resolved.current_revision_id == candidate["id"]
    assert final_event["status"] == "applied"


def test_merged_resolution_supersedes_retained_backup_candidate_event(
    applied_intent_fixture,
):
    scenario = applied_intent_fixture
    scenario.backup_path.write_bytes(
        _valid_wiki_variant(
            scenario.backup_bytes,
            "Late backup candidate before merge.",
        )
    )
    assert IntentExecutor(
        scenario.settings,
        owner="backup-before-merge",
    ).reconcile_retained_backups() == [scenario.intent_id]
    service = WikiRevisionService(scenario.settings)
    current = service.get_page(scenario.page_path)
    with connect_app(scenario.settings) as conn:
        review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
        candidate = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (review["candidate_revision_id"],),
        ).fetchone()
        event_id = json.loads(candidate["metadata_json"])[
            "vault_change_event_id"
        ]

    result = service.resolve_conflict(
        ResolveConflictCommand(
            review_id=review["id"],
            resolution="merged_content",
            merged_content=current.content + "\nMerged after backup candidate.\n",
            expected_current_revision_id=scenario.current_revision_id,
            expected_generated_revision_id=current.generated_revision_id,
            request_id="merge-retained-backup-candidate",
            actor="backup-admin",
            note=None,
        )
    )

    with connect_app(scenario.settings) as conn:
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        revision = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (result.current_revision_id,),
        ).fetchone()
    assert result.status == "resolved"
    assert revision["origin"] == "merge"
    assert event["status"] == "superseded"


def test_merged_candidate_event_stays_prepared_until_resolution_finalize(
    applied_intent_fixture,
    monkeypatch,
):
    scenario = applied_intent_fixture
    scenario.backup_path.write_bytes(
        _valid_wiki_variant(
            scenario.backup_bytes,
            "Late backup candidate before crashed merge.",
        )
    )
    IntentExecutor(
        scenario.settings,
        owner="backup-before-crashed-merge",
    ).reconcile_retained_backups()
    service = WikiRevisionService(scenario.settings)
    current = service.get_page(scenario.page_path)
    with connect_app(scenario.settings) as conn:
        review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
        candidate = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (review["candidate_revision_id"],),
        ).fetchone()
        event_id = json.loads(candidate["metadata_json"])[
            "vault_change_event_id"
        ]

    monkeypatch.setattr(IntentExecutor, "execute", lambda self, intent_id: None)
    with pytest.raises(RevisionConflict):
        service.resolve_conflict(
            ResolveConflictCommand(
                review_id=review["id"],
                resolution="merged_content",
                merged_content=current.content + "\nCrashed merged resolution.\n",
                expected_current_revision_id=scenario.current_revision_id,
                expected_generated_revision_id=current.generated_revision_id,
                request_id="crash-merged-backup-candidate",
                actor="backup-admin",
                note=None,
            )
        )

    with connect_app(scenario.settings) as conn:
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        pending_review = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (review["id"],),
        ).fetchone()
    assert event["status"] == "prepared"
    assert pending_review["status"] == "pending"


def test_retained_backup_change_uses_current_page_path_after_rename(
    applied_intent_fixture,
):
    scenario = applied_intent_fixture
    service = WikiRevisionService(scenario.settings)
    old_target = scenario.settings.vault_path / scenario.page_path
    new_path = "wiki/product/faq/intent-renamed-after-apply.md"
    old_target.rename(scenario.settings.vault_path / new_path)
    service.rename_page("rename-before-backup-monitor", scenario.page_path, new_path)
    changed_bytes = _valid_wiki_variant(
        scenario.backup_bytes,
        "Late write after page rename.",
    )
    scenario.backup_path.write_bytes(changed_bytes)

    changed = IntentExecutor(
        scenario.settings,
        owner="renamed-backup-monitor",
    ).reconcile_retained_backups()

    with connect_app(scenario.settings) as conn:
        observation = conn.execute(
            """
            SELECT * FROM wiki_file_observations
            WHERE page_id=? AND file_hash=?
            """,
            (scenario.page_id, compute_file_hash(changed_bytes)),
        ).fetchone()
        review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
    assert changed == [scenario.intent_id]
    assert observation["page_id"] == scenario.page_id
    assert observation["page_path"] == new_path
    assert review["page_path"] == new_path
    assert scenario.backup_path.exists()


@pytest.mark.parametrize("payload_kind", ["invalid", "truncated"])
def test_retained_backup_invalid_or_truncated_change_stores_only_observation(
    applied_intent_fixture,
    payload_kind,
):
    scenario = applied_intent_fixture
    if payload_kind == "invalid":
        changed_bytes = b"not a wiki backup"
    else:
        changed_bytes = (
            b"---\ntitle: Large backup\nsource_ids: [src_intent]\n"
            b"review_status: draft\n---\n# Large\n"
            + (b"z" * 5_300_000)
        )
    scenario.backup_path.write_bytes(changed_bytes)
    with connect_app(scenario.settings) as conn:
        revisions_before = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()[0]

    changed = IntentExecutor(
        scenario.settings,
        owner=f"{payload_kind}-backup-monitor",
    ).reconcile_retained_backups()

    with connect_app(scenario.settings) as conn:
        observation = conn.execute(
            """
            SELECT * FROM wiki_file_observations
            WHERE page_id=? AND file_hash=?
            """,
            (scenario.page_id, compute_file_hash(changed_bytes)),
        ).fetchone()
        revisions_after = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (scenario.page_id,),
        ).fetchone()[0]
        pending = conn.execute(
            """
            SELECT COUNT(*) FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()[0]
    assert changed == [scenario.intent_id]
    assert observation["parse_status"] == "invalid"
    assert revisions_after == revisions_before
    assert pending == 0
    if payload_kind == "truncated":
        assert observation["content_bytes"] is None
        assert observation["content_prefix"] is not None
        assert observation["content_truncated"] == 1
    else:
        assert observation["content_bytes"] == changed_bytes
        assert observation["content_truncated"] == 0
    assert scenario.backup_path.exists()


def test_retained_backup_invalid_change_supersedes_prior_valid_candidate(
    applied_intent_fixture,
):
    scenario = applied_intent_fixture
    valid_bytes = _valid_wiki_variant(
        scenario.backup_bytes,
        "Valid retained candidate before invalid bytes.",
    )
    scenario.backup_path.write_bytes(valid_bytes)
    monitor = IntentExecutor(
        scenario.settings,
        owner="valid-then-invalid-backup-monitor",
    )
    assert monitor.reconcile_retained_backups() == [scenario.intent_id]

    with connect_app(scenario.settings) as conn:
        review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()
        candidate = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (review["candidate_revision_id"],),
        ).fetchone()
    event_id = json.loads(candidate["metadata_json"])["vault_change_event_id"]

    invalid_bytes = b"---\ntitle: [invalid retained backup\n---\n# Broken\n"
    scenario.backup_path.write_bytes(invalid_bytes)
    assert monitor.reconcile_retained_backups() == [scenario.intent_id]

    with connect_app(scenario.settings) as conn:
        superseded_review = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (review["id"],),
        ).fetchone()
        superseded_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        pending = conn.execute(
            """
            SELECT COUNT(*) FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (scenario.page_id,),
        ).fetchone()[0]
        observation = conn.execute(
            """
            SELECT * FROM wiki_file_observations
            WHERE page_id=? AND file_hash=?
            """,
            (scenario.page_id, compute_file_hash(invalid_bytes)),
        ).fetchone()
    assert superseded_review["status"] == "superseded"
    assert superseded_event["status"] == "superseded"
    assert pending == 0
    assert observation["parse_status"] == "invalid"


def test_retained_backup_release_revalidates_hash_fsyncs_and_audits(
    applied_intent_fixture,
    monkeypatch,
):
    scenario = applied_intent_fixture
    service = WikiRevisionService(scenario.settings)
    fsynced: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        service.writer,
        "_fsync_directory",
        lambda path, *, strict=False: fsynced.append((path, strict)),
    )

    with pytest.raises(RevisionConflict):
        service.release_retained_backup(
            scenario.intent_id,
            "wrong-hash",
            actor="admin",
        )
    with pytest.raises(RevisionConflict):
        service.release_retained_backup(
            scenario.intent_id,
            scenario.backup_hash,
            actor="  ",
        )

    result = service.release_retained_backup(
        scenario.intent_id,
        scenario.backup_hash,
        actor="admin",
    )

    with connect_app(scenario.settings) as conn:
        intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        audit_row = conn.execute(
            """
            SELECT payload_json FROM audit_logs
            WHERE event_type='vault_backup_released'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        observations = conn.execute(
            """
            SELECT id FROM wiki_file_observations
            WHERE page_id=? AND file_hash=? ORDER BY id
            """,
            (scenario.page_id, scenario.backup_hash),
        ).fetchall()
    payload = json.loads(audit_row["payload_json"])
    assert result.status == "released"
    assert result.intent_id == scenario.intent_id
    assert result.page_id == scenario.page_id
    assert result.backup_hash == scenario.backup_hash
    assert intent["backup_retention_status"] == "released"
    assert not scenario.backup_path.exists()
    snapshot_root = service.writer.pending_root / ".release-snapshots"
    assert fsynced == [
        (snapshot_root.parent, True),
        (snapshot_root, True),
        (scenario.backup_path.parent, True),
    ]
    assert payload["intent_id"] == scenario.intent_id
    assert payload["page_id"] == scenario.page_id
    assert payload["current_revision_id"] == scenario.current_revision_id
    assert payload["current_file_hash"]
    assert payload["backup_hash"] == scenario.backup_hash
    assert payload["observation_ids"] == [row["id"] for row in observations]

    replay = service.release_retained_backup(
        scenario.intent_id,
        scenario.backup_hash,
        actor="admin",
    )
    with connect_app(scenario.settings) as conn:
        replay_audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE event_type='vault_backup_released'
            """
        ).fetchone()[0]
    assert replay == result
    assert replay_audit_count == 1

    reappeared = _valid_wiki_variant(
        scenario.backup_bytes,
        "Backup path reappeared after release.",
    )
    scenario.backup_path.write_bytes(reappeared)
    with pytest.raises(RevisionConflict):
        service.release_retained_backup(
            scenario.intent_id,
            scenario.backup_hash,
            actor="admin",
        )
    assert scenario.backup_path.read_bytes() == reappeared


def test_retained_backup_release_hash_race_keeps_retained(
    applied_intent_fixture,
    monkeypatch,
):
    scenario = applied_intent_fixture
    service = WikiRevisionService(scenario.settings)
    original_hash = service.writer._stream_hash
    calls = 0
    raced_bytes = _valid_wiki_variant(
        scenario.backup_bytes,
        "Release hash race.",
    )

    def race_before_revalidation(path):
        nonlocal calls
        calls += 1
        if calls == 3:
            path.write_bytes(raced_bytes)
        return original_hash(path)

    monkeypatch.setattr(service.writer, "_stream_hash", race_before_revalidation)

    with pytest.raises(RevisionConflict):
        service.release_retained_backup(
            scenario.intent_id,
            scenario.backup_hash,
            actor="admin",
        )

    with connect_app(scenario.settings) as conn:
        intent = conn.execute(
            "SELECT backup_retention_status FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE event_type='vault_backup_released'
            """
        ).fetchone()[0]
    assert intent["backup_retention_status"] == "retained"
    assert scenario.backup_path.exists()
    assert scenario.backup_path.read_bytes() == raced_bytes
    assert audit_count == 0


def test_retained_backup_release_durably_snapshots_before_canonical_claim(
    applied_intent_fixture,
    monkeypatch,
):
    scenario = applied_intent_fixture
    service = WikiRevisionService(scenario.settings)
    snapshot_root = service.writer.pending_root / ".release-snapshots"
    events: list[tuple[str, Path, bool | None]] = []
    original_replace = Path.replace

    def tracked_replace(path, destination):
        if path == scenario.backup_path:
            events.append(("claim", Path(destination), None))
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", tracked_replace)
    monkeypatch.setattr(
        service.writer,
        "_fsync_existing_file",
        lambda path: events.append(("file", Path(path), None)),
        raising=False,
    )
    monkeypatch.setattr(
        service.writer,
        "_fsync_directory",
        lambda path, *, strict=False: events.append(
            ("directory", Path(path), strict)
        ),
    )

    service.release_retained_backup(
        scenario.intent_id,
        scenario.backup_hash,
        actor="admin",
    )

    claim_index = next(
        index for index, event in enumerate(events) if event[0] == "claim"
    )
    snapshot_file_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "file" and event[1].parent == snapshot_root
    )
    snapshot_root_index = events.index(("directory", snapshot_root, True))
    snapshot_parent_index = events.index(
        ("directory", snapshot_root.parent, True)
    )
    assert snapshot_parent_index < snapshot_file_index
    assert snapshot_file_index < snapshot_root_index < claim_index


def test_retained_backup_release_interruption_after_displacement_keeps_evidence(
    applied_intent_fixture,
    monkeypatch,
):
    scenario = applied_intent_fixture
    service = WikiRevisionService(scenario.settings)
    original_replace = Path.replace

    def interrupt_after_displacement(path, destination):
        result = original_replace(path, destination)
        if path == scenario.backup_path:
            raise SystemExit("interrupt after canonical displacement")
        return result

    monkeypatch.setattr(Path, "replace", interrupt_after_displacement)

    with pytest.raises(SystemExit, match="interrupt after canonical"):
        service.release_retained_backup(
            scenario.intent_id,
            scenario.backup_hash,
            actor="admin",
        )

    snapshot_root = service.writer.pending_root / ".release-snapshots"
    snapshots = list(snapshot_root.glob(f"{scenario.intent_id}-*.bak"))
    tombstones = list(
        scenario.backup_path.parent.glob(
            f"{scenario.backup_path.name}.release-*"
        )
    )
    with connect_app(scenario.settings) as conn:
        intent = conn.execute(
            "SELECT backup_retention_status FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE event_type='vault_backup_released'
            """
        ).fetchone()[0]
    assert not scenario.backup_path.exists()
    assert len(tombstones) == 1
    assert tombstones[0].read_bytes() == scenario.backup_bytes
    assert len(snapshots) == 1
    assert snapshots[0].read_bytes() == scenario.backup_bytes
    assert intent["backup_retention_status"] == "retained"
    assert audit_count == 0


def test_retained_backup_release_path_replacement_is_not_unlinked(
    applied_intent_fixture,
    monkeypatch,
):
    scenario = applied_intent_fixture
    service = WikiRevisionService(scenario.settings)
    original_hash = service.writer._stream_hash
    calls = 0
    replacement = _valid_wiki_variant(
        scenario.backup_bytes,
        "Replacement after release hash.",
    )
    displaced = scenario.backup_path.with_name("displaced-before-release.md")

    def replace_path_after_hash(path):
        nonlocal calls
        calls += 1
        observed = original_hash(path)
        if calls == 3:
            if scenario.backup_path.exists():
                scenario.backup_path.replace(displaced)
            scenario.backup_path.write_bytes(replacement)
        return observed

    monkeypatch.setattr(
        service.writer,
        "_stream_hash",
        replace_path_after_hash,
    )

    with pytest.raises(OSError) as exc_info:
        service.release_retained_backup(
            scenario.intent_id,
            scenario.backup_hash,
            actor="admin",
        )

    with connect_app(scenario.settings) as conn:
        intent = conn.execute(
            "SELECT backup_retention_status FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE event_type='vault_backup_released'
            """
        ).fetchone()[0]
    assert intent["backup_retention_status"] == "retained"
    assert scenario.backup_path.exists()
    assert scenario.backup_path.read_bytes() == replacement
    collision_evidence = list(
        scenario.backup_path.parent.glob(
            f"{scenario.backup_path.name}.release-*"
        )
    )
    snapshot_root = service.writer.pending_root / ".release-snapshots"
    snapshots = list(snapshot_root.glob(f"{scenario.intent_id}-*.bak"))
    assert len(collision_evidence) == 1
    assert collision_evidence[0].read_bytes() == scenario.backup_bytes
    assert len(snapshots) == 1
    for path in (scenario.backup_path, collision_evidence[0], snapshots[0]):
        assert str(path) in str(exc_info.value)
    assert audit_count == 0


def test_retained_backup_release_restore_collision_preserves_every_artifact(
    applied_intent_fixture,
    monkeypatch,
):
    scenario = applied_intent_fixture
    service = WikiRevisionService(scenario.settings)
    original_hash = service.writer._stream_hash
    original_link = os.link
    original_replace = Path.replace
    replacement = _valid_wiki_variant(
        scenario.backup_bytes,
        "Canonical recreated during restore.",
    )

    def fail_after_claim(path):
        path = Path(path)
        if path.name.startswith(f"{scenario.backup_path.name}.release-"):
            raise OSError("force retained backup rollback")
        return original_hash(path)

    def collide_with_old_replace(path, destination):
        destination = Path(destination)
        if (
            path.name.startswith(f"{scenario.backup_path.name}.release-")
            and destination == scenario.backup_path
        ):
            scenario.backup_path.write_bytes(replacement)
        return original_replace(path, destination)

    def collide_with_no_replace_link(source, destination, *args, **kwargs):
        source = Path(source)
        destination = Path(destination)
        if (
            source.name.startswith(f"{scenario.backup_path.name}.release-")
            and destination == scenario.backup_path
        ):
            scenario.backup_path.write_bytes(replacement)
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(service.writer, "_stream_hash", fail_after_claim)
    monkeypatch.setattr(Path, "replace", collide_with_old_replace)
    monkeypatch.setattr(
        "app.wiki_revisions.os.link",
        collide_with_no_replace_link,
    )

    with pytest.raises(OSError) as exc_info:
        service.release_retained_backup(
            scenario.intent_id,
            scenario.backup_hash,
            actor="admin",
        )

    snapshot_root = service.writer.pending_root / ".release-snapshots"
    snapshots = list(snapshot_root.glob(f"{scenario.intent_id}-*.bak"))
    tombstones = list(
        scenario.backup_path.parent.glob(
            f"{scenario.backup_path.name}.release-*"
        )
    )
    assert scenario.backup_path.read_bytes() == replacement
    assert len(tombstones) == 1
    assert tombstones[0].read_bytes() == scenario.backup_bytes
    assert len(snapshots) == 1
    assert snapshots[0].read_bytes() == scenario.backup_bytes
    for path in (scenario.backup_path, tombstones[0], snapshots[0]):
        assert str(path) in str(exc_info.value)


def test_retained_backup_failed_restore_keeps_evidence_after_post_link_replacement(
    applied_intent_fixture,
    monkeypatch,
):
    scenario = applied_intent_fixture
    service = WikiRevisionService(scenario.settings)
    original_hash = service.writer._stream_hash
    original_restore = wiki_revisions_module._link_or_copy_file_no_replace
    replacement = _valid_wiki_variant(
        scenario.backup_bytes,
        "Canonical replaced after restore link.",
    )
    displaced_restore = scenario.backup_path.with_name(
        "restored-before-post-link-replacement.md"
    )

    def fail_after_claim(path):
        path = Path(path)
        if path.name.startswith(f"{scenario.backup_path.name}.release-"):
            raise OSError("force retained backup rollback")
        return original_hash(path)

    def replace_after_restore(source, destination):
        original_restore(source, destination)
        source = Path(source)
        destination = Path(destination)
        if (
            source.name.startswith(f"{scenario.backup_path.name}.release-")
            and destination == scenario.backup_path
        ):
            scenario.backup_path.replace(displaced_restore)
            scenario.backup_path.write_bytes(replacement)

    monkeypatch.setattr(service.writer, "_stream_hash", fail_after_claim)
    monkeypatch.setattr(
        wiki_revisions_module,
        "_link_or_copy_file_no_replace",
        replace_after_restore,
    )

    with pytest.raises(OSError):
        service.release_retained_backup(
            scenario.intent_id,
            scenario.backup_hash,
            actor="admin",
        )

    snapshot_root = service.writer.pending_root / ".release-snapshots"
    snapshots = list(snapshot_root.glob(f"{scenario.intent_id}-*.bak"))
    tombstones = list(
        scenario.backup_path.parent.glob(
            f"{scenario.backup_path.name}.release-*"
        )
    )
    with connect_app(scenario.settings) as conn:
        intent = conn.execute(
            "SELECT backup_retention_status FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE event_type='vault_backup_released'
            """
        ).fetchone()[0]
    assert scenario.backup_path.read_bytes() == replacement
    assert displaced_restore.read_bytes() == scenario.backup_bytes
    assert len(tombstones) == 1
    assert tombstones[0].read_bytes() == scenario.backup_bytes
    assert len(snapshots) == 1
    assert snapshots[0].read_bytes() == scenario.backup_bytes
    assert intent["backup_retention_status"] == "retained"
    assert audit_count == 0


@pytest.mark.parametrize("failure", ["file", "directory"])
def test_retained_backup_release_restore_sync_failure_preserves_evidence(
    applied_intent_fixture,
    monkeypatch,
    failure,
):
    scenario = applied_intent_fixture
    service = WikiRevisionService(scenario.settings)
    original_hash = service.writer._stream_hash

    def fail_after_claim(path):
        path = Path(path)
        if path.name.startswith(f"{scenario.backup_path.name}.release-"):
            raise OSError("force retained backup rollback")
        return original_hash(path)

    def sync_file(path):
        if failure == "file" and Path(path) == scenario.backup_path:
            raise OSError("restored file fsync failure")

    def sync_directory(path, *, strict=False):
        tombstone_exists = any(
            scenario.backup_path.parent.glob(
                f"{scenario.backup_path.name}.release-*"
            )
        )
        if (
            failure == "directory"
            and Path(path) == scenario.backup_path.parent
            and scenario.backup_path.exists()
            and tombstone_exists
        ):
            raise OSError("restored directory fsync failure")

    monkeypatch.setattr(service.writer, "_stream_hash", fail_after_claim)
    monkeypatch.setattr(
        service.writer,
        "_fsync_existing_file",
        sync_file,
        raising=False,
    )
    monkeypatch.setattr(service.writer, "_fsync_directory", sync_directory)

    with pytest.raises(OSError) as exc_info:
        service.release_retained_backup(
            scenario.intent_id,
            scenario.backup_hash,
            actor="admin",
        )

    snapshot_root = service.writer.pending_root / ".release-snapshots"
    snapshots = list(snapshot_root.glob(f"{scenario.intent_id}-*.bak"))
    tombstones = list(
        scenario.backup_path.parent.glob(
            f"{scenario.backup_path.name}.release-*"
        )
    )
    assert scenario.backup_path.read_bytes() == scenario.backup_bytes
    assert len(tombstones) == 1
    assert tombstones[0].read_bytes() == scenario.backup_bytes
    assert len(snapshots) == 1
    assert snapshots[0].read_bytes() == scenario.backup_bytes
    assert "restore failed" in str(exc_info.value)


@pytest.mark.parametrize("failure", ["unlink", "fsync"])
def test_retained_backup_release_filesystem_failure_restores_retained_state(
    applied_intent_fixture,
    monkeypatch,
    failure,
):
    scenario = applied_intent_fixture
    service = WikiRevisionService(scenario.settings)
    original_unlink = Path.unlink

    if failure == "unlink":
        def fail_unlink(path, *args, **kwargs):
            if (
                path == scenario.backup_path
                or (
                    path.parent == scenario.backup_path.parent
                    and path.name.startswith(
                        f"{scenario.backup_path.name}.release-"
                    )
                )
            ):
                raise OSError("simulated unlink failure")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_unlink)
    else:
        failed = False

        def fail_canonical_directory_sync(path, *, strict):
            nonlocal failed
            tombstone_exists = any(
                scenario.backup_path.parent.glob(
                    f"{scenario.backup_path.name}.release-*"
                )
            )
            if (
                not failed
                and Path(path) == scenario.backup_path.parent
                and not scenario.backup_path.exists()
                and not tombstone_exists
            ):
                failed = True
                raise OSError("simulated fsync failure")

        monkeypatch.setattr(
            service.writer,
            "_fsync_directory",
            fail_canonical_directory_sync,
        )

    with pytest.raises(OSError):
        service.release_retained_backup(
            scenario.intent_id,
            scenario.backup_hash,
            actor="admin",
        )

    with connect_app(scenario.settings) as conn:
        intent = conn.execute(
            "SELECT backup_retention_status FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE event_type='vault_backup_released'
            """
        ).fetchone()[0]
    assert intent["backup_retention_status"] == "retained"
    assert scenario.backup_path.exists()
    assert scenario.backup_path.read_bytes() == scenario.backup_bytes
    assert audit_count == 0


def test_retained_backup_release_restore_failure_preserves_recovery_artifact(
    applied_intent_fixture,
    monkeypatch,
):
    scenario = applied_intent_fixture
    service = WikiRevisionService(scenario.settings)
    original_copy = wiki_revisions_module._copy_file_no_replace
    link_calls: list[tuple[Path, Path]] = []
    copy_calls: list[tuple[Path, Path]] = []

    def fail_link(source, destination, *args, **kwargs):
        link_calls.append((Path(source), Path(destination)))
        raise OSError("simulated hardlink failure")

    def copy_snapshot_then_fail_restore(source, destination, *args, **kwargs):
        copy_calls.append((Path(source), Path(destination)))
        if len(copy_calls) == 1:
            return original_copy(source, destination, *args, **kwargs)
        raise OSError("simulated restore copy failure")

    monkeypatch.setattr("app.wiki_revisions.os.link", fail_link)
    monkeypatch.setattr(
        wiki_revisions_module,
        "_copy_file_no_replace",
        copy_snapshot_then_fail_restore,
    )
    failed = False

    def fail_canonical_directory_sync(path, *, strict):
        nonlocal failed
        tombstone_exists = any(
            scenario.backup_path.parent.glob(
                f"{scenario.backup_path.name}.release-*"
            )
        )
        if (
            not failed
            and Path(path) == scenario.backup_path.parent
            and not scenario.backup_path.exists()
            and not tombstone_exists
        ):
            failed = True
            raise OSError("simulated strict fsync failure")

    monkeypatch.setattr(
        service.writer,
        "_fsync_directory",
        fail_canonical_directory_sync,
    )

    with pytest.raises(OSError) as exc_info:
        service.release_retained_backup(
            scenario.intent_id,
            scenario.backup_hash,
            actor="admin",
        )

    snapshot_root = service.writer.pending_root / ".release-snapshots"
    snapshots = list(snapshot_root.glob(f"{scenario.intent_id}-*.bak"))
    tombstones = list(
        scenario.backup_path.parent.glob(
            f"{scenario.backup_path.name}.release-*"
        )
    )
    evidence = [
        path
        for path in (scenario.backup_path, *tombstones, *snapshots)
        if path.exists()
    ]
    with connect_app(scenario.settings) as conn:
        intent = conn.execute(
            "SELECT backup_retention_status FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE event_type='vault_backup_released'
            """
        ).fetchone()[0]

    assert len(link_calls) == 2
    assert len(copy_calls) == 2
    assert not scenario.backup_path.exists()
    assert tombstones == []
    assert len(snapshots) == 1
    assert snapshots[0].read_bytes() == scenario.backup_bytes
    assert evidence == snapshots
    assert str(snapshots[0]) in str(exc_info.value)
    assert intent["backup_retention_status"] == "retained"
    assert audit_count == 0
