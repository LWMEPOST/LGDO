const { useEffect, useMemo, useState } = React;

const APP_RAIL_ITEMS = [
  { id: "overview", label: "首页", icon: "首" },
  { id: "sources", label: "资料", icon: "库" },
  { id: "wiki", label: "知识", icon: "文" },
  { id: "qa", label: "问答", icon: "问" },
];

const NAV_ITEMS = [
  { id: "overview", label: "空间首页", desc: "知识运营总览", icon: "首" },
  { id: "ingest", label: "上传与采集", desc: "本地文件入库、清洗、分块", icon: "上" },
  { id: "sources", label: "资料库", desc: "资料查看、预览与删除", icon: "资" },
  { id: "wiki", label: "知识页", desc: "Markdown 编辑与审阅状态", icon: "页" },
  { id: "qa", label: "智能问答", desc: "引用回答、反馈、缺口", icon: "答" },
  { id: "reviews", label: "审阅队列", desc: "人工审核与发布", icon: "审" },
];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || response.statusText);
  return body;
}

function App() {
  const [activeSection, setActiveSection] = useState("overview");
  const [searchText, setSearchText] = useState("");
  const [sources, setSources] = useState([]);
  const [reports, setReports] = useState([]);
  const [pages, setPages] = useState([]);
  const [reviews, setReviews] = useState([]);
  const [gaps, setGaps] = useState([]);
  const [ragStatus, setRagStatus] = useState(null);
  const [toast, setToast] = useState("");
  const [scanForm, setScanForm] = useState({
    root_path: "samples/product_service",
    domain: "product",
    owner: "管理员",
    acl_tags: "内部,产品",
    metadata_defaults: '{"source_system":"本地文件"}',
  });
  const [uploadFiles, setUploadFiles] = useState([]);
  const [askForm, setAskForm] = useState({
    question: "用户如何处理退款问题？",
    domain: "product",
    answer_mode: "detail",
  });
  const [lastQueryId, setLastQueryId] = useState(null);
  const [answer, setAnswer] = useState(null);
  const [feedback, setFeedback] = useState({ rating: "partial", comment: "" });
  const [editor, setEditor] = useState({
    path: "",
    content: "",
    review_status: "draft",
    owner: "",
  });
  const [selectedSourceId, setSelectedSourceId] = useState(null);
  const [sourcePreview, setSourcePreview] = useState(null);
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

  function showToast(message) {
    setToast(message);
    window.setTimeout(() => setToast(""), 3200);
  }

  async function refresh() {
    const [nextSources, nextReports, nextPages, nextReviews, nextGaps, nextRagStatus] = await Promise.all([
      api("/api/internal/sources?domain=product"),
      api("/api/internal/ingest/reports"),
      api("/api/internal/wiki/pages?domain=product"),
      api("/api/internal/reviews?status=pending"),
      api("/api/internal/gaps"),
      api("/api/internal/rag/status"),
    ]);
    setSources(nextSources);
    setReports(nextReports);
    setPages(nextPages);
    setReviews(nextReviews);
    setGaps(nextGaps);
    setRagStatus(nextRagStatus);
  }

  async function loadSourcePreview(sourceId) {
    setSourcePreviewLoading(true);
    try {
      const result = await api(`/api/internal/sources/${sourceId}/preview?max_chars=12000`);
      setSourcePreview(result);
    } finally {
      setSourcePreviewLoading(false);
    }
  }

  async function syncPostgresRag() {
    const result = await api("/api/internal/rag/sync-postgres", { method: "POST" });
    showToast(`已同步 ${result.synced_chunks} 个 RAG 分块到 PostgreSQL`);
    await refresh();
  }

  async function scan() {
    const defaults = safeJson(scanForm.metadata_defaults, {});
    const result = await api("/api/internal/sources/scan", {
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
    const result = await api("/api/internal/wiki/compile", {
      method: "POST",
      body: JSON.stringify({ domain: scanForm.domain }),
    });
    showToast(`编译完成：新增 ${result.created_pages}，更新 ${result.updated_pages}`);
    await refresh();
  }

  async function deleteSource(sourceId) {
    const confirmed = window.confirm("确认删除这条资料吗？系统会保留审计记录，并将关联知识页标记为过期。");
    if (!confirmed) return;
    await api(`/api/internal/sources/${sourceId}?note=${encodeURIComponent("管理端删除")}`, { method: "DELETE" });
    if (selectedSourceId === sourceId) setSelectedSourceId(null);
    if (sourcePreview?.id === sourceId) setSourcePreview(null);
    showToast("资料已删除，关联知识页已标记过期");
    await refresh();
  }

  async function ask() {
    const result = await api("/api/internal/ask", {
      method: "POST",
      body: JSON.stringify({ ...askForm, require_citations: true }),
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

  async function loadPage(path) {
    const page = await api(`/api/internal/wiki/pages/${encodePath(path)}`);
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

  async function updateReview(id, status) {
    await api(`/api/internal/reviews/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status, note: `管理端${translateReviewItemStatus(status)}` }),
    });
    showToast(status === "approved" ? "已通过审阅" : "已驳回审阅");
    await refresh();
  }

  async function updateGap(id, status) {
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

  const stats = useMemo(
    () => [
      ["资料", sources.length],
      ["采集报告", reports.length],
      ["知识页", pages.length],
      ["知识缺口", gaps.length],
      ["待审阅", reviews.length],
    ],
    [sources, reports, pages, gaps, reviews],
  );
  const active = NAV_ITEMS.find((item) => item.id === activeSection) || NAV_ITEMS[0];
  const filteredSources = useMemo(
    () => filterRows(sources, searchText, (source) => [
      source.title,
      source.id,
      source.original_path,
      source.owner,
      source.metadata?.normalized_path,
    ]),
    [sources, searchText],
  );
  const filteredPages = useMemo(
    () => filterRows(pages, searchText, (page) => [page.title, page.path, page.page_type, page.review_status]),
    [pages, searchText],
  );
  const filteredGaps = useMemo(
    () => filterRows(gaps, searchText, (gap) => [gap.question, gap.status, gap.priority, gap.owner]),
    [gaps, searchText],
  );
  const sectionCounts = {
    overview: sources.length + pages.length + gaps.length,
    ingest: reports.length,
    sources: sources.length,
    wiki: pages.length,
    qa: gaps.length,
    reviews: reviews.length,
  };

  return (
    <div className="wiki-shell">
      <header className="wiki-topbar">
        <div className="topbar-brand">
          <div className="brand-mark">LG</div>
          <div>
            <strong>知识库核心</strong>
            <span>产品客服知识空间 · 飞书知识库式内部空间</span>
          </div>
        </div>
        <div className="global-search">
          <span>搜索</span>
          <input
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
            placeholder="搜索知识页、资料、缺口"
          />
        </div>
        <div className="topbar-actions">
          <span className="sync-state"><span className="status-dot"></span>内部核心服务</span>
          <button className="secondary" onClick={() => setActiveSection("ingest")}>上传</button>
          <button className="secondary" onClick={() => refresh().catch((error) => showToast(error.message))}>刷新</button>
        </div>
      </header>

      <div className="wiki-layout">
        <aside className="app-rail" aria-label="知识库快捷入口">
          <div className="rail-logo">知</div>
          {APP_RAIL_ITEMS.map((item) => (
            <button
              key={item.id}
              className={`rail-button ${activeSection === item.id ? "active" : ""}`}
              title={item.label}
              onClick={() => setActiveSection(item.id)}
              type="button"
            >
              {item.icon}
            </button>
          ))}
        </aside>

        <aside className="space-sidebar">
          <div className="space-header">
            <span className="space-avatar">知</span>
            <div>
              <strong>产品客服知识库</strong>
              <small>{sources.length} 份资料 · {pages.length} 个知识页</small>
            </div>
          </div>

          <nav className="nav-list">
            {NAV_ITEMS.map((item) => (
              <button
                key={item.id}
                className={`nav-item ${activeSection === item.id ? "active" : ""}`}
                onClick={() => setActiveSection(item.id)}
                type="button"
              >
                <span className="nav-icon">{item.icon}</span>
                <span className="nav-copy">
                  <span>{item.label}</span>
                  <small>{item.desc}</small>
                </span>
                <span className="nav-count">{sectionCounts[item.id] || 0}</span>
              </button>
            ))}
          </nav>

          <div className="tree-section">
            <div className="tree-title">空间目录</div>
            <button className="tree-item" onClick={() => setActiveSection("wiki")} type="button">
              <span className="tree-caret">›</span>
              <span>政策规则</span>
              <small>{countByPageType(pages, "policy")}</small>
            </button>
            <button className="tree-item" onClick={() => setActiveSection("wiki")} type="button">
              <span className="tree-caret">›</span>
              <span>常见问题</span>
              <small>{countByPageType(pages, "faq")}</small>
            </button>
            <button className="tree-item" onClick={() => setActiveSection("wiki")} type="button">
              <span className="tree-caret">›</span>
              <span>功能说明</span>
              <small>{countByPageType(pages, "feature")}</small>
            </button>
            <button className="tree-item" onClick={() => setActiveSection("qa")} type="button">
              <span className="tree-caret">›</span>
              <span>待补充缺口</span>
              <small>{gaps.length}</small>
            </button>
          </div>
        </aside>

        <main className="task-area">
          <header className="task-header">
            <div>
              <p className="eyebrow">产品客服知识库 / {active.label}</p>
              <h1>{active.label}</h1>
              <p>{active.desc}</p>
            </div>
            <div className="header-meta">
              <span className="pill">产品知识</span>
              <span className="pill">内部可见</span>
            </div>
          </header>

          <div className="content-canvas">
            {activeSection === "overview" && <Overview stats={stats} reports={reports} pages={filteredPages} gaps={filteredGaps} ragStatus={ragStatus} />}
            {activeSection === "ingest" && (
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
            )}
            {activeSection === "sources" && (
              <SourcesTask
                sources={filteredSources}
                reports={reports}
                selectedSourceId={selectedSourceId}
                setSelectedSourceId={setSelectedSourceId}
                sourcePreview={sourcePreview}
                sourcePreviewLoading={sourcePreviewLoading}
                loadSourcePreview={loadSourcePreview}
                deleteSource={deleteSource}
                showToast={showToast}
              />
            )}
            {activeSection === "wiki" && (
              <WikiTask pages={filteredPages} editor={editor} setEditor={setEditor} loadPage={loadPage} savePage={savePage} markPageStale={markPageStale} showToast={showToast} />
            )}
            {activeSection === "qa" && (
              <QaTask
                askForm={askForm}
                setAskForm={setAskForm}
                ask={ask}
                answer={answer}
                feedback={feedback}
                setFeedback={setFeedback}
                createGap={createGap}
                gaps={filteredGaps}
                updateGap={updateGap}
                showToast={showToast}
              />
            )}
            {activeSection === "reviews" && <ReviewsTask reviews={reviews} updateReview={updateReview} showToast={showToast} />}
          </div>
        </main>

        <KnowledgeAside
          active={active}
          sources={sources}
          pages={pages}
          gaps={gaps}
          ragStatus={ragStatus}
          selectedSource={sources.find((source) => source.id === selectedSourceId)}
          sourcePreview={sourcePreview}
          setActiveSection={setActiveSection}
          syncPostgresRag={syncPostgresRag}
          showToast={showToast}
        />
      </div>

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function Overview({ stats, reports, pages, gaps, ragStatus }) {
  return (
    <section className="task-grid">
      <Panel title="核心指标" badge="实时">
        <div className="stats">
          {stats.map(([label, value]) => (
            <div className="stat" key={label}><strong>{value}</strong><span>{label}</span></div>
          ))}
        </div>
      </Panel>
      <Panel title="最近采集报告" badge={reports.length}>
        <List rows={reports.slice(0, 6)} empty="暂无采集报告" render={(report) => (
          <Item
            title={`${report.source_id} · ${translateParser(report.parser)}`}
            meta={[`${report.chunk_count} 个分块`, `${report.char_count} 个字符`, report.normalized_path]}
          />
        )} />
      </Panel>
      <Panel title="知识页状态" badge={pages.length}>
        <List rows={pages.slice(0, 6)} empty="暂无知识页" render={(page) => (
          <Item title={page.title} meta={[translatePageType(page.page_type), translateReviewStatus(page.review_status), page.path]} />
        )} />
      </Panel>
      <Panel title="待处理缺口" badge={gaps.length}>
        <List rows={gaps.slice(0, 6)} empty="暂无知识缺口" render={(gap) => (
          <Item title={gap.question} meta={[translateGapStatus(gap.status), translatePriority(gap.priority), gap.owner || "未分配"]} />
        )} />
      </Panel>
      <Panel title="RAG 索引状态" badge={ragStatus?.rag_store_backend || "sqlite"}>
        <div className="stats rag-stats">
          <div className="stat"><strong>{ragStatus?.chunk_count || 0}</strong><span>检索分块</span></div>
          <div className="stat"><strong>{ragStatus?.source_count || 0}</strong><span>已索引资料</span></div>
          <div className="stat"><strong>{ragStatus?.database_backend || "sqlite"}</strong><span>元数据存储</span></div>
          <div className="stat"><strong>{ragStatus?.postgres?.port || 54322}</strong><span>Pg 目标端口</span></div>
        </div>
      </Panel>
      <Panel title="外部系统接入状态" badge="未接入">
        <div className="integration-grid">
          <span>企业微信：未接入</span>
          <span>钉钉：未接入</span>
          <span>飞书：未接入</span>
          <span>业务系统写回：未接入</span>
        </div>
      </Panel>
    </section>
  );
}

function IngestTask({ scanForm, setScanForm, scan, compileWiki, reports, showToast, uploadFiles, setUploadFiles, uploadAndIngest }) {
  return (
    <section className="task-stack">
      <Panel title="上传资料" badge="推荐">
        <div className="upload-box">
          <strong>选择文件后点击上传并标准化</strong>
          <span>支持 PDF、Word、PPT、Excel、Markdown、TXT、CSV、JSON。纸质材料请先扫描为 PDF 或图片；图片 OCR 将在后续接入 OCR 引擎。</span>
          <input
            type="file"
            multiple
            onChange={(event) => setUploadFiles(Array.from(event.target.files || []))}
          />
          <div className="file-list">
            {uploadFiles.length ? uploadFiles.map((file) => <span className="pill" key={file.name}>{file.name}</span>) : <span>未选择文件</span>}
          </div>
        </div>
        <div className="actions">
          <button onClick={() => uploadAndIngest().catch((error) => showToast(error.message))}>上传并标准化</button>
          <button className="secondary" onClick={() => compileWiki().catch((error) => showToast(error.message))}>编译知识页</button>
        </div>
      </Panel>

      <Panel title="采集设置" badge="默认值">
        <Field label="本地目录">
          <input value={scanForm.root_path} onChange={(e) => setScanForm({ ...scanForm, root_path: e.target.value })} />
        </Field>
        <div className="grid-2">
          <Field label="业务域">
            <select value={scanForm.domain} onChange={(e) => setScanForm({ ...scanForm, domain: e.target.value })}>
              <option value="product">产品知识</option>
              <option value="customer_service">客服知识</option>
            </select>
          </Field>
          <Field label="负责人">
            <input value={scanForm.owner} onChange={(e) => setScanForm({ ...scanForm, owner: e.target.value })} />
          </Field>
        </div>
        <Field label="权限标签">
          <input value={scanForm.acl_tags} onChange={(e) => setScanForm({ ...scanForm, acl_tags: e.target.value })} />
        </Field>
        <Field label="默认元数据 JSON">
          <textarea rows="4" value={scanForm.metadata_defaults} onChange={(e) => setScanForm({ ...scanForm, metadata_defaults: e.target.value })} />
        </Field>
        <div className="actions">
          <button className="secondary" onClick={() => scan().catch((error) => showToast(error.message))}>扫描目录</button>
          <button className="secondary" onClick={() => compileWiki().catch((error) => showToast(error.message))}>编译知识页</button>
        </div>
      </Panel>
      <Panel title="解析与标准化报告" badge={reports.length}>
        <ReportTable reports={reports} />
      </Panel>
    </section>
  );
}

function SourcesTask({
  sources,
  reports,
  selectedSourceId,
  setSelectedSourceId,
  sourcePreview,
  sourcePreviewLoading,
  loadSourcePreview,
  deleteSource,
  showToast,
}) {
  const reportBySource = new Map(reports.map((report) => [report.source_id, report]));
  const selected = sources.find((source) => source.id === selectedSourceId) || sources[0];
  const selectedReport = selected ? reportBySource.get(selected.id) : null;
  return (
    <section className="source-workspace">
      <Panel title="资料目录" badge={sources.length}>
        <List rows={sources} empty="暂无资料" render={(source) => {
          const report = reportBySource.get(source.id);
          return (
            <button
              className={`source-row ${selected?.id === source.id ? "active" : ""}`}
              onClick={() => setSelectedSourceId(source.id)}
              type="button"
            >
              <span className="file-icon">{sourceFileKind(source.source_type)}</span>
              <span>
                <strong>{source.title}</strong>
                <small>{translateSourceType(source.source_type)} · {report ? `${report.chunk_count} 个分块` : "未分块"}</small>
              </span>
            </button>
          );
        }} />
      </Panel>

      <section className="source-reader">
        {selected ? (
          <>
            <div className="reader-toolbar">
              <div>
                <p className="eyebrow">资料库 / 标准化预览</p>
                <h2>{selected.title}</h2>
                <div className="meta">
                  <span className="pill">{translateSourceType(selected.source_type)}</span>
                  <span className="pill">{translateParser(selected.metadata?.parser)}</span>
                  <span className="pill">{formatBytes(selected.size_bytes)}</span>
                </div>
              </div>
              <div className="row-actions">
                <button className="secondary" onClick={() => loadSourcePreview(selected.id).catch((error) => showToast(error.message))}>重新预览</button>
                <button className="danger" onClick={() => deleteSource(selected.id).catch((error) => showToast(error.message))}>删除资料</button>
              </div>
            </div>
            <pre className="source-preview">
              {sourcePreviewLoading
                ? "正在读取标准化内容..."
                : sourcePreview?.id === selected.id
                  ? `${sourcePreview.content}${sourcePreview.truncated ? "\n\n... 内容较长，已截断预览 ..." : ""}`
                  : "请选择资料后查看标准化内容。"}
            </pre>
            <Panel title="资料属性" badge={selected.id}>
              <div className="detail-grid source-detail-grid">
                <Detail label="业务域" value={translateDomain(selected.domain)} />
                <Detail label="负责人" value={selected.owner || "未分配"} />
                <Detail label="状态" value={translateSourceStatus(selected.status)} />
                <Detail label="权限标签" value={formatAclTags(selected.metadata?.acl_tags)} />
                <Detail label="原始路径" value={selected.original_path} />
                <Detail label="预览文件" value={sourcePreview?.id === selected.id ? sourcePreview.preview_path : selected.metadata?.normalized_path} />
                <Detail label="JSONL 分块" value={selected.metadata?.jsonl_path} />
                <Detail label="是否 OCR" value={formatBool(selected.metadata?.ocr_used)} />
                <Detail label="警告" value={formatWarnings(selected.metadata?.warnings || sourcePreview?.warnings)} />
                <Detail label="分块数" value={String(selected.metadata?.chunk_count || selectedReport?.chunk_count || 0)} />
              </div>
              <details className="metadata-disclosure">
                <summary>完整元数据</summary>
                <pre>{formatMetadata(selected.metadata?.cleaned || selected.metadata || {})}</pre>
              </details>
            </Panel>
          </>
        ) : (
          <Panel title="资料详情" badge="未选择"><p>暂无资料。</p></Panel>
        )}
      </section>
    </section>
  );
}

function WikiTask({ pages, editor, setEditor, loadPage, savePage, markPageStale, showToast }) {
  return (
    <section className="split-task">
      <Panel title="知识页列表" badge={pages.length}>
        <List rows={pages} empty="暂无知识页" render={(page) => (
          <Item title={page.title} meta={[translatePageType(page.page_type), translateReviewStatus(page.review_status), page.path]}>
            <button onClick={() => loadPage(page.path).catch((error) => showToast(error.message))}>编辑</button>
            <button className="warn" onClick={() => markPageStale(page.path).catch((error) => showToast(error.message))}>标记过期</button>
          </Item>
        )} />
      </Panel>
      <Panel title="Markdown 编辑区" badge={editor.path || "未选择"}>
        <Field label="页面路径">
          <input value={editor.path} readOnly />
        </Field>
        <div className="grid-2">
          <Field label="状态">
            <select value={editor.review_status} onChange={(e) => setEditor({ ...editor, review_status: e.target.value })}>
              <option value="draft">草稿</option>
              <option value="reviewed">已审阅</option>
              <option value="stale">已过期</option>
              <option value="rejected">已驳回</option>
            </select>
          </Field>
          <Field label="负责人">
            <input value={editor.owner} onChange={(e) => setEditor({ ...editor, owner: e.target.value })} />
          </Field>
        </div>
        <textarea className="markdown-editor" value={editor.content} onChange={(e) => setEditor({ ...editor, content: e.target.value })} spellCheck="false" />
        <div className="actions">
          <button onClick={() => savePage().catch((error) => showToast(error.message))}>保存知识页</button>
          <button className="warn" onClick={() => markPageStale().catch((error) => showToast(error.message))}>标记过期</button>
        </div>
      </Panel>
    </section>
  );
}

function QaTask({ askForm, setAskForm, ask, answer, feedback, setFeedback, createGap, gaps, updateGap, showToast }) {
  return (
    <section className="split-task">
      <Panel title="内部问答" badge={answer ? translateConfidence(answer.confidence) : "引用回答"}>
        <Field label="问题">
          <textarea rows="4" value={askForm.question} onChange={(e) => setAskForm({ ...askForm, question: e.target.value })} />
        </Field>
        <div className="grid-2">
          <Field label="业务域">
            <select value={askForm.domain} onChange={(e) => setAskForm({ ...askForm, domain: e.target.value })}>
              <option value="product">产品知识</option>
              <option value="customer_service">客服知识</option>
            </select>
          </Field>
          <Field label="回答模式">
            <select value={askForm.answer_mode} onChange={(e) => setAskForm({ ...askForm, answer_mode: e.target.value })}>
              <option value="detail">详细回答</option>
              <option value="short">简短回答</option>
              <option value="customer_reply_draft">客服回复草稿</option>
            </select>
          </Field>
        </div>
        <button onClick={() => ask().catch((error) => showToast(error.message))}>提问</button>
        <pre className="answer">{answer ? formatAnswer(answer) : "暂无回答"}</pre>
        <div className="grid-2">
          <Field label="反馈结果">
            <select value={feedback.rating} onChange={(e) => setFeedback({ ...feedback, rating: e.target.value })}>
              <option value="partial">部分可用</option>
              <option value="bad">不可用</option>
              <option value="good">可用</option>
            </select>
          </Field>
          <button className="secondary" onClick={() => createGap().catch((error) => showToast(error.message))}>生成缺口</button>
        </div>
        <Field label="反馈说明">
          <input value={feedback.comment} onChange={(e) => setFeedback({ ...feedback, comment: e.target.value })} />
        </Field>
      </Panel>
      <Panel title="知识缺口队列" badge={gaps.length}>
        <List rows={gaps} empty="暂无知识缺口" render={(gap) => (
          <Item title={gap.question} meta={[`缺口 ID：${gap.id}`, translateGapStatus(gap.status), translatePriority(gap.priority), gap.owner || "未分配"]}>
            <button onClick={() => updateGap(gap.id, "in_progress").catch((error) => showToast(error.message))}>处理</button>
            <button onClick={() => updateGap(gap.id, "resolved").catch((error) => showToast(error.message))}>解决</button>
            <button className="danger" onClick={() => updateGap(gap.id, "rejected").catch((error) => showToast(error.message))}>关闭</button>
          </Item>
        )} />
      </Panel>
    </section>
  );
}

function ReviewsTask({ reviews, updateReview, showToast }) {
  return (
    <Panel title="审阅队列" badge={reviews.length}>
      <List rows={reviews} empty="暂无待审阅项" render={(review) => (
        <Item title={review.page_path} meta={[`审阅 ID：${review.id}`, translateReviewIssueType(review.issue_type), translateReviewItemStatus(review.status)]}>
          <button onClick={() => updateReview(review.id, "approved").catch((error) => showToast(error.message))}>通过</button>
          <button className="danger" onClick={() => updateReview(review.id, "rejected").catch((error) => showToast(error.message))}>驳回</button>
        </Item>
      )} />
    </Panel>
  );
}

function ReportTable({ reports }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>资料 ID</th>
            <th>解析器</th>
            <th>分块数</th>
            <th>标准化文件</th>
            <th>警告</th>
          </tr>
        </thead>
        <tbody>
          {reports.map((report) => (
            <tr key={report.id}>
              <td>{report.source_id}</td>
              <td>{translateParser(report.parser)}</td>
              <td>{report.chunk_count}</td>
              <td>{report.normalized_path}</td>
              <td>{formatWarnings(report.warnings)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function KnowledgeAside({
  active,
  sources,
  pages,
  gaps,
  ragStatus,
  selectedSource,
  sourcePreview,
  setActiveSection,
  syncPostgresRag,
  showToast,
}) {
  const activeGaps = gaps.filter((gap) => gap.status !== "resolved" && gap.status !== "rejected").length;
  return (
    <aside className="knowledge-aside">
      <section className="aside-card">
        <div className="aside-title">
          <h2>{active.label}</h2>
          <span className="pill">内部空间</span>
        </div>
        <div className="aside-metrics">
          <Metric label="资料" value={sources.length} />
          <Metric label="知识页" value={pages.length} />
          <Metric label="待补充" value={activeGaps} />
        </div>
      </section>

      <section className="aside-card">
        <div className="aside-title">
          <h2>索引状态</h2>
          <span className="pill">{ragStatus?.rag_store_backend || "sqlite"}</span>
        </div>
        <div className="status-list">
          <StatusLine label="检索分块" value={ragStatus?.chunk_count || 0} state="ok" />
          <StatusLine label="已索引资料" value={ragStatus?.source_count || 0} state="ok" />
          <StatusLine label="元数据存储" value={ragStatus?.database_backend || "sqlite"} state="idle" />
          <StatusLine label="Pg 目标端口" value={ragStatus?.postgres?.port || 54322} state="idle" />
        </div>
        <button className="secondary full-button" onClick={() => syncPostgresRag().catch((error) => showToast(error.message))}>
          同步 PostgreSQL RAG
        </button>
      </section>

      <section className="aside-card">
        <div className="aside-title">
          <h2>当前资料</h2>
          <span className="pill">{selectedSource ? translateSourceType(selectedSource.source_type) : "未选择"}</span>
        </div>
        {selectedSource ? (
          <div className="current-source">
            <strong>{selectedSource.title}</strong>
            <span>{selectedSource.original_path}</span>
            <small>{sourcePreview?.id === selectedSource.id ? `${sourcePreview.char_count} 个字符` : "等待预览"}</small>
          </div>
        ) : (
          <p>尚未选择资料。</p>
        )}
      </section>

      <section className="aside-card">
        <div className="aside-title">
          <h2>外部接入</h2>
          <span className="pill muted-pill">未接入</span>
        </div>
        <div className="integration-stack">
          <StatusLine label="企业微信 / OIDC" value="后续" state="idle" />
          <StatusLine label="钉钉" value="后续" state="idle" />
          <StatusLine label="飞书" value="后续" state="idle" />
          <StatusLine label="业务系统写回" value="人工确认后接入" state="idle" />
        </div>
      </section>

      <section className="aside-card quick-card">
        <button onClick={() => setActiveSection("ingest")}>上传资料</button>
        <button className="secondary" onClick={() => setActiveSection("qa")}>测试问答</button>
      </section>
    </aside>
  );
}

function Metric({ label, value }) {
  return (
    <div className="metric">
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}

function StatusLine({ label, value, state }) {
  return (
    <div className="status-line">
      <span className={`tiny-dot ${state || "idle"}`}></span>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Panel({ title, badge, children }) {
  return (
    <section className="panel">
      <div className="panel-title">
        <h2>{title}</h2>
        <span className="pill">{badge}</span>
      </div>
      {children}
    </section>
  );
}

function Field({ label, children }) {
  return <label>{label}{children}</label>;
}

function List({ rows, empty, render }) {
  return <div className="list">{rows.length ? rows.map((row) => <React.Fragment key={row.id || row.path}>{render(row)}</React.Fragment>) : <div className="item"><span>{empty}</span></div>}</div>;
}

function Item({ title, meta, children }) {
  return (
    <div className="item">
      <div className="item-title">{title}</div>
      <div className="meta">{meta.filter(Boolean).map((value, index) => <span className="pill" key={`${value}-${index}`}>{String(value)}</span>)}</div>
      {children && <div className="row-actions">{children}</div>}
    </div>
  );
}

function Detail({ label, value, pre = false }) {
  const displayValue = value === undefined || value === null || value === "" ? "-" : value;
  return (
    <div className="detail-item">
      <span>{label}</span>
      {pre ? <pre>{displayValue}</pre> : <strong>{displayValue}</strong>}
    </div>
  );
}

function filterRows(rows, keyword, fieldsOf) {
  const q = String(keyword || "").trim().toLowerCase();
  if (!q) return rows;
  return rows.filter((row) => fieldsOf(row).some((value) => String(value || "").toLowerCase().includes(q)));
}

function sourceFileKind(type) {
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
  })[type] || "FILE";
}

function splitTags(value) {
  return value.split(",").map((x) => x.trim()).filter(Boolean);
}

function safeJson(value, fallback) {
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}

function encodePath(path) {
  return encodeURIComponent(path).replaceAll("%2F", "/");
}

function formatAnswer(answer) {
  return `${answer.answer}\n\n引用：\n${answer.citations.map((c) => `- ${c.wiki_page || "未生成知识页"}（资料 ID：${c.source_id}）\n  ${c.snippet}`).join("\n")}`;
}

function formatAclTags(tags) {
  const values = Array.isArray(tags) ? tags : [];
  return values.length ? values.map(translateAclTag).join("、") : "内部";
}

function formatBool(value) {
  return value ? "是" : "否";
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} 字节`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function formatWarnings(warnings) {
  const values = Array.isArray(warnings) ? warnings : [];
  return values.length ? values.map(translateWarning).join("、") : "-";
}

function formatMetadata(metadata) {
  return JSON.stringify(localizeMetadata(metadata), null, 2);
}

function localizeMetadata(value) {
  if (Array.isArray(value)) return value.map(localizeMetadata);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [translateMetadataKey(key), localizeMetadataValue(key, item)]));
}

function localizeMetadataValue(key, value) {
  if (key === "domain") return translateDomain(value);
  if (key === "parser") return translateParser(value);
  if (key === "ocr_used") return formatBool(value);
  if (key === "acl_tags" && Array.isArray(value)) return value.map(translateAclTag);
  if (key === "warnings" && Array.isArray(value)) return value.map(translateWarning);
  return localizeMetadata(value);
}

function translateDomain(value) {
  return ({
    product: "产品知识",
    customer_service: "客服知识",
  })[value] || value || "-";
}

function translateSourceStatus(value) {
  return ({
    active: "可用",
    deleted: "已删除",
  })[value] || value || "可用";
}

function translateSourceType(value) {
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
  })[value] || value || "-";
}

function translateParser(value) {
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
  })[value] || value || "-";
}

function translatePageType(value) {
  return ({
    faq: "常见问题",
    feature: "功能说明",
    known_issue: "已知问题",
    policy: "政策规则",
    index: "索引",
  })[value] || value || "-";
}

function translateReviewStatus(value) {
  return ({
    draft: "草稿",
    reviewed: "已审阅",
    stale: "已过期",
    rejected: "已驳回",
  })[value] || value || "-";
}

function translateGapStatus(value) {
  return ({
    open: "待处理",
    in_progress: "处理中",
    resolved: "已解决",
    rejected: "已关闭",
  })[value] || value || "-";
}

function translateReviewItemStatus(value) {
  return ({
    pending: "待审阅",
    approved: "已通过",
    rejected: "已驳回",
    resolved: "已解决",
  })[value] || value || "-";
}

function translateReviewIssueType(value) {
  return ({
    new_page: "新增知识页",
    changed_page: "知识页变更",
    conflict: "内容冲突",
    stale: "内容过期",
    missing_citation: "缺少引用",
  })[value] || value || "-";
}

function translatePriority(value) {
  return ({
    low: "低优先级",
    medium: "中优先级",
    high: "高优先级",
  })[value] || value || "-";
}

function translateConfidence(value) {
  return ({
    low: "低置信度",
    medium: "中置信度",
    high: "高置信度",
  })[value] || value || "-";
}

function translateAclTag(value) {
  return ({
    internal: "内部",
    "内部": "内部",
    product: "产品",
    "产品": "产品",
    customer_service: "客服",
    "客服": "客服",
    upload: "上传",
    "上传": "上传",
  })[value] || value || "-";
}

function translateWarning(value) {
  return ({
    empty_body: "正文为空",
    legacy_office_format: "旧版 Office 格式",
    pdf_text_empty: "PDF 未提取到文本",
    ocr_engine_not_configured: "OCR 引擎未配置",
    ocr_disabled: "OCR 未启用",
  })[value] || value || "-";
}

function translateMetadataKey(value) {
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
  })[value] || value;
}

function countByPageType(pages, type) {
  return pages.filter((page) => page.page_type === type).length;
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
