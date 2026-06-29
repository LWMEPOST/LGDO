from __future__ import annotations

import uuid

from app.config import Settings
from app.db import audit, connect_app, init_app_db, json_dump
from app.models import AskRequest, EvalQuestionRequest, EvalRunResponse
from app.search import ask
from app.timeutil import now_iso


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
