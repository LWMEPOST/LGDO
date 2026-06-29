# LGDO Knowledge Core

LGDO Knowledge Core 是一个面向产品、客服和售后场景的内部知识库核心系统。项目围绕“资料入库、标准化、知识页编译、RAG 检索、引用问答、人工审阅、知识缺口闭环”构建，适合把分散的产品文档、客服 FAQ、售后政策、历史工单和办公制度沉淀成可追溯、可评测、可维护的知识资产。

当前版本定位为内部 MVP：先保证本地文件和上传资料的知识库闭环稳定，再逐步接入企业微信、钉钉、飞书、OIDC、CRM 或客服系统写回等第二阶段能力。

## 项目说明

这个项目解决的是企业内部知识“散、旧、难查、难追溯”的问题。传统知识库通常只解决文档存放，LGDO Knowledge Core 更关注后续的工程闭环：

- 原始资料保留证据链，LLM 或规则生成的知识页只是衍生产物。
- 回答必须带引用，能追溯到 source、wiki page 和原文片段。
- 知识页支持审阅、编辑和状态流转，避免一次性导入后无人维护。
- 用户反馈可以生成知识缺口，推动资料补齐和知识更新。
- 评测问题可持续回归，用数据判断检索和回答质量。

适用场景：

- 产品知识库：PRD、版本记录、API 文档、竞品资料、FAQ。
- 客服辅助：售后政策、标准话术、历史工单、常见问题。
- 行政制度问答：报销、采购、考勤、远程办公、办公规范。
- 内部试用型 RAG 平台：验证资料解析、chunk 策略、引用准确率和知识维护流程。

## 技术栈

| 层级 | 技术/组件 | 说明 |
|------|-----------|------|
| 后端服务 | FastAPI, Uvicorn, Pydantic v2 | 内部 API、文件上传、问答、评测、迁移接口 |
| 配置管理 | pydantic-settings | `.env` 驱动 SQLite/PostgreSQL/DeepSeek/GBrain 配置 |
| 文档解析 | PyMuPDF, python-docx, openpyxl, python-pptx | PDF、Word、Excel、PPT 等资料解析 |
| 标准化 | Markdown, JSONL, frontmatter | 生成 raw、normalized、jsonl、wiki 等可追溯产物 |
| 默认数据库 | SQLite | 本地轻量试用和快速开发 |
| 可选数据库 | PostgreSQL | 主业务元数据和 RAG chunk 表 |
| 可选向量扩展 | pgvector | PostgreSQL RAG 表的向量列和 ivfflat cosine 索引 |
| 检索策略 | local-hash-v1 + keyword hybrid ranking | 无外部 embedding 服务时也能稳定检索 |
| 可选生成模型 | DeepSeek API | 生成式回答；未配置时回退本地抽取式回答 |
| 可选记忆层 | GBrain MCP adapter | 长期记忆、外部检索和 agent 知识入口 |
| 前端控制台 | React 18, TypeScript, Vite | 高密度内部管理台 |
| 测试 | pytest, httpx | 后端接口、解析、RAG、迁移、GBrain adapter 测试 |

## 核心能力

- 资料导入：本地目录扫描、多文件上传、hash 去重、删除同步清理索引。
- 多格式解析：支持 Markdown、TXT、CSV、JSON、PDF、DOCX、XLSX、PPTX；旧版 Office 文件会提示转换。
- 标准化管线：生成 `vault/raw`、`vault/normalized`、`vault/jsonl`、`ingest_reports`。
- 知识页编译：生成 Obsidian 兼容 Markdown wiki，支持读取、保存、状态更新。
- RAG 问答：优先检索 `document_chunks`，返回 citations、confidence、missing_info、retrieval_strategy。
- 审阅队列：记录新增/更新页面和待处理审阅项。
- 知识缺口：反馈可生成 gap，并支持优先级、负责人、关联页面和状态流转。
- 评测闭环：支持添加评测问题、运行基础评测、输出引用率和回答覆盖情况。
- 存储迁移：支持 SQLite 主业务数据迁移到 PostgreSQL。
- 管理控制台：提供资料库、上传、知识页、问答、审阅和状态概览。

## 性能与评测

以下是本地开发环境中的参考基准，不代表生产 SLA。测试环境使用 PostgreSQL + pgvector + DeepSeek，数据集来自本地 RAG 测试集。

| 指标 | 结果 |
|------|------|
| 测试文件数 | 334 |
| 去重文档数 | 91 |
| 数据体积 | 7.64 MB |
| RAG chunks | 377 |
| 向量后端 | PostgreSQL pgvector |
| Wiki 编译 | 334 个 source 编译为 93 个 wiki 页面，约 6.02s |
| Vault 产物 | 1100 个文件，包含 raw、normalized、jsonl、wiki、index、logs |
| QA 测试 | 30/30 correct |
| source top accuracy | 1.0 |
| source any accuracy | 1.0 |
| answer point accuracy | 1.0 |
| DeepSeek QA 平均延迟 | 7827 ms |
| DeepSeek QA P95 延迟 | 11226 ms |

解析与索引参考：

| 格式 | 文件数 | parser | chars/s 范围 | chunks/s 范围 | warnings |
|------|--------|--------|---------------|----------------|----------|
| TXT / Markdown | 162 | text | 4943 - 8907 | 9.7 - 11.6 | 0 |
| DOCX | 81 | docx | 4324 - 7458 | 8.7 - 8.9 | 0 |
| PDF | 81 | pdf-text | 4391 - 7341 | 9.1 - 10.3 | 0 |
| PPTX | 10 | pptx | 3747 | 7.8 | 0 |

当前自动化检查：

- 后端测试：`35 passed`
- 前端生产构建：通过
- 前端构建产物参考：JS 约 `178.65 kB`，CSS 约 `17.22 kB`

## 系统架构

```text
本地资料 / 上传文件
  -> 文档解析 parser
  -> 元数据清洗与 canonical Markdown
  -> JSONL chunks
  -> SQLite / PostgreSQL RAG 索引
  -> Wiki 编译与审阅
  -> 引用问答 / 反馈 / 知识缺口 / 评测
```

### 数据分层

| 层级 | 路径/表 | 作用 |
|------|---------|------|
| 原始证据层 | `uploads/`, `vault/raw/`, `sources` | 保存原始文件、解析文本、hash、来源和权限标签 |
| 标准化层 | `vault/normalized/`, `vault/jsonl/`, `ingest_reports` | 保存 canonical Markdown、检索 chunks 和采集报告 |
| 知识衍生层 | `vault/wiki/`, `wiki_pages`, `review_items` | 生成可读可审阅的 Markdown 知识页 |
| 检索层 | `document_chunks`, `rag_document_chunks` | 支撑引用问答和 RAG 状态统计 |
| 反馈治理层 | `query_logs`, `feedback`, `knowledge_gaps`, `audit_logs` | 记录问答、反馈、缺口和审计信息 |

## 快速启动

### 1. 后端

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
uvicorn app.main:app --reload
```

启动后访问：

- API 文档：http://127.0.0.1:8000/docs
- 控制台：http://127.0.0.1:8000/console
- 健康检查：http://127.0.0.1:8000/health

### 2. 前端开发模式

```powershell
cd frontend
npm install
npm run dev
```

Vite 会把 `/api` 代理到 `http://127.0.0.1:8000`。

### 3. 前端生产构建

```powershell
cd frontend
npm install
npm run build
cd ..
uvicorn app.main:app --reload
```

FastAPI 会优先托管 `frontend/dist`，访问 `/console` 即可打开 React 控制台。没有构建产物时，会回退到 `app/static/` 下的旧版静态控制台。

## 部署说明

### 方案一：SQLite 单机试用

适合本地开发、小规模演示和功能验证。

```text
DATABASE_BACKEND=sqlite
DATABASE_PATH=data/lgdo.db
RAG_STORE_BACKEND=sqlite
VAULT_PATH=vault
UPLOAD_PATH=uploads
```

启动：

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 方案二：PostgreSQL + pgvector 试用

适合多人试用、RAG 数据量增加、需要更接近生产形态的部署。

```text
DATABASE_BACKEND=postgres
RAG_STORE_BACKEND=postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=54322
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DATABASE=lgdo
```

系统会自动创建 `sources`、`wiki_pages`、`review_items`、`knowledge_gaps`、`query_logs`、`ingest_reports`、`document_chunks`、`audit_logs` 等主业务表。RAG 表会尝试启用 `pgvector`；如果扩展不可用，会回退到 JSONB embedding 存储。

### 方案三：生产构建托管

```powershell
cd frontend
npm ci
npm run build
cd ..
pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

生产部署建议：

- `.env` 不提交到仓库，通过服务器环境变量或密钥管理系统注入。
- `data/`、`uploads/`、`vault/` 放到可备份的数据盘。
- PostgreSQL 定期备份，`vault/` 和 `uploads/` 做文件级备份。
- 对外暴露前建议放在 Nginx、Caddy 或企业网关后面。
- 多人试用前补齐最小用户上下文和 ACL 检索过滤。

## 环境变量

### DeepSeek

不配置 DeepSeek 时，系统使用本地抽取式回答，适合验证资料入库、检索和引用链路。

```text
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=
```

### OCR

OCR 目前是预留通道。`OCR_ENABLED=true` 时，扫描件或空文本 PDF 会进入 OCR 占位流程并记录 warning；后续可接 PaddleOCR 或 Tesseract。

```text
OCR_ENABLED=false
```

### GBrain

GBrain 默认关闭。需要接入长期记忆或 MCP 检索时再开启。

```text
GBRAIN_ENABLED=false
GBRAIN_ENDPOINT=
GBRAIN_API_KEY=
GBRAIN_HOME=data/gbrain
GBRAIN_REPO_PATH=gbrain
GBRAIN_COMMAND=bun
```

## 使用示例

扫描示例资料：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/internal/sources/scan `
  -ContentType 'application/json' `
  -Body '{"root_path":"samples/product_service","domain":"product","owner":"admin","acl_tags":["internal","product"]}'
```

编译知识页：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/internal/wiki/compile `
  -ContentType 'application/json' `
  -Body '{"domain":"product"}'
```

发起问答：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/internal/ask `
  -ContentType 'application/json' `
  -Body '{"question":"如何处理退款问题？","domain":"product","require_citations":true}'
```

SQLite 数据迁移到 PostgreSQL：

```powershell
Invoke-RestMethod -Method Post `
  "http://127.0.0.1:8000/api/internal/database/migrate-sqlite-to-postgres?sqlite_path=data/lgdo.db"
```

## 常用接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/api/internal/sources/scan` | 扫描本地目录资料 |
| POST | `/api/internal/sources/upload` | 上传并标准化资料 |
| GET | `/api/internal/sources` | 查看资料元数据 |
| DELETE | `/api/internal/sources/{source_id}` | 删除资料并清理关联索引 |
| GET | `/api/internal/sources/{source_id}/preview` | 查看标准化 Markdown 预览 |
| GET | `/api/internal/ingest/reports` | 查看采集、解析和分块报告 |
| POST | `/api/internal/wiki/compile` | 编译 Markdown 知识页 |
| GET | `/api/internal/wiki/pages` | 查看知识页列表 |
| GET | `/api/internal/wiki/pages/{page_path}` | 读取知识页内容 |
| PUT | `/api/internal/wiki/pages/{page_path}` | 保存知识页内容 |
| PATCH | `/api/internal/wiki/pages/{page_path}/status` | 更新知识页状态 |
| GET | `/api/internal/reviews` | 查看审阅队列 |
| PATCH | `/api/internal/reviews/{review_id}` | 更新审阅项 |
| GET | `/api/internal/gaps` | 查看知识缺口 |
| PATCH | `/api/internal/gaps/{gap_id}` | 更新知识缺口 |
| POST | `/api/internal/ask` | 内部问答 |
| POST | `/api/internal/feedback` | 提交反馈并可生成知识缺口 |
| POST | `/api/internal/eval/questions` | 添加评测问题 |
| POST | `/api/internal/eval/run` | 运行基础评测 |
| GET | `/api/internal/rag/status` | 查看 RAG 和可选集成状态 |
| POST | `/api/internal/rag/sync-postgres` | 将 SQLite chunks 同步到 PostgreSQL RAG 表 |
| POST | `/api/internal/database/migrate-sqlite-to-postgres` | 迁移 SQLite 主业务数据到 PostgreSQL |

## 项目结构

```text
app/                 FastAPI 后端、解析、入库、RAG、问答、Wiki 编译
frontend/            React + Vite 管理端
tests/               后端测试
samples/             示例产品/客服知识资料
scripts/             评测和样例数据脚本
docs/                方案设计和代码审查记录
gbrain/              可选 GBrain 集成源码/依赖目录
app/static/          旧版静态控制台回退资源
```

## 数据与 Git 忽略

运行后会生成以下本地目录，通常不应提交：

| 路径 | 说明 |
|------|------|
| `data/` | SQLite 数据库、GBrain 本地数据等运行数据 |
| `uploads/` | 用户上传的原始文件 |
| `vault/raw/` | 原始解析文本 |
| `vault/normalized/` | 标准化 Markdown |
| `vault/jsonl/` | 检索/评测用 JSONL chunks |
| `vault/wiki/` | 生成的 Obsidian 兼容知识页 |
| `vault/logs/` | 入库、问答、反馈和评测日志 |
| `output/` | 本地 benchmark 输出 |
| `frontend/dist/` | 前端生产构建产物 |
| `node_modules/` | Node 依赖 |

当前 `.gitignore` 已排除 `.env`、`data/`、`vault/`、`uploads/`、`output/`、`node_modules/`、`dist/` 等运行产物。

## 测试

后端：

```powershell
python -m pytest -q
```

前端：

```powershell
cd frontend
npm run build
```

## 当前边界

- 当前版本优先服务内部知识库试点，不直接面向客户自动回复。
- `acl_tags` 已进入资料元数据，但正式多人权限过滤和统一登录仍属于后续加固项。
- OCR 通道已预留，扫描件或图片型 PDF 需要后续接入 PaddleOCR 或 Tesseract。
- 企业微信、钉钉、飞书、OIDC、CRM/客服系统写回属于第二阶段能力。
- 高风险业务决策需要人工确认，不能仅依赖模型回答。

## Roadmap

- P0：最小用户上下文、ACL 检索过滤、citation 权限校验。
- P0：真实资料入库质量报告和问题资料队列。
- P1：评测体系升级，扩展引用准确率、命中率、拒答质量、答案要点覆盖率。
- P1：知识缺口与审阅体验优化，支持筛选、关联 source/page、处理闭环。
- P1：部署、备份、恢复和日志治理。
- P2：OCR 引擎接入、table-aware chunks、PDF 页码引用。

## License

待补充。
