import io
import json

from app.config import get_settings
from app.llm import stream_generate_answer


class FakeSSEResponse:
    def __init__(self, lines: list[bytes]):
        self._raw = io.BytesIO(b"\n".join(lines))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __iter__(self):
        return self._raw


def test_stream_generate_answer_yields_deepseek_sse_chunks(monkeypatch):
    settings = get_settings().model_copy()
    settings.deepseek_api_key = "test-key"
    settings.deepseek_model = "deepseek-test"

    payloads = [
        {"choices": [{"delta": {"content": "第一段"}}]},
        {"choices": [{"delta": {"content": "第二段"}}]},
    ]
    lines = [
        b"data: " + json.dumps(payloads[0]).encode("utf-8"),
        b"",
        b"data: " + json.dumps(payloads[1]).encode("utf-8"),
        b"data: [DONE]",
    ]

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout=30: FakeSSEResponse(lines))

    chunks = list(stream_generate_answer(settings, "问题", ["资料"], "detail"))

    assert chunks == ["第一段", "第二段"]


def test_stream_generate_answer_yields_nothing_without_credentials():
    settings = get_settings().model_copy()
    settings.deepseek_api_key = None
    settings.deepseek_model = None

    assert list(stream_generate_answer(settings, "问题", ["资料"], "detail")) == []
