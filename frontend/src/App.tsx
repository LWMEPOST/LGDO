import { useEffect, useMemo, useState } from "react";

import { api } from "./api/client";
import { Layout } from "./components/Layout";
import type { SectionId } from "./constants";
import { GapsTask } from "./features/GapsTask";
import { IngestTask } from "./features/IngestTask";
import { Overview } from "./features/Overview";
import { QaTask } from "./features/QaTask";
import { ReviewsTask } from "./features/ReviewsTask";
import { SourcesTask } from "./features/SourcesTask";
import { WikiTask } from "./features/WikiTask";
import type {
  AskResponse,
  EditorState,
  IngestReport,
  KnowledgeGap,
  RagStatus,
  ReviewItem,
  SourcePreview,
  SourceRecord,
  SpaceFilter,
  WikiPage,
} from "./types";
import { encodePath, filterRows, safeJson, splitTags, translateGapStatus, translateReviewItemStatus } from "./utils/format";
import {
  buildSpaceDirectory,
  filterGapsBySpace,
  filterPagesBySpace,
  filterReportsBySources,
  filterSourcesBySpace,
  findSpaceFilter,
} from "./utils/space";

export function App() {
  const [activeSection, setActiveSection] = useState<SectionId>("overview");
  const [searchText, setSearchText] = useState("");
  const [activeSpaceFilterId, setActiveSpaceFilterId] = useState("all");
  const [sources, setSources] = useState<SourceRecord[]>([]);
  const [reports, setReports] = useState<IngestReport[]>([]);
  const [pages, setPages] = useState<WikiPage[]>([]);
  const [reviews, setReviews] = useState<ReviewItem[]>([]);
  const [gaps, setGaps] = useState<KnowledgeGap[]>([]);
  const [ragStatus, setRagStatus] = useState<RagStatus | null>(null);
  const [toast, setToast] = useState("");
  const [scanForm, setScanForm] = useState({
    root_path: "samples/product_service",
    domain: "product",
    owner: "管理员",
    acl_tags: "内部,产品",
    metadata_defaults: '{"source_system":"本地文件"}',
  });
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [askForm, setAskForm] = useState({
    question: "用户如何处理退款问题？",
    domain: "product",
    answer_mode: "detail",
    user_id: "admin",
    role: "admin",
    acl_tags: "内部,产品",
  });
  const [lastQueryId, setLastQueryId] = useState<string | null>(null);
  const [answer, setAnswer] = useState<AskResponse | null>(null);
  const [feedback, setFeedback] = useState({ rating: "partial", comment: "" });
  const [editor, setEditor] = useState<EditorState>({
    path: "",
    content: "",
    review_status: "draft",
    owner: "",
  });
  const [selectedSourceId, setSelectedSourceId] = useState<string | null>(null);
  const [sourcePreview, setSourcePreview] = useState<SourcePreview | null>(null);
  const [sourcePreviewLoading, setSourcePreviewLoading] = useState(false);

  useEffect(() => {
    refresh().catch((error) => showToast(error.message));
  }, []);

  useEffect(() => {
    if (!selectedSourceId && sources.length) setSelectedSourceId(sources[0].id);
  }, [sources, selectedSourceId]);

  useEffect(() => {
    if (activeSection !== "sources" || !selectedSourceId) return;
    loadSourcePreview(selectedSourceId).catch((error) => showToast(error.message));
  }, [activeSection, selectedSourceId]);

  function showToast(message: string) {
    setToast(message);
    window.setTimeout(() => setToast(""), 3200);
  }

  async function refresh() {
    const [nextSources, nextReports, nextPages, nextReviews, nextGaps, nextRagStatus] = await Promise.all([
      api<SourceRecord[]>("/api/internal/sources"),
      api<IngestReport[]>("/api/internal/ingest/reports"),
      api<WikiPage[]>("/api/internal/wiki/pages"),
      api<ReviewItem[]>("/api/internal/reviews?status=pending"),
      api<KnowledgeGap[]>("/api/internal/gaps"),
      api<RagStatus>("/api/internal/rag/status"),
    ]);
    setSources(nextSources);
    setReports(nextReports);
    setPages(nextPages);
    setReviews(nextReviews);
    setGaps(nextGaps);
    setRagStatus(nextRagStatus);
  }

  async function loadSourcePreview(sourceId: string) {
    setSourcePreviewLoading(true);
    try {
      const result = await api<SourcePreview>(`/api/internal/sources/${sourceId}/preview?max_chars=12000`);
      setSourcePreview(result);
    } finally {
      setSourcePreviewLoading(false);
    }
  }

  async function syncPostgresRag() {
    const result = await api<{ synced_chunks: number }>("/api/internal/rag/sync-postgres", { method: "POST" });
    showToast(`已同步 ${result.synced_chunks} 个 RAG 分块到 PostgreSQL`);
    await refresh();
  }

  async function scan() {
    const defaults = safeJson(scanForm.metadata_defaults, {});
    const result = await api<{ new_files: number; changed_files: number }>("/api/internal/sources/scan", {
      method: "POST",
      body: JSON.stringify({
        root_path: scanForm.root_path,
        domain: scanForm.domain,
        owner: scanForm.owner || null,
        acl_tags: splitTags(scanForm.acl_tags),
        metadata_defaults: defaults,
      }),
    });
    showToast(`扫描完成：新增 ${result.new_files}，更新 ${result.changed_files}`);
    await refresh();
  }

  async function uploadAndIngest() {
    if (!uploadFiles.length) return showToast("请选择要上传的文件");
    const form = new FormData();
    uploadFiles.forEach((file) => form.append("files", file));
    form.append("domain", scanForm.domain);
    form.append("owner", scanForm.owner || "");
    form.append("acl_tags", scanForm.acl_tags || "internal");
    form.append("metadata_defaults", scanForm.metadata_defaults || "{}");
    const response = await fetch("/api/internal/sources/upload", { method: "POST", body: form });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || response.statusText);
    showToast(`上传完成：${body.saved_files.length} 个文件`);
    setUploadFiles([]);
    await refresh();
  }

  async function compileWiki() {
    const result = await api<{ created_pages: number; updated_pages: number }>("/api/internal/wiki/compile", {
      method: "POST",
      body: JSON.stringify({ domain: scanForm.domain }),
    });
    showToast(`编译完成：新增 ${result.created_pages}，更新 ${result.updated_pages}`);
    await refresh();
  }

  async function deleteSource(sourceId: string) {
    const confirmed = window.confirm("确认删除这条资料吗？系统会保留审计记录，并将关联知识页标记为过期。");
    if (!confirmed) return;
    await api(`/api/internal/sources/${sourceId}?note=${encodeURIComponent("管理端删除")}`, { method: "DELETE" });
    if (selectedSourceId === sourceId) setSelectedSourceId(null);
    if (sourcePreview?.id === sourceId) setSourcePreview(null);
    showToast("资料已删除，关联知识页已标记过期");
    await refresh();
  }

  async function ask() {
    const result = await api<AskResponse>("/api/internal/ask", {
      method: "POST",
      body: JSON.stringify({
        ...askForm,
        user_id: askForm.user_id || null,
        role: askForm.role || null,
        acl_tags: splitTags(askForm.acl_tags),
        require_citations: true,
      }),
    });
    setAnswer(result);
    setLastQueryId(result.query_id);
  }

  async function createGap() {
    if (!lastQueryId) return showToast("请先提问");
    await api("/api/internal/feedback", {
      method: "POST",
      body: JSON.stringify({
        query_id: lastQueryId,
        rating: feedback.rating,
        comment: feedback.comment || "需要补充知识库资料",
        should_create_gap: true,
      }),
    });
    showToast("已生成知识缺口");
    await refresh();
  }

  async function loadPage(path: string) {
    const page = await api<{ path: string; content: string; metadata: Record<string, any> }>(`/api/internal/wiki/pages/${encodePath(path)}`);
    setEditor({
      path: page.path,
      content: page.content,
      review_status: page.metadata.review_status || "draft",
      owner: page.metadata.owner || "",
    });
    setActiveSection("wiki");
  }

  async function savePage() {
    if (!editor.path) return showToast("请先选择知识页");
    await api(`/api/internal/wiki/pages/${encodePath(editor.path)}`, {
      method: "PUT",
      body: JSON.stringify({
        content: editor.content,
        review_status: editor.review_status,
        owner: editor.owner || null,
        note: "管理端保存",
      }),
    });
    showToast("知识页已保存");
    await refresh();
  }

  async function markPageStale(path = editor.path) {
    if (!path) return showToast("请先选择知识页");
    await api(`/api/internal/wiki/pages/${encodePath(path)}/status`, {
      method: "PATCH",
      body: JSON.stringify({ review_status: "stale", note: "管理端标记过期" }),
    });
    showToast("已标记过期");
    await refresh();
  }

  async function updateReview(id: string, status: string) {
    await api(`/api/internal/reviews/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status, note: `管理端${translateReviewItemStatus(status)}` }),
    });
    showToast(status === "approved" ? "已通过审阅" : "已驳回审阅");
    await refresh();
  }

  async function updateGap(id: string, status: string) {
    await api(`/api/internal/gaps/${id}`, {
      method: "PATCH",
      body: JSON.stringify({
        status,
        owner: scanForm.owner || "admin",
        linked_page_path: status === "resolved" ? editor.path || null : null,
      }),
    });
    showToast(`缺口已更新为${translateGapStatus(status)}`);
    await refresh();
  }

  const spaceDirectory = useMemo(() => buildSpaceDirectory(sources, pages, gaps), [sources, pages, gaps]);
  const activeSpaceFilter = useMemo<SpaceFilter>(
    () => findSpaceFilter(spaceDirectory, activeSpaceFilterId),
    [spaceDirectory, activeSpaceFilterId],
  );

  function selectSpaceFilter(filter: SpaceFilter) {
    setActiveSpaceFilterId(filter.id);
    if (filter.targetSection) {
      setActiveSection(filter.targetSection);
    }
  }

  function clearSpaceFilter() {
    setActiveSpaceFilterId("all");
  }

  const searchedSources = useMemo<SourceRecord[]>(
    () => filterRows<SourceRecord>(sources, searchText, (source) => [source.title, source.id, source.original_path, source.owner, source.metadata?.normalized_path, source.domain, source.source_type]),
    [sources, searchText],
  );
  const searchedPages = useMemo<WikiPage[]>(
    () => filterRows<WikiPage>(pages, searchText, (page) => [page.title, page.path, page.page_type, page.review_status, page.domain]),
    [pages, searchText],
  );
  const searchedGaps = useMemo<KnowledgeGap[]>(
    () => filterRows<KnowledgeGap>(gaps, searchText, (gap) => [gap.question, gap.status, gap.priority, gap.owner]),
    [gaps, searchText],
  );
  const scopedSources = useMemo<SourceRecord[]>(
    () => filterSourcesBySpace(searchedSources, searchedPages, activeSpaceFilter),
    [searchedSources, searchedPages, activeSpaceFilter],
  );
  const scopedPages = useMemo<WikiPage[]>(
    () => filterPagesBySpace(searchedPages, activeSpaceFilter),
    [searchedPages, activeSpaceFilter],
  );
  const scopedGaps = useMemo<KnowledgeGap[]>(
    () => filterGapsBySpace(searchedGaps, activeSpaceFilter),
    [searchedGaps, activeSpaceFilter],
  );
  const scopedReports = useMemo<IngestReport[]>(
    () => filterReportsBySources(reports, scopedSources),
    [reports, scopedSources],
  );

  useEffect(() => {
    if (activeSection !== "sources") return;
    if (!scopedSources.length) {
      if (selectedSourceId) setSelectedSourceId(null);
      return;
    }
    if (!selectedSourceId || !scopedSources.some((source) => source.id === selectedSourceId)) {
      setSelectedSourceId(scopedSources[0].id);
    }
  }, [activeSection, scopedSources, selectedSourceId]);

  const stats = useMemo(
    () => [
      ["资料", scopedSources.length],
      ["采集报告", scopedReports.length],
      ["知识页", scopedPages.length],
      ["知识缺口", scopedGaps.length],
      ["待审阅", reviews.length],
    ] as Array<[string, number]>,
    [scopedSources, scopedReports, scopedPages, scopedGaps, reviews],
  );

  const content = {
    overview: (
      <Overview
        stats={stats}
        reports={scopedReports}
        pages={scopedPages}
        gaps={scopedGaps}
        sources={scopedSources}
        activeSpaceFilter={activeSpaceFilter}
        ragStatus={ragStatus}
        selectSpaceFilter={selectSpaceFilter}
        directory={spaceDirectory}
      />
    ),
    ingest: (
      <IngestTask
        scanForm={scanForm}
        setScanForm={setScanForm}
        scan={scan}
        compileWiki={compileWiki}
        reports={reports}
        showToast={showToast}
        uploadFiles={uploadFiles}
        setUploadFiles={setUploadFiles}
        uploadAndIngest={uploadAndIngest}
      />
    ),
    sources: (
      <SourcesTask
        sources={scopedSources}
        allSources={searchedSources}
        reports={scopedReports}
        activeSpaceFilter={activeSpaceFilter}
        clearSpaceFilter={clearSpaceFilter}
        selectedSourceId={selectedSourceId}
        setSelectedSourceId={setSelectedSourceId}
        sourcePreview={sourcePreview}
        sourcePreviewLoading={sourcePreviewLoading}
        loadSourcePreview={loadSourcePreview}
        deleteSource={deleteSource}
        showToast={showToast}
      />
    ),
    wiki: <WikiTask pages={scopedPages} activeSpaceFilter={activeSpaceFilter} clearSpaceFilter={clearSpaceFilter} editor={editor} setEditor={setEditor} loadPage={loadPage} savePage={savePage} markPageStale={markPageStale} showToast={showToast} />,
    qa: <QaTask askForm={askForm} setAskForm={setAskForm} ask={ask} answer={answer} feedback={feedback} setFeedback={setFeedback} createGap={createGap} showToast={showToast} />,
    gaps: <GapsTask gaps={scopedGaps} updateGap={updateGap} showToast={showToast} />,
    reviews: <ReviewsTask reviews={reviews} loadPage={loadPage} updateReview={updateReview} showToast={showToast} />,
  }[activeSection];

  return (
    <Layout
      activeSection={activeSection}
      setActiveSection={setActiveSection}
      searchText={searchText}
      setSearchText={setSearchText}
      sources={sources}
      pages={pages}
      gaps={gaps}
      scopedSources={scopedSources}
      scopedPages={scopedPages}
      scopedGaps={scopedGaps}
      directory={spaceDirectory}
      activeSpaceFilter={activeSpaceFilter}
      selectSpaceFilter={selectSpaceFilter}
      clearSpaceFilter={clearSpaceFilter}
      reportCount={reports.length}
      reviewCount={reviews.length}
      ragStatus={ragStatus}
      selectedSource={scopedSources.find((source) => source.id === selectedSourceId)}
      sourcePreview={sourcePreview}
      refresh={refresh}
      syncPostgresRag={syncPostgresRag}
      showToast={showToast}
    >
      {content}
      {toast && <div className="toast">{toast}</div>}
    </Layout>
  );
}
