from __future__ import annotations

import csv
import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.catalog import rag_status
from app.config import get_settings
from app.db import connect_app, rows_to_dicts
from app.gbrain import get_gbrain_status
from app.models import AskRequest
from app.search import ask


TEST_FILE = Path(r"C:\d\XM\RAG_test_data\UPGRADED_QA_TESTS.md")
OUTPUT_ROOT = Path("output") / "upgraded_qa_benchmark"


@dataclass(frozen=True)
class UpgradedQuestion:
    id: str
    tier: str
    label: str
    question: str
    domain: str | None
    expected_docs: list[str]
    required_terms: list[str]
    gbrain_mark: str


QUESTIONS: list[UpgradedQuestion] = [
    UpgradedQuestion("Q01", "T1", "跨文档歧义", "API 调用收到 429 错误后，系统建议怎么处理？", "product", ["API-0014"], ["429|rate_limited", "频率限制", "等待后重试|指数退避"], "green"),
    UpgradedQuestion("Q02", "T1", "跨文档歧义", "专业套餐的用户生成图片时积分不够了，有哪些补充积分的办法？", "product", ["KB-0045"], ["付费购买", "每日签到", "邀请好友"], "green"),
    UpgradedQuestion("Q03", "T1", "跨文档歧义", "企业标准套餐和专业版套餐，对于日均 500 次 API 调用的团队，哪个更合适？", "product", ["TBL-0063"], ["企业标准", "30,000|30000", "1,000次/天|1000次/天|够用"], "green"),
    UpgradedQuestion("Q04", "T1", "跨文档歧义", "GPU A100 节点故障时，用户能获得什么补偿？", "product", ["04"], ["退还积分|退积分", "100积分"], "green"),
    UpgradedQuestion("Q05", "T1", "跨文档歧义", "灵创AI的输出格式中，哪几种支持透明通道？", "product", ["API-0015"], ["PNG", "WebP", "TIFF"], "green"),
    UpgradedQuestion("Q06", "T1", "跨文档歧义", "年度套餐退款的具体规则是什么？", "product", ["05"], ["7天", "500", "剩余天数", "优惠差额"], "green"),
    UpgradedQuestion("Q07", "T1", "跨文档歧义", "灵创AI的图片生成在哪些场景下比 Midjourney 表现更好？", "product", ["KB-0050"], ["产品图", "中国风", "生成速度"], "green"),
    UpgradedQuestion("Q08", "T1", "跨文档歧义", "生成的内容被内容审核拦截后，用户如何申诉？", "customer_service", ["TKT-0038"], ["人工复审", "恢复", "50积分"], "green"),
    UpgradedQuestion("Q09", "T1", "跨文档歧义", "远程办公需要满足哪些条件？", "customer_service", ["09"], ["提前", "OA", "2天", "10分钟"], "green"),
    UpgradedQuestion("Q10", "T1", "跨文档歧义", "灵创AI相比竞品，有哪些能力是所有竞品都不具备的？", "product", ["KB-0050", "KB-0051", "KB-0052"], ["视频", "图片", "一体化|一站式", "模型微调"], "red"),
    UpgradedQuestion("Q11", "T2", "多跳推理", "用户调用文生图API返回401错误，但API Key确认没有过期。客服建议检查什么？背后可能的原因包括哪些？", "product", ["04", "API-0009", "API-0014"], ["Bearer", "空格", "X-API-Key|API Key", "missing_api_key|invalid_api_key|expired_api_key|401"], "yellow"),
    UpgradedQuestion("Q12", "T2", "多跳推理", "一个 P1 级别的 GPU 节点故障，从发生到最终关闭的完整流程是什么？用户会获得什么补偿？", None, ["SUP-0082", "04"], ["War Room|应急", "恢复", "复盘|故障报告", "100积分"], "yellow"),
    UpgradedQuestion("Q13", "T2", "多跳推理", "新员工入职后，需要了解的考勤规则、请假权限和远程办公政策分别是什么？", "customer_service", ["06", "09"], ["9:00-18:00|9-18", "补卡", "请假", "远程", "2天"], "yellow"),
    UpgradedQuestion("Q14", "T2", "多跳推理", "用户购买了专业版年付套餐，第5天想退款。能退多少钱？实际到账前有什么隐藏扣减？", "product", ["05", "TBL-0063", "04"], ["7天", "剩余天数", "优惠差额", "299|239"], "yellow"),
    UpgradedQuestion("Q15", "T2", "多跳推理", "从 API 错误码文档和实际工单案例中，总结出“文生图 API 调用失败”的完整排查清单。", "product", ["API-0014", "04", "TKT-0027", "TKT-0031"], ["API Key|Bearer", "积分|403", "429|频率", "1024", "500|503"], "yellow"),
    UpgradedQuestion("Q16", "T2", "多跳推理", "企业标准套餐的 API 调用量（30,000次/月）用完后，有哪些继续使用服务的方案？每种方案的额外成本是多少？", "product", ["TBL-0063", "KB-0045"], ["积分包", "0.12|0.29", "企业旗舰", "149,999|149999"], "yellow"),
    UpgradedQuestion("Q17", "T2", "多跳推理", "如果公司要做一次数据隐私合规审计，需要检查的平台政策条款有哪些？", None, ["05", "API-0018", "09", "API-0017"], ["不用于模型训练|不会用于模型训练", "90天", "30天", "个人网盘|加密"], "yellow"),
    UpgradedQuestion("Q18", "T2", "多跳推理", "一个日均 500 次文生图调用的电商团队，应该选择什么套餐组合？给出总成本和理由。", "product", ["TBL-0063", "KB-0045"], ["企业标准", "59,999|59999", "30,000次|30000次", "150,000积分|150000积分"], "yellow"),
    UpgradedQuestion("Q19", "T3", "实体关系", "灵创AI 平台中，哪些子系统或 API 直接依赖“内容安全审核引擎”？如果审核引擎从 V2.5 升级到 V3.0，哪些服务会受到直接影响？", None, ["PRD-0007", "API-0010", "API-0011", "API-0012", "PRD-0001", "05", "TKT-0038"], ["文生图API", "文生视频API", "Webhook", "画布", "客服|申诉"], "red"),
    UpgradedQuestion("Q20", "T3", "实体关系", "梳理公司所有制度文档中，与“时间窗口”或“期限”相关的全部条款，并判断是否存在相互矛盾。", None, ["05", "06", "07", "08", "09", "04"], ["7天", "次月5日", "24小时", "10分钟", "15个工作日", "无实质矛盾|不矛盾"], "red"),
    UpgradedQuestion("Q21", "T3", "实体关系", "从所有文档中分析“部门总监”这个角色拥有哪些权力和审批权限？汇总成一张权限清单。", "customer_service", ["07", "08", "06", "09"], ["报销", "采购", "请假", "远程", "招待"], "red"),
    UpgradedQuestion("Q22", "T3", "实体关系", "灵创AI 的技术栈中，哪些技术组件同时被多个 PRD 模块引用？画出共享组件的复用关系。", "product", ["PRD-0001", "PRD-0006", "PRD-0003", "PRD-0008"], ["Yjs", "WebSocket", "PostgreSQL", "SDXL"], "red"),
    UpgradedQuestion("Q23", "T3", "实体关系", "公司的采购制度中，“常规采购”和“大额采购”在流程、审批层级、供应商要求上有什么区别？采购一台 ¥80,000 的 GPU 服务器属于哪一类？", None, ["08", "API-0016"], ["80,000|80000", "大额采购", "50,000-200,000|50000-200000", "管理层", "CTO"], "red"),
    UpgradedQuestion("Q24", "T3", "实体关系", "从 PRD 文档中提取所有明确标注了优先级（P0/P1/P2）的功能需求，并分析各 PRD 模块之间的依赖关系。", "product", ["PRD-0001", "PRD-0006", "PRD-0007", "API-0010", "API-0011"], ["CAN-001", "CAN-002", "P0", "PRD-0006|协作引擎", "PRD-0007|审核引擎"], "red"),
    UpgradedQuestion("Q25", "T3", "实体关系", "如果公司的 GPU 资源从 A10 全部升级到 L40S，哪些生成任务的性能会提升？提升幅度有多大？", "product", ["API-0016", "API-0010", "API-0011", "TBL-0070"], ["文生图", "8s", "5s", "37.5", "成本"], "red"),
    UpgradedQuestion("Q26", "T3", "实体关系", "汇总公司所有面向“企业客户”的服务和能力，与面向“个人用户”的做一张对比表，指出企业独有的功能。", "product", ["TBL-0063", "PRD-0004", "PRD-0008", "PRD-0002", "PRD-0006", "KB-0048", "PPT-0058", "SUP-0076"], ["成员管理|RBAC", "共享积分池", "专属客户经理", "定制模型", "API集成"], "red"),
    UpgradedQuestion("Q27", "T4", "否定边界", "灵创AI 平台不支持哪些图片输出格式？", "product", ["API-0015"], ["BMP", "GIF", "SVG|AVIF|HEIC", "不支持"], "green"),
    UpgradedQuestion("Q28", "T4", "否定边界", "用户在购买年度套餐的第8天申请退款，还有机会退吗？", "product", ["05"], ["不可以|不能|不可", "超过", "7天"], "green"),
    UpgradedQuestion("Q29", "T4", "否定边界", "Midjourney 的视频生成功能和灵创AI相比有什么优势？", "product", ["KB-0050"], ["Midjourney", "不支持视频|没有视频|无视频"], "green"),
    UpgradedQuestion("Q30", "T4", "否定边界", "文档集中有没有提到灵创AI公司的注册地完整地址？", None, [], ["未包含|没有|未找到", "完整地址"], "green"),
    UpgradedQuestion("Q31", "T4", "否定边界", "专业版套餐用户当月积分用完、且不想升级套餐、也不想买积分包的情况下，有没有任何办法继续生成图片？", "product", ["KB-0045"], ["每日签到", "5积分", "邀请好友", "30积分", "精选|100积分"], "green"),
]


def main() -> None:
    started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = OUTPUT_ROOT / started_at
    output_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    status = rag_status(settings)
    if status["source_count"] < 300:
        raise RuntimeError("当前 RAG 数据不足，请先运行 scripts/rag_benchmark_test_data.py 导入升级数据集。")

    source_map = load_source_map(settings)
    gbrain_status = get_gbrain_status(settings)

    rows: list[dict[str, Any]] = []
    for index, question in enumerate(QUESTIONS, start=1):
        print(f"[{index:02d}/{len(QUESTIONS)}] {question.id} {question.tier} ...", flush=True)
        rows.append(run_question(settings, source_map, question))

    summary = summarize(rows)
    report = {
        "run_id": started_at,
        "test_file": str(TEST_FILE),
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
        "summary": summary,
        "rows": rows,
    }
    write_outputs(output_dir, report)
    print(f"REPORT_DIR={output_dir.resolve()}", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def run_question(settings: Any, source_map: dict[str, dict[str, Any]], question: UpgradedQuestion) -> dict[str, Any]:
    start = time.perf_counter()
    response = ask(
        settings,
        AskRequest(question=question.question, domain=question.domain, answer_mode="detail"),
    )
    latency_ms = (time.perf_counter() - start) * 1000

    cited_docs = [doc_code_for_source(source_map.get(citation.source_id)) for citation in response.citations]
    top_docs = [
        doc_code_for_source(source_map.get(hit.get("source_id")))
        for hit in response.retrieval_strategy.get("top_hits", [])
    ]
    gbrain_docs = [
        infer_doc_code_from_gbrain_hit(hit)
        for hit in response.retrieval_strategy.get("gbrain_top_hits", [])
    ]
    observed_docs = [doc for doc in [*cited_docs, *top_docs, *gbrain_docs] if doc]
    expected_hit_any = (
        True
        if not question.expected_docs
        else any(expected in observed_docs for expected in question.expected_docs)
    )
    expected_hit_top = (
        True
        if not question.expected_docs
        else bool(observed_docs and observed_docs[0] in question.expected_docs)
    )

    combined_text = normalize_for_match(
        response.answer
        + "\n"
        + "\n".join(citation.snippet for citation in response.citations)
        + "\n"
        + "\n".join(str(hit.get("title") or "") for hit in response.retrieval_strategy.get("gbrain_top_hits", []))
    )
    matched_terms = [term for term in question.required_terms if term_matches(term, combined_text)]
    term_rate = len(matched_terms) / len(question.required_terms) if question.required_terms else 1.0
    answer_points_ok = term_rate >= 0.75
    correct = bool(expected_hit_any and answer_points_ok)
    gbrain_hits = int(response.retrieval_strategy.get("gbrain_hits") or 0)

    return {
        "id": question.id,
        "tier": question.tier,
        "label": question.label,
        "gbrain_mark": question.gbrain_mark,
        "domain": question.domain or "all",
        "question": question.question,
        "expected_docs": ",".join(question.expected_docs),
        "observed_docs": ",".join(dict.fromkeys(observed_docs)),
        "citation_docs": ",".join(cited_docs),
        "top_docs": ",".join(top_docs),
        "gbrain_docs": ",".join(gbrain_docs),
        "latency_ms": round(latency_ms, 3),
        "confidence": response.confidence,
        "citation_count": len(response.citations),
        "gbrain_hits": gbrain_hits,
        "gbrain_used": gbrain_hits > 0,
        "source_hit_top": expected_hit_top,
        "source_hit_any": expected_hit_any,
        "matched_terms": ",".join(matched_terms),
        "term_rate": round(term_rate, 4),
        "answer_points_ok": answer_points_ok,
        "correct": correct,
    }


def load_source_map(settings: Any) -> dict[str, dict[str, Any]]:
    with connect_app(settings) as conn:
        rows = conn.execute("SELECT * FROM sources").fetchall()
    return {row["id"]: row for row in rows_to_dicts(rows)}


def doc_code_for_source(source: dict[str, Any] | None) -> str:
    if not source:
        return ""
    original = Path(source.get("original_path") or "").name
    return extract_doc_code(Path(original).stem)


def extract_doc_code(stem: str) -> str:
    if len(stem) >= 2 and stem[:2].isdigit():
        return stem[:2]
    if "_" in stem:
        first = stem.split("_", 1)[0]
        if "-" in first:
            return first
    return stem


def infer_doc_code_from_gbrain_hit(hit: dict[str, Any]) -> str:
    for key in ("source_id", "title", "slug", "chunk_id"):
        value = str(hit.get(key) or "")
        for token in value.replace("/", "_").replace("-", " ").replace(".", "_").split("_"):
            code = extract_doc_code(token)
            if code and (code[:2].isdigit() or "-" in code):
                return code
        if value:
            parts = value.split()
            for index, part in enumerate(parts[:-1]):
                if part.isalpha() and parts[index + 1].isdigit():
                    return f"{part}-{parts[index + 1]}"
    return ""


def normalize_for_match(text: str) -> str:
    return "".join(text.lower().split())


def term_matches(term: str, normalized_text: str) -> bool:
    alternatives = [part.strip() for part in term.split("|") if part.strip()]
    return any(normalize_for_match(alt) in normalized_text for alt in alternatives)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    latencies = [float(row["latency_ms"]) for row in rows]
    summary: dict[str, Any] = {
        "total": total,
        "correct": sum(1 for row in rows if row["correct"]),
        "accuracy": rate(sum(1 for row in rows if row["correct"]), total),
        "source_top_accuracy": rate(sum(1 for row in rows if row["source_hit_top"]), total),
        "source_any_accuracy": rate(sum(1 for row in rows if row["source_hit_any"]), total),
        "answer_point_accuracy": rate(sum(1 for row in rows if row["answer_points_ok"]), total),
        "gbrain_used_rate": rate(sum(1 for row in rows if row["gbrain_used"]), total),
        "latency_avg_ms": round(statistics.mean(latencies), 3) if latencies else 0,
        "latency_p50_ms": round(statistics.median(latencies), 3) if latencies else 0,
        "latency_p95_ms": round(percentile(latencies, 0.95), 3) if latencies else 0,
        "latency_max_ms": round(max(latencies), 3) if latencies else 0,
        "by_tier": {},
        "by_gbrain_mark": {},
    }
    for key in ("tier", "gbrain_mark"):
        target = summary["by_tier"] if key == "tier" else summary["by_gbrain_mark"]
        for value in sorted({str(row[key]) for row in rows}):
            group = [row for row in rows if row[key] == value]
            group_latencies = [float(row["latency_ms"]) for row in group]
            target[value] = {
                "total": len(group),
                "correct": sum(1 for row in group if row["correct"]),
                "accuracy": rate(sum(1 for row in group if row["correct"]), len(group)),
                "source_any_accuracy": rate(sum(1 for row in group if row["source_hit_any"]), len(group)),
                "answer_point_accuracy": rate(sum(1 for row in group if row["answer_points_ok"]), len(group)),
                "gbrain_used_rate": rate(sum(1 for row in group if row["gbrain_used"]), len(group)),
                "latency_avg_ms": round(statistics.mean(group_latencies), 3) if group_latencies else 0,
            }
    return summary


def rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def write_outputs(output_dir: Path, report: dict[str, Any]) -> None:
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "qa_results.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(report["rows"][0].keys()))
        writer.writeheader()
        writer.writerows(report["rows"])
    (output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Upgraded QA Benchmark Report",
        "",
        f"- Run ID: `{report['run_id']}`",
        f"- Test file: `{report['test_file']}`",
        f"- DeepSeek enabled: `{report['settings']['deepseek_enabled']}`",
        f"- GBrain: `{report['gbrain_status']['note']}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Total | {summary['total']} |",
        f"| Correct | {summary['correct']} |",
        f"| Accuracy | {summary['accuracy']} |",
        f"| Source top accuracy | {summary['source_top_accuracy']} |",
        f"| Source any accuracy | {summary['source_any_accuracy']} |",
        f"| Answer point accuracy | {summary['answer_point_accuracy']} |",
        f"| GBrain used rate | {summary['gbrain_used_rate']} |",
        f"| Avg latency ms | {summary['latency_avg_ms']} |",
        f"| P95 latency ms | {summary['latency_p95_ms']} |",
        "",
        "## By Tier",
        "",
        "| Tier | Total | Correct | Accuracy | Source Any | Answer Points | GBrain Used | Avg ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for tier, row in summary["by_tier"].items():
        lines.append(
            f"| {tier} | {row['total']} | {row['correct']} | {row['accuracy']} | "
            f"{row['source_any_accuracy']} | {row['answer_point_accuracy']} | "
            f"{row['gbrain_used_rate']} | {row['latency_avg_ms']} |"
        )
    lines.extend([
        "",
        "## Details",
        "",
        "| ID | Tier | Need | Correct | Source | Points | GBrain | Latency ms | Expected | Observed |",
        "|---|---|---|---|---|---|---|---:|---|---|",
    ])
    for row in report["rows"]:
        lines.append(
            f"| {row['id']} | {row['tier']} | {row['gbrain_mark']} | {row['correct']} | "
            f"{row['source_hit_any']} | {row['answer_points_ok']} | {row['gbrain_used']} | "
            f"{row['latency_ms']} | {row['expected_docs']} | {row['observed_docs']} |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
