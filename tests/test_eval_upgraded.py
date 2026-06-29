from app.eval import UpgradedEvalQuestion, compare_upgraded_eval, summarize_upgraded_rows
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

    result = compare_upgraded_eval(object(), questions=questions, mode="both")

    assert calls == [False, True]
    assert result["off"]["summary"]["passed"] == 1
    assert result["on"]["summary"]["passed"] == 1
    assert result["delta"]["passed"] == 0
