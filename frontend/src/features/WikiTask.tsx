import { ExternalLink, RefreshCw } from "lucide-react";

import { Field, Item, List, Panel } from "../components/common";
import type { AuthUser, EditorState, SpaceFilter, VaultStatus, WikiPage } from "../types";
import { translatePageType, translateReviewStatus } from "../utils/format";
import { countPagesByType } from "../utils/space";

export function WikiTask({
  pages,
  activeSpaceFilter,
  clearSpaceFilter,
  editor,
  setEditor,
  loadPage,
  savePage,
  markPageStale,
  openInObsidian,
  requestReconcile,
  vaultStatus,
  currentUser,
  showToast,
}: {
  pages: WikiPage[];
  activeSpaceFilter: SpaceFilter;
  clearSpaceFilter: () => void;
  editor: EditorState;
  setEditor: (editor: EditorState) => void;
  loadPage: (path: string) => Promise<void>;
  savePage: () => Promise<void>;
  markPageStale: (path?: string) => Promise<void>;
  openInObsidian: (path: string) => Promise<void>;
  requestReconcile: () => Promise<void>;
  vaultStatus: VaultStatus | null;
  currentUser: AuthUser;
  showToast: (message: string) => void;
}) {
  const buckets = countPagesByType(pages);
  const canReconcile = currentUser.role === "admin" || currentUser.role === "owner" || currentUser.acl_tags.includes("*");
  const syncTone = vaultStatus === null ? "unknown" : vaultStatus.degraded ? "warn" : "ok";
  const syncLabel = vaultStatus === null
    ? "同步状态未知"
    : !vaultStatus.configured
      ? "同步未启用"
      : vaultStatus.degraded
        ? "同步异常"
        : vaultStatus.running
          ? "同步运行中"
          : "同步已停止";
  return (
    <section className="wiki-workspace">
      <Panel title="知识页列表" badge={pages.length}>
        <div className="source-filter-bar">
          <span>{activeSpaceFilter.label}</span>
          <small>{activeSpaceFilter.desc}</small>
          {activeSpaceFilter.id !== "all" && <button className="link-button" onClick={clearSpaceFilter} type="button">清除</button>}
        </div>
        <div className={`sync-status ${syncTone}`}>
          <div className="sync-status-copy">
            <strong>{syncLabel}</strong>
            {vaultStatus && (
              <div className="sync-status-metrics">
                <span>{vaultStatus.pending_occurrences} 个待处理事件</span>
                <span>{vaultStatus.open_issues} 个同步问题</span>
              </div>
            )}
          </div>
          {canReconcile && (
            <button
              aria-label="立即对账"
              className="icon-button"
              onClick={() => requestReconcile().catch((error) => showToast(error instanceof Error ? error.message : "Vault 对账失败"))}
              title="立即对账"
              type="button"
            >
              <RefreshCw aria-hidden="true" size={16} />
            </button>
          )}
        </div>
        <div className="wiki-type-strip">
          <TypeStat label="全部" value={buckets.all} />
          <TypeStat label="政策" value={buckets.policy} />
          <TypeStat label="FAQ" value={buckets.faq} />
          <TypeStat label="功能" value={buckets.feature} />
          <TypeStat label="草稿" value={buckets.draft} />
          <TypeStat label="过期" value={buckets.stale} tone={buckets.stale ? "warn" : "idle"} />
        </div>
        <List rows={pages} empty="暂无知识页" render={(page) => (
          <Item title={page.title} meta={[translatePageType(page.page_type), translateReviewStatus(page.review_status), page.domain, page.path]}>
            <button
              aria-label="在 Obsidian 中打开"
              className="icon-button"
              onClick={() => openInObsidian(page.path).catch((error) => showToast(error instanceof Error ? error.message : "无法打开 Obsidian"))}
              title="在 Obsidian 中打开"
              type="button"
            >
              <ExternalLink aria-hidden="true" size={16} />
            </button>
            <button onClick={() => loadPage(page.path).catch((error) => showToast(error.message))}>编辑</button>
            <button className="warn" onClick={() => markPageStale(page.path).catch((error) => showToast(error.message))}>标记过期</button>
          </Item>
        )} />
      </Panel>
      <Panel title="Markdown 编辑区" badge={editor.path || "未选择"}>
        <div className="editor-path-row">
          <Field label="页面路径">
            <input value={editor.path} readOnly />
          </Field>
          <button
            aria-label="在 Obsidian 中打开"
            className="icon-button"
            disabled={!editor.path}
            onClick={() => editor.path && openInObsidian(editor.path).catch((error) => showToast(error instanceof Error ? error.message : "无法打开 Obsidian"))}
            title="在 Obsidian 中打开"
            type="button"
          >
            <ExternalLink aria-hidden="true" size={16} />
          </button>
        </div>
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

function TypeStat({ label, value, tone = "idle" }: { label: string; value: number; tone?: "idle" | "warn" }) {
  return (
    <div className={`wiki-type ${tone}`}>
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}
