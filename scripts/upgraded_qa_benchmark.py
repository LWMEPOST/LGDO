from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from app.catalog import rag_status
from app.config import get_settings
from app.eval import UPGRADED_QUESTIONS, UpgradedEvalQuestion, compare_upgraded_eval
from app.gbrain import get_gbrain_status


OUTPUT_ROOT = Path("output") / "upgraded_qa_benchmark"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the built-in upgraded QA benchmark.")
    parser.add_argument("--mode", choices=["on", "off", "both"], default="both")
    parser.add_argument("--domain", choices=["product", "customer_service"], default=None)
    parser.add_argument("--min-pass-rate", type=float, default=None)
    parser.add_argument("--min-citation-rate", type=float, default=None)
    parser.add_argument("--max-p95-ms", type=float, default=None)
    parser.add_argument("--question-id", action="append", default=None, help="Run only selected upgraded QA question IDs. Can be repeated.")
    parser.add_argument("--limit", type=int, default=None, help="Run at most this many selected questions.")
    args = parser.parse_args()

    started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = OUTPUT_ROOT / started_at
    output_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    selected_questions = select_questions(args.question_id, args.limit, args.domain)
    report = {
        "run_id": started_at,
        "rag_status": rag_status(settings),
        "gbrain_status": _gbrain_status(settings),
        "settings": _settings_summary(settings),
        "result": compare_upgraded_eval(
            settings,
            questions=selected_questions,
            mode=args.mode,
            pass_rate_threshold=args.min_pass_rate,
            citation_rate_threshold=args.min_citation_rate,
            p95_ms_threshold=args.max_p95_ms,
            progress_callback=print_progress,
        ),
    }
    write_outputs(output_dir, report)
    print(f"REPORT_DIR={output_dir.resolve()}", flush=True)
    print(json.dumps(_summary_view(report), ensure_ascii=False, indent=2), flush=True)
    code = exit_code_for_report(report)
    if code:
        raise SystemExit(code)


def _gbrain_status(settings: Any) -> dict[str, Any]:
    status = get_gbrain_status(settings)
    return {
        "enabled": status.enabled,
        "available": status.available,
        "endpoint_configured": status.endpoint_configured,
        "note": status.note,
    }


def _summary_view(report: dict[str, Any]) -> dict[str, Any]:
    result = report["result"]
    return {key: value["summary"] for key, value in result.items() if isinstance(value, dict) and "summary" in value}


def _settings_summary(settings: Any) -> dict[str, Any]:
    deepseek_api_key = getattr(settings, "deepseek_api_key", None)
    deepseek_model = getattr(settings, "deepseek_model", None)
    return {
        "database_backend": getattr(settings, "database_backend", None),
        "rag_store_backend": getattr(settings, "rag_store_backend", None),
        "deepseek_enabled": bool(deepseek_api_key and deepseek_model),
        "deepseek_model": deepseek_model,
        "gbrain_query_limit": getattr(settings, "gbrain_query_limit", None),
        "gbrain_query_expand": getattr(settings, "gbrain_query_expand", None),
        "gbrain_candidate_limit": getattr(settings, "gbrain_candidate_limit", None),
    }


def exit_code_for_report(report: dict[str, Any]) -> int:
    for value in report.get("result", {}).values():
        if not isinstance(value, dict):
            continue
        gate = value.get("summary", {}).get("quality_gate", {})
        if gate.get("configured") and not gate.get("passed"):
            return 1
    return 0


def select_questions(
    question_ids: list[str] | None,
    limit: int | None,
    domain: str | None = None,
) -> list[UpgradedEvalQuestion]:
    selected = list(UPGRADED_QUESTIONS)
    if domain:
        selected = [question for question in selected if question.domain == domain]
    if question_ids:
        wanted = {question_id.strip().upper() for question_id in question_ids if question_id.strip()}
        selected = [question for question in selected if question.id.upper() in wanted]
    if limit is not None:
        selected = selected[: max(0, limit)]
    return selected


def print_progress(
    mode: str,
    index: int,
    total: int,
    question: UpgradedEvalQuestion,
    row: dict[str, Any],
) -> None:
    status = "PASS" if row.get("passed") else "FAIL"
    elapsed_ms = row.get("elapsed_ms", 0)
    print(f"[{mode}] {question.id} {status} {elapsed_ms}ms ({index}/{total})", flush=True)


def write_outputs(output_dir: Path, report: dict[str, Any]) -> None:
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = _flat_rows(report["result"])
    if rows:
        with (output_dir / "qa_results.csv").open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    (output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")


def _flat_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for mode in ("off", "on"):
        for row in result.get(mode, {}).get("rows", []):
            rows.append(
                {
                    "mode": mode,
                    "id": row["id"],
                    "tier": row["tier"],
                    "category": row["category"],
                    "risk": row["risk"],
                    "domain": row["domain"] or "all",
                    "passed": row["passed"],
                    "confidence": row["confidence"],
                    "elapsed_ms": row["elapsed_ms"],
                    "expected_sources": ",".join(row["expected_sources"]),
                    "matched_sources": ",".join(row["matched_sources"]),
                    "required_terms": ",".join(row["required_terms"]),
                    "matched_terms": ",".join(row["matched_terms"]),
                    "citation_count": len(row["citations"]),
                    "gbrain_candidate_count": row.get("gbrain_diagnostics", {}).get("candidate_count", 0),
                    "gbrain_candidates": "|".join(row.get("gbrain_diagnostics", {}).get("candidates", [])),
                    "question": row["question"],
                }
            )
    return rows


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Upgraded QA Benchmark Report",
        "",
        f"- Run ID: `{report['run_id']}`",
        f"- DeepSeek enabled: `{report['settings']['deepseek_enabled']}`",
        f"- GBrain: `{report['gbrain_status']['note']}`",
        "",
        "## Summary",
        "",
        "| Mode | Total | Passed | Pass Rate | Citation Rate | P50 ms | P95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for mode, result in report["result"].items():
        if not isinstance(result, dict) or "summary" not in result:
            continue
        summary = result["summary"]
        lines.append(
            f"| {mode} | {summary['total']} | {summary['passed']} | {summary['pass_rate']} | "
            f"{summary['citation_rate']} | {summary['p50_ms']} | {summary['p95_ms']} |"
        )
    if "delta" in report["result"]:
        delta = report["result"]["delta"]
        lines.extend([
            "",
            "## GBrain Delta",
            "",
            f"- Passed delta: `{delta['passed']}`",
            f"- Pass-rate delta: `{delta['pass_rate']}`",
            f"- P95 latency delta ms: `{delta['p95_ms']}`",
        ])
    diff = report["result"].get("failure_diff")
    if isinstance(diff, dict):
        lines.extend(["", "## Failure Diff", ""])
        lines.extend(_failure_diff_lines("OFF pass / ON fail", diff.get("off_pass_on_fail") or []))
        lines.extend(_failure_diff_lines("OFF fail / ON pass", diff.get("off_fail_on_pass") or []))
        lines.extend(_failure_diff_lines("Both fail", diff.get("both_fail") or []))
    gate_lines = []
    for mode, result in report["result"].items():
        if not isinstance(result, dict) or "summary" not in result:
            continue
        gate = result["summary"].get("quality_gate", {})
        if gate.get("configured"):
            status = "PASS" if gate.get("passed") else "FAIL"
            failures = ", ".join(gate.get("failures") or []) or "none"
            gate_lines.append(f"- {mode}: `{status}` failures=`{failures}`")
    if gate_lines:
        lines.extend(["", "## Quality Gate", "", *gate_lines])
    lines.append("")
    return "\n".join(lines)


def _failure_diff_lines(title: str, rows: list[dict[str, Any]], limit: int = 8) -> list[str]:
    lines = [f"### {title}", ""]
    if not rows:
        lines.extend(["- none", ""])
        return lines
    for row in rows[:limit]:
        question = str(row.get("question") or "").replace("\n", " ").strip()
        if len(question) > 100:
            question = question[:97].rstrip() + "..."
        lines.append(f"- `{row.get('id')}` {question}")
        lines.extend(_diff_detail_lines("OFF", row.get("off") or {}))
        lines.extend(_diff_detail_lines("ON", row.get("on") or {}))
    if len(rows) > limit:
        lines.append(f"- ...and {len(rows) - limit} more")
    lines.append("")
    return lines


def _diff_detail_lines(label: str, detail: dict[str, Any]) -> list[str]:
    lines = []
    sources = _format_list(detail.get("matched_sources") or [])
    terms = _format_list(detail.get("matched_terms") or [])
    candidates = _format_list(detail.get("gbrain_candidates") or [])
    hits = _format_gbrain_hits(detail.get("gbrain_hits") or [])
    if sources:
        lines.append(f"  - {label} sources: `{sources}`")
    if terms:
        lines.append(f"  - {label} terms: `{terms}`")
    if candidates:
        lines.append(f"  - {label} gbrain candidates: `{candidates}`")
    if hits:
        lines.append(f"  - {label} gbrain hits: `{hits}`")
    return lines


def _format_list(values: list[Any], limit: int = 4) -> str:
    items = [str(value).strip() for value in values if str(value).strip()]
    if not items:
        return ""
    clipped = items[:limit]
    suffix = f", +{len(items) - limit} more" if len(items) > limit else ""
    return ", ".join(clipped) + suffix


def _format_gbrain_hits(values: list[Any], limit: int = 3) -> str:
    items = []
    for value in values:
        if isinstance(value, dict):
            source_id = str(value.get("source_id") or value.get("id") or "").strip()
            title = str(value.get("title") or value.get("wiki_page") or "").strip()
            item = " ".join(part for part in [source_id, title] if part)
        else:
            item = str(value).strip()
        if item:
            items.append(item)
    return _format_list(items, limit=limit)


if __name__ == "__main__":
    sys.exit(main())
