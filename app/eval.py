from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from app.config import Settings
from app.db import audit, connect_app, init_app_db, json_dump
from app.gbrain import gbrain_query_candidates
from app.models import AskRequest, EvalQuestionRequest, EvalRunResponse
from app.search import ask
from app.timeutil import now_iso


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


UPGRADED_QUESTIONS: list[UpgradedEvalQuestion] = [
    UpgradedEvalQuestion("Q01", "T1", "跨文档歧义", "API 调用收到 429 错误后，系统建议怎么处理？", "product", ["API-0014"], ["429|rate_limited", "频率限制", "等待后重试|指数退避"], "green"),
    UpgradedEvalQuestion("Q02", "T1", "跨文档歧义", "专业套餐的用户生成图片时积分不够了，有哪些补充积分的办法？", "product", ["KB-0045"], ["付费购买", "每日签到", "邀请好友"], "green"),
    UpgradedEvalQuestion("Q03", "T1", "跨文档歧义", "企业标准套餐和专业版套餐，对于日均 500 次 API 调用的团队，哪个更合适？", "product", ["TBL-0063"], ["企业标准", "30,000|30000", "1,000次/天|1000次/天|够用"], "green"),
    UpgradedEvalQuestion("Q04", "T1", "跨文档歧义", "GPU A100 节点故障时，用户能获得什么补偿？", "product", ["04"], ["退还积分|退积分", "100积分"], "green"),
    UpgradedEvalQuestion("Q05", "T1", "跨文档歧义", "灵创AI的输出格式中，哪几种支持透明通道？", "product", ["API-0015"], ["PNG", "WebP", "TIFF"], "green"),
    UpgradedEvalQuestion("Q06", "T1", "跨文档歧义", "年度套餐退款的具体规则是什么？", "product", ["05"], ["7天", "500", "剩余天数", "优惠差额"], "green"),
    UpgradedEvalQuestion("Q07", "T1", "跨文档歧义", "灵创AI的图片生成在哪些场景下比 Midjourney 表现更好？", "product", ["KB-0050"], ["产品图", "中国风", "生成速度"], "green"),
    UpgradedEvalQuestion("Q08", "T1", "跨文档歧义", "生成的内容被内容审核拦截后，用户如何申诉？", "customer_service", ["TKT-0038"], ["人工复审", "恢复", "50积分"], "green"),
    UpgradedEvalQuestion("Q09", "T1", "跨文档歧义", "远程办公需要满足哪些条件？", "customer_service", ["09"], ["提前", "OA", "2天", "10分钟"], "green"),
    UpgradedEvalQuestion("Q10", "T1", "跨文档歧义", "灵创AI相比竞品，有哪些能力是所有竞品都不具备的？", "product", ["KB-0050", "KB-0051", "KB-0052"], ["视频", "图片", "一体化|一站式", "模型微调"], "red"),
    UpgradedEvalQuestion("Q11", "T2", "多跳推理", "用户调用文生图API返回401错误，但API Key确认没有过期。客服建议检查什么？背后可能的原因包括哪些？", "product", ["04", "API-0009", "API-0014"], ["Bearer", "空格", "X-API-Key|API Key", "missing_api_key|invalid_api_key|expired_api_key|401"], "yellow"),
    UpgradedEvalQuestion("Q12", "T2", "多跳推理", "一个 P1 级别的 GPU 节点故障，从发生到最终关闭的完整流程是什么？用户会获得什么补偿？", None, ["SUP-0082", "04"], ["War Room|应急", "恢复", "复盘|故障报告", "100积分"], "yellow"),
    UpgradedEvalQuestion("Q13", "T2", "多跳推理", "新员工入职后，需要了解的考勤规则、请假权限和远程办公政策分别是什么？", "customer_service", ["06", "09"], ["9:00-18:00|9-18", "补卡", "请假", "远程", "2天"], "yellow"),
    UpgradedEvalQuestion("Q14", "T2", "多跳推理", "用户购买了专业版年付套餐，第5天想退款。能退多少钱？实际到账前有什么隐藏扣减？", "product", ["05", "TBL-0063", "04"], ["7天", "剩余天数", "优惠差额", "299|239"], "yellow"),
    UpgradedEvalQuestion("Q15", "T2", "多跳推理", "从 API 错误码文档和实际工单案例中，总结出“文生图 API 调用失败”的完整排查清单。", "product", ["API-0014", "04", "TKT-0027", "TKT-0031"], ["API Key|Bearer", "积分|403", "429|频率", "1024", "500|503"], "yellow"),
    UpgradedEvalQuestion("Q16", "T2", "多跳推理", "企业标准套餐的 API 调用量（30,000次/月）用完后，有哪些继续使用服务的方案？每种方案的额外成本是多少？", "product", ["TBL-0063", "KB-0045"], ["积分包", "0.12|0.29", "企业旗舰", "149,999|149999"], "yellow"),
    UpgradedEvalQuestion("Q17", "T2", "多跳推理", "如果公司要做一次数据隐私合规审计，需要检查的平台政策条款有哪些？", None, ["05", "API-0018", "09", "API-0017"], ["不用于模型训练|不会用于模型训练", "90天", "30天", "个人网盘|加密"], "yellow"),
    UpgradedEvalQuestion("Q18", "T2", "多跳推理", "一个日均 500 次文生图调用的电商团队，应该选择什么套餐组合？给出总成本和理由。", "product", ["TBL-0063", "KB-0045"], ["企业标准", "59,999|59999", "30,000次|30000次", "150,000积分|150000积分"], "yellow"),
    UpgradedEvalQuestion("Q19", "T3", "实体关系", "灵创AI 平台中，哪些子系统或 API 直接依赖“内容安全审核引擎”？如果审核引擎从 V2.5 升级到 V3.0，哪些服务会受到直接影响？", None, ["PRD-0007", "API-0010", "API-0011", "API-0012", "PRD-0001", "05", "TKT-0038"], ["文生图API", "文生视频API", "Webhook", "画布", "客服|申诉"], "red"),
    UpgradedEvalQuestion("Q20", "T3", "实体关系", "梳理公司所有制度文档中，与“时间窗口”或“期限”相关的全部条款，并判断是否存在相互矛盾。", None, ["05", "06", "07", "08", "09", "04"], ["7天", "次月5日", "24小时", "10分钟", "15个工作日", "无实质矛盾|不矛盾"], "red"),
    UpgradedEvalQuestion("Q21", "T3", "实体关系", "从所有文档中分析“部门总监”这个角色拥有哪些权力和审批权限？汇总成一张权限清单。", "customer_service", ["07", "08", "06", "09"], ["报销", "采购", "请假", "远程", "招待"], "red"),
    UpgradedEvalQuestion("Q22", "T3", "实体关系", "灵创AI 的技术栈中，哪些技术组件同时被多个 PRD 模块引用？画出共享组件的复用关系。", "product", ["PRD-0001", "PRD-0006", "PRD-0003", "PRD-0008"], ["Yjs", "WebSocket", "PostgreSQL", "SDXL"], "red"),
    UpgradedEvalQuestion("Q23", "T3", "实体关系", "公司的采购制度中，“常规采购”和“大额采购”在流程、审批层级、供应商要求上有什么区别？采购一台 ¥80,000 的 GPU 服务器属于哪一类？", None, ["08", "API-0016"], ["80,000|80000", "大额采购", "50,000-200,000|50000-200000", "管理层", "CTO"], "red"),
    UpgradedEvalQuestion("Q24", "T3", "实体关系", "从 PRD 文档中提取所有明确标注了优先级（P0/P1/P2）的功能需求，并分析各 PRD 模块之间的依赖关系。", "product", ["PRD-0001", "PRD-0006", "PRD-0007", "API-0010", "API-0011"], ["CAN-001", "CAN-002", "P0", "PRD-0006|协作引擎", "PRD-0007|审核引擎"], "red"),
    UpgradedEvalQuestion("Q25", "T3", "实体关系", "如果公司的 GPU 资源从 A10 全部升级到 L40S，哪些生成任务的性能会提升？提升幅度有多大？", "product", ["API-0016", "API-0010", "API-0011", "TBL-0070"], ["文生图", "8s", "5s", "37.5", "成本"], "red"),
    UpgradedEvalQuestion("Q26", "T3", "实体关系", "汇总公司所有面向“企业客户”的服务和能力，与面向“个人用户”的做一张对比表，指出企业独有的功能。", "product", ["TBL-0063", "PRD-0004", "PRD-0008", "PRD-0002", "PRD-0006", "KB-0048", "PPT-0058", "SUP-0076"], ["成员管理|RBAC", "共享积分池", "专属客户经理", "定制模型", "API集成"], "red"),
    UpgradedEvalQuestion("Q27", "T4", "否定边界", "灵创AI 平台不支持哪些图片输出格式？", "product", ["API-0015"], ["BMP", "GIF", "SVG|AVIF|HEIC", "不支持"], "green"),
    UpgradedEvalQuestion("Q28", "T4", "否定边界", "用户在购买年度套餐的第8天申请退款，还有机会退吗？", "product", ["05"], ["不可以|不能|不可", "超过", "7天"], "green"),
    UpgradedEvalQuestion("Q29", "T4", "否定边界", "Midjourney 的视频生成功能和灵创AI相比有什么优势？", "product", ["KB-0050"], ["Midjourney", "不支持视频|没有视频|无视频"], "green"),
    UpgradedEvalQuestion("Q30", "T4", "否定边界", "文档集中有没有提到灵创AI公司的注册地完整地址？", None, [], ["未包含|没有|未找到", "完整地址"], "green"),
    UpgradedEvalQuestion("Q31", "T4", "否定边界", "专业版套餐用户当月积分用完、且不想升级套餐、也不想买积分包的情况下，有没有任何办法继续生成图片？", "product", ["KB-0045"], ["每日签到", "5积分", "邀请好友", "30积分", "精选|100积分"], "green"),
]


def add_eval_question(settings: Settings, request: EvalQuestionRequest) -> dict[str, str]:
    init_app_db(settings)
    timestamp = now_iso()
    eval_id = f"eval_{uuid.uuid4().hex[:12]}"
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO eval_questions(
              id, question, domain, expected_sources_json, expected_answer_points_json,
              risk_level, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                eval_id,
                request.question,
                request.domain,
                json_dump(request.expected_sources),
                json_dump(request.expected_answer_points),
                request.risk_level,
                "active",
                timestamp,
                timestamp,
            ),
        )
        audit(conn, "eval_question_added", {"eval_id": eval_id}, timestamp)
    return {"id": eval_id}


def run_eval(settings: Settings, domain: str | None = None) -> EvalRunResponse:
    init_app_db(settings)
    with connect_app(settings) as conn:
        params: list[object] = []
        query = "SELECT * FROM eval_questions WHERE status = 'active'"
        if domain:
            query += " AND domain = ?"
            params.append(domain)
        questions = conn.execute(query, params).fetchall()

    total = len(questions)
    answered = 0
    with_citations = 0
    missing = 0
    for question in questions:
        result = ask(
            settings,
            AskRequest(
                question=question["question"],
                domain=question["domain"],
                require_citations=True,
            ),
        )
        if result.confidence != "low":
            answered += 1
        if result.citations:
            with_citations += 1
        if result.missing_info:
            missing += 1

    citation_rate = with_citations / total if total else 0.0
    return EvalRunResponse(
        total=total,
        answered=answered,
        with_citations=with_citations,
        missing=missing,
        citation_rate=round(citation_rate, 4),
    )


def _term_matches(answer: str, patterns: list[str]) -> list[str]:
    matched: list[str] = []
    normalized = _normalize_eval_text(answer)
    for pattern in patterns:
        alternatives = [part.strip() for part in pattern.split("|") if part.strip()]
        if any(_normalize_eval_text(part) in normalized for part in alternatives):
            matched.append(pattern)
    return matched


def _source_matches(citations: list[dict], expected_sources: list[str]) -> list[str]:
    matched: list[str] = []
    for expected in expected_sources:
        expected_key = expected.lower()
        for citation in citations:
            source_id = str(citation.get("source_id") or "").lower()
            wiki_page = str(citation.get("wiki_page") or "").lower()
            if expected_key and (expected_key in source_id or expected_key in wiki_page):
                matched.append(expected)
                break
    return matched


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * pct)))
    return float(ordered[index])


def summarize_upgraded_rows(
    rows: list[dict[str, Any]],
    *,
    pass_rate_threshold: float | None = None,
    citation_rate_threshold: float | None = None,
    p95_ms_threshold: float | None = None,
) -> dict[str, Any]:
    total = len(rows)
    passed = sum(1 for row in rows if row.get("passed"))
    citation_count = sum(1 for row in rows if row.get("citations"))
    latencies = [float(row.get("elapsed_ms") or 0) for row in rows]
    pass_rate = round(passed / total, 4) if total else 0.0
    citation_rate = round(citation_count / total, 4) if total else 0.0
    p95_ms = round(_percentile(latencies, 0.95), 2)
    summary = {
        "total": total,
        "passed": passed,
        "pass_rate": pass_rate,
        "with_citations": citation_count,
        "citation_rate": citation_rate,
        "p50_ms": round(_percentile(latencies, 0.5), 2),
        "p95_ms": p95_ms,
    }
    summary["quality_gate"] = _quality_gate(
        pass_rate,
        citation_rate,
        p95_ms,
        pass_rate_threshold=pass_rate_threshold,
        citation_rate_threshold=citation_rate_threshold,
        p95_ms_threshold=p95_ms_threshold,
    )
    return summary


def run_upgraded_question(settings: Settings, question: UpgradedEvalQuestion) -> dict[str, Any]:
    started = perf_counter()
    result = ask(
        settings,
        AskRequest(question=question.question, domain=question.domain, require_citations=True),
    )
    elapsed_ms = (perf_counter() - started) * 1000
    citations = [citation.model_dump() if hasattr(citation, "model_dump") else dict(citation) for citation in result.citations]
    matched_sources = _source_matches(citations, question.expected_sources)
    combined_answer = result.answer + "\n" + "\n".join(str(citation.get("snippet") or "") for citation in citations)
    matched_terms = _term_matches(combined_answer, question.required_terms)
    min_terms = max(1, min(len(question.required_terms), 2)) if question.required_terms else 0
    passed = (
        (not question.expected_sources or len(matched_sources) > 0)
        and len(matched_terms) >= min_terms
        and result.confidence != "low"
    )
    return {
        "id": question.id,
        "tier": question.tier,
        "category": question.category,
        "risk": question.risk,
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
        "gbrain_diagnostics": _gbrain_diagnostics(settings, question.question),
    }


def run_upgraded_eval(
    settings: Settings,
    questions: list[UpgradedEvalQuestion] | None = None,
    domain: str | None = None,
    pass_rate_threshold: float | None = None,
    citation_rate_threshold: float | None = None,
    p95_ms_threshold: float | None = None,
) -> dict[str, Any]:
    selected = list(UPGRADED_QUESTIONS if questions is None else questions)
    if domain:
        selected = [question for question in selected if question.domain == domain]
    rows = [run_upgraded_question(settings, question) for question in selected]
    return {
        "summary": summarize_upgraded_rows(
            rows,
            pass_rate_threshold=pass_rate_threshold,
            citation_rate_threshold=citation_rate_threshold,
            p95_ms_threshold=p95_ms_threshold,
        ),
        "rows": rows,
    }


def _settings_with_gbrain(settings: Settings, enabled: bool) -> Settings:
    copied = settings.model_copy() if hasattr(settings, "model_copy") else copy.copy(settings)
    copied.gbrain_enabled = enabled
    return copied


def compare_upgraded_eval(
    settings: Settings,
    questions: list[UpgradedEvalQuestion] | None = None,
    domain: str | None = None,
    mode: str = "both",
    pass_rate_threshold: float | None = None,
    citation_rate_threshold: float | None = None,
    p95_ms_threshold: float | None = None,
) -> dict[str, Any]:
    if mode not in {"off", "on", "both"}:
        raise ValueError("mode must be one of: off, on, both")
    result: dict[str, Any] = {}
    if mode in {"off", "both"}:
        result["off"] = run_upgraded_eval(
            _settings_with_gbrain(settings, False),
            questions=questions,
            domain=domain,
            pass_rate_threshold=pass_rate_threshold,
            citation_rate_threshold=citation_rate_threshold,
            p95_ms_threshold=p95_ms_threshold,
        )
    if mode in {"on", "both"}:
        result["on"] = run_upgraded_eval(
            _settings_with_gbrain(settings, True),
            questions=questions,
            domain=domain,
            pass_rate_threshold=pass_rate_threshold,
            citation_rate_threshold=citation_rate_threshold,
            p95_ms_threshold=p95_ms_threshold,
        )
    if "off" in result and "on" in result:
        result["delta"] = {
            "passed": result["on"]["summary"]["passed"] - result["off"]["summary"]["passed"],
            "pass_rate": round(result["on"]["summary"]["pass_rate"] - result["off"]["summary"]["pass_rate"], 4),
            "p95_ms": round(result["on"]["summary"]["p95_ms"] - result["off"]["summary"]["p95_ms"], 2),
        }
    return result


def _quality_gate(
    pass_rate: float,
    citation_rate: float,
    p95_ms: float,
    *,
    pass_rate_threshold: float | None,
    citation_rate_threshold: float | None,
    p95_ms_threshold: float | None,
) -> dict[str, Any]:
    thresholds = {
        "pass_rate": pass_rate_threshold,
        "citation_rate": citation_rate_threshold,
        "p95_ms": p95_ms_threshold,
    }
    failures: list[str] = []
    if pass_rate_threshold is not None and pass_rate < pass_rate_threshold:
        failures.append("pass_rate")
    if citation_rate_threshold is not None and citation_rate < citation_rate_threshold:
        failures.append("citation_rate")
    if p95_ms_threshold is not None and p95_ms > p95_ms_threshold:
        failures.append("p95_ms")
    configured = any(value is not None for value in thresholds.values())
    return {
        "configured": configured,
        "passed": not failures,
        "failures": failures,
        "thresholds": thresholds,
    }


def _gbrain_diagnostics(settings: Settings, question: str) -> dict[str, Any]:
    candidates = gbrain_query_candidates(question, settings.gbrain_candidate_limit)
    return {
        "enabled": settings.gbrain_enabled,
        "candidate_limit": settings.gbrain_candidate_limit,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def _normalize_eval_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()
