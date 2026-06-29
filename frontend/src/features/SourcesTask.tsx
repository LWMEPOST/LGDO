import { Detail, List, Panel } from "../components/common";
import type { IngestReport, SourcePreview, SourceRecord, SpaceFilter } from "../types";
import {
  formatAclTags,
  formatBool,
  formatBytes,
  formatMetadata,
  formatWarnings,
  sourceFileKind,
  translateDomain,
  translateParser,
  translateSourceStatus,
  translateSourceType,
} from "../utils/format";
import { countSourcesByBucket } from "../utils/space";

export function SourcesTask({
  sources,
  allSources,
  reports,
  activeSpaceFilter,
  clearSpaceFilter,
  selectedSourceId,
  setSelectedSourceId,
  sourcePreview,
  sourcePreviewLoading,
  loadSourcePreview,
  deleteSource,
  showToast,
}: {
  sources: SourceRecord[];
  allSources: SourceRecord[];
  reports: IngestReport[];
  activeSpaceFilter: SpaceFilter;
  clearSpaceFilter: () => void;
  selectedSourceId: string | null;
  setSelectedSourceId: (sourceId: string | null) => void;
  sourcePreview: SourcePreview | null;
  sourcePreviewLoading: boolean;
  loadSourcePreview: (sourceId: string) => Promise<void>;
  deleteSource: (sourceId: string) => Promise<void>;
  showToast: (message: string) => void;
}) {
  const reportBySource = new Map(reports.map((report) => [report.source_id, report]));
  const selected = sources.find((source) => source.id === selectedSourceId) || sources[0];
  const selectedReport = selected ? reportBySource.get(selected.id) : null;
  const buckets = countSourcesByBucket(sources);
  return (
    <section className="source-workspace">
      <Panel title="资料目录" badge={`${sources.length} 份`}>
        <div className="source-filter-bar">
          <span>{activeSpaceFilter.label}</span>
          <small>{sources.length}/{allSources.length} 份 · 按上方搜索框过滤</small>
          {activeSpaceFilter.id !== "all" && <button className="link-button" onClick={clearSpaceFilter} type="button">清除</button>}
        </div>
        <div className="source-bucket-strip">
          <Bucket label="全部" value={buckets.all} />
          <Bucket label="文档" value={buckets.document} />
          <Bucket label="数据" value={buckets.data} />
          <Bucket label="演示" value={buckets.deck} />
          <Bucket label="警告" value={buckets.warning} tone={buckets.warning ? "warn" : "idle"} />
        </div>
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
                <small>{translateSourceType(source.source_type)} · {translateDomain(source.domain)} · {report ? `${report.chunk_count} 个分块` : "未分块"} · {source.owner || "未分配"}</small>
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
                  <span className="pill">{selected.owner || "未分配"}</span>
                </div>
              </div>
              <div className="row-actions">
                <button className="secondary" onClick={() => loadSourcePreview(selected.id).catch((error) => showToast(error.message))}>重新预览</button>
                <button className="danger" onClick={() => deleteSource(selected.id).catch((error) => showToast(error.message))}>删除资料</button>
              </div>
            </div>
            <div className="document-layout">
              <article className="document-page">
                <div className="doc-titlebar">
                  <span className="file-icon large">{sourceFileKind(selected.source_type)}</span>
                  <div>
                    <h3>{selected.title}</h3>
                    <p>{selected.original_path}</p>
                  </div>
                </div>
                <pre className="source-preview">
                  {sourcePreviewLoading
                    ? "正在读取标准化内容..."
                    : sourcePreview?.id === selected.id
                      ? `${sourcePreview.content}${sourcePreview.truncated ? "\n\n... 内容较长，已截断预览 ..." : ""}`
                      : "请选择资料后查看标准化内容。"}
                </pre>
              </article>

              <aside className="document-inspector">
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
              </aside>
            </div>
          </>
        ) : (
          <Panel title="资料详情" badge="未选择"><p>暂无资料。</p></Panel>
        )}
      </section>
    </section>
  );
}

function Bucket({ label, value, tone = "idle" }: { label: string; value: number; tone?: "idle" | "warn" }) {
  return (
    <div className={`source-bucket ${tone}`}>
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}
