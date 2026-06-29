from __future__ import annotations

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
    started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = OUTPUT_ROOT / started_at
    output_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    status = rag_status(settings)
    if status["source_count"] < 300:
        raise RuntimeError("当前 RAG 数据不足，请先运行 scripts/rag_benchmark_test_data.py 导入升级数据集。")

    gbrain_status = get_gbrain_status(settings)
    report = {
        "run_id": started_at,
        "rag_status": status,
        "gbrain_status": {
            "enabled": gbrain_status.enabled,
            "available": gbrain_status.available,
            "endpoint_configured": gbrain_status.endpoint_configured,
            "note": gbrain_status.note,
        },
        "settings": {
            "database_backend": settings.database_backend,
            "rag_store_backend": settings.rag_store_backend,
            "deepseek_enabled": bool(settings.deepseek_api_key and settings.deepseek_model),
            "deepseek_model": settings.deepseek_model,
            "gbrain_query_limit": settings.gbrain_query_limit,
            "gbrain_query_expand": settings.gbrain_query_expand,
        },
        "result": compare_upgraded_eval(settings, mode="on"),
    }
    write_outputs(output_dir, report)
    print(f"REPORT_DIR={output_dir.resolve()}", flush=True)
    print(json.dumps(report["result"]["on"]["summary"], ensure_ascii=False, indent=2), flush=True)


def write_outputs(output_dir: Path, report: dict[str, Any]) -> None:
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = report["result"]["on"]["rows"]
    if rows:
        with (output_dir / "qa_results.csv").open("w", encoding="utf-8-sig", newline="") as file:
            fieldnames = [
                "id",
                "tier",
                "category",
                "domain",
                "question",
                "expected_sources",
                "matched_sources",
                "required_terms",
                "matched_terms",
                "passed",
                "confidence",
                "elapsed_ms",
            ]
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})
    (output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")


def _csv_value(value: Any) -> Any:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return value


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["result"]["on"]["summary"]
    lines = [
        "# Upgraded QA Benchmark Report",
        "",
        f"- Run ID: `{report['run_id']}`",
        f"- DeepSeek enabled: `{report['settings']['deepseek_enabled']}`",
        f"- GBrain: `{report['gbrain_status']['note']}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Total | {summary['total']} |",
        f"| Passed | {summary['passed']} |",
        f"| Pass rate | {summary['pass_rate']} |",
        f"| Citation rate | {summary['citation_rate']} |",
        f"| P50 latency ms | {summary['p50_ms']} |",
        f"| P95 latency ms | {summary['p95_ms']} |",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()

