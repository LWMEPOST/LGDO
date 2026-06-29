import { Item, List, Panel } from "../components/common";
import type { KnowledgeGap } from "../types";
import { translateGapStatus, translatePriority } from "../utils/format";

export function GapsTask({
  gaps,
  updateGap,
  showToast,
}: {
  gaps: KnowledgeGap[];
  updateGap: (id: string, status: string) => Promise<void>;
  showToast: (message: string) => void;
}) {
  const activeCount = gaps.filter((gap) => gap.status !== "resolved" && gap.status !== "rejected").length;

  return (
    <section className="gaps-workspace">
      <Panel title="缺口队列" badge={`${activeCount} 个待处理`}>
        <div className="gap-summary">
          <div>
            <strong>{gaps.length}</strong>
            <span>当前视图缺口</span>
          </div>
          <div>
            <strong>{activeCount}</strong>
            <span>仍需补充</span>
          </div>
          <div>
            <strong>{gaps.length - activeCount}</strong>
            <span>已关闭或解决</span>
          </div>
        </div>
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
