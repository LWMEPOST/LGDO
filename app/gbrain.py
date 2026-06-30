from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from app.config import Settings
from app.vault import ensure_vault


_QUERY_CACHE_LOCK = threading.Lock()
_QUERY_CACHE: dict[tuple[Any, ...], tuple[float, list["GBrainHit"]]] = {}


@dataclass(frozen=True)
class GBrainStatus:
    enabled: bool
    available: bool
    endpoint_configured: bool
    note: str


@dataclass(frozen=True)
class GBrainHit:
    slug: str
    title: str
    snippet: str
    score: float
    source_id: str | None = None
    page_type: str | None = None
    chunk_id: int | None = None
    relational_path: list[str] | None = None
    relational_via_link_types: list[str] | None = None


@dataclass(frozen=True)
class GBrainImportResult:
    enabled: bool
    attempted: bool
    ok: bool
    imported: int = 0
    skipped: int = 0
    errors: int = 0
    chunks: int = 0
    note: str = ""


class GBrainError(RuntimeError):
    pass


class GBrainUnavailable(GBrainError):
    pass


def get_gbrain_status(settings: Settings) -> GBrainStatus:
    endpoint_configured = bool(settings.gbrain_endpoint)
    if not settings.gbrain_enabled:
        return GBrainStatus(
            enabled=False,
            available=False,
            endpoint_configured=endpoint_configured,
            note="GBrain adapter is disabled by configuration",
        )

    if endpoint_configured:
        return GBrainStatus(
            enabled=True,
            available=True,
            endpoint_configured=True,
            note="GBrain HTTP MCP endpoint is configured",
        )

    gbrain_home = _resolve_path(settings.gbrain_home)
    gbrain_repo = _resolve_path(settings.gbrain_repo_path)
    config_path = gbrain_home / ".gbrain" / "config.json"
    cli_path = gbrain_repo / "src" / "cli.ts"
    if not config_path.exists():
        return GBrainStatus(
            enabled=True,
            available=False,
            endpoint_configured=False,
            note=f"GBRAIN_ENABLED is true but config was not found at {config_path}",
        )
    if not cli_path.exists():
        return GBrainStatus(
            enabled=True,
            available=False,
            endpoint_configured=False,
            note=f"GBRAIN_ENABLED is true but CLI was not found at {cli_path}",
        )

    return GBrainStatus(
        enabled=True,
        available=True,
        endpoint_configured=False,
        note="GBrain local stdio MCP is configured",
    )


def import_vault_to_gbrain(settings: Settings, source_id: str | None = None) -> GBrainImportResult:
    if not settings.gbrain_enabled:
        return GBrainImportResult(
            enabled=False,
            attempted=False,
            ok=False,
            note="GBrain import skipped because adapter is disabled",
        )

    status = get_gbrain_status(settings)
    if not status.available:
        return GBrainImportResult(
            enabled=True,
            attempted=False,
            ok=False,
            note=status.note,
        )

    vault_path = _resolve_path(settings.vault_path)
    ensure_vault(vault_path)
    import_root = vault_path / "wiki"
    if not import_root.exists():
        return GBrainImportResult(
            enabled=True,
            attempted=False,
            ok=False,
            note=f"GBrain import skipped because {import_root} does not exist",
        )

    args = ["import", str(import_root), "--json", "--fresh", "--workers", "1"]
    if settings.gbrain_import_no_embed:
        args.append("--no-embed")
    resolved_source = source_id or settings.gbrain_source_id
    if resolved_source:
        args.extend(["--source-id", resolved_source])

    try:
        completed = _run_gbrain_cli(settings, args, timeout=settings.gbrain_import_timeout_seconds)
    except GBrainError as exc:
        return GBrainImportResult(
            enabled=True,
            attempted=True,
            ok=False,
            note=str(exc),
        )

    payload = _last_json_object(completed.stdout)
    if not payload:
        return GBrainImportResult(
            enabled=True,
            attempted=True,
            ok=False,
            note="GBrain import completed but did not return JSON summary",
        )

    errors = int(payload.get("errors") or 0)
    return GBrainImportResult(
        enabled=True,
        attempted=True,
        ok=errors == 0,
        imported=int(payload.get("imported") or 0),
        skipped=int(payload.get("skipped") or 0),
        errors=errors,
        chunks=int(payload.get("chunks") or 0),
        note=str(payload.get("status") or "success"),
    )


def query_gbrain(settings: Settings, question: str, limit: int | None = None) -> list[GBrainHit]:
    if not settings.gbrain_enabled:
        return []
    status = get_gbrain_status(settings)
    if not status.available:
        return []
    resolved_limit = limit or settings.gbrain_query_limit
    cache_key = _query_cache_key(settings, question, resolved_limit)
    cached_hits = _get_cached_query(cache_key, settings.gbrain_query_cache_ttl_seconds)
    if cached_hits is not None:
        return cached_hits

    params: dict[str, Any] = {
        "query": question,
        "limit": resolved_limit,
        "detail": settings.gbrain_query_detail,
        "expand": settings.gbrain_query_expand,
        "relational": True,
    }
    if settings.gbrain_source_id:
        params["source_id"] = settings.gbrain_source_id

    if settings.gbrain_endpoint:
        def http_call(tool_name: str, args: dict[str, Any]) -> Any:
            return call_gbrain_tool(settings, tool_name, args, timeout=settings.gbrain_query_timeout_seconds)

        hits = _query_gbrain_with_caller(http_call, params, question, resolved_limit, settings.gbrain_candidate_limit)
        if hits:
            _set_cached_query(cache_key, hits)
        return hits

    try:
        with LocalMcpClient(settings, settings.gbrain_query_timeout_seconds) as client:
            hits = _query_gbrain_with_caller(client.call_tool, params, question, resolved_limit, settings.gbrain_candidate_limit)
            if hits:
                _set_cached_query(cache_key, hits)
            return hits
    except GBrainError:
        return []


def _query_gbrain_with_caller(
    caller,
    params: dict[str, Any],
    question: str,
    limit: int,
    candidate_limit: int = 8,
) -> list[GBrainHit]:
    collected: list[GBrainHit] = []
    try:
        collected.extend(normalize_gbrain_hits(caller("query", params)))
    except GBrainError:
        pass
    for candidate in gbrain_query_candidates(question, candidate_limit):
        try:
            collected.extend(normalize_gbrain_hits(caller("search", {"query": candidate, "limit": limit})))
        except GBrainError:
            continue
    return rank_gbrain_hits(dedupe_gbrain_hits(collected), question, limit)


LOW_INFORMATION_CJK_PREFIXES = ("梳理", "公司", "所有", "全部", "哪些", "有哪些", "如果", "一个", "文档")
LOW_INFORMATION_PHRASES = {
    "哪些",
    "如果",
    "一个",
    "所有",
    "全部",
    "公司",
    "文档",
    "用户",
    "客户",
    "系统",
    "平台",
    "办法",
    "方式",
    "相关",
}
LOW_INFORMATION_SUBSTRINGS = ("哪些", "什么", "所有", "全部", "公司", "文档", "用户", "客户", "相关")
CANDIDATE_SIGNAL_TERMS = (
    "窗口",
    "期限",
    "条款",
    "矛盾",
    "优先级",
    "依赖",
    "审核",
    "引擎",
    "套餐",
    "组合",
    "积分",
    "权限",
    "审批",
    "错误",
    "失败",
    "限制",
    "api",
)
QUERY_CONNECTOR_RE = re.compile(r"(?:或者|以及|并且|并|和|与|或|及|、|之间的|相关(?:的)?|直接依赖|间接依赖|依赖于)")
CORE_TERM_SUFFIX_RE = re.compile(
    r"[\u4e00-\u9fff]{2,18}(?:"
    r"优先级|引擎|关系|组合|窗口|期限|条款|规则|政策|指南|白皮书|文档|需求|模块|系统|接口|套餐|"
    r"积分|权限|审批人|负责人|总监|调用|成本|清单"
    r")"
)
STATE_PHRASE_RE = re.compile(r"[\u4e00-\u9fff]{2,16}(?:不够|不足|失败|异常|过期|限制|错误|矛盾)")


def gbrain_query_candidates(question: str, candidate_limit: int = 8) -> list[str]:
    if candidate_limit <= 0:
        return []
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", question).strip()
    candidates: list[str] = []
    for code in re.findall(r"\b(?:API|KB|PRD|TBL|PPT|SUP|TKT)-\d{4}\b", question, flags=re.I):
        code = code.upper()
        if code not in candidates:
            candidates.append(code)
    for phrase in extract_gbrain_key_phrases(question):
        if phrase in question and _useful_gbrain_candidate(phrase) and phrase not in candidates:
            candidates.append(phrase)
    if (
        cleaned
        and cleaned != question
        and len(_normalize_match(cleaned)) <= 48
        and _useful_gbrain_candidate(cleaned)
        and cleaned not in candidates
    ):
        candidates.append(cleaned)
    if not candidates:
        for cjk_part in re.findall(r"[\u4e00-\u9fff]{4,}", cleaned):
            for size in (4, 5, 6):
                for index in range(0, max(0, len(cjk_part) - size + 1)):
                    token = cjk_part[index : index + size]
                    if not _useful_gbrain_candidate(token):
                        continue
                    if token not in candidates:
                        candidates.append(token)
                    if len(candidates) >= candidate_limit:
                        break
                if len(candidates) >= candidate_limit:
                    break
            if len(candidates) >= candidate_limit:
                break
    if not candidates:
        try:
            from app.rag import tokenize

            tokens = tokenize(question)
        except Exception:
            tokens = []
        for token in tokens:
            if len(candidates) >= candidate_limit:
                break
            if 3 <= len(token) <= 8 and _useful_gbrain_candidate(token) and token not in candidates:
                candidates.append(token)
        for token in tokens:
            if len(candidates) >= candidate_limit:
                break
            if len(token) == 2 and _useful_gbrain_candidate(token) and token not in candidates:
                candidates.append(token)
    return _rank_gbrain_candidates(candidates)[:candidate_limit]


def extract_gbrain_key_phrases(question: str) -> list[str]:
    phrases: list[str] = []
    for quoted in re.findall(r"[“\"']([^”\"']{2,24})[”\"']", question):
        phrases.extend(_phrase_variants(quoted))
    for phrase in re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{1,16}\b", question):
        phrases.extend(_phrase_variants(phrase))
    for phrase in CORE_TERM_SUFFIX_RE.findall(question):
        phrases.extend(_phrase_variants(phrase))
    for phrase in STATE_PHRASE_RE.findall(question):
        phrases.extend(_phrase_variants(phrase))
    for clause in re.split(r"[\s,，。；;：:?？!！()（）<>《》\"'`]+", question):
        phrases.extend(_phrase_variants(clause))
    return _dedupe_phrases([phrase for phrase in phrases if _useful_gbrain_phrase(phrase)])


def _phrase_variants(raw_phrase: str) -> list[str]:
    phrase = _clean_gbrain_phrase(raw_phrase)
    if not phrase:
        return []

    variants: list[str] = []
    _append_phrase_variant(variants, phrase)
    _append_phrase_variant(variants, _trim_query_glue(phrase))

    for marker in ("有哪些", "选择什么", "应该选择", "什么", "哪些", "标注了", "之间的", "直接依赖", "间接依赖", "依赖于", "依赖"):
        if marker in phrase:
            for part in phrase.split(marker):
                _append_phrase_variant(variants, _trim_query_glue(part))

    for part in QUERY_CONNECTOR_RE.split(phrase):
        _append_phrase_variant(variants, _trim_query_glue(part))

    for term in CORE_TERM_SUFFIX_RE.findall(phrase):
        _append_phrase_variant(variants, _trim_query_glue(term))

    for term in STATE_PHRASE_RE.findall(phrase):
        _append_phrase_variant(variants, _trim_query_glue(_tail_after_context_marker(term)))

    return variants


def _append_phrase_variant(variants: list[str], phrase: str) -> None:
    phrase = _clean_gbrain_phrase(phrase)
    if not phrase:
        return
    if _useful_gbrain_phrase(phrase) and phrase not in variants:
        variants.append(phrase)


def _clean_gbrain_phrase(phrase: str) -> str:
    cleaned = re.sub(r"\s+", " ", phrase or "").strip(" -_/()（）[]【】{}<>《》:：,，。；;?？!！、")
    return cleaned.strip()


def _trim_query_glue(phrase: str) -> str:
    cleaned = _clean_gbrain_phrase(phrase)
    if not cleaned:
        return ""

    changed = True
    while changed:
        before = cleaned
        cleaned = re.sub(r"^(?:请|帮我|从|把|将|在|对|若|如果|哪些|什么|有哪些|应该|选择|明确|标注了|提取|分析|判断|全部|所有|相关的?|直接|间接|并)+", "", cleaned)
        cleaned = re.sub(r"(?:是什么|有哪些|多少|是谁|吗|呢|了|的办法|的方法|的方式|相关的全部条款|全部条款|相关条款|条款|中|里|内|时)$", "", cleaned)
        cleaned = _clean_gbrain_phrase(cleaned)
        changed = cleaned != before
    return _tail_after_context_marker(cleaned)


def _tail_after_context_marker(phrase: str) -> str:
    cleaned = _clean_gbrain_phrase(phrase)
    for marker in ("时", "后"):
        if marker in cleaned and not cleaned.startswith("时间"):
            tail = cleaned.rsplit(marker, 1)[-1]
            if len(_normalize_match(tail)) >= 2:
                return _clean_gbrain_phrase(tail)
    return cleaned


def _useful_gbrain_phrase(phrase: str) -> bool:
    key = _normalize_match(phrase)
    if len(key) < 2:
        return False
    if key in LOW_INFORMATION_PHRASES:
        return False
    if re.fullmatch(r"\d+", key):
        return False
    if any(key.startswith(_normalize_match(prefix)) for prefix in LOW_INFORMATION_CJK_PREFIXES):
        return False
    if len(key) > 24:
        return False
    return True


def _useful_gbrain_candidate(phrase: str) -> bool:
    key = _normalize_match(phrase)
    if not _useful_gbrain_phrase(phrase):
        return False
    if len(key) > 18 and not re.search(r"\b[A-Z]+-\d{4}\b", phrase, flags=re.I):
        return False
    if any(part in key for part in LOW_INFORMATION_SUBSTRINGS) and len(key) > 12:
        return False
    if len(key) > 8 and any(part in key for part in ("怎么", "如何", "原因", "办法")):
        return False
    if any(part in key for part in LOW_INFORMATION_SUBSTRINGS) and not any(signal in key for signal in CANDIDATE_SIGNAL_TERMS):
        return False
    return True


def _rank_gbrain_candidates(candidates: list[str]) -> list[str]:
    indexed = list(enumerate(_dedupe_phrases(candidates)))

    def sort_key(item: tuple[int, str]) -> tuple[int, int, int, int, int]:
        index, candidate = item
        key = _normalize_match(candidate)
        if re.fullmatch(r"(?:api|kb|prd|tbl|ppt|sup|tkt)-?\d{4}", key, flags=re.I):
            return (0, 0, 0, len(key), index)
        signal_count = sum(1 for term in CANDIDATE_SIGNAL_TERMS if term in key)
        low_info = sum(1 for term in LOW_INFORMATION_SUBSTRINGS if term in key)
        if signal_count and len(key) <= 10:
            bucket = 1
        elif signal_count:
            bucket = 2
        elif re.fullmatch(r"[a-z0-9_-]{2,16}", candidate, flags=re.I):
            bucket = 3
        elif len(key) <= 8:
            bucket = 4
        else:
            bucket = 5
        return (bucket, low_info, -signal_count, abs(len(key) - 6), index)

    return [candidate for _, candidate in sorted(indexed, key=sort_key)]


def _domain_query_phrases(question: str) -> list[str]:
    phrases: list[str] = []
    for phrase in extract_gbrain_key_phrases(question):
        if phrase not in phrases:
            phrases.append(phrase)
    return _dedupe_phrases(phrases)


def normalize_gbrain_hits(result: Any) -> list[GBrainHit]:
    if isinstance(result, dict):
        for key in ("results", "hits", "items", "data"):
            value = result.get(key)
            if isinstance(value, list):
                result = value
                break
    if not isinstance(result, list):
        return []

    hits: list[GBrainHit] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or item.get("page_slug") or item.get("id") or "").strip()
        title = str(item.get("title") or slug or "GBrain").strip()
        snippet = str(
            item.get("chunk_text")
            or item.get("text")
            or item.get("compiled_truth")
            or item.get("snippet")
            or ""
        ).strip()
        inferred_source_id = _optional_str(item.get("source_id"))
        doc_code = infer_gbrain_doc_code(slug, title, snippet, inferred_source_id)
        if doc_code:
            inferred_source_id = doc_code
        if not slug and not snippet:
            continue
        hits.append(
            GBrainHit(
                slug=slug,
                title=title,
                snippet=clip_gbrain_text(snippet),
                score=_as_float(item.get("score")),
                source_id=inferred_source_id,
                page_type=_optional_str(item.get("type") or item.get("page_type")),
                chunk_id=_optional_int(item.get("chunk_id")),
                relational_path=_optional_str_list(item.get("relational_path")),
                relational_via_link_types=_optional_str_list(item.get("relational_via_link_types")),
            )
        )
    return hits


def dedupe_gbrain_hits(hits: list[GBrainHit]) -> list[GBrainHit]:
    best: dict[str, GBrainHit] = {}
    for hit in hits:
        key = hit.slug or hit.title or hit.snippet[:80]
        previous = best.get(key)
        if previous is None or hit.score > previous.score:
            best[key] = hit
    return list(best.values())


def rank_gbrain_hits(hits: list[GBrainHit], question: str, limit: int) -> list[GBrainHit]:
    if not hits:
        return []

    query = _normalize_match(question)
    ranked: list[tuple[float, GBrainHit]] = []
    for hit in hits:
        haystack = _normalize_match(f"{hit.slug}\n{hit.title}\n{hit.snippet}\n{hit.source_id or ''}")
        score = hit.score
        if hit.source_id and _normalize_match(hit.source_id) in query:
            score += 4.0
        for phrase in _domain_query_phrases(question):
            if _normalize_match(phrase) in haystack:
                score += 2.0
        for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", question.lower()):
            if len(token) >= 2 and _normalize_match(token) in haystack:
                score += 0.08
        if "api-rate-limit" in hit.slug and "429" not in query and "频率限制" not in query:
            score -= 1.0
        ranked.append((score, hit))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [hit for _, hit in ranked[:limit]]


def infer_gbrain_doc_code(*values: str | None) -> str | None:
    for value in values:
        text = value or ""
        match = re.search(r"(?<![A-Z0-9])(?:API|KB|PRD|TBL|PPT|SUP|TKT)[-_\s]\d{4}(?![A-Z0-9])", text, flags=re.I)
        if match:
            return re.sub(r"[-_\s]+", "-", match.group(0).upper())
    return None


def _normalize_match(value: str) -> str:
    return re.sub(r"[\s_\-:：/\\|,，。；;?？!！()（）<>《》\"'`]+", "", value.lower())


def _dedupe_phrases(phrases: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for phrase in phrases:
        key = _normalize_match(phrase)
        if key and key not in seen:
            seen.add(key)
            result.append(phrase)
    return result


def _query_cache_key(settings: Settings, question: str, limit: int) -> tuple[Any, ...]:
    return (
        settings.gbrain_endpoint or "local",
        str(settings.gbrain_home),
        str(settings.gbrain_repo_path),
        settings.gbrain_source_id or "",
        settings.gbrain_query_detail,
        settings.gbrain_query_expand,
        limit,
        question.strip(),
    )


def _get_cached_query(cache_key: tuple[Any, ...], ttl_seconds: int) -> list[GBrainHit] | None:
    if ttl_seconds <= 0:
        return None
    now = time.monotonic()
    with _QUERY_CACHE_LOCK:
        cached = _QUERY_CACHE.get(cache_key)
        if not cached:
            return None
        cached_at, hits = cached
        if now - cached_at > ttl_seconds:
            _QUERY_CACHE.pop(cache_key, None)
            return None
        return list(hits)


def _set_cached_query(cache_key: tuple[Any, ...], hits: list[GBrainHit]) -> None:
    with _QUERY_CACHE_LOCK:
        _QUERY_CACHE[cache_key] = (time.monotonic(), list(hits))
        if len(_QUERY_CACHE) > 256:
            oldest_key = min(_QUERY_CACHE, key=lambda key: _QUERY_CACHE[key][0])
            _QUERY_CACHE.pop(oldest_key, None)


def call_gbrain_tool(
    settings: Settings,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> Any:
    if settings.gbrain_endpoint:
        return _call_http_mcp(
            settings.gbrain_endpoint,
            settings.gbrain_api_key,
            tool_name,
            arguments or {},
            timeout or settings.gbrain_query_timeout_seconds,
        )

    with LocalMcpClient(settings, timeout or settings.gbrain_query_timeout_seconds) as client:
        return client.call_tool(tool_name, arguments or {})


class LocalMcpClient:
    def __init__(self, settings: Settings, timeout: int):
        self.settings = settings
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._stderr_tail: list[str] = []
        self._stdout_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

    def __enter__(self) -> LocalMcpClient:
        env = _gbrain_env(self.settings)
        cwd = _resolve_path(self.settings.gbrain_repo_path)
        self.process = subprocess.Popen(
            [*_gbrain_command_args(self.settings), "run", "src/cli.ts", "serve"],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._stdout_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            self._request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "lgdo", "version": "1"}})
            self._notify("notifications/initialized", {})
        except Exception:
            self._stop_process()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop_process()

    def _stop_process(self) -> None:
        if self.process is None:
            return
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        finally:
            self.process = None

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        response = self._request("tools/call", {"name": tool_name, "arguments": arguments})
        result = response.get("result")
        if not isinstance(result, dict):
            return result
        if result.get("isError"):
            text = _tool_text(result)
            raise GBrainError(f"GBrain tool {tool_name} failed: {text}")
        return _parse_tool_payload(result)

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.process is None or self.process.stdin is None:
            raise GBrainUnavailable("GBrain MCP process is not running")
        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except OSError as exc:
            raise GBrainUnavailable(f"Failed to write to GBrain MCP: {exc}") from exc
        return self._read_response(request_id)

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise GBrainUnavailable("GBrain MCP process is not running")
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _read_response(self, request_id: int) -> dict[str, Any]:
        if self.process is None:
            raise GBrainUnavailable("GBrain MCP process is not running")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise GBrainUnavailable(f"GBrain MCP exited early: {self._stderr_note()}")
            remaining = max(0.01, deadline - time.monotonic())
            try:
                message = self._stdout_queue.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise GBrainError(f"GBrain MCP {request_id} error: {message['error']}")
            return message
        raise GBrainUnavailable(f"GBrain MCP request timed out after {self.timeout}s: {self._stderr_note()}")

    def _drain_stdout(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self._stdout_queue.put(message)

    def _drain_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        for line in self.process.stderr:
            clean = line.strip()
            if clean:
                self._stderr_tail.append(clean)
                self._stderr_tail = self._stderr_tail[-6:]

    def _stderr_note(self) -> str:
        return " | ".join(self._stderr_tail[-3:]) or "no stderr"


def _call_http_mcp(
    endpoint: str,
    api_key: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: int,
) -> Any:
    init_response = _post_mcp(
        endpoint,
        api_key,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "lgdo", "version": "1"}}},
        timeout,
    )
    if "error" in init_response:
        raise GBrainError(f"GBrain HTTP MCP initialize failed: {init_response['error']}")
    tool_response = _post_mcp(
        endpoint,
        api_key,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}},
        timeout,
    )
    if "error" in tool_response:
        raise GBrainError(f"GBrain HTTP MCP tool failed: {tool_response['error']}")
    result = tool_response.get("result")
    if isinstance(result, dict) and result.get("isError"):
        raise GBrainError(f"GBrain tool {tool_name} failed: {_tool_text(result)}")
    return _parse_tool_payload(result)


def _post_mcp(endpoint: str, api_key: str | None, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Protocol-Version": "2025-11-25",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urlrequest.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GBrainUnavailable(f"GBrain HTTP MCP returned {exc.code}: {detail[:300]}") from exc
    except URLError as exc:
        raise GBrainUnavailable(f"GBrain HTTP MCP is unreachable: {exc.reason}") from exc

    if "text/event-stream" in content_type:
        parsed = _parse_sse_jsonrpc(raw)
    else:
        parsed = json.loads(raw)
    if isinstance(parsed, list):
        return parsed[-1] if parsed else {}
    if not isinstance(parsed, dict):
        raise GBrainError("GBrain HTTP MCP returned unexpected payload")
    return parsed


def _parse_sse_jsonrpc(raw: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif not line.strip() and data_lines:
            joined = "\n".join(data_lines)
            data_lines = []
            try:
                events.append(json.loads(joined))
            except json.JSONDecodeError:
                continue
    if data_lines:
        try:
            events.append(json.loads("\n".join(data_lines)))
        except json.JSONDecodeError:
            pass
    if not events:
        raise GBrainError("GBrain HTTP MCP returned empty SSE response")
    return events[-1]


def _run_gbrain_cli(settings: Settings, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            [*_gbrain_command_args(settings), "run", "src/cli.ts", *args],
            cwd=str(_resolve_path(settings.gbrain_repo_path)),
            env=_gbrain_env(settings),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GBrainUnavailable(f"GBrain CLI failed to start or timed out: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise GBrainError(f"GBrain CLI exited {completed.returncode}: {detail[:500]}")
    return completed


def _gbrain_command_args(settings: Settings) -> list[str]:
    configured = settings.gbrain_command.strip()
    if not configured:
        raise GBrainUnavailable("GBRAIN_COMMAND is empty")
    if os.name == "nt" and configured.lower() == "bun":
        npm_bun = shutil.which("bun.cmd") or shutil.which("bun")
        if npm_bun:
            direct = Path(npm_bun).parent / "node_modules" / "bun" / "bin" / "bun.exe"
            if direct.exists():
                return [str(direct)]
    if os.name == "nt" and Path(configured).suffix == "":
        for candidate in (f"{configured}.cmd", f"{configured}.exe", configured):
            resolved = shutil.which(candidate)
            if resolved:
                return [resolved]
    resolved = shutil.which(configured)
    return [resolved or configured]


def _gbrain_env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    gbrain_home = _resolve_path(settings.gbrain_home)
    env["GBRAIN_HOME"] = str(gbrain_home)
    env.update(_read_dotenv(gbrain_home / ".gbrain" / ".env"))
    return env


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _resolve_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _parse_tool_payload(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    text = _tool_text(result)
    if text == "":
        return result
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _tool_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts).strip()


def _last_json_object(text: str) -> dict[str, Any] | None:
    for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def clip_gbrain_text(text: str, limit: int = 1400) -> str:
    body = " ".join((text or "").split())
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "..."


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = [str(item) for item in value if item is not None]
    return items or None


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
