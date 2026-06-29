from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnswerModeConfig:
    key: str
    label: str
    retrieval_limit: int
    context_limit: int
    memory_limit: int
    keyword_weight: float
    vector_weight: float
    confidence_high: float
    prompt_instruction: str


ANSWER_MODES: dict[str, AnswerModeConfig] = {
    "detail": AnswerModeConfig(
        key="detail",
        label="详细回答",
        retrieval_limit=10,
        context_limit=6,
        memory_limit=3,
        keyword_weight=1.0,
        vector_weight=14.0,
        confidence_high=8.0,
        prompt_instruction=(
            "按「结论 / 操作建议 / 依据 / 缺口」组织回答。"
            "结论要直接，操作建议要可执行，依据必须来自资料片段。"
        ),
    ),
    "short": AnswerModeConfig(
        key="short",
        label="简短回答",
        retrieval_limit=4,
        context_limit=2,
        memory_limit=1,
        keyword_weight=1.15,
        vector_weight=10.0,
        confidence_high=9.0,
        prompt_instruction=(
            "只输出 1-3 句直接答案，保留最关键的限制条件或步骤。"
            "不要展开长段依据，资料不足时用一句话说明缺口。"
        ),
    ),
    "customer_reply_draft": AnswerModeConfig(
        key="customer_reply_draft",
        label="客服回复草稿",
        retrieval_limit=8,
        context_limit=5,
        memory_limit=3,
        keyword_weight=1.05,
        vector_weight=12.0,
        confidence_high=8.0,
        prompt_instruction=(
            "输出可直接发送给客户的回复草稿，语气礼貌克制。"
            "包含处理动作、需客户提供的信息和必要的边界说明，不暴露内部系统实现。"
        ),
    ),
}


ANSWER_MODE_ALIASES = {
    "brief": "short",
    "concise": "short",
    "customer": "customer_reply_draft",
    "draft": "customer_reply_draft",
}


def get_answer_mode_config(answer_mode: str | None) -> AnswerModeConfig:
    key = (answer_mode or "detail").strip()
    key = ANSWER_MODE_ALIASES.get(key, key)
    return ANSWER_MODES.get(key, ANSWER_MODES["detail"])
