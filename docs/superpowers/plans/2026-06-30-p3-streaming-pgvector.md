# P3 Streaming And Pgvector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a backend NDJSON streaming answer endpoint and use pgvector SQL candidate preselection for Postgres RAG while preserving existing `/ask` behavior.

**Architecture:** Split `app/search.py::ask()` into shared retrieval assembly, answer finalization, synchronous response, and streaming events. Add `app/llm.py::stream_generate_answer()` for DeepSeek-compatible SSE chunks, expose `/ask/stream` in `app/api.py`, and update `app/pg_rag.py::search_pg_chunks()` to use `<=>` preselection when pgvector is available before the existing Python reranker.

**Tech Stack:** Python 3, FastAPI `StreamingResponse`, Pydantic models, urllib SSE parsing, psycopg/Postgres pgvector, pytest.

---

## File Structure

- Modify `app/llm.py`: add DeepSeek-compatible streaming payload and SSE parsing.
- Modify `tests/test_llm_streaming.py`: add focused parser/fallback tests for `stream_generate_answer()`.
- Modify `app/search.py`: extract shared retrieval assembly/finalization, keep `ask()` compatible, add `stream_ask_events()`.
- Modify `app/api.py`: add `/ask/stream` endpoint returning `StreamingResponse`.
- Modify `tests/test_streaming_ask.py`: add endpoint tests for metadata, delta, and done events.
- Modify `app/pg_rag.py`: add pgvector candidate query path with safe fallback.
- Modify `tests/test_pg_rag_store.py`: add unit-style cursor tests for pgvector SQL selection and fallback.

---

### Task 1: LLM SSE Streaming

**Files:**
- Modify: `app/llm.py`
- Create: `tests/test_llm_streaming.py`

- [ ] **Step 1: Write the failing SSE parser test**

Create `tests/test_llm_streaming.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
python -m pytest tests/test_llm_streaming.py -q
```

Expected: FAIL because `stream_generate_answer` does not exist.

- [ ] **Step 3: Implement `stream_generate_answer()`**

In `app/llm.py`, add a helper after `generate_answer()`:

```python
def _iter_sse_lines(response) -> Iterator[str]:
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="ignore").strip()
        if line:
            yield line
```

Add the streaming function:

```python
def stream_generate_answer(
    settings: Settings,
    question: str,
    context_blocks: list[str],
    answer_mode: str = "detail",
    *,
    memory_blocks: list[str] | None = None,
) -> Iterator[str]:
    if not settings.deepseek_api_key or not settings.deepseek_model:
        return

    prompt = build_prompt(question, context_blocks, answer_mode, memory_blocks or [])
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是内部产品/客服知识库助手。只能基于给定资料回答；资料不足时明确说明缺口。"
                    "回答必须保留资料中的关键数字、单位、日期、英文参数名、错误码和金额，不要改写成近义但丢失精确信息的表达。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "stream": True,
    }
    request = urllib.request.Request(
        f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            for line in _iter_sse_lines(response):
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                try:
                    body = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for choice in body.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield content
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return
```

Ensure imports include:

```python
from collections.abc import Iterator
```

- [ ] **Step 4: Run Task 1 tests to verify GREEN**

Run:

```bash
python -m pytest tests/test_llm_streaming.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add app/llm.py tests/test_llm_streaming.py
git commit -m "feat: add llm streaming generator"
```

---

### Task 2: Shared Ask Assembly And Streaming Endpoint

**Files:**
- Modify: `app/search.py`
- Modify: `app/api.py`
- Create: `tests/test_streaming_ask.py`

- [ ] **Step 1: Write failing streaming endpoint test**

Create `tests/test_streaming_ask.py`:

```python
import json

from fastapi.testclient import TestClient

from app.api import app
from app.config import get_settings


def _configure_sqlite_settings(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-test")
    return settings


def test_ask_stream_endpoint_emits_metadata_delta_and_done(tmp_path, monkeypatch):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "policy.md").write_text(
        "# 退款制度\n\n客户在签收后 7 天内可以申请退款，逾期需要主管审批。",
        encoding="utf-8",
    )
    _configure_sqlite_settings(tmp_path, monkeypatch)
    monkeypatch.setattr("app.search.stream_generate_answer", lambda *args, **kwargs: iter(["答案", "片段"]))

    client = TestClient(app)
    assert client.post(
        "/api/internal/sources/scan",
        json={"root_path": str(sample_dir), "domain": "customer_service", "owner": "tester", "acl_tags": ["internal"]},
    ).status_code == 200
    assert client.post("/api/internal/wiki/compile", json={"domain": "customer_service"}).status_code == 200

    with client.stream(
        "POST",
        "/api/internal/ask/stream",
        json={"question": "退款期限是多久？", "domain": "customer_service", "acl_tags": ["internal"]},
    ) as response:
        assert response.status_code == 200
        events = [json.loads(line) for line in response.iter_lines() if line]

    assert [event["event"] for event in events] == ["metadata", "answer_delta", "answer_delta", "done"]
    assert events[1]["text"] == "答案"
    done = events[-1]["response"]
    assert done["answer"] == "答案片段"
    assert done["confidence"] in {"medium", "high"}
    assert done["retrieval_strategy"]["chunk_hits"] >= 1
```

Add a fallback test in the same file:

```python
def test_ask_stream_endpoint_falls_back_to_local_answer(tmp_path, monkeypatch):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "policy.md").write_text("# 退款制度\n\n退款期限是 7 天。", encoding="utf-8")
    _configure_sqlite_settings(tmp_path, monkeypatch)
    monkeypatch.setattr("app.search.stream_generate_answer", lambda *args, **kwargs: iter([]))

    client = TestClient(app)
    client.post(
        "/api/internal/sources/scan",
        json={"root_path": str(sample_dir), "domain": "customer_service", "owner": "tester", "acl_tags": ["internal"]},
    )
    client.post("/api/internal/wiki/compile", json={"domain": "customer_service"})

    with client.stream(
        "POST",
        "/api/internal/ask/stream",
        json={"question": "退款期限？", "domain": "customer_service", "acl_tags": ["internal"]},
    ) as response:
        events = [json.loads(line) for line in response.iter_lines() if line]

    deltas = [event["text"] for event in events if event["event"] == "answer_delta"]
    assert deltas
    assert "退款" in events[-1]["response"]["answer"]
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
python -m pytest tests/test_streaming_ask.py -q
```

Expected: FAIL because `/ask/stream` is not registered and `stream_ask_events()` does not exist.

- [ ] **Step 3: Add shared assembly objects in `app/search.py`**

Import:

```python
from dataclasses import dataclass
from collections.abc import Iterator
```

Add a dataclass near the top of `app/search.py`:

```python
@dataclass
class AskAssembly:
    query_id: str
    timestamp: str
    request: AskRequest
    mode_config: AnswerModeConfig
    resolved_user: UserContext | None
    chunk_hits: list[dict[str, Any]]
    gbrain_hits: list[GBrainHit]
    memory_hits: list[dict[str, Any]]
    citations: list[Citation]
    answer_parts: list[str]
    context_blocks: list[str]
    gbrain_context_blocks: list[str]
    missing_info: list[str]
    confidence: str
    retrieval_strategy: dict[str, Any]
```

If `AnswerModeConfig` is not exported, either import its concrete type from `app.answer_modes` or use `Any` for `mode_config`.

Move the retrieval/context portion of `ask()` into:

```python
def build_ask_assembly(settings: Settings, request: AskRequest, user_context: UserContext | None = None) -> AskAssembly:
    ...
```

The helper should preserve existing behavior up to the point where `generate_answer()` is called. It should compute `missing_info`, `confidence`, citations, answer parts, context blocks, memory blocks, and `retrieval_strategy` exactly as `ask()` does today.

- [ ] **Step 4: Add finalization helper and keep sync `ask()` compatible**

Add:

```python
def _memory_blocks_for_assembly(assembly: AskAssembly) -> list[str]:
    return build_memory_context(assembly.memory_hits)
```

Add:

```python
def finalize_ask_response(settings: Settings, assembly: AskAssembly, answer: str) -> AskResponse:
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO query_logs(
              id, question, domain, answer, citations_json, confidence, missing_info_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                assembly.query_id,
                assembly.request.question,
                assembly.request.domain,
                answer,
                json_dump([citation.model_dump() for citation in assembly.citations]),
                assembly.confidence,
                json_dump(assembly.missing_info),
                assembly.timestamp,
            ),
        )
        audit(
            conn,
            "question_answered",
            {
                "query_id": assembly.query_id,
                "question": assembly.request.question,
                "answer_mode": assembly.mode_config.key,
                "citation_count": len(assembly.citations),
                "confidence": assembly.confidence,
                "memory_hits": len(assembly.memory_hits),
                "user_context": assembly.resolved_user.to_public_dict() if assembly.resolved_user else None,
                "alias_expanded": assembly.retrieval_strategy.get("alias_expanded", False),
            },
            assembly.timestamp,
        )
    append_log(
        settings.vault_path,
        "qa_eval_log.md",
        f"- {assembly.timestamp} {assembly.query_id}: confidence={assembly.confidence} citations={len(assembly.citations)} question={assembly.request.question}",
    )
    return AskResponse(
        query_id=assembly.query_id,
        answer=answer,
        citations=assembly.citations,
        confidence=assembly.confidence,
        missing_info=assembly.missing_info,
        memory_hits=assembly.memory_hits,
        retrieval_strategy=assembly.retrieval_strategy,
        user_context=assembly.resolved_user.to_public_dict() if assembly.resolved_user else None,
    )
```

Update `ask()` so it calls `build_ask_assembly()`, computes the same generated/local answer, and returns `finalize_ask_response()`.

- [ ] **Step 5: Add NDJSON stream generator**

In `app/search.py`, import `stream_generate_answer` from `app.llm`.

Add:

```python
def _ndjson_event(payload: dict[str, Any]) -> str:
    return f"{json_dump(payload)}\n"
```

Add:

```python
def stream_ask_events(settings: Settings, request: AskRequest, user_context: UserContext | None = None) -> Iterator[str]:
    assembly = build_ask_assembly(settings, request, user_context)
    yield _ndjson_event(
        {
            "event": "metadata",
            "query_id": assembly.query_id,
            "confidence": assembly.confidence,
            "citations": [citation.model_dump() for citation in assembly.citations],
            "retrieval_strategy": assembly.retrieval_strategy,
        }
    )
    chunks: list[str] = []
    if not assembly.citations and not assembly.gbrain_context_blocks:
        fallback = "当前知识库没有找到足够依据回答这个问题。建议补充相关产品/客服资料后重新编译知识库。"
        chunks.append(fallback)
        yield _ndjson_event({"event": "answer_delta", "text": fallback})
    else:
        for chunk in stream_generate_answer(
            settings,
            request.question,
            assembly.context_blocks,
            assembly.mode_config.key,
            memory_blocks=_memory_blocks_for_assembly(assembly),
        ):
            if not chunk:
                continue
            chunks.append(chunk)
            yield _ndjson_event({"event": "answer_delta", "text": chunk})
        if not chunks:
            fallback = build_local_answer(assembly.mode_config, assembly.answer_parts, assembly.memory_hits)
            chunks.append(fallback)
            yield _ndjson_event({"event": "answer_delta", "text": fallback})
    response = finalize_ask_response(settings, assembly, "".join(chunks))
    yield _ndjson_event({"event": "done", "response": response.model_dump()})
```

- [ ] **Step 6: Add `/ask/stream` endpoint**

In `app/api.py`, import:

```python
from fastapi.responses import StreamingResponse
from app.search import ask, stream_ask_events
```

Add endpoint next to `ask_endpoint`:

```python
@router.post("/ask/stream")
def ask_stream_endpoint(request: AskRequest, user: UserContext = Depends(current_user)) -> StreamingResponse:
    try:
        return StreamingResponse(
            stream_ask_events(get_settings(), request, user),
            media_type="application/x-ndjson",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

Because the router is mounted under both public and internal prefixes, this should expose both `/ask/stream` and `/api/internal/ask/stream`.

- [ ] **Step 7: Run Task 2 tests to verify GREEN**

Run:

```bash
python -m pytest tests/test_streaming_ask.py -q
```

Expected: PASS.

- [ ] **Step 8: Run sync regression tests**

Run:

```bash
python -m pytest tests/test_rag_retrieval.py tests/test_internal_flow.py -q
```

Expected: PASS. If failures occur, adjust only the shared assembly/finalization to preserve prior `/ask` behavior.

- [ ] **Step 9: Commit Task 2**

Run:

```bash
git add app/search.py app/api.py tests/test_streaming_ask.py
git commit -m "feat: add streaming ask endpoint"
```

---

### Task 3: Pgvector SQL Candidate Preselection

**Files:**
- Modify: `app/pg_rag.py`
- Modify: `tests/test_pg_rag_store.py`

- [ ] **Step 1: Write failing pgvector SQL tests**

Add to `tests/test_pg_rag_store.py`:

```python
def test_search_pg_chunks_uses_pgvector_preselection_when_available(monkeypatch):
    import app.pg_rag as pg_rag

    executed: list[tuple[str, tuple | None]] = []

    class Cursor:
        description = [
            type("Desc", (), {"name": name})
            for name in [
                "id",
                "source_id",
                "domain",
                "title",
                "chunk_index",
                "text",
                "tokens",
                "metadata",
                "embedding",
                "pgvector_distance",
            ]
        ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed.append((query, params))

        def fetchall(self):
            return [
                (
                    "row1",
                    "source1",
                    "customer_service",
                    "退款制度",
                    0,
                    "退款期限是 7 天。",
                    "退款 期限",
                    "{}",
                    "[0.1, 0.2]",
                    0.12,
                )
            ]

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(pg_rag, "init_pg_rag", lambda settings: None)
    monkeypatch.setattr(pg_rag, "connect_postgres", lambda settings: Conn())
    monkeypatch.setattr(pg_rag, "_pgvector_available", lambda cur: True)
    monkeypatch.setattr(pg_rag, "_column_exists", lambda cur, table, column: column == "embedding_vector")
    monkeypatch.setattr(pg_rag, "embed_text", lambda question: [0.1, 0.2])
    monkeypatch.setattr(pg_rag, "rank_search_rows", lambda rows, question, **kwargs: rows)
    monkeypatch.setattr(pg_rag, "diversify_ranked_rows", lambda rows, limit: rows[:limit])

    rows = pg_rag.search_pg_chunks(object(), "退款期限", "customer_service", limit=5)

    assert rows[0]["pgvector_distance"] == 0.12
    vector_queries = [query for query, _params in executed if "<=>" in query]
    assert vector_queries
    assert "LIMIT %s" in vector_queries[0]
    assert executed[-1][1][-1] >= 40


def test_search_pg_chunks_falls_back_without_pgvector(monkeypatch):
    import app.pg_rag as pg_rag

    executed: list[str] = []

    class Cursor:
        description = [
            type("Desc", (), {"name": name})
            for name in ["id", "source_id", "domain", "title", "chunk_index", "text", "tokens", "metadata", "embedding"]
        ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed.append(query)

        def fetchall(self):
            return []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(pg_rag, "init_pg_rag", lambda settings: None)
    monkeypatch.setattr(pg_rag, "connect_postgres", lambda settings: Conn())
    monkeypatch.setattr(pg_rag, "_pgvector_available", lambda cur: False)
    monkeypatch.setattr(pg_rag, "_column_exists", lambda cur, table, column: False)
    monkeypatch.setattr(pg_rag, "embed_text", lambda question: [0.1, 0.2])
    monkeypatch.setattr(pg_rag, "rank_search_rows", lambda rows, question, **kwargs: rows)

    pg_rag.search_pg_chunks(object(), "退款期限", "customer_service", limit=5)

    assert executed
    assert all("<=>" not in query for query in executed)
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
python -m pytest tests/test_pg_rag_store.py::test_search_pg_chunks_uses_pgvector_preselection_when_available tests/test_pg_rag_store.py::test_search_pg_chunks_falls_back_without_pgvector -q
```

Expected: FAIL because `search_pg_chunks()` always runs the non-vector fetch.

- [ ] **Step 3: Implement pgvector preselection helper**

In `app/pg_rag.py`, add:

```python
def _pg_candidate_limit(limit: int) -> int:
    return max(limit * 8, 40)
```

Add:

```python
def _can_use_pgvector_search(cur) -> bool:
    return _pgvector_available(cur) and _column_exists(cur, "rag_document_chunks", "embedding_vector")
```

- [ ] **Step 4: Update `search_pg_chunks()` SQL selection**

Inside `search_pg_chunks()` cursor block, before the existing domain query:

```python
query_vector = embed_text(question)
use_pgvector = bool(query_vector) and _can_use_pgvector_search(cur)
candidate_limit = _pg_candidate_limit(limit)
```

If `use_pgvector` is true and `domain` is set:

```python
cur.execute(
    """
    SELECT id, source_id, domain, title, chunk_index, text, tokens, metadata, embedding,
           embedding_vector <=> %s::vector AS pgvector_distance
    FROM rag_document_chunks
    WHERE domain = %s AND embedding_vector IS NOT NULL
    ORDER BY embedding_vector <=> %s::vector
    LIMIT %s
    """,
    (_vector(query_vector), domain, _vector(query_vector), candidate_limit),
)
```

If `use_pgvector` is true and no domain is set:

```python
cur.execute(
    """
    SELECT id, source_id, domain, title, chunk_index, text, tokens, metadata, embedding,
           embedding_vector <=> %s::vector AS pgvector_distance
    FROM rag_document_chunks
    WHERE embedding_vector IS NOT NULL
    ORDER BY embedding_vector <=> %s::vector
    LIMIT %s
    """,
    (_vector(query_vector), _vector(query_vector), candidate_limit),
)
```

Otherwise keep the current all-row domain/no-domain queries.

Wrap the pgvector query in a broad database-safe fallback:

```python
try:
    ...
except Exception:
    # run the existing non-vector query path
```

Keep the rest of the function unchanged: rows are converted to dicts, filtered by ACL, passed to `rank_search_rows(..., alias_context=alias_context)`, and diversified.

- [ ] **Step 5: Run Task 3 tests to verify GREEN**

Run:

```bash
python -m pytest tests/test_pg_rag_store.py::test_search_pg_chunks_uses_pgvector_preselection_when_available tests/test_pg_rag_store.py::test_search_pg_chunks_falls_back_without_pgvector -q
```

Expected: PASS.

- [ ] **Step 6: Run Postgres RAG tests**

Run:

```bash
python -m pytest tests/test_pg_rag_store.py tests/test_postgres_metadata_backend.py -q
```

Expected: PASS or external Postgres tests skip when no service is configured. Do not claim pass unless pytest exits 0.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git add app/pg_rag.py tests/test_pg_rag_store.py
git commit -m "feat: use pgvector candidate preselection"
```

---

### Task 4: Integration Verification And Cleanup

**Files:**
- Inspect: `app/llm.py`
- Inspect: `app/search.py`
- Inspect: `app/api.py`
- Inspect: `app/pg_rag.py`
- Inspect: `tests/`

- [ ] **Step 1: Run focused P3 tests**

Run:

```bash
python -m pytest tests/test_llm_streaming.py tests/test_streaming_ask.py tests/test_pg_rag_store.py -q
```

Expected: PASS or external-service tests skip with clear skip reasons.

- [ ] **Step 2: Run existing retrieval/eval regression tests**

Run:

```bash
python -m pytest tests/test_acl_and_aliases.py tests/test_rag_reranking.py tests/test_gbrain_integration.py tests/test_eval_upgraded.py tests/test_rag_retrieval.py tests/test_internal_flow.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full Python test suite**

Run:

```bash
python -m pytest -q
```

Expected: PASS. If external-service tests are unavailable, record the exact failures/skips and keep focused tests green.

- [ ] **Step 4: Check removed benchmark hardcoding stayed removed**

Run:

```bash
rg "pricing_calculation_intent|content_safety_dependency_intent|privacy_audit_intent|time_window_policy_intent|prd_priority_dependency_intent|api_failure_checklist_intent" app tests
```

Expected: no matches.

Run:

```bash
rg "boost \\+= (2[0-9][0-9]|3[0-9][0-9])" app/rag.py
```

Expected: no matches.

- [ ] **Step 5: Try upgraded benchmark if runtime allows**

Run:

```bash
python -m scripts.upgraded_qa_benchmark
```

Expected: benchmark report output. If it times out or data is missing, report that honestly and do not treat it as verified.

- [ ] **Step 6: Final git status**

Run:

```bash
git status --short --branch
```

Expected: no staged files. Existing unrelated dirty files may remain; mention that they were preserved.

- [ ] **Step 7: Commit cleanup if required**

If verification required small cleanup edits:

```bash
git add app/llm.py app/search.py app/api.py app/pg_rag.py tests/test_llm_streaming.py tests/test_streaming_ask.py tests/test_pg_rag_store.py
git commit -m "test: verify p3 streaming pgvector"
```

If no cleanup edits were required, do not create an empty commit.
