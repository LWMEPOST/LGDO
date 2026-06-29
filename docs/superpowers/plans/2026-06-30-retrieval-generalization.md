# Retrieval Generalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace benchmark-shaped retrieval boosts with generic alias/embedding-aware retrieval, fix Q20/Q21 behavior, add built-in GBrain ON/OFF evaluation, and trim GBrain candidate calls.

**Architecture:** Keep the existing RAG and GBrain structure, but move entity understanding into `app/aliases.py` as deterministic data-driven helpers. `app/search.py` passes alias context into SQLite/Postgres chunk search, `app/rag.py` uses bounded alias scoring instead of hard-coded intent boosts, `app/gbrain.py` isolates candidate failures and trims noisy candidates, and `app/eval.py` owns the upgraded benchmark runner.

**Tech Stack:** Python 3, FastAPI/Pydantic models, SQLite/Postgres metadata stores, existing BGE-M3-compatible embedding helpers, pytest.

---

## File Structure

- Modify `app/aliases.py`: add pure alias matching, scoring, and seed helpers.
- Modify `app/rag.py`: add `alias_context` to `rank_search_rows()` and `search_chunks()`, remove benchmark intent helpers from contextual scoring, expose `alias_boost` diagnostics.
- Modify `app/pg_rag.py`: pass `alias_context` into `rank_search_rows()` for Postgres retrieval.
- Modify `app/search.py`: pass matched alias context from `ask()` into chunk retrieval.
- Modify `app/gbrain.py`: isolate candidate failures and trim generated candidates.
- Modify `app/eval.py`: add upgraded benchmark data, per-question runner, GBrain ON/OFF comparison, and summary metrics.
- Modify `app/models.py`: add evaluation request/response models for upgraded eval comparison.
- Modify `app/api.py`: expose upgraded evaluation endpoint.
- Modify `scripts/upgraded_qa_benchmark.py`: make it call the app-owned runner instead of owning benchmark logic.
- Modify `tests/test_acl_and_aliases.py`: add alias scoring and Q21 alias seed tests.
- Modify `tests/test_rag_reranking.py`: replace benchmark-intent assertions with generic alias/scoring assertions and removal checks.
- Modify `tests/test_gbrain_integration.py`: add candidate failure isolation and candidate trimming tests.
- Add or modify `tests/test_eval_upgraded.py`: test built-in upgraded eval and GBrain ON/OFF comparison.

---

### Task 1: Alias Context And Q21 Seed

**Files:**
- Modify: `app/aliases.py`
- Test: `tests/test_acl_and_aliases.py`

- [ ] **Step 1: Write failing tests for pure alias context and idempotent Q21 seed**

Add these tests to `tests/test_acl_and_aliases.py`:

```python
def test_alias_context_matches_role_alias_and_scores_candidate_text():
    from app.aliases import build_alias_context, score_alias_context

    aliases = [
        {
            "domain": "customer_service",
            "canonical_name": "部门负责人",
            "canonical_key": "部门负责人",
            "alias": "部门总监",
            "alias_key": "部门总监",
            "entity_type": "role",
            "metadata_json": '{"terms":["审批","报销","采购"]}',
        }
    ]

    context = build_alias_context("部门总监有哪些审批权限？", aliases)

    assert context["matched_aliases"][0]["alias"] == "部门总监"
    assert "部门负责人" in context["expansion_terms"]
    assert "审批" in context["expansion_terms"]
    score = score_alias_context(
        context,
        "费用报销制度",
        "部门负责人审批超过 10000 元的报销，采购申请也需要审批。",
    )
    assert 0 < score <= 48


def test_seed_role_aliases_is_idempotent(tmp_path, monkeypatch):
    from app.aliases import list_entity_aliases, seed_default_entity_aliases
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "test.db")

    first = seed_default_entity_aliases(settings)
    second = seed_default_entity_aliases(settings)
    aliases = list_entity_aliases(settings, "customer_service")

    role_aliases = [
        item for item in aliases
        if item["alias"] == "部门总监" and item["canonical_name"] in {"部门负责人", "直属上级", "审批人"}
    ]
    assert first >= 1
    assert second >= 0
    assert role_aliases
    assert len({item["alias_key"] for item in role_aliases}) == 1
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
pytest tests/test_acl_and_aliases.py::test_alias_context_matches_role_alias_and_scores_candidate_text tests/test_acl_and_aliases.py::test_seed_role_aliases_is_idempotent -q
```

Expected: FAIL because `build_alias_context`, `score_alias_context`, and `seed_default_entity_aliases` do not exist.

- [ ] **Step 3: Implement alias helpers**

In `app/aliases.py`, add helpers after `normalize_alias()`:

```python
def _parse_alias_metadata(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("metadata")
    if isinstance(raw, dict):
        return raw
    raw_json = item.get("metadata_json")
    if not raw_json:
        return {}
    try:
        parsed = json.loads(raw_json)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _alias_terms(item: dict[str, Any]) -> list[str]:
    metadata = _parse_alias_metadata(item)
    terms: list[str] = []
    for value in [item.get("canonical_name"), item.get("alias"), item.get("entity_type")]:
        if value and str(value) not in terms:
            terms.append(str(value))
    for value in metadata.get("terms") or []:
        if value and str(value) not in terms:
            terms.append(str(value))
    return terms


def build_alias_context(question: str, aliases: list[dict[str, Any]]) -> dict[str, Any]:
    question_key = normalize_alias(question)
    matched: list[dict[str, Any]] = []
    expansion_terms: list[str] = []
    for item in aliases:
        alias_key = item.get("alias_key") or normalize_alias(item.get("alias") or "")
        canonical_key = item.get("canonical_key") or normalize_alias(item.get("canonical_name") or "")
        if not alias_key or not canonical_key:
            continue
        if alias_key not in question_key and canonical_key not in question_key:
            continue
        matched.append(item)
        for term in _alias_terms(item):
            if term not in expansion_terms and term not in question:
                expansion_terms.append(term)
    return {"matched_aliases": matched, "expansion_terms": expansion_terms}


def score_alias_context(alias_context: dict[str, Any] | None, title: str, text: str) -> float:
    if not alias_context:
        return 0.0
    haystack = normalize_alias(f"{title}\n{text}")
    score = 0.0
    for item in alias_context.get("matched_aliases") or []:
        terms = _alias_terms(item)
        canonical = normalize_alias(item.get("canonical_name") or "")
        alias = normalize_alias(item.get("alias") or "")
        if canonical and canonical in haystack:
            score += 18.0
        if alias and alias in haystack:
            score += 12.0
        for term in terms:
            key = normalize_alias(term)
            if key and key in haystack:
                score += 6.0
    return min(score, 48.0)
```

Also update `expand_query_with_aliases()` to reuse `build_alias_context()`:

```python
def expand_query_with_aliases(settings: Settings, question: str, domain: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    aliases = list_entity_aliases(settings, domain)
    context = build_alias_context(question, aliases)
    additions = context["expansion_terms"]
    matched = context["matched_aliases"]
    if not additions:
        return question, []
    return f"{question}\n\n实体别名扩展：{'、'.join(additions)}", matched
```

Add seed helper near the CRUD helpers:

```python
DEFAULT_ENTITY_ALIASES: list[dict[str, Any]] = [
    {
        "domain": "customer_service",
        "canonical_name": "部门负责人",
        "alias": "部门总监",
        "entity_type": "role",
        "metadata": {"terms": ["审批", "报销", "采购", "请假", "远程", "招待"]},
    },
    {
        "domain": "customer_service",
        "canonical_name": "直属上级",
        "alias": "部门总监",
        "entity_type": "role",
        "metadata": {"terms": ["审批", "请假", "远程办公"]},
    },
]


def seed_default_entity_aliases(settings: Settings, actor: str | None = "system") -> int:
    count = 0
    for item in DEFAULT_ENTITY_ALIASES:
        upsert_entity_alias(settings, EntityAliasRequest(**item), actor=actor)
        count += 1
    return count
```

Ensure imports include:

```python
import json
from typing import Any
```

- [ ] **Step 4: Run Task 1 tests to verify GREEN**

Run:

```bash
pytest tests/test_acl_and_aliases.py::test_alias_context_matches_role_alias_and_scores_candidate_text tests/test_acl_and_aliases.py::test_seed_role_aliases_is_idempotent -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add app/aliases.py tests/test_acl_and_aliases.py
git commit -m "feat: add alias retrieval context"
```

---

### Task 2: Replace Benchmark Intent Boosts With Alias Boost

**Files:**
- Modify: `app/rag.py`
- Modify: `app/pg_rag.py`
- Modify: `app/search.py`
- Test: `tests/test_rag_reranking.py`
- Test: `tests/test_acl_and_aliases.py`

- [ ] **Step 1: Write failing tests for alias rerank and removal of benchmark intent helpers**

Add to `tests/test_rag_reranking.py`:

```python
def test_reranker_uses_alias_context_without_benchmark_intent_rules(monkeypatch):
    import app.rag as rag
    from app.aliases import build_alias_context

    monkeypatch.setattr(rag, "embed_text", lambda _: [1.0, 0.0])
    rows = [
        make_row("generic", "普通制度", "审批流程说明。", source_id="06"),
        make_row("role", "费用报销制度", "部门负责人审批报销、采购、招待等事项。", source_id="07"),
    ]
    context = build_alias_context(
        "部门总监有哪些权限？",
        [
            {
                "canonical_name": "部门负责人",
                "canonical_key": "部门负责人",
                "alias": "部门总监",
                "alias_key": "部门总监",
                "entity_type": "role",
                "metadata_json": '{"terms":["报销","采购","招待"]}',
            }
        ],
    )

    ranked = rag.rank_search_rows(rows, "部门总监有哪些权限？", alias_context=context)

    assert ranked[0]["id"] == "role"
    assert ranked[0]["alias_boost"] > 0
    assert ranked[0]["context_boost"] < 100
    for name in [
        "pricing_calculation_intent",
        "content_safety_dependency_intent",
        "privacy_audit_intent",
        "time_window_policy_intent",
        "prd_priority_dependency_intent",
        "api_failure_checklist_intent",
    ]:
        assert not hasattr(rag, name)
```

Add to `tests/test_acl_and_aliases.py`, after the existing ask alias test:

```python
def test_ask_reports_alias_boost_in_top_hits(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    vault_dir = tmp_path / "vault"
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "role_policy.md").write_text(
        "# 权限规则\n\n部门负责人审批报销、采购、请假和远程办公申请。",
        encoding="utf-8",
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", data_dir / "test.db")
    monkeypatch.setattr(settings, "vault_path", vault_dir)
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "deepseek_model", None)

    client = TestClient(app)
    assert client.post(
        "/api/internal/sources/scan",
        json={"root_path": str(sample_dir), "domain": "customer_service", "owner": "tester", "acl_tags": ["internal"]},
    ).status_code == 200
    assert client.post("/api/internal/wiki/compile", json={"domain": "customer_service"}).status_code == 200
    assert client.post(
        "/api/internal/aliases",
        json={
            "domain": "customer_service",
            "canonical_name": "部门负责人",
            "alias": "部门总监",
            "entity_type": "role",
            "metadata": {"terms": ["报销", "采购", "请假", "远程"]},
        },
    ).status_code == 200

    answer = client.post(
        "/api/internal/ask",
        json={"question": "部门总监有哪些审批权限？", "domain": "customer_service", "acl_tags": ["internal"]},
    )

    assert answer.status_code == 200
    top_hits = answer.json()["retrieval_strategy"]["top_hits"]
    assert top_hits
    assert top_hits[0]["alias_boost"] > 0
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
pytest tests/test_rag_reranking.py::test_reranker_uses_alias_context_without_benchmark_intent_rules tests/test_acl_and_aliases.py::test_ask_reports_alias_boost_in_top_hits -q
```

Expected: FAIL because `rank_search_rows()` does not accept `alias_context`, intent helpers still exist, and top hits do not expose `alias_boost`.

- [ ] **Step 3: Modify `rank_search_rows()` and retrieval entrypoints**

In `app/rag.py`, import:

```python
from app.aliases import score_alias_context
```

Change the `rank_search_rows()` signature to:

```python
def rank_search_rows(
    rows: list[dict[str, Any]],
    question: str,
    *,
    keyword_weight: float = 1.0,
    vector_weight: float = 12.0,
    alias_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
```

Inside the row loop, after `doc_code_score`, add:

```python
alias_score = score_alias_context(alias_context, title, text)
```

Include `alias_score` in the candidate filter, diagnostics, lexical score, and final score:

```python
and alias_score <= 0
```

```python
ranked_row["alias_boost"] = round(alias_score, 6)
```

```python
+ alias_score
```

```python
+ item["alias_boost"]
```

Update the final sort tuple to include alias evidence after document code:

```python
item["alias_boost"],
```

Change `search_chunks()` signature to accept:

```python
alias_context: dict[str, Any] | None = None,
```

Pass it to `rank_search_rows()`:

```python
ranked = rank_search_rows(
    filter_rows_by_acl(rows_to_dicts(conn.execute(query, params).fetchall()), user_context),
    question,
    keyword_weight=keyword_weight,
    vector_weight=vector_weight,
    alias_context=alias_context,
)
```

In `app/pg_rag.py`, update `search_pg_chunks()` with the same `alias_context` keyword and pass it to `rank_search_rows()`.

In `app/search.py`, update the `search_chunks` executor call:

```python
alias_context={"matched_aliases": matched_aliases, "expansion_terms": []},
```

Also include `alias_boost` in `retrieval_strategy["top_hits"]`:

```python
"alias_boost": item.get("alias_boost"),
```

- [ ] **Step 4: Remove benchmark intent helpers and high-weight branches**

In `app/rag.py`, delete these functions completely:

```python
pricing_calculation_intent
content_safety_dependency_intent
content_safety_dependency_terms
privacy_audit_intent
time_window_policy_intent
prd_priority_dependency_intent
api_failure_checklist_intent
```

In `contextual_boost()`, remove every branch that calls those helpers and any remaining 200-360 benchmark-shaped branch. Keep only generic low-weight branches if needed, with individual additions below 100.

- [ ] **Step 5: Run Task 2 tests to verify GREEN**

Run:

```bash
pytest tests/test_rag_reranking.py::test_reranker_uses_alias_context_without_benchmark_intent_rules tests/test_acl_and_aliases.py::test_ask_reports_alias_boost_in_top_hits -q
```

Expected: PASS.

- [ ] **Step 6: Run existing reranking tests and adjust test intent**

Run:

```bash
pytest tests/test_rag_reranking.py -q
```

Expected: some old tests that assert benchmark-specific boosts may fail. For each failure, rewrite the test to assert generic behavior:

- exact document code still boosts explicit source references
- table row evidence still works for table questions
- alias context works for entity synonym questions
- no test should assert a benchmark helper or a 200-360 context boost

Do not reintroduce benchmark intent branches to make old tests pass.

- [ ] **Step 7: Commit Task 2**

Run:

```bash
git add app/rag.py app/pg_rag.py app/search.py tests/test_rag_reranking.py tests/test_acl_and_aliases.py
git commit -m "refactor: replace benchmark boosts with alias scoring"
```

---

### Task 3: GBrain Candidate Failure Isolation And Trimming

**Files:**
- Modify: `app/gbrain.py`
- Test: `tests/test_gbrain_integration.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_gbrain_integration.py`:

```python
def test_query_gbrain_keeps_hits_when_one_candidate_search_fails(monkeypatch):
    from app.gbrain import GBrainError, _query_gbrain_with_caller

    calls = []

    def caller(tool_name, params):
        calls.append((tool_name, params["query"]))
        if tool_name == "query":
            return [{"slug": "main", "title": "时间窗口制度", "content": "退款窗口期 7天。", "score": 0.8}]
        if "失败候选" in params["query"]:
            raise GBrainError("candidate failed")
        return [{"slug": "other", "title": "报销制度", "content": "次月5日提交。", "score": 0.7}]

    monkeypatch.setattr(
        "app.gbrain.gbrain_query_candidates",
        lambda question, candidate_limit=8: ["失败候选", "期限"],
    )

    hits = _query_gbrain_with_caller(caller, {"query": "时间窗口和期限", "limit": 5}, "时间窗口和期限", 5, 8)

    assert [hit.slug for hit in hits] == ["main", "other"]
    assert ("search", "期限") in calls


def test_gbrain_candidates_trim_low_information_cjk_windows():
    from app.gbrain import gbrain_query_candidates

    candidates = gbrain_query_candidates(
        "梳理公司所有制度文档中，与时间窗口或期限相关的全部条款，并判断是否存在相互矛盾。",
        candidate_limit=8,
    )

    assert "时间窗口" in candidates
    assert "期限" in candidates
    assert len(candidates) <= 8
    assert not any(len(item) == 4 and item in {"梳理公司", "公司所有", "所有制度"} for item in candidates)
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
pytest tests/test_gbrain_integration.py::test_query_gbrain_keeps_hits_when_one_candidate_search_fails tests/test_gbrain_integration.py::test_gbrain_candidates_trim_low_information_cjk_windows -q
```

Expected: FAIL because a candidate `GBrainError` currently discards all hits, and CJK windows are still noisy.

- [ ] **Step 3: Isolate candidate failures**

In `app/gbrain.py`, change `_query_gbrain_with_caller()` to:

```python
def _query_gbrain_with_caller(
    caller,
    params: dict[str, Any],
    question: str,
    limit: int,
    candidate_limit: int = 8,
) -> list[GBrainHit]:
    collected: list[GBrainHit] = []
    try:
        collected.extend(normalize_gbrain_hits(caller("query", params)))
    except GBrainError:
        pass
    for candidate in gbrain_query_candidates(question, candidate_limit):
        try:
            collected.extend(normalize_gbrain_hits(caller("search", {"query": candidate, "limit": limit})))
        except GBrainError:
            continue
    return rank_gbrain_hits(dedupe_gbrain_hits(collected), question, limit)
```

- [ ] **Step 4: Trim candidates**

In `gbrain_query_candidates()`, add a helper near the function:

```python
LOW_INFORMATION_CJK_PREFIXES = ("梳理", "公司", "所有", "全部", "哪些", "如果", "一个")
```

Inside `gbrain_query_candidates()`, add meaningful phrases before sliding windows:

```python
for phrase in ["时间窗口", "期限", "时限", "条款", "相互矛盾", "部门总监", "部门负责人"]:
    if phrase in question and phrase not in candidates:
        candidates.append(phrase)
```

Then change the CJK sliding-window block so it only runs when there are fewer than half the requested candidates and skips low-information prefixes:

```python
if len(candidates) < max(2, candidate_limit // 2):
    for cjk_part in re.findall(r"[\u4e00-\u9fff]{4,}", cleaned):
        for size in (4, 5, 6):
            for index in range(0, max(0, len(cjk_part) - size + 1)):
                token = cjk_part[index : index + size]
                if token.startswith(LOW_INFORMATION_CJK_PREFIXES):
                    continue
                if token not in candidates:
                    candidates.append(token)
                if len(candidates) >= candidate_limit:
                    break
            if len(candidates) >= candidate_limit:
                break
        if len(candidates) >= candidate_limit:
            break
```

- [ ] **Step 5: Run Task 3 tests to verify GREEN**

Run:

```bash
pytest tests/test_gbrain_integration.py::test_query_gbrain_keeps_hits_when_one_candidate_search_fails tests/test_gbrain_integration.py::test_gbrain_candidates_trim_low_information_cjk_windows -q
```

Expected: PASS.

- [ ] **Step 6: Run full GBrain integration tests**

Run:

```bash
pytest tests/test_gbrain_integration.py -q
```

Expected: PASS. If old candidate ordering tests fail, update them so they assert preserved high-signal candidates and bounded count rather than noisy sliding-window retries.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git add app/gbrain.py tests/test_gbrain_integration.py
git commit -m "fix: make gbrain candidate search resilient"
```

---

### Task 4: Built-In Upgraded Evaluation And ON/OFF Comparison

**Files:**
- Modify: `app/eval.py`
- Modify: `app/models.py`
- Modify: `app/api.py`
- Modify: `scripts/upgraded_qa_benchmark.py`
- Test: `tests/test_eval_upgraded.py`

- [ ] **Step 1: Write failing tests for app-owned upgraded eval**

Create `tests/test_eval_upgraded.py`:

```python
from app.eval import UpgradedEvalQuestion, compare_upgraded_eval, summarize_upgraded_rows
from app.models import AskResponse


def test_summarize_upgraded_rows_reports_pass_rate_and_latency():
    rows = [
        {"passed": True, "elapsed_ms": 10, "citations": [{"source_id": "05"}]},
        {"passed": False, "elapsed_ms": 30, "citations": []},
    ]

    summary = summarize_upgraded_rows(rows)

    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["p95_ms"] >= 30


def test_compare_upgraded_eval_runs_gbrain_off_and_on(monkeypatch):
    questions = [
        UpgradedEvalQuestion(
            id="QX",
            tier="T",
            category="test",
            question="部门总监有哪些审批权限？",
            domain="customer_service",
            expected_sources=["07"],
            required_terms=["报销"],
            risk="red",
        )
    ]
    calls = []

    def fake_ask(settings, request):
        calls.append(settings.gbrain_enabled)
        return AskResponse(
            query_id="qry_test",
            answer="部门负责人审批报销。",
            citations=[{"source_id": "07", "wiki_page": None, "snippet": "报销"}],
            confidence="high",
            missing_info=[],
            memory_hits=[],
            retrieval_strategy={"top_hits": []},
            user_context={},
        )

    monkeypatch.setattr("app.eval.ask", fake_ask)

    result = compare_upgraded_eval(object(), questions=questions, mode="both")

    assert calls == [False, True]
    assert result["off"]["summary"]["passed"] == 1
    assert result["on"]["summary"]["passed"] == 1
    assert result["delta"]["passed"] == 0
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
pytest tests/test_eval_upgraded.py -q
```

Expected: FAIL because the upgraded eval classes/functions do not exist.

- [ ] **Step 3: Add upgraded eval dataclass and summary helpers**

In `app/eval.py`, add:

```python
from dataclasses import dataclass
from time import perf_counter
import re


@dataclass(frozen=True)
class UpgradedEvalQuestion:
    id: str
    tier: str
    category: str
    question: str
    domain: str | None
    expected_sources: list[str]
    required_terms: list[str]
    risk: str
```

Move the 31 `QUESTIONS` definitions from `scripts/upgraded_qa_benchmark.py` into `UPGRADED_QUESTIONS` in `app/eval.py`, preserving IDs, domains, expected sources, required terms, and risk.

Add helpers:

```python
def _term_matches(answer: str, patterns: list[str]) -> list[str]:
    matched: list[str] = []
    for pattern in patterns:
        alternatives = [part.strip() for part in pattern.split("|") if part.strip()]
        if any(re.search(re.escape(part), answer, flags=re.I) for part in alternatives):
            matched.append(pattern)
    return matched


def _source_matches(citations: list[dict], expected_sources: list[str]) -> list[str]:
    matched: list[str] = []
    for expected in expected_sources:
        expected_key = expected.lower()
        for citation in citations:
            source_id = str(citation.get("source_id") or "").lower()
            if expected_key and expected_key in source_id:
                matched.append(expected)
                break
    return matched
```

Add `summarize_upgraded_rows(rows)` with p50/p95 helper:

```python
def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * pct)))
    return float(ordered[index])


def summarize_upgraded_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    passed = sum(1 for row in rows if row.get("passed"))
    citation_count = sum(1 for row in rows if row.get("citations"))
    latencies = [float(row.get("elapsed_ms") or 0) for row in rows]
    return {
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "with_citations": citation_count,
        "citation_rate": round(citation_count / total, 4) if total else 0.0,
        "p50_ms": round(_percentile(latencies, 0.5), 2),
        "p95_ms": round(_percentile(latencies, 0.95), 2),
    }
```

- [ ] **Step 4: Add runner and comparison functions**

In `app/eval.py`, add:

```python
def run_upgraded_question(settings: Settings, question: UpgradedEvalQuestion) -> dict[str, Any]:
    started = perf_counter()
    result = ask(
        settings,
        AskRequest(question=question.question, domain=question.domain, require_citations=True),
    )
    elapsed_ms = (perf_counter() - started) * 1000
    citations = [citation.model_dump() if hasattr(citation, "model_dump") else dict(citation) for citation in result.citations]
    matched_sources = _source_matches(citations, question.expected_sources)
    matched_terms = _term_matches(result.answer, question.required_terms)
    passed = (
        (not question.expected_sources or len(matched_sources) > 0)
        and len(matched_terms) >= max(1, min(len(question.required_terms), 2))
        and result.confidence != "low"
    )
    return {
        "id": question.id,
        "tier": question.tier,
        "category": question.category,
        "question": question.question,
        "domain": question.domain,
        "expected_sources": question.expected_sources,
        "matched_sources": matched_sources,
        "required_terms": question.required_terms,
        "matched_terms": matched_terms,
        "passed": passed,
        "answer": result.answer,
        "confidence": result.confidence,
        "citations": citations,
        "elapsed_ms": round(elapsed_ms, 2),
        "retrieval_strategy": result.retrieval_strategy,
    }


def run_upgraded_eval(settings: Settings, questions: list[UpgradedEvalQuestion] | None = None, domain: str | None = None) -> dict[str, Any]:
    selected = list(questions or UPGRADED_QUESTIONS)
    if domain:
        selected = [question for question in selected if question.domain == domain]
    rows = [run_upgraded_question(settings, question) for question in selected]
    return {"summary": summarize_upgraded_rows(rows), "rows": rows}


def _settings_with_gbrain(settings: Settings, enabled: bool) -> Settings:
    copied = settings.model_copy() if hasattr(settings, "model_copy") else copy.copy(settings)
    copied.gbrain_enabled = enabled
    return copied


def compare_upgraded_eval(settings: Settings, questions: list[UpgradedEvalQuestion] | None = None, domain: str | None = None, mode: str = "both") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if mode in {"off", "both"}:
        result["off"] = run_upgraded_eval(_settings_with_gbrain(settings, False), questions=questions, domain=domain)
    if mode in {"on", "both"}:
        result["on"] = run_upgraded_eval(_settings_with_gbrain(settings, True), questions=questions, domain=domain)
    if "off" in result and "on" in result:
        result["delta"] = {
            "passed": result["on"]["summary"]["passed"] - result["off"]["summary"]["passed"],
            "pass_rate": round(result["on"]["summary"]["pass_rate"] - result["off"]["summary"]["pass_rate"], 4),
            "p95_ms": round(result["on"]["summary"]["p95_ms"] - result["off"]["summary"]["p95_ms"], 2),
        }
    return result
```

Ensure imports include:

```python
import copy
from typing import Any
```

- [ ] **Step 5: Add API models and endpoint**

In `app/models.py`, add:

```python
class UpgradedEvalRunRequest(BaseModel):
    domain: Domain | None = None
    gbrain_mode: Literal["on", "off", "both"] = "both"
```

In `app/api.py`, import `compare_upgraded_eval` and `UpgradedEvalRunRequest`, then add endpoint near existing eval endpoints:

```python
@app.post("/api/internal/eval/upgraded")
@app.post("/eval/upgraded")
def upgraded_eval_endpoint(request: UpgradedEvalRunRequest) -> dict:
    try:
        return compare_upgraded_eval(get_settings(), domain=request.domain, mode=request.gbrain_mode)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 6: Update script wrapper**

In `scripts/upgraded_qa_benchmark.py`, replace local benchmark ownership with:

```python
from app.config import get_settings
from app.eval import compare_upgraded_eval
```

Keep `write_outputs()` if desired, but call:

```python
report = compare_upgraded_eval(settings, mode="on")
```

The script should no longer define the canonical 31 questions itself.

- [ ] **Step 7: Run Task 4 tests to verify GREEN**

Run:

```bash
pytest tests/test_eval_upgraded.py -q
```

Expected: PASS.

- [ ] **Step 8: Run existing eval/API tests**

Run:

```bash
pytest tests/test_internal_flow.py tests/test_eval_upgraded.py -q
```

Expected: PASS. If `tests/test_internal_flow.py` has existing endpoint assumptions, keep old endpoints working while adding `/eval/upgraded`.

- [ ] **Step 9: Commit Task 4**

Run:

```bash
git add app/eval.py app/models.py app/api.py scripts/upgraded_qa_benchmark.py tests/test_eval_upgraded.py
git commit -m "feat: add built-in upgraded evaluation"
```

---

### Task 5: Integration Verification And Cleanup

**Files:**
- Inspect: `app/rag.py`
- Inspect: `app/gbrain.py`
- Inspect: `app/eval.py`
- Inspect: `tests/`

- [ ] **Step 1: Search for removed benchmark helpers**

Run:

```bash
rg "pricing_calculation_intent|content_safety_dependency_intent|privacy_audit_intent|time_window_policy_intent|prd_priority_dependency_intent|api_failure_checklist_intent" app tests
```

Expected: no matches. If matches remain in tests, remove or rewrite them. If matches remain in app code, remove the benchmark helper branch.

- [ ] **Step 2: Search for high benchmark-shaped context boosts**

Run:

```bash
rg "boost \\+= (2[0-9][0-9]|3[0-9][0-9])" app/rag.py
```

Expected: no matches. If there are matches, replace them with data-driven alias/phrase/table/document-code scoring.

- [ ] **Step 3: Run focused test suite**

Run:

```bash
pytest tests/test_acl_and_aliases.py tests/test_rag_reranking.py tests/test_gbrain_integration.py tests/test_eval_upgraded.py -q
```

Expected: PASS.

- [ ] **Step 4: Run broader Python tests**

Run:

```bash
pytest -q
```

Expected: PASS, except tests that require unavailable external services may skip. Do not claim full pass unless the command exits 0.

- [ ] **Step 5: Run built-in eval if data is present**

Run:

```bash
python -m scripts.upgraded_qa_benchmark
```

Expected: report output with summary. If local benchmark data is missing, record the exact error and do not count this as verified.

- [ ] **Step 6: Final status check**

Run:

```bash
git status --short
```

Expected: only intended files modified since the task commits, with no accidental unrelated changes staged.

- [ ] **Step 7: Commit verification cleanup if needed**

If any cleanup edits were required:

```bash
git add app tests scripts
git commit -m "test: verify retrieval generalization"
```

If no cleanup edits were required, do not create an empty commit.

