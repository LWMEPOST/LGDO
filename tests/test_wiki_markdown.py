import os
from pathlib import Path

import pytest

from app.wiki_markdown import (
    FrontmatterLimits,
    MarkdownParseError,
    ObservationChanged,
    capture_file_observation,
    compute_file_hash,
    compute_semantic_hash,
    parse_wiki_bytes,
    render_managed_frontmatter,
)


def test_round_trip_preserves_comments_order_and_body_while_managed_fields_change():
    raw = (
        b"---\r\n"
        b"title: Demo # keep title comment\r\n"
        b"source_ids: [src_1]\r\n"
        b"custom: 'quoted'\r\n"
        b"lgdo_revision_id: wrev_old\r\n"
        b"---\r\n\r\n# Demo\r\n\r\nHuman body.\r\n"
    )
    document = parse_wiki_bytes(raw)

    rendered = render_managed_frontmatter(
        document,
        page_id="page_1",
        revision_id="wrev_2",
        write_token="write_2",
        review_status="reviewed",
    )

    assert b"# keep title comment" in rendered
    assert rendered.index(b"title:") < rendered.index(b"source_ids:") < rendered.index(b"custom:")
    assert b"custom: 'quoted'" in rendered
    assert b"# Demo\r\n\r\nHuman body.\r\n" in rendered
    assert b"lgdo_page_id: page_1" in rendered
    assert b"lgdo_revision_id: wrev_2" in rendered
    assert b"lgdo_write_token: write_2" in rendered
    assert b"review_status: reviewed" in rendered


def test_semantic_hash_ignores_managed_fields_comments_and_newline_style():
    first = parse_wiki_bytes(
        b"---\ntitle: Demo # one\nsource_ids: [src_1]\nlgdo_revision_id: old\n---\nBody\n"
    )
    second = parse_wiki_bytes(
        b"---\r\nsource_ids:\r\n  - src_1\r\ntitle: Demo # two\r\nlgdo_revision_id: new\r\n---\r\nBody\r\n"
    )

    assert compute_file_hash(first.raw_bytes) != compute_file_hash(second.raw_bytes)
    assert compute_semantic_hash(first) == compute_semantic_hash(second)


@pytest.mark.parametrize(
    "raw,error_code",
    [
        (b"\xff\xfe", "invalid_utf8"),
        (b"---\ntitle: [broken\n---\nBody", "invalid_yaml"),
        (b"---\na: &x [1]\nb: *x\n---\nBody", "yaml_alias_limit"),
        (b"---\na: !python/object value\n---\nBody", "yaml_tag_forbidden"),
    ],
)
def test_parser_fails_closed_with_stable_error_codes(raw: bytes, error_code: str):
    with pytest.raises(MarkdownParseError) as exc_info:
        parse_wiki_bytes(raw, FrontmatterLimits(max_aliases=0))
    assert exc_info.value.code == error_code


def test_capture_file_observation_streams_large_file_and_keeps_only_prefix(tmp_path: Path):
    path = tmp_path / "large.md"
    path.write_bytes(b"a" * 200_000)

    observed = capture_file_observation(path, max_content_bytes=1_024, prefix_bytes=64)

    assert observed.size_bytes == 200_000
    assert observed.content_bytes is None
    assert observed.content_prefix == b"a" * 64
    assert observed.content_truncated is True
    assert observed.file_hash == compute_file_hash(b"a" * 200_000)


def test_capture_file_observation_rejects_file_that_grows_while_reading(tmp_path: Path, monkeypatch):
    path = tmp_path / "growing.md"
    path.write_bytes(b"a" * 128)
    real_open = Path.open

    class GrowingReader:
        def __init__(self, handle):
            self.handle = handle
            self.grown = False

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self.handle.__exit__(exc_type, exc, tb)

        def read(self, size: int) -> bytes:
            chunk = self.handle.read(size)
            if chunk and not self.grown:
                self.grown = True
                descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
                try:
                    os.write(descriptor, b"b" * 200_000)
                finally:
                    os.close(descriptor)
            return chunk

    def growing_open(self: Path, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        return GrowingReader(handle) if self == path and "b" in args[0] else handle

    monkeypatch.setattr(Path, "open", growing_open)

    with pytest.raises(ObservationChanged):
        capture_file_observation(path, max_content_bytes=1_024, prefix_bytes=64)
