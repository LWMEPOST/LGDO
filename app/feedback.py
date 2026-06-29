from __future__ import annotations

import uuid

from app.config import Settings
from app.db import audit, connect_app, init_app_db
from app.models import FeedbackRequest, FeedbackResponse
from app.timeutil import now_iso
from app.vault import append_log, ensure_vault, slugify


def submit_feedback(settings: Settings, request: FeedbackRequest) -> FeedbackResponse:
    init_app_db(settings)
    ensure_vault(settings.vault_path)
    timestamp = now_iso()
    feedback_id = f"fb_{uuid.uuid4().hex[:12]}"
    gap_created = False

    with connect_app(settings) as conn:
        query = conn.execute("SELECT * FROM query_logs WHERE id = ?", (request.query_id,)).fetchone()
        if query is None:
            raise ValueError(f"query_id 不存在: {request.query_id}")

        if request.should_create_gap:
            gap_created = True
            gap_id = f"gap_{uuid.uuid4().hex[:12]}"
            gap_path = settings.vault_path / "reviews" / f"gap_{slugify(request.query_id)}.md"
            gap_path.write_text(
                f"""# 知识缺口: {request.query_id}

- query_id: `{request.query_id}`
- rating: `{request.rating}`
- created_at: `{timestamp}`

## 问题

{query["question"]}

## 当前回答

{query["answer"]}

## 用户反馈

{request.comment or "未填写"}
""",
                encoding="utf-8",
            )
            conn.execute(
                """
                INSERT INTO knowledge_gaps(
                  id, query_id, feedback_id, question, answer, comment, status, priority,
                  owner, linked_page_path, gap_path, created_at, updated_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gap_id,
                    request.query_id,
                    feedback_id,
                    query["question"],
                    query["answer"],
                    request.comment,
                    "open",
                    "medium",
                    None,
                    None,
                    str(gap_path.relative_to(settings.vault_path)).replace("\\", "/"),
                    timestamp,
                    timestamp,
                    None,
                ),
            )

        conn.execute(
            """
            INSERT INTO feedback(id, query_id, rating, comment, gap_created, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                feedback_id,
                request.query_id,
                request.rating,
                request.comment,
                1 if gap_created else 0,
                timestamp,
            ),
        )
        audit(
            conn,
            "feedback_submitted",
            {
                "feedback_id": feedback_id,
                "query_id": request.query_id,
                "rating": request.rating,
                "gap_created": gap_created,
            },
            timestamp,
        )

    append_log(
        settings.vault_path,
        "qa_eval_log.md",
        f"- {timestamp} {feedback_id}: rating={request.rating} gap={gap_created} query={request.query_id}",
    )
    return FeedbackResponse(feedback_id=feedback_id, gap_created=gap_created)
