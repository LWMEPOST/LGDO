import { Field, Item, List, Panel } from "../components/common";
import type { AskResponse, KnowledgeGap } from "../types";
import { formatAnswer, translateConfidence, translateGapStatus, translatePriority } from "../utils/format";

export function QaTask({
  askForm,
  setAskForm,
  ask,
  answer,
  feedback,
  setFeedback,
  createGap,
  gaps,
  updateGap,
  showToast,
}: {
  askForm: Record<string, string>;
  setAskForm: (form: Record<string, string>) => void;
  ask: () => Promise<void>;
  answer: AskResponse | null;
  feedback: Record<string, string>;
  setFeedback: (feedback: Record<string, string>) => void;
  createGap: () => Promise<void>;
  gaps: KnowledgeGap[];
  updateGap: (id: string, status: string) => Promise<void>;
  showToast: (message: string) => void;
}) {
  return (
    <section className="split-task">
      <Panel title="内部问答" badge={answer ? translateConfidence(answer.confidence) : "引用回答"}>
        <Field label="问题">
          <textarea rows={4} value={askForm.question} onChange={(e) => setAskForm({ ...askForm, question: e.target.value })} />
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
