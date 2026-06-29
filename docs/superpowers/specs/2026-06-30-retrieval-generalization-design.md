# Retrieval Generalization Design

## Scope

This phase covers:

- P0: Replace test-set-specific retrieval intent rules with reusable entity alias and embedding-aware retrieval signals.
- P1: Fix Q20 GBrain degradation and Q21 role alias coverage.
- P2: Move the upgraded 31-question evaluation into the app and support automated GBrain ON/OFF comparison.
- P3 partial: Trim GBrain query candidates and isolate per-candidate failures.

This phase does not cover LLM streaming or pgvector index work. Those require separate API and migration changes.

## Current Findings

`app/rag.py` contains `contextual_boost()`, an 86-line scoring function with high-weight rules tied to known benchmark questions. The worst offenders are the six intent helpers:

- `pricing_calculation_intent`
- `content_safety_dependency_intent`
- `privacy_audit_intent`
- `time_window_policy_intent`
- `prd_priority_dependency_intent`
- `api_failure_checklist_intent`

These add scores in the 200-360 range and dominate BM25, vector similarity, phrase matching, and document-code matching. This makes the system fit the current 31 questions instead of general retrieval behavior.

The app already has useful building blocks:

- `entity_aliases` table and CRUD helpers in `app/aliases.py`
- query expansion in `expand_query_with_aliases()`
- BGE-M3-compatible embedding flow through `embed_text()` and row embeddings
- `rank_search_rows()` hybrid scoring in `app/rag.py`
- Python GBrain bridge in `app/gbrain.py`
- basic app eval API in `app/eval.py`
- external upgraded benchmark in `scripts/upgraded_qa_benchmark.py`

## Recommended Approach

Replace benchmark intent scoring with generic retrieval signals:

1. Keep BM25, vector similarity, exact phrase boost, table row boost, and document code boost.
2. Add an alias-derived retrieval context from `entity_aliases`.
3. Use alias context to expand queries and provide a bounded, explainable row boost when canonical names, aliases, entity types, or metadata terms appear in the question and candidate row.
4. Remove the six benchmark intent helper functions and their 200-360 point boosts.
5. Keep any remaining contextual rules only if they are generic and low-weight; prefer moving them into alias seed data or phrase extraction when possible.

This keeps retrieval data-driven: a new concept is added by alias data or source content, not by editing Python scoring branches.

## Architecture

### Alias Matching

Add pure helper functions in `app/aliases.py`:

- build a normalized alias context for a domain
- return matched aliases for a question
- derive expansion terms from canonical name, alias, entity type, and metadata
- score a candidate row against matched aliases with bounded weights

The helper should be deterministic and testable without a database. Database access remains in existing list/upsert functions.

### RAG Ranking

Update `rank_search_rows()` to accept optional alias context. The score model becomes:

- lexical score: keyword + BM25 + exact phrase + table + document code + alias boost
- vector score: existing BGE-M3-compatible embedding similarity
- fusion: existing RRF/vector/keyword composition

Alias boost must be smaller than exact document-code evidence and must not swamp vector/BM25 by hundreds of points. It should make relevant aliased rows competitive, not guarantee first place.

`contextual_boost()` should be removed or reduced to generic low-weight behavior with no benchmark intent helpers.

### Ask Flow

`app/search.py` already calls `expand_query_with_aliases()` before retrieval. Extend this flow so the matched alias context is passed into chunk search/ranking. Keep `retrieval_strategy.matched_aliases` for observability.

### Q21 Alias Seed

Add seedable alias data for role terms used by company制度 documents:

- canonical role terms such as `部门负责人`, `直属上级`, `审批人`
- aliases such as `部门总监`
- entity type `role`
- domain `customer_service` unless the alias is clearly global

The seed path should use existing upsert semantics so it is safe to run repeatedly.

### GBrain Robustness And Candidate Trimming

Change `_query_gbrain_with_caller()` so:

- the main `query` call is attempted once
- each candidate `search` call is isolated; one candidate failure does not discard collected hits
- candidate generation deduplicates aggressively
- low-information CJK sliding-window tokens are capped or removed when stronger phrases/codes exist
- configured `gbrain_candidate_limit` remains the hard upper bound

For Q20, preserve high-signal phrases such as `时间窗口`, `期限`, `制度`, `条款`, `相互矛盾`, and exact duration terms discovered from the query. Do not encode Q20 as a special case.

### Built-In Evaluation

Move the upgraded benchmark definition and runner into app-owned evaluation code. The external script can become a thin CLI wrapper around the app module.

The app evaluation runner should support:

- all upgraded 31 questions
- optional domain filter
- GBrain mode: `on`, `off`, or `both`
- per-question result rows with citations, expected source coverage, required term coverage, confidence, elapsed time, and top retrieval diagnostics
- summary with pass rate, citation rate, p50/p95 latency, and ON/OFF deltas

This makes quality regression checks reproducible from the project itself instead of relying on a separate script.

## Data Flow

1. User question enters `ask()`.
2. Alias helper loads domain/global aliases and matches question.
3. Query is expanded with canonical and alias terms.
4. Chunk search runs with expanded query plus alias context.
5. GBrain query runs in parallel with trimmed candidate expansion.
6. Results are filtered by authorization.
7. Answer generation uses citations plus GBrain context.
8. Retrieval diagnostics record alias matches, alias boost, GBrain hit count, and top hits.

## Error Handling

- Alias matching failures should degrade to no alias context, not fail the answer.
- A failed GBrain candidate search should be recorded or ignored locally, not erase other hits.
- If GBrain is unavailable, evaluation still runs in OFF behavior and reports availability.
- Evaluation rows should include enough diagnostics to identify missing source coverage without reading logs.

## Testing Strategy

Use TDD before production edits.

Initial failing tests:

- `contextual_boost()` no longer exposes the six benchmark intent helpers or high-weight intent branches.
- alias scoring ranks a role-permission document for a `部门总监` query without adding a Python special case.
- Q21 seed upsert is idempotent and creates expected role aliases.
- GBrain keeps main/candidate hits when one candidate search raises `GBrainError`.
- GBrain candidate trimming keeps document codes and meaningful phrases while reducing sliding-window noise.
- built-in upgraded eval can run GBrain OFF and BOTH modes and returns per-question plus summary data.

Verification:

- run focused pytest modules for aliases, RAG reranking, GBrain integration, and eval
- run broader pytest if runtime is reasonable
- run built-in upgraded eval when local benchmark data is present
- inspect code search results to confirm removed benchmark intent helpers

## Acceptance Criteria

- The six benchmark-specific intent helpers are removed from retrieval scoring.
- No remaining retrieval branch gives hundreds of points for a benchmark-shaped question pattern.
- Q21 succeeds through alias data and generic retrieval scoring.
- Q20 is resilient to GBrain candidate failures and can still use available local/GBrain evidence.
- Evaluation is app-owned and supports automated GBrain ON/OFF comparison.
- GBrain candidate count is bounded, deduplicated, and lower-noise than the current sliding-window-heavy behavior.
- Existing dirty worktree changes unrelated to this phase are preserved.

