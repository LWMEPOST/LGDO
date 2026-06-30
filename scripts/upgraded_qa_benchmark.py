from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.catalog import rag_status
from app.config import get_settings
from app.eval import compare_upgraded_eval
from app.gbrain import get_gbrain_status


OUTPUT_ROOT = Path("output") / "upgraded_qa_benchmark"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the built-in upgraded QA benchmark.")
    parser.add_argument("--mode", choices=["on", "off", "both"], default="both")
    parser.add_argument("--domain", choices=["product", "customer_service"], default=None)
    parser.add_argument("--min-pass-rate", type=float, default=None)
    parser.add_argument("--min-citation-rate", type=float, default=None)
    parser.add_argument("--max-p95-ms", type=float, default=None)
    args = parser.parse_args()

    started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = OUTPUT_ROOT / started_at
    output_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    report = {
        "run_id": started_at,
        "rag_status": rag_status(settings),
        "gbrain_status": _gbrain_status(settings),
        "settings": {
            "database_backend": settings.database_backend,
            "rag_store_backend": settings.rag_store_backend,
            "deepseek_enabled": bool(settings.deepseek_api_key and settings.deepseek_model),
            "deepseek_model": settings.deepseek_model,
            "gbrain_query_limit": settings.gbrain_query_limit,
            "gbrain_query_expand": settings.gbrain_query_expand,
            "gbrain_candidate_limit": settings.gbrain_candidate_limit,
        },
        "result": compare_upgraded_eval(
            settings,
            domain=args.domain,
            mode=args.mode,
            pass_rate_threshold=args.min_pass_rate,
            citation_rate_threshold=args.min_citation_rate,
            p95_ms_threshold=args.max_p95_ms,
        ),
    }
    write_outputs(output_dir, report)
    print(f"REPORT_DIR={output_dir.resolve()}", flush=True)
    print(json.dumps(_summary_view(report), ensure_ascii=False, indent=2), flush=True)


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


if __name__ == "__main__":
    main()
