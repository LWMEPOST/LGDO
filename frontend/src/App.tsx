import { useEffect, useMemo, useRef, useState } from "react";

import { api, ApiError, getAuthToken, setAuthToken } from "./api/client";
import { Layout } from "./components/Layout";
import type { SectionId } from "./constants";
import { AccountsTask, type AccountPayload } from "./features/AccountsTask";
import { GapsTask } from "./features/GapsTask";
import { IngestTask } from "./features/IngestTask";
import { LoginView } from "./features/LoginView";
import { Overview } from "./features/Overview";
import { QaTask } from "./features/QaTask";
import { ReviewsTask } from "./features/ReviewsTask";
import { SourcesTask } from "./features/SourcesTask";
import { WikiTask } from "./features/WikiTask";
import type {
  AskResponse,
  AccountRecord,
  AuthSession,
  AuthUser,
  EditorState,
  IngestReport,
  KnowledgeGap,
  RagStatus,
  ReviewItem,
  SourcePreview,
  SourceRecord,
  SpaceFilter,
  VaultReconcileJob,
  VaultStatus,
  WikiPage,
  WikiMutationResponse,
  WikiPageContentResponse,
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

const VAULT_RECONCILE_POLL_MS = 1_000;
const VAULT_RECONCILE_MAX_BACKOFF_MS = 8_000;

function reconcileStatusRank(status: string) {
  if (status === "queued") return 0;
  if (status === "running") return 1;
  return 2;
}

function isActiveReconcile(job: VaultReconcileJob) {
  return job.status === "queued" || job.status === "running";
}

export function App({ navigateTo = (url: string) => window.location.assign(url) }: { navigateTo?: (url: string) => void } = {}) {
  const [activeSection, setActiveSection] = useState<SectionId>("overview");
  const [searchText, setSearchText] = useState("");
  const [activeSpaceFilterId, setActiveSpaceFilterId] = useState("all");
  const [sources, setSources] = useState<SourceRecord[]>([]);
  const [reports, setReports] = useState<IngestReport[]>([]);
  const [pages, setPages] = useState<WikiPage[]>([]);
  const [reviews, setReviews] = useState<ReviewItem[]>([]);
  const [gaps, setGaps] = useState<KnowledgeGap[]>([]);
  const [ragStatus, setRagStatus] = useState<RagStatus | null>(null);
  const [vaultStatus, setVaultStatus] = useState<VaultStatus | null>(null);
  const [trackedReconcileJob, setTrackedReconcileJob] = useState<VaultReconcileJob | null>(null);
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);
  const [accounts, setAccounts] = useState<AccountRecord[]>([]);
  const [authLoading, setAuthLoading] = useState(true);
  const [authError, setAuthError] = useState("");
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
  });
  const [lastQueryId, setLastQueryId] = useState<string | null>(null);
  const [answer, setAnswer] = useState<AskResponse | null>(null);
  const [feedback, setFeedback] = useState({ rating: "partial", comment: "" });
  const [editor, setEditor] = useState<EditorState>({
    path: "",
    page_id: "",
    content: "",
    current_revision_id: "",
    generated_revision_id: null,
    accepted_generated_revision_id: null,
    lifecycle_status: "active",
    projection_epoch: 0,
    write_in_progress: false,
    write_intent_id: null,
    review_status: "draft",
    owner: "",
  });
  const wikiMutationInFlight = useRef(false);
  const mountedRef = useRef(true);
  const sessionGenerationRef = useRef(0);
  const reconcileTimerRef = useRef<number | null>(null);
  const reconcileJobRef = useRef<VaultReconcileJob | null>(null);
  const reconcileRequestGenerationRef = useRef<number | null>(null);
  const reconcilePollFailureCountRef = useRef(0);
  const [selectedSourceId, setSelectedSourceId] = useState<string | null>(null);
  const [sourcePreview, setSourcePreview] = useState<SourcePreview | null>(null);
  const [sourcePreviewLoading, setSourcePreviewLoading] = useState(false);

  function resetVaultReconcileTracking(clearReactiveState = true) {
    if (reconcileTimerRef.current !== null) window.clearTimeout(reconcileTimerRef.current);
    reconcileTimerRef.current = null;
    reconcileJobRef.current = null;
    reconcileRequestGenerationRef.current = null;
    reconcilePollFailureCountRef.current = 0;
    if (clearReactiveState) setTrackedReconcileJob(null);
  }

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      sessionGenerationRef.current += 1;
      resetVaultReconcileTracking(false);
    };
  }, []);

  useEffect(() => {
    bootstrapAuth().catch((error) => {
      setAuthError(error.message);
      setAuthLoading(false);
    });
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

  function isCurrentGeneration(generation: number) {
    return mountedRef.current && sessionGenerationRef.current === generation;
  }

  function mergeTrackedReconcile(job: VaultReconcileJob) {
    const tracked = reconcileJobRef.current;
    if (tracked?.job_id === job.job_id && reconcileStatusRank(tracked.status) > reconcileStatusRank(job.status)) {
      return tracked;
    }
    reconcileJobRef.current = job;
    return job;
  }

  function trackReconcileJob(job: VaultReconcileJob) {
    const tracked = mergeTrackedReconcile(job);
    setTrackedReconcileJob(tracked);
    return tracked;
  }

  function mergeVaultStatus(snapshot: VaultStatus) {
    const tracked = reconcileJobRef.current;
    if (!tracked) {
      if (snapshot.reconcile) trackReconcileJob(snapshot.reconcile);
      return snapshot;
    }
    if (!snapshot.reconcile || snapshot.reconcile.job_id !== tracked.job_id) {
      return { ...snapshot, reconcile: tracked };
    }
    const reconcile = trackReconcileJob(snapshot.reconcile);
    return reconcile === snapshot.reconcile ? snapshot : { ...snapshot, reconcile };
  }

  async function refreshVaultStatus(generation: number) {
    try {
      const status = await api<VaultStatus>("/api/internal/vault/status");
      if (isCurrentGeneration(generation)) setVaultStatus(mergeVaultStatus(status));
    } catch {
      // Vault status is optional; the terminal job already remains visible.
    }
  }

  function scheduleVaultReconcilePoll(jobId: string, generation: number, delay = VAULT_RECONCILE_POLL_MS) {
    if (reconcileTimerRef.current !== null) window.clearTimeout(reconcileTimerRef.current);
    reconcileTimerRef.current = window.setTimeout(() => {
      reconcileTimerRef.current = null;
      if (
        !isCurrentGeneration(generation)
        || reconcileJobRef.current?.job_id !== jobId
        || !isActiveReconcile(reconcileJobRef.current)
      ) return;
      void api<VaultReconcileJob>(`/api/internal/vault/reconcile/${encodeURIComponent(jobId)}`)
        .then((response) => {
          if (!isCurrentGeneration(generation) || reconcileJobRef.current?.job_id !== jobId) return;
          reconcilePollFailureCountRef.current = 0;
          const job = trackReconcileJob(response);
          setVaultStatus((current) => current ? { ...current, reconcile: job } : current);
          if (isActiveReconcile(job)) {
            scheduleVaultReconcilePoll(jobId, generation);
          } else {
            void refreshVaultStatus(generation);
          }
        })
        .catch((error) => {
          if (
            !isCurrentGeneration(generation)
            || reconcileJobRef.current?.job_id !== jobId
            || !isActiveReconcile(reconcileJobRef.current)
          ) return;
          const previousFailures = reconcilePollFailureCountRef.current;
          reconcilePollFailureCountRef.current = Math.min(previousFailures + 1, 3);
          if (previousFailures === 0) {
            showToast(error instanceof Error ? error.message : "Vault 对账状态查询失败");
          }
          const retryDelay = Math.min(
            VAULT_RECONCILE_POLL_MS * (2 ** reconcilePollFailureCountRef.current),
            VAULT_RECONCILE_MAX_BACKOFF_MS,
          );
          scheduleVaultReconcilePoll(jobId, generation, retryDelay);
        });
    }, delay);
  }

  async function bootstrapAuth() {
    const generation = sessionGenerationRef.current;
    if (!getAuthToken()) {
      if (isCurrentGeneration(generation)) setAuthLoading(false);
      return;
    }
    try {
      const user = await api<AuthUser>("/api/internal/auth/me");
      if (!isCurrentGeneration(generation)) return;
      setCurrentUser(user);
      setAuthLoading(false);
      await refresh(user, generation);
    } catch (error) {
      if (!isCurrentGeneration(generation)) return;
      sessionGenerationRef.current += 1;
      resetVaultReconcileTracking();
      setAuthToken("");
      setCurrentUser(null);
      setAuthLoading(false);
    }
  }

  async function login(username: string, password: string) {
    const generation = sessionGenerationRef.current + 1;
    sessionGenerationRef.current = generation;
    resetVaultReconcileTracking();
    setAuthError("");
    try {
      const session = await api<AuthSession>("/api/internal/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      if (!isCurrentGeneration(generation)) return;
      setAuthToken(session.token);
      setCurrentUser(session.user);
      await refresh(session.user, generation);
    } catch (error) {
      if (isCurrentGeneration(generation)) {
        setAuthError(error instanceof Error ? error.message : "登录失败");
      }
    }
  }

  async function logout() {
    const logoutRequest = api("/api/internal/auth/logout", { method: "POST" }).catch(() => undefined);
    sessionGenerationRef.current += 1;
    resetVaultReconcileTracking();
    setAuthToken("");
    setCurrentUser(null);
    setAccounts([]);
    setSources([]);
    setReports([]);
    setPages([]);
    setReviews([]);
    setGaps([]);
    setRagStatus(null);
    setVaultStatus(null);
    await logoutRequest;
  }

  async function refresh(user = currentUser, generation = sessionGenerationRef.current) {
    const nextVaultStatus = api<VaultStatus>("/api/internal/vault/status").catch(() => null);
    const [nextSources, nextReports, nextPages, nextReviews, nextGaps, nextRagStatus] = await Promise.all([
      api<SourceRecord[]>("/api/internal/sources"),
      api<IngestReport[]>("/api/internal/ingest/reports"),
      api<WikiPage[]>("/api/internal/wiki/pages"),
      api<ReviewItem[]>("/api/internal/reviews?status=pending"),
      api<KnowledgeGap[]>("/api/internal/gaps"),
      api<RagStatus>("/api/internal/rag/status"),
    ]);
    if (!isCurrentGeneration(generation)) return;
    setSources(nextSources);
    setReports(nextReports);
    setPages(nextPages);
    setReviews(nextReviews);
    setGaps(nextGaps);
    setRagStatus(nextRagStatus);
    void nextVaultStatus.then((status) => {
      if (isCurrentGeneration(generation)) setVaultStatus(status ? mergeVaultStatus(status) : status);
    });
    if (user?.role === "admin" || user?.acl_tags?.includes("*")) {
      const nextAccounts = await api<AccountRecord[]>("/api/internal/accounts");
      if (isCurrentGeneration(generation)) setAccounts(nextAccounts);
    } else if (isCurrentGeneration(generation)) {
      setAccounts([]);
    }
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
    const token = getAuthToken();
    const response = await fetch("/api/internal/sources/upload", {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      body: form,
    });
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
        question: askForm.question,
        domain: askForm.domain,
        answer_mode: askForm.answer_mode,
        require_citations: true,
      }),
    });
    setAnswer(result);
    setLastQueryId(result.query_id);
  }

  async function createAccount(payload: AccountPayload) {
    await api<AccountRecord>("/api/internal/accounts", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showToast("账户已创建");
    await refresh();
  }

  async function updateAccount(userId: string, payload: Partial<AccountPayload>) {
    await api<AccountRecord>(`/api/internal/accounts/${encodeURIComponent(userId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    showToast("账户权限已更新");
    await refresh();
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

  async function openInObsidian(path: string) {
    const result = await api<{ url: string }>(`/api/internal/wiki/pages/${encodePath(path)}/obsidian-link`);
    navigateTo(result.url);
  }

  async function requestVaultReconcile() {
    const generation = sessionGenerationRef.current;
    if (reconcileJobRef.current && isActiveReconcile(reconcileJobRef.current)) return;
    if (reconcileRequestGenerationRef.current === generation) return;
    reconcileRequestGenerationRef.current = generation;
    try {
      const job = await api<VaultReconcileJob>("/api/internal/vault/reconcile", { method: "POST" });
      if (!isCurrentGeneration(generation)) return;
      const trackedJob = trackReconcileJob(job);
      reconcilePollFailureCountRef.current = 0;
      setVaultStatus((current) => current ? { ...current, reconcile: trackedJob } : null);
      showToast(`Vault 对账已进入${job.status}`);
      if (isActiveReconcile(trackedJob)) {
        scheduleVaultReconcilePoll(trackedJob.job_id, generation);
      } else {
        void refreshVaultStatus(generation);
      }
    } finally {
      if (reconcileRequestGenerationRef.current === generation) {
        reconcileRequestGenerationRef.current = null;
      }
    }
  }

  async function loadPage(path: string) {
    const page = await api<WikiPageContentResponse>(`/api/internal/wiki/pages/${encodePath(path)}`);
    setEditor({
      path: page.path,
      page_id: page.page_id,
      content: page.content,
      current_revision_id: page.current_revision_id,
      generated_revision_id: page.generated_revision_id,
      accepted_generated_revision_id: page.accepted_generated_revision_id,
      lifecycle_status: page.lifecycle_status,
      projection_epoch: page.projection_epoch,
      write_in_progress: page.write_in_progress,
      write_intent_id: page.write_intent_id,
      review_status: page.metadata.review_status || "draft",
      owner: page.metadata.owner || "",
    });
    setActiveSection("wiki");
  }

  async function reconcilePageConflict(attemptedPath: string, attemptedContent: string) {
    const latestPage = await api<WikiPageContentResponse>(`/api/internal/wiki/pages/${encodePath(attemptedPath)}`);
    setEditor((prev) => {
      if (prev.path !== attemptedPath) return prev;
      return {
        ...prev,
        page_id: latestPage.page_id,
        content: prev.content,
        current_revision_id: latestPage.current_revision_id,
        generated_revision_id: latestPage.generated_revision_id,
        accepted_generated_revision_id: latestPage.accepted_generated_revision_id,
        lifecycle_status: latestPage.lifecycle_status,
        projection_epoch: latestPage.projection_epoch,
        write_in_progress: latestPage.write_in_progress,
        write_intent_id: latestPage.write_intent_id,
      };
    });
    const localContent = attemptedContent ? "本地未保存内容" : "本地空白内容";
    showToast(`检测到知识页版本冲突，已同步最新版本号；${localContent}已保留，请核对后再次保存`);
  }

  async function savePage() {
    if (!editor.path) return showToast("请先选择知识页");
    if (wikiMutationInFlight.current) return showToast("知识页操作正在进行，请稍候");
    wikiMutationInFlight.current = true;
    const attemptedPath = editor.path;
    const attemptedContent = editor.content;
    try {
      let result: WikiMutationResponse;
      try {
        result = await api<WikiMutationResponse>(`/api/internal/wiki/pages/${encodePath(attemptedPath)}`, {
          method: "PUT",
          body: JSON.stringify({
            content: attemptedContent,
            expected_revision_id: editor.current_revision_id,
            request_id: crypto.randomUUID(),
            review_status: editor.review_status,
            owner: editor.owner || null,
            note: "管理端保存",
          }),
        });
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) {
          await reconcilePageConflict(attemptedPath, attemptedContent);
          return;
        }
        throw error;
      }
      setEditor((prev) => {
        if (prev.path !== attemptedPath) return prev;
        return {
          ...prev,
          page_id: result.page_id,
          content: prev.content,
          current_revision_id: result.current_revision_id ?? prev.current_revision_id,
          generated_revision_id: result.generated_revision_id ?? prev.generated_revision_id,
          review_status: result.review_status ?? prev.review_status,
          write_in_progress: false,
          write_intent_id: result.write_intent_id,
        };
      });
      showToast("知识页已保存");
      await refresh();
    } finally {
      wikiMutationInFlight.current = false;
    }
  }

  async function markPageStale(path = editor.path) {
    if (!path) return showToast("请先选择知识页");
    if (wikiMutationInFlight.current) return showToast("知识页操作正在进行，请稍候");
    wikiMutationInFlight.current = true;
    const attemptedPath = path;
    const attemptedContent = editor.content;
    try {
      const listedPage = pages.find((page) => page.path === path);
      const targetRevisionId = path === editor.path ? editor.current_revision_id : listedPage?.current_revision_id;
      const targetPage = targetRevisionId
        ? { current_revision_id: targetRevisionId }
        : await api<WikiPageContentResponse>(`/api/internal/wiki/pages/${encodePath(path)}`);
      let result: WikiMutationResponse;
      try {
        result = await api<WikiMutationResponse>(`/api/internal/wiki/pages/${encodePath(path)}/status`, {
          method: "PATCH",
          body: JSON.stringify({
            review_status: "stale",
            expected_revision_id: targetPage.current_revision_id,
            request_id: crypto.randomUUID(),
            note: "管理端标记过期",
          }),
        });
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) {
          await reconcilePageConflict(attemptedPath, attemptedContent);
          return;
        }
        throw error;
      }
      setEditor((prev) => {
        if (prev.path !== attemptedPath) return prev;
        return {
          ...prev,
          page_id: result.page_id,
          content: prev.content,
          current_revision_id: result.current_revision_id ?? prev.current_revision_id,
          generated_revision_id: result.generated_revision_id ?? prev.generated_revision_id,
          review_status: result.review_status ?? "stale",
          write_in_progress: false,
          write_intent_id: result.write_intent_id,
        };
      });
      showToast("已标记过期");
      await refresh();
    } finally {
      wikiMutationInFlight.current = false;
    }
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

  if (authLoading) {
    return <div className="login-shell"><section className="login-panel"><h1>正在检查登录状态</h1></section></div>;
  }

  if (!currentUser) {
    return <LoginView login={login} error={authError} />;
  }

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
        setScanForm={(form) => setScanForm((current) => ({ ...current, ...form }))}
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
    wiki: (
      <WikiTask
        pages={scopedPages}
        activeSpaceFilter={activeSpaceFilter}
        clearSpaceFilter={clearSpaceFilter}
        editor={editor}
        setEditor={setEditor}
        loadPage={loadPage}
        savePage={savePage}
        markPageStale={markPageStale}
        openInObsidian={openInObsidian}
        requestReconcile={requestVaultReconcile}
        reconcileJob={trackedReconcileJob}
        vaultStatus={vaultStatus}
        currentUser={currentUser}
        showToast={showToast}
      />
    ),
    qa: (
      <QaTask
        askForm={askForm}
        setAskForm={(form) => setAskForm((current) => ({ ...current, ...form }))}
        ask={ask}
        answer={answer}
        feedback={feedback}
        setFeedback={(nextFeedback) => setFeedback((current) => ({ ...current, ...nextFeedback }))}
        createGap={createGap}
        showToast={showToast}
        currentUser={currentUser}
      />
    ),
    gaps: <GapsTask gaps={scopedGaps} updateGap={updateGap} showToast={showToast} />,
    reviews: <ReviewsTask reviews={reviews} loadPage={loadPage} updateReview={updateReview} showToast={showToast} />,
    accounts: (
      <AccountsTask
        accounts={accounts}
        currentUser={currentUser}
        createAccount={createAccount}
        updateAccount={updateAccount}
        showToast={showToast}
      />
    ),
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
      currentUser={currentUser}
      logout={logout}
    >
      {content}
      {toast && <div className="toast">{toast}</div>}
    </Layout>
  );
}
