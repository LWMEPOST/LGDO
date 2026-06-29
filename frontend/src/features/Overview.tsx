import { Item, List, Panel } from "../components/common";
import type { IngestReport, KnowledgeGap, RagStatus, WikiPage } from "../types";
import { translateGapStatus, translatePageType, translateParser, translatePriority, translateReviewStatus } from "../utils/format";

export function Overview({
  stats,
  reports,
  pages,
  gaps,
  ragStatus,
}: {
  stats: Array<[string, number]>;
  reports: IngestReport[];
  pages: WikiPage[];
  gaps: KnowledgeGap[];
  ragStatus: RagStatus | null;
}) {
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
          <div className="stat"><strong>{ragStatus?.embedding_count ?? 0}</strong><span>向量分块</span></div>
          <div className="stat"><strong>{ragStatus?.vector_backend || "jsonb"}</strong><span>向量后端</span></div>
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
