from app.config import Settings
from app.eval import (
    UpgradedEvalQuestion,
    compare_upgraded_eval,
    failure_diff,
    run_upgraded_eval,
    run_upgraded_question,
    summarize_upgraded_rows,
)
from app.models import AskResponse, Citation


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


def test_summarize_upgraded_rows_applies_quality_gate_thresholds():
    rows = [
        {"passed": True, "elapsed_ms": 100, "citations": [{"source_id": "05"}]},
        {"passed": False, "elapsed_ms": 250, "citations": []},
    ]

    summary = summarize_upgraded_rows(
        rows,
        pass_rate_threshold=0.75,
        citation_rate_threshold=0.75,
        p95_ms_threshold=200,
    )

    assert summary["quality_gate"]["passed"] is False
    assert summary["quality_gate"]["failures"] == ["pass_rate", "citation_rate", "p95_ms"]
    assert summary["quality_gate"]["thresholds"]["pass_rate"] == 0.75


def test_summarize_upgraded_rows_quality_gate_passes_when_thresholds_are_met():
    rows = [
        {"passed": True, "elapsed_ms": 100, "citations": [{"source_id": "05"}]},
        {"passed": True, "elapsed_ms": 150, "citations": [{"source_id": "06"}]},
    ]

    summary = summarize_upgraded_rows(
        rows,
        pass_rate_threshold=1.0,
        citation_rate_threshold=1.0,
        p95_ms_threshold=200,
    )

    assert summary["quality_gate"]["passed"] is True
    assert summary["quality_gate"]["failures"] == []


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
            citations=[Citation(source_id="07", wiki_page=None, snippet="报销")],
            confidence="high",
            missing_info=[],
            memory_hits=[],
            retrieval_strategy={"top_hits": []},
            user_context={},
        )

    monkeypatch.setattr("app.eval.ask", fake_ask)

    result = compare_upgraded_eval(Settings(), questions=questions, mode="both")

    assert calls == [False, True]
    assert result["off"]["summary"]["passed"] == 1
    assert result["on"]["summary"]["passed"] == 1
    assert result["delta"]["passed"] == 0
    assert result["failure_diff"]["off_pass_on_fail"] == []


def test_run_upgraded_eval_calls_progress_callback(monkeypatch):
    question = UpgradedEvalQuestion("QX", "T1", "cat", "question", None, [], [], "green")
    events = []

    monkeypatch.setattr(
        "app.eval.run_upgraded_question",
        lambda settings, item: {"id": item.id, "passed": True, "elapsed_ms": 1, "citations": []},
    )

    run_upgraded_eval(
        Settings(deepseek_api_key="", deepseek_model=""),
        questions=[question],
        progress_callback=lambda mode, index, total, item, row: events.append((mode, index, total, item.id, row["id"])),
        mode_label="off",
    )

    assert events == [("off", 1, 1, "QX", "QX")]


def test_failure_diff_reports_on_off_regressions_and_improvements():
    result = failure_diff(
        {
            "off": {
                "rows": [
                    {"id": "Q1", "passed": True, "matched_sources": ["A"], "matched_terms": ["x"]},
                    {"id": "Q2", "passed": False, "matched_sources": [], "matched_terms": []},
                    {"id": "Q3", "passed": False, "matched_sources": ["C"], "matched_terms": []},
                ]
            },
            "on": {
                "rows": [
                    {"id": "Q1", "passed": False, "matched_sources": [], "matched_terms": ["x"]},
                    {"id": "Q2", "passed": True, "matched_sources": ["B"], "matched_terms": ["y"]},
                    {"id": "Q3", "passed": False, "matched_sources": [], "matched_terms": []},
                ]
            },
        }
    )

    assert result["off_pass_on_fail"][0]["id"] == "Q1"
    assert result["off_fail_on_pass"][0]["id"] == "Q2"
    assert result["both_fail"][0]["id"] == "Q3"


def test_run_upgraded_eval_respects_explicit_empty_question_list(monkeypatch):
    def fail_ask(settings, request):
        raise AssertionError("ask should not run for an explicit empty eval set")

    monkeypatch.setattr("app.eval.ask", fail_ask)

    result = run_upgraded_eval(Settings(), questions=[])

    assert result["summary"]["total"] == 0
    assert result["rows"] == []


def test_run_upgraded_question_includes_gbrain_candidate_diagnostics(monkeypatch):
    question = UpgradedEvalQuestion(
        id="QX",
        tier="T",
        category="test",
        question="生成失败原因怎么排查？",
        domain="product",
        expected_sources=["SUP-0074"],
        required_terms=["生成失败"],
        risk="green",
    )

    def fake_ask(settings, request):
        return AskResponse(
            query_id="qry_test",
            answer="生成失败通常可先重试。",
            citations=[Citation(source_id="SUP-0074", wiki_page=None, snippet="生成失败")],
            confidence="high",
            missing_info=[],
            memory_hits=[],
            retrieval_strategy={"gbrain_hits": 0},
            user_context={},
        )

    monkeypatch.setattr("app.eval.ask", fake_ask)

    row = run_upgraded_question(Settings(gbrain_candidate_limit=3), question)

    diagnostics = row["gbrain_diagnostics"]
    assert diagnostics["candidate_limit"] == 3
    assert diagnostics["candidate_count"] <= 3
    assert "生成失败" in diagnostics["candidates"]


def test_upgraded_eval_endpoint_forwards_mode_and_domain(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    captured = {}

    def fake_compare(
        settings,
        *,
        domain=None,
        mode="both",
        questions=None,
        pass_rate_threshold=None,
        citation_rate_threshold=None,
        p95_ms_threshold=None,
    ):
        captured["domain"] = domain
        captured["mode"] = mode
        captured["pass_rate_threshold"] = pass_rate_threshold
        captured["citation_rate_threshold"] = citation_rate_threshold
        captured["p95_ms_threshold"] = p95_ms_threshold
        return {"off": {"summary": {"total": 0, "passed": 0}}}

    monkeypatch.setattr("app.api.compare_upgraded_eval", fake_compare)

    response = TestClient(app).post(
        "/api/internal/eval/upgraded",
        json={
            "domain": "customer_service",
            "gbrain_mode": "off",
            "pass_rate_threshold": 0.8,
            "citation_rate_threshold": 0.9,
            "p95_ms_threshold": 15000,
        },
    )

    assert response.status_code == 200
    assert response.json()["off"]["summary"]["total"] == 0
    assert captured == {
        "domain": "customer_service",
        "mode": "off",
        "pass_rate_threshold": 0.8,
        "citation_rate_threshold": 0.9,
        "p95_ms_threshold": 15000,
    }
