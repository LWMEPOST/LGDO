# LGDO Knowledge Core

LGDO Knowledge Core 是一个面向产品、客服和售后场景的内部知识库 MVP。它把本地资料导入后标准化为可追溯的 Markdown/JSONL 知识资产，并提供带引用的问答、知识页编辑、审阅队列、知识缺口和基础评测能力。

项目当前重点是“内部核心闭环”，适合先用少量真实资料验证资料解析、RAG 检索、引用问答和人工维护流程，再逐步扩展到企业微信、钉钉、飞书、OIDC 或业务系统写回。

## 功能概览

- 资料导入：支持目录扫描和多文件上传。
- 多格式解析：支持 Markdown、TXT、CSV、JSON、PDF、DOCX、XLSX、PPTX。
- 标准化产物：生成 raw 文本、canonical Markdown、JSONL chunks 和采集报告。
- RAG 检索：基于 `document_chunks` 的本地混合检索，使用确定性 embedding `local-hash-v1` + 关键词评分。
- 引用问答：回答优先基于分块检索，并返回来源片段、置信度和缺失信息。
- 知识页管理：编译 Obsidian 兼容 Markdown 知识页，支持在线读取、编辑、保存和状态标记。
- 审阅与缺口：支持待审阅项、知识缺口队列、反馈生成 gap。
- 存储后端：默认可使用 SQLite，也支持 PostgreSQL 主业务元数据和 PostgreSQL RAG 表。
- 可选增强：DeepSeek 生成式回答、GBrain 检索记忆接入、pgvector 向量索引。
- 管理端：React + Vite + TypeScript 控制台，FastAPI 可直接托管生产构建产物。

## 技术栈

| 模块 | 技术 |
|------|------|
| 后端 API | FastAPI, Pydantic, Uvicorn |
| 文档解析 | PyMuPDF, python-docx, openpyxl, python-pptx |
| 默认存储 | SQLite |
| 可选存储 | PostgreSQL, pgvector |
| 前端控制台 | React 18, TypeScript, Vite |
| 可选 LLM | DeepSeek API |
| 可选记忆层 | GBrain |
| 测试 | pytest, httpx |

## 系统流程

```text
本地资料 / 上传文件
  -> 文档解析
  -> 元数据清洗
  -> raw 文本 / normalized Markdown / JSONL chunks
  -> SQLite 或 PostgreSQL chunk 索引
  -> Wiki 编译与审阅
  -> 带引用问答 / 反馈 / 知识缺口 / 评测
```

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

Vite 开发服务会把 `/api` 代理到 `http://127.0.0.1:8000`。

### 3. 前端生产构建

```powershell
cd frontend
npm install
npm run build
cd ..
uvicorn app.main:app --reload
```

FastAPI 会优先托管 `frontend/dist`，访问 `/console` 即可打开新控制台。如果没有构建产物，会回退到 `app/static/` 下的旧版静态控制台。

## 环境配置

复制 `.env.example` 后按本机环境调整。

### 本地轻量模式

如果只想先跑通本地 MVP，可以使用 SQLite：

```text
DATABASE_BACKEND=sqlite
DATABASE_PATH=data/lgdo.db
RAG_STORE_BACKEND=sqlite
VAULT_PATH=vault
UPLOAD_PATH=uploads
```

### PostgreSQL 模式

如果要使用 PostgreSQL 承载主业务表和 RAG 表：

```text
DATABASE_BACKEND=postgres
RAG_STORE_BACKEND=postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=54322
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DATABASE=lgdo
```

系统会自动创建主业务表；RAG 表会尝试启用 `pgvector`。如果环境中没有 pgvector，会回退到 JSONB embedding 存储，核心入库和问答仍可运行。

### DeepSeek 可选配置

不配置 DeepSeek 时，系统会使用本地抽取式回答，适合先验证资料入库、检索和引用链路。

```text
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=
```

模型名建议通过 `.env` 配置，不要写死在代码里。

### GBrain 可选配置

GBrain 默认关闭。需要接入时再配置：

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

## 数据目录

运行后会生成以下本地目录，这些目录通常不应提交到 GitHub：

| 路径 | 说明 |
|------|------|
| `data/` | SQLite 数据库、GBrain 本地数据等运行数据 |
| `uploads/` | 用户上传的原始文件 |
| `vault/raw/` | 原始解析文本 |
| `vault/normalized/` | 标准化 Markdown |
| `vault/jsonl/` | 检索/评测用 JSONL chunks |
| `vault/wiki/` | 生成的 Obsidian 兼容知识页 |
| `vault/reviews/` | 审阅相关产物 |
| `vault/logs/` | 入库、问答、反馈和评测日志 |
| `frontend/dist/` | 前端生产构建产物 |
| `frontend/node_modules/` | 前端依赖 |

当前 `.gitignore` 已排除 `.env`、`data/`、`vault/`、`uploads/`、`frontend/node_modules/`、`frontend/dist/` 等本地运行产物。

## 测试

后端测试：

```powershell
python -m pytest
```

前端构建检查：

```powershell
cd frontend
npm run build
```

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

## 当前边界

- 当前版本优先服务内部知识库试点，不直接面向客户自动回复。
- `acl_tags` 已进入资料元数据，但正式多人权限过滤和统一登录仍属于后续加固项。
- OCR 通道已预留；扫描件或图片型 PDF 需要后续接入 PaddleOCR 或 Tesseract。
- 企业微信、钉钉、飞书、OIDC、CRM/客服系统写回属于第二阶段能力。
- 高风险业务决策场景需要人工确认，不能仅依赖模型回答。

## GitHub 上传前检查

上传前建议确认：

- 不提交 `.env`、真实 API Key、数据库文件、上传资料和真实企业文档。
- 保留 `.env.example`，但其中只放示例值。
- 如仓库要公开，先检查 `samples/`、`docs/` 和 `vault/` 中是否包含敏感信息。
- 如果不准备开源 GBrain 源码或大体积依赖，上传前确认 `gbrain/` 是否需要保留。
- 补充 `LICENSE` 后再正式声明开源协议。

## License

待补充。
