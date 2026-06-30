import pytest

from app.eval import UpgradedEvalQuestion
from scripts import upgraded_qa_benchmark as cli


def test_render_markdown_includes_failure_diff():
    report = {
        "run_id": "run_test",
        "settings": {"deepseek_enabled": False},
        "gbrain_status": {"note": "disabled"},
        "result": {
            "off": {
                "summary": {
                    "total": 1,
                    "passed": 1,
                    "pass_rate": 1.0,
                    "citation_rate": 1.0,
                    "p50_ms": 10,
                    "p95_ms": 10,
                    "quality_gate": {"configured": False, "passed": True, "failures": []},
                }
            },
            "on": {
                "summary": {
                    "total": 1,
                    "passed": 0,
                    "pass_rate": 0.0,
                    "citation_rate": 1.0,
                    "p50_ms": 20,
                    "p95_ms": 20,
                    "quality_gate": {"configured": False, "passed": True, "failures": []},
                }
            },
            "delta": {"passed": -1, "pass_rate": -1.0, "p95_ms": 10},
            "failure_diff": {
                "off_pass_on_fail": [
                    {
                        "id": "Q1",
                        "question": "bad with gbrain",
                        "off": {"matched_sources": ["DOC-1"], "matched_terms": ["term"], "gbrain_candidates": [], "gbrain_hits": []},
                        "on": {
                            "matched_sources": [],
                            "matched_terms": [],
                            "gbrain_candidates": ["candidate"],
                            "gbrain_hits": [{"source_id": "GB-1", "title": "GBrain hit"}],
                        },
                    }
                ],
                "off_fail_on_pass": [],
                "both_fail": [],
            },
        },
    }

    markdown = cli.render_markdown(report)

    assert "## Failure Diff" in markdown
    assert "OFF pass / ON fail" in markdown
    assert "Q1" in markdown
    assert "OFF sources: `DOC-1`" in markdown
    assert "ON gbrain candidates: `candidate`" in markdown
    assert "ON gbrain hits: `GB-1 GBrain hit`" in markdown


def test_exit_code_fails_when_quality_gate_fails():
    report = {
        "result": {
            "off": {"summary": {"quality_gate": {"configured": True, "passed": False}}},
            "on": {"summary": {"quality_gate": {"configured": True, "passed": True}}},
        }
    }

    assert cli.exit_code_for_report(report) == 1


def test_main_exits_non_zero_on_quality_gate_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "rag_status", lambda settings: {})
    monkeypatch.setattr(cli, "get_settings", lambda: object())
    monkeypatch.setattr(
        cli,
        "_gbrain_status",
        lambda settings: {"enabled": False, "available": False, "endpoint_configured": False, "note": "disabled"},
    )
    monkeypatch.setattr(
        cli,
        "compare_upgraded_eval",
        lambda *args, **kwargs: {
            "off": {
                "summary": {
                    "total": 1,
                    "passed": 0,
                    "pass_rate": 0.0,
                    "citation_rate": 0.0,
                    "p50_ms": 10,
                    "p95_ms": 10,
                    "quality_gate": {"configured": True, "passed": False, "failures": ["pass_rate"]},
                },
                "rows": [],
            }
        },
    )
    monkeypatch.setattr("sys.argv", ["upgraded_qa_benchmark", "--min-pass-rate", "0.9"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1


def test_main_passes_selected_questions_and_limit_to_eval(monkeypatch, tmp_path):
    captured = {}

    monkeypatch.setattr(cli, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "rag_status", lambda settings: {})
    monkeypatch.setattr(cli, "get_settings", lambda: object())
    monkeypatch.setattr(
        cli,
        "_gbrain_status",
        lambda settings: {"enabled": False, "available": False, "endpoint_configured": False, "note": "disabled"},
    )
    monkeypatch.setattr(
        cli,
        "UPGRADED_QUESTIONS",
        [
            UpgradedEvalQuestion("Q01", "T1", "cat", "one", None, [], [], "green"),
            UpgradedEvalQuestion("Q20", "T3", "cat", "twenty", None, [], [], "red"),
            UpgradedEvalQuestion("Q21", "T3", "cat", "twenty one", None, [], [], "red"),
        ],
    )

    def fake_compare(*args, **kwargs):
        captured["question_ids"] = [question.id for question in kwargs["questions"]]
        return {
            "off": {
                "summary": {
                    "total": 1,
                    "passed": 1,
                    "pass_rate": 1.0,
                    "citation_rate": 1.0,
                    "p50_ms": 10,
                    "p95_ms": 10,
                    "quality_gate": {"configured": False, "passed": True, "failures": []},
                },
                "rows": [],
            }
        }

    monkeypatch.setattr(cli, "compare_upgraded_eval", fake_compare)
    monkeypatch.setattr("sys.argv", ["upgraded_qa_benchmark", "--question-id", "Q20", "--question-id", "Q21", "--limit", "1"])

    cli.main()

    assert captured["question_ids"] == ["Q20"]


def test_select_questions_applies_domain_before_limit(monkeypatch):
    monkeypatch.setattr(
        cli,
        "UPGRADED_QUESTIONS",
        [
            UpgradedEvalQuestion("Q01", "T1", "cat", "one", "product", [], [], "green"),
            UpgradedEvalQuestion("Q08", "T1", "cat", "eight", "customer_service", [], [], "green"),
            UpgradedEvalQuestion("Q09", "T1", "cat", "nine", "customer_service", [], [], "green"),
        ],
    )

    selected = cli.select_questions(question_ids=None, limit=1, domain="customer_service")

    assert [question.id for question in selected] == ["Q08"]


def test_progress_callback_prints_question_status(capsys):
    question = UpgradedEvalQuestion("Q20", "T3", "cat", "twenty", None, [], [], "red")
    row = {"id": "Q20", "passed": True, "elapsed_ms": 123.45}

    cli.print_progress("off", 1, 3, question, row)

    assert "[off] Q20 PASS 123.45ms (1/3)" in capsys.readouterr().out
