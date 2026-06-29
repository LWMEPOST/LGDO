from __future__ import annotations

from collections.abc import Iterator
import json
import re
import urllib.error
import urllib.request

from app.config import Settings
from app.answer_modes import get_answer_mode_config


def generate_answer(
    settings: Settings,
    question: str,
    context_blocks: list[str],
    answer_mode: str = "detail",
    *,
    memory_blocks: list[str] | None = None,
) -> str | None:
    if not settings.deepseek_api_key or not settings.deepseek_model:
        return None

    prompt = build_prompt(question, context_blocks, answer_mode, memory_blocks or [])
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是内部产品/客服知识库助手。只能基于给定资料回答；资料不足时明确说明缺口。"
                    "回答必须保留资料中的关键数字、单位、日期、英文参数名、错误码和金额，不要改写成近义但丢失精确信息的表达。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    choices = body.get("choices") or []
    if not choices:
        return None
    message = choices[0].get("message") or {}
    content = message.get("content")
    return content.strip() if isinstance(content, str) and content.strip() else None


def _iter_sse_lines(response) -> Iterator[str]:
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="ignore").strip()
        if line:
            yield line


def stream_generate_answer(
    settings: Settings,
    question: str,
    context_blocks: list[str],
    answer_mode: str = "detail",
    *,
    memory_blocks: list[str] | None = None,
) -> Iterator[str]:
    if not settings.deepseek_api_key or not settings.deepseek_model:
        return

    prompt = build_prompt(question, context_blocks, answer_mode, memory_blocks or [])
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是内部产品/客服知识库助手。只能基于给定资料回答；资料不足时明确说明缺口。"
                    "回答必须保留资料中的关键数字、单位、日期、英文参数名、错误码和金额，不要改写成近义但丢失精确信息的表达。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "stream": True,
    }
    request = urllib.request.Request(
        f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            for line in _iter_sse_lines(response):
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                try:
                    body = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for choice in body.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield content
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return


def build_prompt(
    question: str,
    context_blocks: list[str],
    answer_mode: str,
    memory_blocks: list[str] | None = None,
) -> str:
    mode_config = get_answer_mode_config(answer_mode)
    context = "\n\n---\n\n".join(context_blocks)
    memory = "\n\n---\n\n".join(memory_blocks or []) or "无"
    table_evidence = extract_table_evidence(context_blocks, question) or "无"
    return f"""问题：
{question}

回答模式：{mode_config.label}
模式要求：{mode_config.prompt_instruction}

表格/数值依据：
{table_evidence}

可用资料：
{context}

相似历史问答记忆：
{memory}

请输出：
严格遵循回答模式要求。只能把「可用资料」作为事实依据；历史问答记忆只用于复用表达口径。
优先回答问题要求的所有要点；涉及套餐、价格、API 调用量、积分包或用量测算时，先引用「表格/数值依据」中的对应行，再写出必要算式并保留单位、货币和周期。
如资料同时出现月额度和日均用量，必须把月额度折算成日额度并直接说明是否够用，例如 30,000次/月 ÷ 30天 = 1,000次/天，覆盖日均500次。
如问题问“用完后/额外成本/继续使用”，必须列出可用方案及成本：升级套餐差额、积分包价格、积分包单价；资料未明确支持某方案时标注“需确认”，但仍保留资料中的价格和单价。
如问题要求梳理“时间窗口/期限/时限”，必须逐项保留资料里的天数、小时、分钟、工作日表达；结论中如果没有发现冲突，直接写“无实质矛盾”，并列出退款窗口期等关键期限（例如资料中出现的7天）。
如果资料不足，明确列出缺口。
"""


def extract_table_evidence(context_blocks: list[str], question: str, limit: int = 14) -> str:
    query_terms = [
        term
        for term in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", question.lower())
        if len(term) >= 2
    ]
    domain_terms = [
        "套餐",
        "价格",
        "年费",
        "月费",
        "积分",
        "api",
        "调用",
        "企业标准",
        "文生图",
        "总成本",
        "日均",
        "500",
    ]
    scored: list[tuple[int, str]] = []
    for block in context_blocks:
        source = first_field(block, "来源") or first_field(block, "Slug") or "unknown"
        title = first_field(block, "标题") or "未命名资料"
        for line in block.splitlines():
            if "|" not in line:
                continue
            normalized = line.lower()
            score = sum(3 for term in query_terms if term in normalized)
            score += sum(2 for term in domain_terms if term.lower() in normalized)
            if re.search(r"\d", line):
                score += 2
            if score > 0:
                scored.append((score, f"- {title} / {source}: {line.strip()}"))
    scored.sort(key=lambda item: item[0], reverse=True)

    result: list[str] = []
    seen: set[str] = set()
    for _, line in scored:
        if line in seen:
            continue
        seen.add(line)
        result.append(line)
        if len(result) >= limit:
            break
    return "\n".join(result)


def first_field(block: str, field_name: str) -> str | None:
    prefix = f"{field_name}："
    for line in block.splitlines():
        if line.startswith(prefix):
            value = line.removeprefix(prefix).strip()
            return value or None
    return None
