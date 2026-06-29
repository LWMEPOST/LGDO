# P3 Streaming And Pgvector Design

## Scope

This phase finishes the remaining P3 work from the retrieval generalization goal:

- Add a backend streaming answer path so callers can receive answer text incrementally.
- Use pgvector distance ordering to preselect Postgres RAG candidates when vector columns and the extension are available.
- Keep the existing synchronous `/ask` behavior unchanged.
- Preserve current fallback behavior when no LLM key, no pgvector extension, or no vector embedding is available.

This phase does not add a frontend streaming UI. The API and tests come first so the backend behavior is stable before the UI consumes it.

## Current Findings

The current answer flow is centered in `app/search.py::ask()`. It performs alias expansion, chunk retrieval, GBrain retrieval, authorization filtering, context construction, synchronous LLM generation through `app/llm.py::generate_answer()`, query logging, audit logging, and response assembly.

The current API only exposes synchronous `/ask`. The endpoint returns an `AskResponse` after retrieval and generation are complete.

The current Postgres RAG path in `app/pg_rag.py::search_pg_chunks()` fetches all rows for a domain, filters ACL in Python, then calls the shared Python reranker. The table already stores row embeddings and has helper checks for pgvector availability and vector columns. That makes SQL vector preselection possible without changing the public ranking contract.

## Approach Options

### Option A: Minimal Streaming Wrapper

Keep `ask()` untouched and stream only after the full response is generated. This is safe but does not reduce perceived LLM latency because the answer text is not available until generation finishes.

### Option B: Backend Streaming With Shared Retrieval Assembly

Extract the retrieval and context-building part of `ask()` into an internal helper. The synchronous path calls the helper, then calls `generate_answer()`. The streaming path calls the same helper, then calls a new streaming LLM generator and emits NDJSON events. If the LLM cannot stream, it falls back to the local answer in one final delta.

This is the recommended option. It keeps retrieval behavior identical between `/ask` and `/ask/stream`, limits duplication, and gives clients incremental tokens as soon as the provider sends them.

### Option C: Full Chat Protocol And Frontend Streaming

Add a richer event protocol, frontend streaming UI, retry controls, and cancellation UX in the same phase. This is useful later but too broad for the current performance-focused backend task.

## Recommended Design

Use Option B.

Add an internal retrieval assembly result in `app/search.py` that contains:

- `query_id`
- `timestamp`
- `mode_config`
- `resolved_user`
- `chunk_hits`
- `gbrain_hits`
- `memory_hits`
- `citations`
- `context_blocks`
- `answer_parts`
- `gbrain_context_blocks`
- `missing_info`
- `confidence`
- `retrieval_strategy`

The helper performs the same retrieval, ACL filtering, fallback wiki search, GBrain context construction, and diagnostics currently inside `ask()`. It does not call the LLM and does not write the final query log until an answer is known.

`ask()` then becomes:

1. Build the retrieval assembly.
2. If there is no usable context, use the existing low-confidence missing-info answer.
3. Otherwise call `generate_answer()` and fallback to `build_local_answer()`.
4. Persist the query log and audit record through a shared finalize helper.
5. Return the existing `AskResponse`.

`stream_ask()` becomes:

1. Build the same retrieval assembly.
2. Emit an initial NDJSON metadata event containing `query_id`, confidence, citations, and retrieval diagnostics.
3. If there is no usable context, emit the existing missing-info answer as one delta, then finalize.
4. Otherwise call `stream_generate_answer()` and emit each non-empty delta as an NDJSON `answer_delta` event.
5. If streaming yields no text or the provider is unavailable, emit the local answer as one delta.
6. Persist the same query log and audit record with the final accumulated answer.
7. Emit a final NDJSON `done` event containing the completed `AskResponse` payload.

The stream format is newline-delimited JSON:

```json
{"event":"metadata","query_id":"qry_...","confidence":"medium","citations":[],"retrieval_strategy":{}}
{"event":"answer_delta","text":"..."}
{"event":"done","response":{}}
```

On recoverable provider errors, the stream should emit a local fallback answer and still finish with `done`. Request validation errors and retrieval setup failures should still be normal HTTP errors before streaming begins.

## LLM Streaming

Add `stream_generate_answer()` in `app/llm.py`.

It uses the same `build_prompt()` function and DeepSeek-compatible chat completion endpoint as `generate_answer()`, but sends `stream: true`. It parses server-sent event lines that start with `data:` and yields `choices[].delta.content` text. It stops cleanly on `[DONE]`.

The function returns an iterator of text chunks. If the API key or model is missing, it yields nothing. If URL, timeout, or JSON parsing errors occur, it yields nothing and lets the caller use local fallback. This matches the existing synchronous degradation policy.

## Pgvector Candidate Preselection

Update `search_pg_chunks()` so it can use SQL vector ordering before Python reranking:

1. Call `embed_text(question)` once to get a query vector.
2. Check that pgvector is available and `embedding_vector` exists.
3. If both are true and the query vector is non-empty, run a SQL candidate query ordered by `embedding_vector <=> %s::vector`.
4. Limit SQL candidates to a bounded preselection size, for example `max(limit * 8, 40)`, before Python ACL filtering and reranking.
5. Fall back to the current all-domain fetch when pgvector cannot be used.

The final ranking still goes through `rank_search_rows()` so alias boost, BM25, phrase evidence, ACL filtering, and existing diagnostics remain consistent.

The SQL query should include the vector distance as a diagnostic field such as `pgvector_distance`. The response may expose this in top-hit diagnostics later, but it is not required for this phase.

## Error Handling

- `/ask` remains backward compatible.
- `/ask/stream` returns HTTP validation errors before streaming starts.
- LLM streaming errors degrade to local answer fallback.
- A provider that sends malformed SSE chunks only loses that chunk; parsing continues where possible.
- Pgvector failures degrade to the existing Postgres fetch and Python rerank path.
- Missing embeddings, null vectors, or unavailable extensions never fail a user query.

## Testing Strategy

Use test-driven development before production edits.

Streaming tests:

- `stream_generate_answer()` parses DeepSeek-style SSE chunks and yields text.
- `stream_generate_answer()` yields nothing when credentials are missing.
- `/ask/stream` emits `metadata`, one or more `answer_delta`, and `done` NDJSON events.
- The `done` event contains the same response shape as synchronous `AskResponse`.
- Streaming fallback emits a local answer when the LLM stream yields no chunks.

Pgvector tests:

- `search_pg_chunks()` uses a SQL query containing `<=>` and a bounded `LIMIT` when pgvector and `embedding_vector` are available.
- The function falls back to the existing non-vector query when the extension or vector column is unavailable.
- Rows returned from SQL preselection still pass through `rank_search_rows()` with alias context and ACL filtering.

Regression tests:

- Existing `/ask` tests keep passing.
- Existing RAG reranking and GBrain tests keep passing.
- Full pytest should pass after focused tests.

## Acceptance Criteria

- A new backend `/ask/stream` endpoint streams NDJSON events and finalizes with an `AskResponse` payload.
- Synchronous `/ask` behavior and response schema remain compatible.
- Streaming uses provider tokens when available and local fallback when not.
- Postgres RAG uses pgvector SQL preselection when available.
- Postgres RAG falls back safely when pgvector is unavailable.
- Retrieval diagnostics and authorization behavior remain consistent across sync and streaming paths.
- The implementation includes focused tests for streaming parsing, streaming endpoint behavior, and pgvector query selection.
