import { Item, List, Panel } from "../components/common";
import type { IngestReport, KnowledgeGap, RagStatus, SourceRecord, SpaceDirectoryGroup, SpaceFilter, WikiPage } from "../types";
import { translateGapStatus, translatePageType, translateParser, translatePriority, translateReviewStatus } from "../utils/format";
import { countPagesByType, countSourcesByBucket } from "../utils/space";

export function Overview({
  stats,
  reports,
  pages,
  gaps,
  sources,
  activeSpaceFilter,
  ragStatus,
  directory,
  selectSpaceFilter,
}: {
  stats: Array<[string, number]>;
  reports: IngestReport[];
  pages: WikiPage[];
  gaps: KnowledgeGap[];
  sources: SourceRecord[];
  activeSpaceFilter: SpaceFilter;
  ragStatus: RagStatus | null;
  directory: SpaceDirectoryGroup[];
  selectSpaceFilter: (filter: SpaceFilter) => void;
}) {
  const sourceBuckets = countSourcesByBucket(sources);
  const pageBuckets = countPagesByType(pages);
  const quickFilters = directory.flatMap((group) => group.items).filter((item) => item.id !== "all" && item.count > 0).slice(0, 8);

  return (
    <section className="overview-workspace">
      <Panel title="当前空间" badge={activeSpaceFilter.label}>
        <div className="space-hero">
          <div>
            <p className="eyebrow">空间目录 / {activeSpaceFilter.desc}</p>
            <h2>{activeSpaceFilter.label}</h2>
            <p>当前视图会同步过滤资料库、知识页、缺口和采集报告，便于围绕一个分类做整理和校对。</p>
          </div>
          <div className="scope-score">
            <strong>{activeSpaceFilter.count}</strong>
            <span>目录项内容</span>
          </div>
        </div>
        <div className="stats">
          {stats.map(([label, value]) => (
            <div className="stat" key={label}><strong>{value}</strong><span>{label}</span></div>
          ))}
        </div>
      </Panel>

      <Panel title="空间目录快捷入口" badge={quickFilters.length}>
        <div className="directory-grid">
          {quickFilters.map((filter) => (
            <button className="directory-card" key={filter.id} onClick={() => selectSpaceFilter(filter)} type="button">
              <span>{filter.desc}</span>
              <strong>{filter.label}</strong>
              <small>{filter.count} 项</small>
            </button>
          ))}
        </div>
      </Panel>

      <Panel title="资料分类" badge={`${sourceBuckets.all} 份`}>
        <div className="classification-strip">
          <ClassStat label="文档" value={sourceBuckets.document} />
          <ClassStat label="数据表" value={sourceBuckets.data} />
          <ClassStat label="演示稿" value={sourceBuckets.deck} />
          <ClassStat label="有警告" value={sourceBuckets.warning} tone={sourceBuckets.warning ? "warn" : "ok"} />
        </div>
      </Panel>

      <Panel title="知识页分类" badge={`${pageBuckets.all} 页`}>
        <div className="classification-strip">
          <ClassStat label="政策" value={pageBuckets.policy} />
          <ClassStat label="FAQ" value={pageBuckets.faq} />
          <ClassStat label="功能" value={pageBuckets.feature} />
          <ClassStat label="过期" value={pageBuckets.stale} tone={pageBuckets.stale ? "warn" : "ok"} />
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
          <div className="stat"><strong>{ragStatus?.embedding_count ?? 0}</strong><span>向量分块</span></div>
          <div className="stat"><strong>{ragStatus?.vector_backend || "jsonb"}</strong><span>向量后端</span></div>
          <div className="stat"><strong>{ragStatus?.database_backend || "sqlite"}</strong><span>元数据存储</span></div>
          <div className="stat"><strong>{ragStatus?.postgres?.port || 5432}</strong><span>Pg 目标端口</span></div>
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

function ClassStat({ label, value, tone = "idle" }: { label: string; value: number; tone?: "idle" | "ok" | "warn" }) {
  return (
    <div className={`class-stat ${tone}`}>
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}
