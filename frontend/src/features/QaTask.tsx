import { Field, Panel } from "../components/common";
import type { AskResponse } from "../types";
import { formatAnswer, translateConfidence } from "../utils/format";

export function QaTask({
  askForm,
  setAskForm,
  ask,
  answer,
  feedback,
  setFeedback,
  createGap,
  showToast,
}: {
  askForm: Record<string, string>;
  setAskForm: (form: Record<string, string>) => void;
  ask: () => Promise<void>;
  answer: AskResponse | null;
  feedback: Record<string, string>;
  setFeedback: (feedback: Record<string, string>) => void;
  createGap: () => Promise<void>;
  showToast: (message: string) => void;
}) {
  return (
    <section className="qa-workspace">
      <Panel title="内部问答" badge={answer ? translateConfidence(answer.confidence) : "引用回答"}>
        <div className="qa-panel-grid">
          <div className="qa-compose">
            <Field label="问题">
              <textarea rows={6} value={askForm.question} onChange={(e) => setAskForm({ ...askForm, question: e.target.value })} />
            </Field>
            <div className="grid-2">
              <Field label="业务域">
                <select value={askForm.domain} onChange={(e) => setAskForm({ ...askForm, domain: e.target.value })}>
                  <option value="product">产品知识</option>
                  <option value="customer_service">客服知识</option>
                  <option value="administration">行政知识</option>
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
            <div className="grid-3">
              <Field label="用户 ID">
                <input value={askForm.user_id || ""} onChange={(e) => setAskForm({ ...askForm, user_id: e.target.value })} />
              </Field>
              <Field label="角色">
                <select value={askForm.role || "viewer"} onChange={(e) => setAskForm({ ...askForm, role: e.target.value })}>
                  <option value="admin">管理员</option>
                  <option value="editor">编辑者</option>
                  <option value="viewer">查看者</option>
                </select>
              </Field>
              <Field label="权限标签">
                <input value={askForm.acl_tags || ""} onChange={(e) => setAskForm({ ...askForm, acl_tags: e.target.value })} />
              </Field>
            </div>
            <button className="qa-submit" onClick={() => ask().catch((error) => showToast(error.message))}>提问</button>
          </div>
          <div className="qa-result">
            <div className="qa-result-title">
              <strong>回答</strong>
              <span>{answer ? translateConfidence(answer.confidence) : "等待提问"}</span>
            </div>
            <pre className="answer">{answer ? formatAnswer(answer) : "暂无回答"}</pre>
            <div className="qa-feedback">
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
          </div>
        </div>
      </Panel>
    </section>
  );
}
