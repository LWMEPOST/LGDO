import { Field, Item, List, Panel } from "../components/common";
import type { EditorState, WikiPage } from "../types";
import { translatePageType, translateReviewStatus } from "../utils/format";

export function WikiTask({
  pages,
  editor,
  setEditor,
  loadPage,
  savePage,
  markPageStale,
  showToast,
}: {
  pages: WikiPage[];
  editor: EditorState;
  setEditor: (editor: EditorState) => void;
  loadPage: (path: string) => Promise<void>;
  savePage: () => Promise<void>;
  markPageStale: (path?: string) => Promise<void>;
  showToast: (message: string) => void;
}) {
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
