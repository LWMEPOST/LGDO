import type { AskResponse, WikiPage } from "../types";

export function splitTags(value: string): string[] {
  return value.split(",").map((x) => x.trim()).filter(Boolean);
}

export function safeJson(value: string, fallback: Record<string, unknown>): Record<string, unknown> {
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}

export function encodePath(path: string): string {
  return encodeURIComponent(path).replaceAll("%2F", "/");
}

export function formatAnswer(answer: AskResponse): string {
  const strategy = answer.retrieval_strategy;
  const strategyText = strategy
    ? [
        `回答模式：${strategy.mode_label || strategy.answer_mode || "-"}`,
        `检索分块：${strategy.chunk_hits ?? 0}`,
        `上下文：${strategy.context_limit ?? 0}`,
        `历史记忆：${strategy.memory_hits ?? 0}`,
      ].join(" / ")
    : "";
  const memories = answer.memory_hits?.length
    ? `\n\n相似历史问答：\n${answer.memory_hits.map((hit) => `- ${hit.question}\n  ${hit.answer_snippet}`).join("\n")}`
    : "";
  const strategyBlock = strategyText ? `\n\n策略：\n${strategyText}` : "";
  return `${answer.answer}${strategyBlock}${memories}\n\n引用：\n${answer.citations.map((c) => `- ${c.wiki_page || "未生成知识页"}（资料 ID：${c.source_id}）\n  ${c.snippet}`).join("\n")}`;
}

export function formatAclTags(tags: unknown): string {
  const values = Array.isArray(tags) ? tags : [];
  return values.length ? values.map(translateAclTag).join("、") : "内部";
}

export function formatBool(value: unknown): string {
  return value ? "是" : "否";
}

export function formatBytes(value: unknown): string {
  const size = Number(value || 0);
  if (size < 1024) return `${size} 字节`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

export function formatWarnings(warnings: unknown): string {
  const values = Array.isArray(warnings) ? warnings : [];
  return values.length ? values.map(translateWarning).join("、") : "-";
}

export function formatMetadata(metadata: Record<string, unknown>): string {
  return JSON.stringify(localizeMetadata(metadata), null, 2);
}

export function localizeMetadata(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(localizeMetadata);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [translateMetadataKey(key), localizeMetadataValue(key, item)]),
  );
}

function localizeMetadataValue(key: string, value: unknown): unknown {
  if (key === "domain") return translateDomain(value);
  if (key === "parser") return translateParser(value);
  if (key === "ocr_used") return formatBool(value);
  if (key === "acl_tags" && Array.isArray(value)) return value.map(translateAclTag);
  if (key === "warnings" && Array.isArray(value)) return value.map(translateWarning);
  return localizeMetadata(value);
}

export function translateDomain(value: unknown): string {
  return ({
    product: "产品知识",
    customer_service: "客服知识",
  } as Record<string, string>)[String(value)] || String(value || "-");
}

export function translateSourceStatus(value: unknown): string {
  return ({
    active: "可用",
    deleted: "已删除",
  } as Record<string, string>)[String(value)] || String(value || "可用");
}

export function translateSourceType(value: unknown): string {
  return ({
    md: "Markdown",
    markdown: "Markdown",
    txt: "文本",
    log: "日志",
    csv: "CSV 表格",
    json: "JSON 数据",
    pdf: "PDF",
    doc: "Word 旧版",
    docx: "Word",
    xls: "Excel 旧版",
    xlsx: "Excel",
    ppt: "PPT 旧版",
    pptx: "PPT",
    file: "文件",
  } as Record<string, string>)[String(value)] || String(value || "-");
}

export function translateParser(value: unknown): string {
  return ({
    text: "文本解析",
    csv: "CSV 解析",
    json: "JSON 解析",
    "pdf-text": "PDF 文本解析",
    "pdf-ocr-placeholder": "PDF OCR 待接入",
    docx: "Word 解析",
    xlsx: "Excel 解析",
    pptx: "PPT 解析",
    "legacy-doc": "Word 旧版提示",
    "legacy-xls": "Excel 旧版提示",
    "legacy-ppt": "PPT 旧版提示",
  } as Record<string, string>)[String(value)] || String(value || "-");
}

export function translatePageType(value: unknown): string {
  return ({
    faq: "常见问题",
    feature: "功能说明",
    known_issue: "已知问题",
    policy: "政策规则",
    index: "索引",
  } as Record<string, string>)[String(value)] || String(value || "-");
}

export function translateReviewStatus(value: unknown): string {
  return ({
    draft: "草稿",
    reviewed: "已审阅",
    stale: "已过期",
    rejected: "已驳回",
  } as Record<string, string>)[String(value)] || String(value || "-");
}

export function translateGapStatus(value: unknown): string {
  return ({
    open: "待处理",
    in_progress: "处理中",
    resolved: "已解决",
    rejected: "已关闭",
  } as Record<string, string>)[String(value)] || String(value || "-");
}

export function translateReviewItemStatus(value: unknown): string {
  return ({
    pending: "待审阅",
    approved: "已通过",
    rejected: "已驳回",
    resolved: "已解决",
  } as Record<string, string>)[String(value)] || String(value || "-");
}

export function translateReviewIssueType(value: unknown): string {
  return ({
    new_page: "新增知识页",
    changed_page: "知识页变更",
    conflict: "内容冲突",
    stale: "内容过期",
    missing_citation: "缺少引用",
  } as Record<string, string>)[String(value)] || String(value || "-");
}

export function translatePriority(value: unknown): string {
  return ({
    low: "低优先级",
    medium: "中优先级",
    high: "高优先级",
  } as Record<string, string>)[String(value)] || String(value || "-");
}

export function translateConfidence(value: unknown): string {
  return ({
    low: "低置信度",
    medium: "中置信度",
    high: "高置信度",
  } as Record<string, string>)[String(value)] || String(value || "-");
}

export function translateAclTag(value: unknown): string {
  return ({
    internal: "内部",
    "内部": "内部",
    product: "产品",
    "产品": "产品",
    customer_service: "客服",
    "客服": "客服",
    upload: "上传",
    "上传": "上传",
  } as Record<string, string>)[String(value)] || String(value || "-");
}

export function translateWarning(value: unknown): string {
  return ({
    empty_body: "正文为空",
    legacy_office_format: "旧版 Office 格式",
    pdf_text_empty: "PDF 未提取到文本",
    ocr_engine_not_configured: "OCR 引擎未配置",
    ocr_disabled: "OCR 未启用",
  } as Record<string, string>)[String(value)] || String(value || "-");
}

function translateMetadataKey(value: string): string {
  return ({
    title: "标题",
    source_id: "资料 ID",
    domain: "业务域",
    owner: "负责人",
    acl_tags: "权限标签",
    content_hash: "内容哈希",
    parser: "解析器",
    ocr_used: "是否 OCR",
    char_count: "字符数",
    source_system: "来源系统",
    extension: "文件扩展名",
    relative_to_scan_root: "扫描目录内路径",
    scan_job_id: "采集任务 ID",
    warnings: "警告",
    normalized_path: "标准 Markdown 路径",
    jsonl_path: "JSONL 路径",
    chunk_count: "分块数",
    cleaned: "清洗后元数据",
    original_path: "原始路径",
    page_count: "页数",
  } as Record<string, string>)[value] || value;
}

export function filterRows<T>(rows: T[], keyword: string, fieldsOf: (row: T) => unknown[]): T[] {
  const q = String(keyword || "").trim().toLowerCase();
  if (!q) return rows;
  return rows.filter((row) => fieldsOf(row).some((value) => String(value || "").toLowerCase().includes(q)));
}

export function sourceFileKind(type: string): string {
  return ({
    md: "MD",
    markdown: "MD",
    txt: "TXT",
    log: "LOG",
    csv: "CSV",
    json: "JSON",
    pdf: "PDF",
    doc: "DOC",
    docx: "DOC",
    xls: "XLS",
    xlsx: "XLS",
    ppt: "PPT",
    pptx: "PPT",
  } as Record<string, string>)[type] || "FILE";
}

export function countByPageType(pages: WikiPage[], type: string): number {
  return pages.filter((page) => page.page_type === type).length;
}
