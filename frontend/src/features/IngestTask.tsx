import { Field, Panel } from "../components/common";
import type { IngestReport } from "../types";
import { translateParser, formatWarnings } from "../utils/format";

export function IngestTask({
  scanForm,
  setScanForm,
  scan,
  compileWiki,
  reports,
  showToast,
  uploadFiles,
  setUploadFiles,
  uploadAndIngest,
}: {
  scanForm: Record<string, string>;
  setScanForm: (form: Record<string, string>) => void;
  scan: () => Promise<void>;
  compileWiki: () => Promise<void>;
  reports: IngestReport[];
  showToast: (message: string) => void;
  uploadFiles: File[];
  setUploadFiles: (files: File[]) => void;
  uploadAndIngest: () => Promise<void>;
}) {
  const selectedSize = uploadFiles.reduce((total, file) => total + file.size, 0);

  return (
    <section className="task-stack">
      <div className="ingest-steps">
        <StepCard step="1" title="上传本地资料" desc="PDF、Word、PPT、表格、Markdown、TXT、CSV、JSON。" active />
        <StepCard step="2" title="标准化与分块" desc="生成 Markdown、JSONL 和 RAG 检索分块。" />
        <StepCard step="3" title="编译知识页" desc="沉淀到资料库和知识页，进入人工审阅。" />
      </div>

      <section className="import-workbench">
        <Panel title="上传资料" badge="推荐入口">
          <label className="drop-zone">
            <input type="file" multiple onChange={(event) => setUploadFiles(Array.from(event.target.files || []))} />
            <span className="drop-icon">+</span>
            <strong>选择文件上传并标准化</strong>
            <small>支持 PDF、Word、PPT、Excel、Markdown、TXT、CSV、JSON。纸质材料请先扫描为 PDF；图片 OCR 后续接入。</small>
          </label>

          <div className="upload-summary">
            <div>
              <strong>{uploadFiles.length}</strong>
              <span>已选文件</span>
            </div>
            <div>
              <strong>{formatUploadSize(selectedSize)}</strong>
              <span>合计大小</span>
            </div>
          </div>

          <div className="file-list">
            {uploadFiles.length ? uploadFiles.map((file) => (
              <span className="file-chip" key={`${file.name}-${file.size}`}>
                <span>{file.name}</span>
                <small>{formatUploadSize(file.size)}</small>
              </span>
            )) : <span className="empty-tip">还没有选择文件</span>}
          </div>

          <div className="actions">
            <button onClick={() => uploadAndIngest().catch((error) => showToast(error.message))}>上传并标准化</button>
            <button className="secondary" onClick={() => compileWiki().catch((error) => showToast(error.message))}>编译知识页</button>
          </div>
        </Panel>

        <Panel title="入库信息" badge="默认应用到本次导入">
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
          <Field label="来源元数据">
            <textarea rows={4} value={scanForm.metadata_defaults} onChange={(e) => setScanForm({ ...scanForm, metadata_defaults: e.target.value })} />
          </Field>
        </Panel>
      </section>

      <Panel title="目录扫描" badge="高级入口">
        <div className="scan-row">
          <Field label="本地目录">
            <input value={scanForm.root_path} onChange={(e) => setScanForm({ ...scanForm, root_path: e.target.value })} />
          </Field>
          <button className="secondary" onClick={() => scan().catch((error) => showToast(error.message))}>扫描目录</button>
        </div>
      </Panel>
      <Panel title="解析与标准化报告" badge={reports.length}>
        <ReportTable reports={reports} />
      </Panel>
    </section>
  );
}

function StepCard({ step, title, desc, active = false }: { step: string; title: string; desc: string; active?: boolean }) {
  return (
    <div className={`step-card ${active ? "active" : ""}`}>
      <span>{step}</span>
      <div>
        <strong>{title}</strong>
        <small>{desc}</small>
      </div>
    </div>
  );
}

function formatUploadSize(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function ReportTable({ reports }: { reports: IngestReport[] }) {
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
