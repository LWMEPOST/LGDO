from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

DOMAIN_PATTERN = "^(product|customer_service|administration)$"


class ScanRequest(BaseModel):
    root_path: str
    domain: str = Field(pattern=DOMAIN_PATTERN)
    owner: str | None = None
    acl_tags: list[str] = Field(default_factory=list)
    metadata_defaults: dict = Field(default_factory=dict)
    force_reindex: bool = False


class ScanResponse(BaseModel):
    job_id: str
    new_files: int
    changed_files: int
    skipped_files: int
    unsupported_files: int


class UploadResponse(ScanResponse):
    saved_files: list[str]


class SourcePreviewResponse(BaseModel):
    id: str
    title: str
    source_type: str
    parser: str | None = None
    preview_path: str
    content: str
    truncated: bool = False
    char_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class CompileRequest(BaseModel):
    source_ids: list[str] | None = None
    domain: str = Field(pattern=DOMAIN_PATTERN)
    page_types: list[str] | None = None


class CompileResponse(BaseModel):
    job_id: str
    created_pages: int
    updated_pages: int
    review_items: int


class AskRequest(BaseModel):
    question: str
    domain: str | None = Field(default=None, pattern=DOMAIN_PATTERN)
    answer_mode: str = "detail"
    require_citations: bool = True
    user_id: str | None = None
    username: str | None = None
    role: str | None = None
    acl_tags: list[str] | None = None

    @field_validator("acl_tags", mode="before")
    @classmethod
    def normalize_acl_tags(cls, value: Any) -> Any:
        if value is None or isinstance(value, list):
            return value
        if isinstance(value, str):
            return [tag.strip() for tag in re.split(r"[,，;；\s]+", value) if tag.strip()]
        return value


class Citation(BaseModel):
    source_id: str
    wiki_page: str | None = None
    snippet: str


class MemoryHit(BaseModel):
    query_id: str
    question: str
    answer_snippet: str
    score: float
    confidence: str
    created_at: str


class AskResponse(BaseModel):
    query_id: str
    answer: str
    citations: list[Citation]
    confidence: str
    missing_info: list[str]
    memory_hits: list[MemoryHit] = Field(default_factory=list)
    retrieval_strategy: dict = Field(default_factory=dict)
    user_context: dict = Field(default_factory=dict)


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthSessionResponse(BaseModel):
    token: str
    user: dict


class AccountCreateRequest(BaseModel):
    user_id: str
    username: str | None = None
    password: str | None = None
    role: Literal["admin", "editor", "viewer"] = "viewer"
    acl_tags: list[str] = Field(default_factory=list)
    status: Literal["active", "disabled"] = "active"


class AccountUpdateRequest(BaseModel):
    username: str | None = None
    password: str | None = None
    role: Literal["admin", "editor", "viewer"] | None = None
    acl_tags: list[str] | None = None
    status: Literal["active", "disabled"] | None = None


class AccountResponse(BaseModel):
    user_id: str
    username: str | None = None
    role: str
    acl_tags: list[str] = Field(default_factory=list)
    status: str
    auth_provider: str = "local"
    password_configured: bool = False
    created_at: str
    updated_at: str
    last_login_at: str | None = None


class FeedbackRequest(BaseModel):
    query_id: str
    rating: str = Field(pattern="^(good|bad|partial)$")
    comment: str | None = None
    should_create_gap: bool = False


class FeedbackResponse(BaseModel):
    feedback_id: str
    gap_created: bool


class EvalQuestionRequest(BaseModel):
    question: str
    domain: str = Field(pattern=DOMAIN_PATTERN)
    expected_sources: list[str] = Field(default_factory=list)
    expected_answer_points: list[str] = Field(default_factory=list)
    risk_level: str = Field(default="low", pattern="^(low|medium|high)$")


class EvalRunResponse(BaseModel):
    total: int
    answered: int
    with_citations: int
    missing: int
    citation_rate: float


class EntityAliasRequest(BaseModel):
    canonical_name: str
    alias: str
    domain: str | None = Field(default=None, pattern=DOMAIN_PATTERN)
    entity_type: str = "entity"
    metadata: dict = Field(default_factory=dict)


class UpgradedEvalRunRequest(BaseModel):
    domain: str | None = Field(default=None, pattern=DOMAIN_PATTERN)
    gbrain_mode: Literal["on", "off", "both"] = "both"
    pass_rate_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    citation_rate_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    p95_ms_threshold: float | None = Field(default=None, gt=0)


class ReviewUpdateRequest(BaseModel):
    status: str = Field(pattern="^(pending|approved|rejected|resolved)$")
    owner: str | None = None
    note: str | None = None


class WikiStatusUpdateRequest(BaseModel):
    review_status: str = Field(pattern="^(draft|reviewed|stale|rejected)$")
    owner: str | None = None
    note: str | None = None


class WikiPageContentResponse(BaseModel):
    path: str
    content: str
    metadata: dict


class WikiPageSaveRequest(BaseModel):
    content: str
    review_status: str = Field(default="draft", pattern="^(draft|reviewed|stale|rejected)$")
    owner: str | None = None
    note: str | None = None


class GapUpdateRequest(BaseModel):
    status: str = Field(pattern="^(open|in_progress|resolved|rejected)$")
    priority: str | None = Field(default=None, pattern="^(low|medium|high)$")
    owner: str | None = None
    linked_page_path: str | None = None
    note: str | None = None
