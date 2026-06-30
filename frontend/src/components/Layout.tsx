import type { ReactNode } from "react";

import { APP_RAIL_ITEMS, NAV_ITEMS, type SectionId } from "../constants";
import type { AuthUser, KnowledgeGap, RagStatus, SourcePreview, SourceRecord, SpaceDirectoryGroup, SpaceFilter, WikiPage } from "../types";
import { translateSourceType } from "../utils/format";

interface LayoutProps {
  activeSection: SectionId;
  setActiveSection: (section: SectionId) => void;
  searchText: string;
  setSearchText: (value: string) => void;
  sources: SourceRecord[];
  pages: WikiPage[];
  gaps: KnowledgeGap[];
  scopedSources: SourceRecord[];
  scopedPages: WikiPage[];
  scopedGaps: KnowledgeGap[];
  directory: SpaceDirectoryGroup[];
  activeSpaceFilter: SpaceFilter;
  selectSpaceFilter: (filter: SpaceFilter) => void;
  clearSpaceFilter: () => void;
  reportCount: number;
  reviewCount: number;
  ragStatus: RagStatus | null;
  selectedSource?: SourceRecord;
  sourcePreview: SourcePreview | null;
  refresh: () => Promise<void>;
  syncPostgresRag: () => Promise<void>;
  showToast: (message: string) => void;
  currentUser: AuthUser | null;
  logout: () => Promise<void>;
  children: ReactNode;
}

export function Layout({
  activeSection,
  setActiveSection,
  searchText,
  setSearchText,
  sources,
  pages,
  gaps,
  scopedSources,
  scopedPages,
  scopedGaps,
  directory,
  activeSpaceFilter,
  selectSpaceFilter,
  clearSpaceFilter,
  reportCount,
  reviewCount,
  ragStatus,
  selectedSource,
  sourcePreview,
  refresh,
  syncPostgresRag,
  showToast,
  currentUser,
  logout,
  children,
}: LayoutProps) {
  const active = NAV_ITEMS.find((item) => item.id === activeSection) || NAV_ITEMS[0];
  const canManageAccounts = currentUser?.role === "admin" || currentUser?.acl_tags?.includes("*");
  const visibleRailItems = APP_RAIL_ITEMS.filter((item) => item.id !== "accounts" || canManageAccounts);
  const visibleNavItems = NAV_ITEMS.filter((item) => item.id !== "accounts" || canManageAccounts);
  const sectionCounts = {
    overview: sources.length + pages.length + gaps.length,
    ingest: reportCount,
    sources: sources.length,
    wiki: pages.length,
    qa: gaps.length,
    gaps: gaps.length,
    reviews: reviewCount,
    accounts: canManageAccounts ? 1 : 0,
  };
  const spaceFilters = directory.flatMap((group) => group.items);
  const activeDomain = activeSpaceFilter.kind === "domain" ? activeSpaceFilter.value : "";
  const domainPills = [
    { value: "product", label: "产品知识" },
    { value: "customer_service", label: "客服知识" },
    { value: "administration", label: "行政知识" },
  ].map((domain) => ({
    ...domain,
    filter: spaceFilters.find((filter) => filter.kind === "domain" && filter.value === domain.value),
    active: activeDomain === domain.value,
  }));

  return (
    <div className="wiki-shell">
      <header className="wiki-topbar">
        <div className="topbar-brand">
          <div className="brand-mark">L</div>
          <div className="brand-copy">
            <span className="brand-kicker">LGDO Console</span>
            <strong>知识工作台</strong>
            <span>内部核心空间 · 产品、客服与行政</span>
          </div>
        </div>
        <div className="global-search">
          <span className="search-icon">⌕</span>
          <input
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
            placeholder="搜索资料、知识页、问题或负责人"
          />
        </div>
        <div className="topbar-actions">
          <span className="sync-state"><span className="status-dot"></span><span>{currentUser?.username || currentUser?.user_id || "未登录"} · {currentUser?.role || "viewer"}</span></span>
          <button className="primary-action" onClick={() => setActiveSection("ingest")}>导入</button>
          <button className="secondary" onClick={() => refresh().catch((error) => showToast(error.message))}>刷新</button>
          <button className="secondary" onClick={() => logout().catch((error) => showToast(error.message))}>退出</button>
        </div>
      </header>

      <div className="wiki-layout">
        <aside className="app-rail" aria-label="知识库快捷入口">
          <div className="rail-logo">L</div>
          {visibleRailItems.map((item) => (
            <button
              key={item.id}
              className={`rail-button ${activeSection === item.id ? "active" : ""}`}
              title={item.label}
              onClick={() => setActiveSection(item.id as SectionId)}
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
              <strong>核心知识库</strong>
              <small>{activeSpaceFilter.label} · {activeSpaceFilter.count} 项 · 内部可见</small>
            </div>
          </div>
          <button className="new-page-button" onClick={() => setActiveSection("ingest")} type="button">
            <span className="new-page-icon">+</span>
            <strong>导入资料</strong>
          </button>

          <nav className="nav-list">
            {visibleNavItems.map((item) => (
              <button
                key={item.id}
                className={`nav-item ${activeSection === item.id ? "active" : ""}`}
                onClick={() => setActiveSection(item.id)}
                aria-current={activeSection === item.id ? "page" : undefined}
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

          <SpaceTree
            directory={directory}
            activeSpaceFilter={activeSpaceFilter}
            selectSpaceFilter={selectSpaceFilter}
            clearSpaceFilter={clearSpaceFilter}
          />

          <div className="tree-section muted-tree">
            <div className="tree-title">外部连接</div>
            <TreeItem label="企业微信 OIDC" count={0} muted onClick={() => showToast("第二阶段接入：当前先完成内部核心功能")} />
            <TreeItem label="钉钉 / 飞书" count={0} muted onClick={() => showToast("第二阶段接入：当前先完成内部核心功能")} />
            <TreeItem label="业务系统写回" count={0} muted onClick={() => showToast("后续接入，写回前需要使用者人工确认")} />
          </div>
        </aside>

        <main className="task-area">
          <header className="task-header">
            <div className="task-heading">
              <p className="eyebrow">核心知识库 / {active.label}</p>
              <h1>{active.label}</h1>
              <p>{active.desc}</p>
            </div>
            <div className="header-meta" aria-label="知识域标签">
              {domainPills.map((domain) => (
                <button
                  key={domain.value}
                  className={`pill domain-pill ${domain.active ? "strong-pill active" : ""} ${domain.filter ? "" : "muted-pill"}`}
                  type="button"
                  aria-pressed={domain.active}
                  onClick={() => domain.filter ? selectSpaceFilter(domain.filter) : showToast(`${domain.label}暂无内容`)}
                >
                  {domain.label}
                </button>
              ))}
              <button
                className="pill muted-pill domain-pill"
                type="button"
                onClick={() => showToast("外部平台会在第二阶段接入")}
              >
                外部平台未接入
              </button>
            </div>
          </header>
          <div className="content-canvas">{children}</div>
        </main>

        <KnowledgeAside
          activeLabel={active.label}
          sources={scopedSources}
          pages={scopedPages}
          gaps={scopedGaps}
          activeSpaceFilter={activeSpaceFilter}
          ragStatus={ragStatus}
          selectedSource={selectedSource}
          sourcePreview={sourcePreview}
          setActiveSection={setActiveSection}
          syncPostgresRag={syncPostgresRag}
          showToast={showToast}
        />
      </div>
    </div>
  );
}

function SpaceTree({
  directory,
  activeSpaceFilter,
  selectSpaceFilter,
  clearSpaceFilter,
}: {
  directory: SpaceDirectoryGroup[];
  activeSpaceFilter: SpaceFilter;
  selectSpaceFilter: (filter: SpaceFilter) => void;
  clearSpaceFilter: () => void;
}) {
  return (
    <div className="space-tree" aria-label="空间目录">
      {directory.map((group) => (
        <div className="tree-section" key={group.id}>
          <div className="tree-title">{group.label}</div>
          {group.items.map((item) => (
            <div key={item.id}>
              <TreeItem
                label={item.label}
                desc={item.desc}
                count={item.count}
                active={activeSpaceFilter.id === item.id}
                onClick={() => selectSpaceFilter(item)}
              />
            </div>
          ))}
        </div>
      ))}
      {activeSpaceFilter.id !== "all" && (
        <button className="clear-tree-filter" type="button" onClick={clearSpaceFilter}>
          清除目录筛选
        </button>
      )}
    </div>
  );
}

function TreeItem({
  label,
  desc,
  count,
  active = false,
  muted = false,
  onClick,
}: {
  label: string;
  desc?: string;
  count: number;
  active?: boolean;
  muted?: boolean;
  onClick: () => void;
}) {
  return (
    <button className={`tree-item ${muted ? "muted" : ""} ${active ? "active" : ""}`} onClick={onClick} type="button">
      <span className="tree-caret">⌄</span>
      <span>
        <strong>{label}</strong>
        {desc && <small className="tree-desc">{desc}</small>}
      </span>
      <small>{count}</small>
    </button>
  );
}

function KnowledgeAside({
  activeLabel,
  sources,
  pages,
  gaps,
  activeSpaceFilter,
  ragStatus,
  selectedSource,
  sourcePreview,
  setActiveSection,
  syncPostgresRag,
  showToast,
}: {
  activeLabel: string;
  sources: SourceRecord[];
  pages: WikiPage[];
  gaps: KnowledgeGap[];
  activeSpaceFilter: SpaceFilter;
  ragStatus: RagStatus | null;
  selectedSource?: SourceRecord;
  sourcePreview: SourcePreview | null;
  setActiveSection: (section: SectionId) => void;
  syncPostgresRag: () => Promise<void>;
  showToast: (message: string) => void;
}) {
  const activeGaps = gaps.filter((gap) => gap.status !== "resolved" && gap.status !== "rejected").length;
  return (
    <aside className="knowledge-aside">
      <section className="aside-card">
        <div className="aside-title">
          <h2>{activeLabel}</h2>
          <span className="pill strong-pill">{activeSpaceFilter.label}</span>
        </div>
        <p className="aside-context">{activeSpaceFilter.desc} · 当前视图</p>
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
          <StatusLine label="向量分块" value={ragStatus?.embedding_count ?? 0} state={ragStatus?.embedding_count ? "ok" : "idle"} />
          <StatusLine label="向量后端" value={ragStatus?.vector_backend || "jsonb"} state={ragStatus?.pgvector_enabled ? "ok" : "idle"} />
          <StatusLine label="PgVector 列" value={ragStatus?.vector_count ?? 0} state={ragStatus?.vector_count ? "ok" : "idle"} />
          <StatusLine label="元数据存储" value={ragStatus?.database_backend || "sqlite"} state="idle" />
          <StatusLine label="Pg 目标端口" value={ragStatus?.postgres?.port || 5432} state="idle" />
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
          <StatusLine label="企业微信 OIDC" value="第二阶段" state="idle" />
          <StatusLine label="钉钉" value="第二阶段" state="idle" />
          <StatusLine label="飞书" value="第二阶段" state="idle" />
          <StatusLine label="业务写回" value="人工确认后" state="idle" />
        </div>
      </section>

      <section className="aside-card quick-card">
        <button onClick={() => setActiveSection("ingest")}>上传资料</button>
        <button className="secondary" onClick={() => setActiveSection("qa")}>测试问答</button>
      </section>
    </aside>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="metric">
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}

function StatusLine({ label, value, state }: { label: string; value: ReactNode; state: string }) {
  return (
    <div className="status-line">
      <span className={`tiny-dot ${state || "idle"}`}></span>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
