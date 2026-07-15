from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.catalog import rag_status
from app.config import get_settings
from app.db import MAIN_TABLES, connect_app, connect_postgres, init_app_db, rows_to_dicts
from app.gbrain import get_gbrain_status
from app.ingest import scan_sources
from app.models import AskRequest, CompileRequest, ScanRequest
from app.pg_rag import init_pg_rag
from app.search import ask
from app.vault import ensure_vault
from app.wiki import compile_wiki


TEST_DATA_ROOT = Path(r"C:\d\XM\RAG_test_data")
QUALITY_REPORT_PATH = TEST_DATA_ROOT / "QUALITY_REPORT.md"
MANIFEST_PATH = TEST_DATA_ROOT / "batch" / "manifest.json"
OUTPUT_ROOT = Path("output") / "rag_benchmark"
FORMATS = ("txt", "md", "docx", "pdf", "pptx")
BASE_FORMAT_DIRS = ("txt", "md", "docx", "pdf")
BATCH_FORMAT_DIRS = ("txt", "md", "docx", "pdf", "pptx")


@dataclass(frozen=True)
class TestFile:
    source_path: Path
    domain: str
    category: str
    fmt: str
    doc_code: str
    doc_name: str
    dataset_part: str
    manifest_id: str | None = None


QUESTIONS: list[dict[str, Any]] = [
    {
        "id": "q01",
        "domain": "product",
        "expected_doc": "01",
        "question": "灵创AI创作平台的核心价值主张是什么？",
        "terms": ["10分钟", "创意", "成品"],
    },
    {
        "id": "q02",
        "domain": "product",
        "expected_doc": "01",
        "question": "文生图模块支持多少种预设风格和哪些输出尺寸？",
        "terms": ["16种", "1:1", "9:16", "16:9"],
    },
    {
        "id": "q03",
        "domain": "product",
        "expected_doc": "01",
        "question": "V3.0正式版计划什么时候上线？",
        "terms": ["2025-10-01", "正式版"],
    },
    {
        "id": "q04",
        "domain": "product",
        "expected_doc": "02",
        "question": "新用户注册后会赠送多少创作积分？",
        "terms": ["50", "创作积分"],
    },
    {
        "id": "q05",
        "domain": "product",
        "expected_doc": "02",
        "question": "专业套餐的API调用频率限制是多少？",
        "terms": ["300次/分钟", "专业套餐"],
    },
    {
        "id": "q06",
        "domain": "product",
        "expected_doc": "02",
        "question": "系统故障导致积分扣了但没出图，平台怎么处理？",
        "terms": ["24小时", "退还"],
    },
    {
        "id": "q07",
        "domain": "product",
        "expected_doc": "03",
        "question": "V3.0.2正式版新增了哪些代表性功能？",
        "terms": ["提示词助手", "团队协作空间"],
    },
    {
        "id": "q08",
        "domain": "product",
        "expected_doc": "04",
        "question": "充值200元但少了200积分的工单是怎么解决的？",
        "terms": ["补回", "200积分", "50积分"],
    },
    {
        "id": "q09",
        "domain": "product",
        "expected_doc": "04",
        "question": "文生图API返回401但API Key没过期，客服建议检查什么？",
        "terms": ["Authorization", "Bearer", "空格"],
    },
    {
        "id": "q10",
        "domain": "product",
        "expected_doc": "05",
        "question": "年度套餐的退款窗口期和积分使用限制是什么？",
        "terms": ["7天", "500", "按剩余天数比例退款"],
    },
    {
        "id": "q11",
        "domain": "product",
        "expected_doc": "05",
        "question": "平台月度服务可用性承诺是多少，不可用时间上限是多少？",
        "terms": ["99.9", "43.2"],
    },
    {
        "id": "q12",
        "domain": "customer_service",
        "expected_doc": "06",
        "question": "公司的标准工作时间和核心工作时段是什么？",
        "terms": ["9:00-18:00", "10:00-16:00"],
    },
    {
        "id": "q13",
        "domain": "customer_service",
        "expected_doc": "06",
        "question": "忘记打卡补卡每月最多几次？",
        "terms": ["补卡", "3次"],
    },
    {
        "id": "q14",
        "domain": "customer_service",
        "expected_doc": "06",
        "question": "婚假有多少天，审批人是谁？",
        "terms": ["10天", "直属上级", "HR"],
    },
    {
        "id": "q15",
        "domain": "customer_service",
        "expected_doc": "07",
        "question": "普通员工在一线城市出差住宿标准是多少？",
        "terms": ["一线城市", "350"],
    },
    {
        "id": "q16",
        "domain": "customer_service",
        "expected_doc": "07",
        "question": "超过10000元的报销需要哪些审批层级？",
        "terms": ["财务总监", "总经理"],
    },
    {
        "id": "q17",
        "domain": "customer_service",
        "expected_doc": "08",
        "question": "常规采购的金额范围和采购方式是什么？",
        "terms": ["10000-50000", "至少3家比价"],
    },
    {
        "id": "q18",
        "domain": "customer_service",
        "expected_doc": "08",
        "question": "紧急采购的限额和频次要求是什么？",
        "terms": ["5000元", "每人每月不超过1次"],
    },
    {
        "id": "q19",
        "domain": "customer_service",
        "expected_doc": "09",
        "question": "远程办公每周最多几天，消息需要多久内回复？",
        "terms": ["2天", "10分钟"],
    },
    {
        "id": "q20",
        "domain": "customer_service",
        "expected_doc": "09",
        "question": "超过21:00的非紧急消息应该怎么发送？",
        "terms": ["定时发送", "次日9:00"],
    },
    {
        "id": "q21",
        "domain": "product",
        "expected_doc": "API-0010",
        "question": "文生图API的prompt参数最长多少字符？",
        "terms": ["1024字符", "prompt"],
    },
    {
        "id": "q22",
        "domain": "product",
        "expected_doc": "API-0012",
        "question": "任务完成后系统如何主动通知客户服务？",
        "terms": ["Webhook", "POST"],
    },
    {
        "id": "q23",
        "domain": "product",
        "expected_doc": "API-0014",
        "question": "429 rate_limited 错误代表什么，建议如何处理？",
        "terms": ["频率限制", "等待后重试"],
    },
    {
        "id": "q24",
        "domain": "product",
        "expected_doc": "API-0016",
        "question": "A100 80GB的文生图并发和文生视频并发是多少？",
        "terms": ["A100 80GB", "8", "2"],
    },
    {
        "id": "q25",
        "domain": "customer_service",
        "expected_doc": "TKT-0038",
        "question": "用户对内容审核结果有异议时，客服如何处理并补偿？",
        "terms": ["人工复审", "恢复", "50积分"],
    },
    {
        "id": "q26",
        "domain": "customer_service",
        "expected_doc": "TKT-0032",
        "question": "生成任务卡在processing超过1小时的原因和补偿是什么？",
        "terms": ["terminating", "100积分"],
    },
    {
        "id": "q27",
        "domain": "product",
        "expected_doc": "TBL-0063",
        "question": "企业标准套餐的年费、月积分共享池和API调用/月是多少？",
        "terms": ["¥59,999", "150,000", "30,000"],
    },
    {
        "id": "q28",
        "domain": "product",
        "expected_doc": "SUP-0074",
        "question": "生成失败请重试通常是什么原因，解决方案是什么？",
        "terms": ["GPU节点瞬时故障", "自动切换节点"],
    },
    {
        "id": "q29",
        "domain": "product",
        "expected_doc": "SUP-0082",
        "question": "P1严重故障的响应时间、解决时限和例子是什么？",
        "terms": ["15分钟", "2小时", "GPU节点故障"],
    },
    {
        "id": "q30",
        "domain": "product",
        "expected_doc": "PPT-0060",
        "question": "产品Roadmap 2025-2026 这份PPT主要记录了什么？",
        "terms": ["Roadmap", "2025", "2026"],
    },
]


def main() -> None:
    started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = OUTPUT_ROOT / started_at
    output_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    reset_current_rag_data(settings)
    ensure_vault(settings.vault_path)

    manifest = load_manifest()
    files = discover_test_files(manifest)
    import_root = settings.upload_path / "rag_benchmark"
    copy_metrics = copy_files_for_import(files, import_root)
    ingest_metrics = ingest_files(settings, import_root)

    compile_metrics = compile_all(settings)
    source_map = load_source_map(settings)
    status_after_ingest = rag_status(settings)
    obsidian_metrics = collect_vault_metrics(settings.vault_path)
    qa_metrics, qa_rows = run_qa(settings, source_map)
    chain_metrics = collect_chain_metrics(settings, compile_metrics, obsidian_metrics, qa_metrics)

    report = {
        "run_id": started_at,
        "test_data_root": str(TEST_DATA_ROOT),
        "quality_report_path": str(QUALITY_REPORT_PATH),
        "manifest_path": str(MANIFEST_PATH),
        "source_quality_report": read_quality_report_summary(),
        "manifest_summary": summarize_manifest(manifest),
        "settings": {
            "database_backend": settings.database_backend,
            "rag_store_backend": settings.rag_store_backend,
            "postgres_host": settings.postgres_host,
            "postgres_port": settings.postgres_port,
            "postgres_database": settings.postgres_database,
            "deepseek_model": settings.deepseek_model,
            "deepseek_api_key_configured": bool(settings.deepseek_api_key),
        },
        "dataset": summarize_dataset(files),
        "copy_metrics": copy_metrics,
        "ingest_metrics": ingest_metrics,
        "compile_metrics": compile_metrics,
        "rag_status": status_after_ingest,
        "obsidian_metrics": obsidian_metrics,
        "qa_metrics": qa_metrics,
        "chain_metrics": chain_metrics,
        "qa_rows": qa_rows,
    }

    write_outputs(output_dir, report)
    print(f"REPORT_DIR={output_dir.resolve()}")
    print(json.dumps(report["qa_metrics"], ensure_ascii=False, indent=2))


def reset_current_rag_data(settings: Any) -> None:
    if settings.database_backend == "postgres" or settings.rag_store_backend == "postgres":
        init_app_db(settings)
        init_pg_rag(settings)
        with connect_postgres(settings) as conn:
            with conn.cursor() as cur:
                for table in ["rag_document_chunks", *MAIN_TABLES]:
                    cur.execute(f"DELETE FROM {table}")
            conn.commit()

    sqlite_path = Path(settings.database_path).resolve()
    workspace = Path.cwd().resolve()
    if sqlite_path.exists():
        if workspace not in sqlite_path.parents and sqlite_path != workspace:
            raise RuntimeError(f"Refusing to clear SQLite outside workspace: {sqlite_path}")
        with sqlite3.connect(sqlite_path) as conn:
            for table in MAIN_TABLES:
                try:
                    conn.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute("DELETE FROM sqlite_sequence")
            except sqlite3.OperationalError:
                pass
            conn.commit()

    for path in [settings.vault_path, settings.upload_path]:
        clear_directory(path)


def clear_directory(path: Path) -> None:
    workspace = Path.cwd().resolve()
    resolved = path.resolve()
    if workspace not in resolved.parents and resolved != workspace:
        raise RuntimeError(f"Refusing to clear outside workspace: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    for child in resolved.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def load_manifest() -> list[dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return []
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def read_quality_report_summary() -> dict[str, Any]:
    if not QUALITY_REPORT_PATH.exists():
        return {"exists": False}
    text = QUALITY_REPORT_PATH.read_text(encoding="utf-8", errors="ignore")
    return {
        "exists": True,
        "bytes": QUALITY_REPORT_PATH.stat().st_size,
        "first_heading": next((line.strip("# ").strip() for line in text.splitlines() if line.startswith("# ")), ""),
    }


def summarize_manifest(manifest: list[dict[str, Any]]) -> dict[str, Any]:
    categories: dict[str, int] = {}
    formats: dict[str, int] = {}
    for item in manifest:
        category = str(item.get("category") or "unknown")
        categories[category] = categories.get(category, 0) + 1
        for fmt in item.get("formats") or []:
            formats[str(fmt)] = formats.get(str(fmt), 0) + 1
    return {
        "exists": MANIFEST_PATH.exists(),
        "items": len(manifest),
        "categories": categories,
        "formats": formats,
    }


def discover_test_files(manifest: list[dict[str, Any]]) -> list[TestFile]:
    if not TEST_DATA_ROOT.exists():
        raise FileNotFoundError(f"Missing test data directory: {TEST_DATA_ROOT}")

    manifest_by_id = {str(item.get("id")): item for item in manifest if item.get("id")}
    files: list[TestFile] = []
    for fmt in BASE_FORMAT_DIRS:
        files.extend(discover_format_dir(TEST_DATA_ROOT / fmt, fmt, "base", manifest_by_id))
    for fmt in BATCH_FORMAT_DIRS:
        files.extend(discover_format_dir(TEST_DATA_ROOT / "batch" / fmt, fmt, "batch", manifest_by_id))
    if len(files) != 334:
        raise RuntimeError(f"Expected 334 test data files, found {len(files)}")
    return files


def discover_format_dir(
    root: Path,
    fmt: str,
    dataset_part: str,
    manifest_by_id: dict[str, dict[str, Any]],
) -> list[TestFile]:
    if not root.exists():
        return []
    discovered: list[TestFile] = []
    for path in sorted(root.glob(f"*.{fmt}")):
        doc_code = extract_doc_code(path.stem)
        manifest_item = manifest_by_id.get(doc_code)
        domain, category = classify_document(doc_code, path.stem, manifest_item)
        discovered.append(
            TestFile(
                source_path=path,
                domain=domain,
                category=category,
                fmt=fmt,
                doc_code=doc_code,
                doc_name=path.stem,
                dataset_part=dataset_part,
                manifest_id=str(manifest_item.get("id")) if manifest_item else None,
            )
        )
    return discovered


def extract_doc_code(stem: str) -> str:
    if len(stem) >= 2 and stem[:2].isdigit():
        return stem[:2]
    if "_" in stem:
        first = stem.split("_", 1)[0]
        if "-" in first:
            return first
    return stem


def classify_document(
    doc_code: str,
    stem: str,
    manifest_item: dict[str, Any] | None,
) -> tuple[str, str]:
    if doc_code in {"01", "02", "03", "04", "05"}:
        return "product", "product_customer_support"
    if doc_code in {"06", "07", "08", "09"}:
        return "customer_service", "admin_policy_qa"
    if doc_code.startswith("TKT-"):
        return "customer_service", "customer_ticket"
    if manifest_item and manifest_item.get("category"):
        return "product", str(manifest_item["category"])
    if doc_code.startswith("PRD-"):
        return "product", "product_requirement"
    if doc_code.startswith("API-"):
        return "product", "technical_api"
    if doc_code.startswith("KB-"):
        return "product", "knowledge_base"
    if doc_code.startswith("PPT-"):
        return "product", "presentation"
    if doc_code.startswith("TBL-"):
        return "product", "table_report"
    if doc_code.startswith("SUP-"):
        return "product", "supplement"
    return "product", stem.split("_", 1)[0].lower() or "uncategorized"


def copy_files_for_import(files: list[TestFile], import_root: Path) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for domain in ("product", "customer_service"):
        domain_group = [item for item in files if item.domain == domain]
        if not domain_group:
            continue
        for fmt in FORMATS:
            group = [item for item in domain_group if item.fmt == fmt]
            if not group:
                continue
            target_dir = import_root / domain / fmt
            target_dir.mkdir(parents=True, exist_ok=True)
            start = time.perf_counter()
            total_bytes = 0
            for item in group:
                target = target_dir / unique_import_name(item)
                shutil.copy2(item.source_path, target)
                total_bytes += target.stat().st_size
            elapsed = time.perf_counter() - start
            metrics.append(
                {
                    "domain": domain,
                    "format": fmt,
                    "files": len(group),
                    "bytes": total_bytes,
                    "upload_copy_ms": round(elapsed * 1000, 3),
                    "files_per_sec": round(len(group) / elapsed, 3) if elapsed else len(group),
                    "mb_per_sec": round((total_bytes / 1024 / 1024) / elapsed, 3) if elapsed else 0,
                    "target_dir": str(target_dir),
                }
            )
    return metrics


def unique_import_name(item: TestFile) -> str:
    if item.dataset_part == "base":
        return item.source_path.name
    return item.source_path.name


def ingest_files(settings: Any, import_root: Path) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for domain in ("product", "customer_service"):
        for fmt in FORMATS:
            root = import_root / domain / fmt
            if not root.exists():
                continue
            start = time.perf_counter()
            response = scan_sources(
                settings,
                ScanRequest(
                    root_path=str(root),
                    domain=domain,
                    owner="rag-benchmark",
                    acl_tags=["internal", domain, fmt],
                    metadata_defaults={
                        "source_system": "rag_test_data",
                        "benchmark_format": fmt,
                        "benchmark_dataset": "quality_report_v1",
                    },
                    force_reindex=False,
                ),
            )
            elapsed = time.perf_counter() - start
            reports = load_ingest_reports(settings, response.job_id)
            char_count = sum(int(row["char_count"]) for row in reports)
            chunk_count = sum(int(row["chunk_count"]) for row in reports)
            bytes_total = sum(path.stat().st_size for path in root.glob(f"*.{fmt}"))
            parser_counts: dict[str, int] = {}
            warning_count = 0
            for row in reports:
                parser_counts[row["parser"]] = parser_counts.get(row["parser"], 0) + 1
                warning_count += len(row.get("warnings") or [])
            metrics.append(
                {
                    "domain": domain,
                    "format": fmt,
                    "job_id": response.job_id,
                    "files": response.new_files + response.changed_files,
                    "new_files": response.new_files,
                    "changed_files": response.changed_files,
                    "skipped_files": response.skipped_files,
                    "unsupported_files": response.unsupported_files,
                    "bytes": bytes_total,
                    "parse_ms": round(elapsed * 1000, 3),
                    "chars": char_count,
                    "chunks": chunk_count,
                    "warnings": warning_count,
                    "parser_counts": parser_counts,
                    "files_per_sec": round((response.new_files + response.changed_files) / elapsed, 3)
                    if elapsed
                    else response.new_files + response.changed_files,
                    "chars_per_sec": round(char_count / elapsed, 3) if elapsed else char_count,
                    "chunks_per_sec": round(chunk_count / elapsed, 3) if elapsed else chunk_count,
                }
            )
    return metrics


def load_ingest_reports(settings: Any, job_id: str) -> list[dict[str, Any]]:
    with connect_app(settings) as conn:
        rows = conn.execute("SELECT * FROM ingest_reports WHERE job_id = ?", (job_id,)).fetchall()
    return rows_to_dicts(rows)


def compile_all(settings: Any) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for domain in ("product", "customer_service"):
        start = time.perf_counter()
        response = compile_wiki(settings, CompileRequest(domain=domain))
        elapsed = time.perf_counter() - start
        metrics.append(
            {
                "domain": domain,
                "job_id": response.job_id,
                "compile_ms": round(elapsed * 1000, 3),
                "created_pages": response.created_pages,
                "updated_pages": response.updated_pages,
                "review_items": response.review_items,
                "pages_touched": response.created_pages + response.updated_pages,
                "pages_per_sec": round((response.created_pages + response.updated_pages) / elapsed, 3)
                if elapsed
                else response.created_pages + response.updated_pages,
            }
        )
    return metrics


def load_source_map(settings: Any) -> dict[str, dict[str, Any]]:
    with connect_app(settings) as conn:
        rows = conn.execute("SELECT * FROM sources").fetchall()
    return {row["id"]: row for row in rows_to_dicts(rows)}


def collect_vault_metrics(vault_path: Path) -> dict[str, Any]:
    sections = ["raw", "normalized", "jsonl", "wiki", "indexes", "reviews", "logs"]
    by_section: dict[str, dict[str, Any]] = {}
    for section in sections:
        root = vault_path / section
        files = [path for path in root.rglob("*") if path.is_file()] if root.exists() else []
        by_section[section] = {
            "files": len(files),
            "bytes": sum(path.stat().st_size for path in files),
        }
    return {
        "total_files": sum(value["files"] for value in by_section.values()),
        "total_bytes": sum(value["bytes"] for value in by_section.values()),
        "sections": by_section,
    }


def run_qa(settings: Any, source_map: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    for item in QUESTIONS:
        start = time.perf_counter()
        result = ask(settings, AskRequest(question=item["question"], domain=item["domain"]))
        elapsed_ms = (time.perf_counter() - start) * 1000
        latencies.append(elapsed_ms)
        citation_ids = [citation.source_id for citation in result.citations]
        cited_docs = [doc_code_for_source(source_map.get(source_id)) for source_id in citation_ids]
        top_doc = cited_docs[0] if cited_docs else ""
        combined_text = normalize_for_match(
            result.answer + "\n" + "\n".join(citation.snippet for citation in result.citations)
        )
        matched_terms = [term for term in item["terms"] if term_matches(term, combined_text)]
        source_hit_top = top_doc == item["expected_doc"]
        source_hit_any = item["expected_doc"] in cited_docs
        answer_points_ok = len(matched_terms) == len(item["terms"])
        correct = bool(source_hit_any and answer_points_ok)
        rows.append(
            {
                "id": item["id"],
                "domain": item["domain"],
                "question": item["question"],
                "expected_doc": item["expected_doc"],
                "top_doc": top_doc,
                "cited_docs": ",".join(cited_docs),
                "latency_ms": round(elapsed_ms, 3),
                "confidence": result.confidence,
                "citation_count": len(result.citations),
                "source_hit_top": source_hit_top,
                "source_hit_any": source_hit_any,
                "answer_points_ok": answer_points_ok,
                "matched_terms": ",".join(matched_terms),
                "correct": correct,
            }
        )

    total = len(rows)
    correct_count = sum(1 for row in rows if row["correct"])
    source_top_count = sum(1 for row in rows if row["source_hit_top"])
    source_any_count = sum(1 for row in rows if row["source_hit_any"])
    point_count = sum(1 for row in rows if row["answer_points_ok"])
    return (
        {
            "questions": total,
            "correct": correct_count,
            "accuracy": round(correct_count / total, 4) if total else 0,
            "source_top_correct": source_top_count,
            "source_top_accuracy": round(source_top_count / total, 4) if total else 0,
            "source_any_correct": source_any_count,
            "source_any_accuracy": round(source_any_count / total, 4) if total else 0,
            "answer_point_correct": point_count,
            "answer_point_accuracy": round(point_count / total, 4) if total else 0,
            "latency_avg_ms": round(statistics.mean(latencies), 3) if latencies else 0,
            "latency_p50_ms": round(statistics.median(latencies), 3) if latencies else 0,
            "latency_p95_ms": round(percentile(latencies, 0.95), 3) if latencies else 0,
            "latency_min_ms": round(min(latencies), 3) if latencies else 0,
            "latency_max_ms": round(max(latencies), 3) if latencies else 0,
            "deepseek_enabled": bool(settings.deepseek_api_key and settings.deepseek_model),
        },
        rows,
    )


def doc_code_for_source(source: dict[str, Any] | None) -> str:
    if not source:
        return ""
    original = Path(source.get("original_path") or "").name
    stem = Path(original).stem
    code = extract_doc_code(stem)
    if code:
        return code
    if len(original) >= 2 and original[:2].isdigit():
        return original[:2]
    title = source.get("title") or ""
    return title[:2] if len(title) >= 2 and title[:2].isdigit() else ""


def normalize_for_match(text: str) -> str:
    return "".join(text.lower().split())


def term_matches(term: str, normalized_text: str) -> bool:
    alternatives = [part.strip() for part in term.split("|") if part.strip()]
    return any(normalize_for_match(alt) in normalized_text for alt in alternatives)


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def collect_chain_metrics(
    settings: Any,
    compile_metrics: list[dict[str, Any]],
    obsidian_metrics: dict[str, Any],
    qa_metrics: dict[str, Any],
) -> list[dict[str, Any]]:
    gbrain = get_gbrain_status(settings)
    return [
        {
            "chain": "llmwiki",
            "status": "ok",
            "metric": "compile_ms_total",
            "value": round(sum(item["compile_ms"] for item in compile_metrics), 3),
            "note": "compile_wiki generated Markdown knowledge pages from active sources",
        },
        {
            "chain": "obsidian",
            "status": "ok",
            "metric": "vault_files",
            "value": obsidian_metrics["total_files"],
            "note": "vault/raw, normalized, jsonl, wiki, indexes and logs materialized on disk",
        },
        {
            "chain": "deepseek",
            "status": "ok" if qa_metrics["deepseek_enabled"] else "fallback",
            "metric": "qa_avg_latency_ms",
            "value": qa_metrics["latency_avg_ms"],
            "note": "DeepSeek API key is configured" if qa_metrics["deepseek_enabled"] else "DEEPSEEK_API_KEY is empty; local extractive answer path was measured",
        },
        {
            "chain": "gbrain",
            "status": "available" if gbrain.available else "disabled" if not gbrain.enabled else "not_configured",
            "metric": "adapter_available",
            "value": int(gbrain.available),
            "note": gbrain.note,
        },
        {
            "chain": "postgres_pgvector_rag",
            "status": "ok",
            "metric": "backend",
            "value": settings.rag_store_backend,
            "note": "RAG chunks were indexed into the configured backend",
        },
    ]


def summarize_dataset(files: list[TestFile]) -> dict[str, Any]:
    by_category: dict[str, int] = {}
    by_domain: dict[str, int] = {}
    by_format: dict[str, int] = {}
    by_dataset_part: dict[str, int] = {}
    for item in files:
        by_category[item.category] = by_category.get(item.category, 0) + 1
        by_domain[item.domain] = by_domain.get(item.domain, 0) + 1
        by_format[item.fmt] = by_format.get(item.fmt, 0) + 1
        by_dataset_part[item.dataset_part] = by_dataset_part.get(item.dataset_part, 0) + 1
    return {
        "files": len(files),
        "distinct_docs": len({item.doc_code for item in files}),
        "by_category": by_category,
        "by_domain": by_domain,
        "by_format": by_format,
        "by_dataset_part": by_dataset_part,
        "bytes": sum(item.source_path.stat().st_size for item in files),
    }


def write_outputs(output_dir: Path, report: dict[str, Any]) -> None:
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with (output_dir / "qa_results.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(report["qa_rows"][0].keys()))
        writer.writeheader()
        writer.writerows(report["qa_rows"])

    markdown = render_markdown_report(report)
    (output_dir / "report.md").write_text(markdown, encoding="utf-8")


def render_markdown_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# RAG Test Data Benchmark Report")
    lines.append("")
    lines.append(f"- Run ID: `{report['run_id']}`")
    lines.append(f"- Test data: `{report['test_data_root']}`")
    lines.append(f"- Quality report: `{report['quality_report_path']}`")
    lines.append(f"- Manifest: `{report['manifest_path']}`")
    lines.append(f"- Database backend: `{report['settings']['database_backend']}`")
    lines.append(f"- RAG backend: `{report['settings']['rag_store_backend']}`")
    lines.append(f"- DeepSeek enabled: `{report['qa_metrics']['deepseek_enabled']}`")
    lines.append("")
    lines.append("## Dataset")
    lines.append("")
    dataset = report["dataset"]
    lines.append("| Item | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Files | {dataset['files']} |")
    lines.append(f"| Distinct documents | {dataset['distinct_docs']} |")
    lines.append(f"| Bytes | {dataset['bytes']} |")
    lines.append(f"| Manifest items | {report['manifest_summary']['items']} |")
    lines.append("")
    lines.append("## Upload Copy")
    lines.append("")
    lines.append("| Domain | Format | Files | Bytes | Copy ms | Files/s | MB/s |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for row in report["copy_metrics"]:
        lines.append(
            f"| {row['domain']} | {row['format']} | {row['files']} | {row['bytes']} | "
            f"{row['upload_copy_ms']} | {row['files_per_sec']} | {row['mb_per_sec']} |"
        )
    lines.append("")
    lines.append("## Parse And Index")
    lines.append("")
    lines.append("| Domain | Format | Files | Parser | Parse ms | Chars | Chunks | Chars/s | Chunks/s | Warnings |")
    lines.append("|---|---|---:|---|---:|---:|---:|---:|---:|---:|")
    for row in report["ingest_metrics"]:
        parser = ", ".join(f"{name}:{count}" for name, count in row["parser_counts"].items())
        lines.append(
            f"| {row['domain']} | {row['format']} | {row['files']} | {parser} | {row['parse_ms']} | "
            f"{row['chars']} | {row['chunks']} | {row['chars_per_sec']} | {row['chunks_per_sec']} | {row['warnings']} |"
        )
    lines.append("")
    lines.append("## Chain Summary")
    lines.append("")
    lines.append("| Chain | Status | Metric | Value | Note |")
    lines.append("|---|---|---|---:|---|")
    for row in report["chain_metrics"]:
        lines.append(f"| {row['chain']} | {row['status']} | {row['metric']} | {row['value']} | {row['note']} |")
    lines.append("")
    lines.append("## QA Performance")
    lines.append("")
    qa = report["qa_metrics"]
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    for key in [
        "questions",
        "correct",
        "accuracy",
        "source_top_accuracy",
        "source_any_accuracy",
        "answer_point_accuracy",
        "latency_avg_ms",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_min_ms",
        "latency_max_ms",
    ]:
        lines.append(f"| {key} | {qa[key]} |")
    lines.append("")
    lines.append("## QA Details")
    lines.append("")
    lines.append("| ID | Domain | Expected | Top | Latency ms | Confidence | Source any | Points | Correct | Question |")
    lines.append("|---|---|---:|---:|---:|---|---|---|---|---|")
    for row in report["qa_rows"]:
        lines.append(
            f"| {row['id']} | {row['domain']} | {row['expected_doc']} | {row['top_doc']} | "
            f"{row['latency_ms']} | {row['confidence']} | {row['source_hit_any']} | "
            f"{row['answer_points_ok']} | {row['correct']} | {row['question']} |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
