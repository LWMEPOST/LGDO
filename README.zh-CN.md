<p align="center">
  <h1 align="center">LGDO Knowledge Core</h1>
  <p align="center"><strong>🧠 可评测、可追溯、可闭环的内部知识库核心系统</strong></p>
  <p align="center">
    把分散的产品文档、客服 FAQ、售后政策、历史工单和行政制度<br>
    沉淀为带引用、带审阅、带缺口的可维护知识资产。
  </p>
</p>

<p align="center">
  <a href="#-快速开始"><b>🚀 快速开始</b></a> |
  <a href="#-系统架构"><b>🛠️ 系统架构</b></a> |
  <a href="#-能力"><b>🧩 能力</b></a> |
  <a href="#-评测"><b>📊 评测</b></a> |
  <a href="更新说明.md"><b>📝 更新说明</b></a> |
  <a href="#-局限"><b>🚧 局限</b></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11">
  <img src="https://img.shields.io/badge/FastAPI-后端-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/React-18-61DAFB?style=flat-square&logo=react&logoColor=white" alt="React 18">
  <img src="https://img.shields.io/badge/向量数据库-pgvector-336791?style=flat-square&logo=postgresql&logoColor=white" alt="pgvector">
  <img src="https://img.shields.io/badge/知识编译-LLMWiki-0f766e?style=flat-square" alt="LLMWiki">
  <img src="https://img.shields.io/badge/图谱记忆-GBrain-6366f1?style=flat-square" alt="GBrain">
  <img src="https://img.shields.io/badge/生成模型-DeepSeek-111827?style=flat-square" alt="DeepSeek">
  <img src="https://img.shields.io/badge/Wiki工作流-Obsidian-7c3aed?style=flat-square" alt="Obsidian">
  <img src="https://img.shields.io/badge/许可证-Apache_2.0-green?style=flat-square" alt="Apache 2.0">
</p>

<p align="center">
  <a href="README.md">English</a> | <b>简体中文</b>
</p>

## ✨ 简介

企业内部知识最怕三件事：**散在各处找不到、版本过时不可信、答完没人管越来越烂**。

大多数知识库只解决“把文档存起来”，但存起来之后呢？谁来保证这些文档的答案是对的？谁来追踪哪条回答来自哪份文档？谁来判断知识库缺了什么？当用户问了一个刁钻的问题，系统说错了，谁来发现、谁来修？

**LGDO Knowledge Core** 不是又一个文档仓库。它是一个围绕 **资料 -> 知识 -> 答案 -> 缺口 -> 补齐** 的完整闭环设计的知识核心系统。每一份原始资料保留完整证据链，每一条回答必须带引用并能追溯到源文档和原文片段，每一个知识页面支持审阅和状态流转，每一次用户反馈可以生成知识缺口推动补齐，每一道评测问题持续回归并用数据说话。

它不是面向终端客户的自动客服，而是面向**内部知识管理者、产品团队和运营团队**的 RAG 中台：让你知道知识库里有什么、缺什么、答对了吗、答错在哪。

### 🌟 关键特性

|     | 特性 | 说明 |
| --- | ---- | ---- |
| 🔗 | **回答必带引用** | 每条回答追溯至 source -> wiki page -> 原文片段，证据链完整。 |
| 🔍 | **多格式深度解析** | PDF、DOCX、PPTX、XLSX、Markdown、CSV、JSON 全支持，PPT 全量文本提取。 |
| 📊 | **可量化评测体系** | 四层难度测试覆盖跨文档歧义、多跳推理、图谱依赖、否定边界，精确量化检索和回答质量。 |
| 🧠 | **GBrain 知识图谱** | 实体关系遍历、跨文档依赖分析、角色权限聚合，补足纯向量检索做不到的事。 |
| 🔄 | **知识缺口闭环** | 用户反馈 -> 自动生成 gap -> 指定优先级和负责人 -> 追踪补齐状态。 |
| ✅ | **人工审阅队列** | 新增或更新知识页自动进入审阅队列，支持状态流转，避免无人维护。 |
| 🔐 | **本地登录与账户 ACL** | 内置管理员登录、会话 token、账户角色、ACL 标签和受保护的内部控制台接口。 |
| 🗄️ | **双后端存储** | SQLite 单机秒起开发试用；PostgreSQL + pgvector 支撑生产级向量检索。 |
| 🎛️ | **React 管理控制台** | 高密度内部管理台：资料库、上传、知识页、问答、审阅、状态概览。 |

---

## 🚀 快速开始

```bash
git clone https://github.com/LWMEPOST/LGDO.git
cd LGDO

# 后端
python -m venv .venv
source .venv/bin/activate  # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env

# 设置首次管理员密码（不再提供默认 admin/admin）
# AUTH_BOOTSTRAP_ADMIN_PASSWORD=请替换为强密码

# 启动后端
uvicorn app.main:app --reload

# 前端，可选开发模式
cd frontend
npm install
npm run dev -- --port 8011
```

然后在浏览器中打开 `http://127.0.0.1:8000/docs` 查看 API 文档，或访问 `/console` 打开管理控制台。

管理控制台默认启用本地账户登录。只有显式设置 `AUTH_BOOTSTRAP_ADMIN_PASSWORD` 时才会创建初始化管理员；生产环境不要开启 `AUTH_DEV_FALLBACK_ENABLED`。

### 三行命令跑通第一个引用问答

```bash
# 先通过 /api/internal/auth/login 获取 token
export TOKEN='<登录接口返回的 token>'

# 1. 扫描示例资料入库
curl -X POST http://127.0.0.1:8000/api/internal/sources/scan \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"root_path":"samples/product_service","domain":"product","owner":"admin","acl_tags":["internal"]}'

# 2. 编译知识页
curl -X POST http://127.0.0.1:8000/api/internal/wiki/compile \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"domain":"product"}'

# 3. 发起必须带引用的问答
curl -X POST http://127.0.0.1:8000/api/internal/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question":"灵创AI平台的核心价值主张是什么？","domain":"product","require_citations":true}'
```

完整部署指南（含 PostgreSQL + pgvector 生产配置）请参阅[部署说明](#-部署说明)。

---

## 🛠️ 系统架构

LGDO Knowledge Core 的核心是一个五层数据管道：原始证据经过解析、标准化、知识衍生，最终进入检索和反馈治理层。

```text
本地资料 / 上传文件
  -> 文档解析 (PyMuPDF / python-docx / python-pptx / openpyxl)
  -> 标准化 (canonical Markdown + JSONL chunks)
  -> RAG 索引 (SQLite / PostgreSQL + pgvector)
  -> Wiki 编译 + 审阅队列
  -> 引用问答 + 反馈 + 知识缺口 + 评测回归
```

### 技术路线：LLMWiki + GBrain + DeepSeek + Obsidian

LGDO Knowledge Core 采用 **LLMWiki + GBrain + DeepSeek + Obsidian** 的组合路线，把资料处理、知识关系、生成回答和人工维护拆成四个可替换的环节：

| 技术点 | 在项目中的作用 |
| ------ | -------------- |
| **LLMWiki** | 将原始资料编译为可审阅、可引用、可版本化的 Markdown 知识页，承担资料到知识资产的转换层。 |
| **GBrain** | 作为 MCP 图谱记忆层，补足纯向量检索对实体关系、跨文档依赖、角色权限聚合的表达能力。 |
| **DeepSeek** | 作为可选生成模型，在检索证据充分时生成自然语言回答；未配置时系统回退到本地抽取式回答。 |
| **Obsidian** | 作为共享 Vault 的人工维护端：LGDO 写入托管 Markdown，watcher 接收外部编辑并进入 revision/冲突流程；当前不包含原生 Obsidian 插件。 |

#### Obsidian 集成边界

当前采用 **共享 Vault + 文件 watcher + API deep link**，而不是 Obsidian Plugin API：

- 托管页面的 frontmatter 同时写入 `id` 与 `lgdo_page_id`，两者均为稳定 `page_id`。
- Obsidian 的新增、修改、rename、delete、restore 由 `VaultSyncService` 观察、去抖、reconcile，并写入 immutable revision。
- 生成内容与人工内容冲突时保留人工文件，创建 review/conflict 和可恢复备份，不直接覆盖。
- `/api/internal/wiki/pages/{path}/obsidian-link` 返回 `obsidian://` deep link。
- 项目没有 Obsidian 插件包、插件 `manifest.json` 或插件进程；仓库内其他组件的同名 manifest 与 Obsidian 插件无关。若需要在 Obsidian 内展示冲突和同步按钮，应把现有 API 封装成后续插件，而不是在插件中复制 revision 逻辑。

### 数据分层

| 层级 | 路径/表 | 作用 |
| ---- | ------- | ---- |
| 🗄️ **原始证据层** | `uploads/`, `vault/raw/`, `sources` | 保存原始文件、解析文本、hash 指纹、来源标签。 |
| 📐 **标准化层** | `vault/normalized/`, `vault/jsonl/` | 保存 canonical Markdown、检索 chunks、采集报告。 |
| 📝 **知识衍生层** | `vault/wiki/`, `wiki_pages`, `wiki_page_revisions`, `review_items` | 保存共享 Vault 的事实内容、immutable revision 与冲突审阅。 |
| 🔍 **检索/图谱投影层** | `document_chunks`, `wiki_chunks`, `gbrain_page_projections`, `gbrain_projection_protections`, `projection_state` | 保存可重建的 RAG/GBrain 派生投影与 generation 水位。 |
| 🔄 **反馈治理层** | `query_logs`, `feedback`, `knowledge_gaps`, `audit_logs` | 记录问答、反馈、缺口、审计。 |

### 组件总览

| 组件 | 技术 | 概述 |
| ---- | ---- | ---- |
| 🧠 **检索与生成** | local-hash-v1 + keyword hybrid + DeepSeek API | 默认混合检索，无需外部 embedding 服务也能稳定运行；DeepSeek 可选用于生成式回答。 |
| 🗂️ **向量存储** | PostgreSQL + pgvector (ivfflat cosine) | 主业务元数据 + RAG chunk 表，可选 embedding 向量列。 |
| 📄 **文档解析** | PyMuPDF, python-docx, python-pptx, openpyxl | PDF、DOCX、PPTX、XLSX 解析，支持 PPT 全量 slide 文本提取。 |
| 🔗 **知识图谱** | GBrain MCP adapter (bun + PGLite) | 实体关系遍历、跨文档依赖分析、角色权限聚合、缺口推理。 |
| 📊 **评测引擎** | pytest + 四层难度框架 | 跨文档歧义、多跳推理、图谱依赖、否定边界评测。 |
| 🎛️ **管理控制台** | React 18 + TypeScript + Vite | 资料库管理、上传、知识页编辑、问答交互、审阅队列、状态概览。 |

---

## 🧩 能力

LGDO Knowledge Core 的能力围绕一个核心理念：**不只是存文档，而是让知识活起来，并且可以被修复**。

### 资料全生命周期

```text
入库             标准化            知识衍生          检索问答          治理闭环
  |                |                 |                 |                 |
  |- 目录扫描      |- 格式统一       |- Wiki 编译      |- 混合检索       |- 用户反馈
  |- 多文件上传    |- JSONL 分块     |- 状态流转       |- 引用追溯       |- 缺口生成
  |- hash 去重     |- 质量报告       |- 人工审阅       |- 置信度评估     |- 优先级指派
  `- 删除同步      `- 编码校验       `- Obsidian 兼容  `- 策略标记       `- 追踪补齐
```

### 评测四层金字塔

与一般 RAG 系统仅测“检索到没到”不同，LGDO 建立了四层评测体系，每一层对应一类真实场景中的检索和推理挑战：

| 层级 | 名称 | 挑战 | 示例 |
| :--: | ---- | ---- | ---- |
| **T1** | 跨文档歧义 | 同一实体出现在 3+ 个文档中，谁才是权威答案？ | “429 错误怎么处理”需要在错误码文档、工单案例、定价表之间判断。 |
| **T2** | 多跳推理 | 答案需要从 2+ 个文档中拼接。 | P1 故障全流程和用户补偿需要应急流程文档 + 实际工单。 |
| **T3** | 实体关系 | 必须通过知识图谱遍历关系。 | 哪些系统依赖审核引擎，V2.5 -> V3.0 升级影响哪些服务？ |
| **T4** | 否定/边界 | 否定检索、边界条件、幻觉陷阱。 | 平台不支持哪些图片格式？第 8 天还能退款吗？ |

> GBrain 是可选关系检索来源，不应预设一定提升准确率。当前可复现的 31 题基线中，GBrain OFF 为 16/31，ON 为 14/31，且 ON 的 P95 增加约 949 ms；在扩大图谱覆盖和建立逐题贡献证明前，生产默认应保持关闭。详见 `docs/方案设计.md`。

---

## 📊 评测

仓库内置 **31 道升级版评测题**（覆盖四层难度）。较新的可追溯基线记录在 `docs/方案设计.md`：GBrain OFF 为 16/31，ON 为 14/31。历史 91 文档/334 文件指标依赖仓库外数据路径，不能仅凭当前仓库复现。

### 解析性能

| 格式 | 文件数 | Chars/s 范围 | Chunks/s 范围 | Warnings |
| ---- | :----: | :----------: | :-----------: | :------: |
| TXT / Markdown | 162 | 4,943 - 8,907 | 9.7 - 11.6 | 0 |
| DOCX | 81 | 4,324 - 7,458 | 8.7 - 8.9 | 0 |
| PDF | 81 | 4,391 - 7,341 | 9.1 - 10.3 | 0 |
| PPTX | 10 | 3,747 | 7.8 | 0 |

### 测试覆盖

| 检查项 | 结果 |
| ------ | :--: |
| Python 全量 | 771 passed，9 skipped（2026-07-18） |
| Revision/watcher/API | 213 passed（2026-07-18） |
| PostgreSQL | 31 passed（2026-07-18） |
| GBrain TypeScript | 31 单元/契约 + 13 PGLite E2E，typecheck 通过（2026-07-18） |
| 前端 | 32 passed，生产构建通过（2026-07-18） |
| 真实动态 GBrain watcher SLA | add 1.935s，rename 1.665s，delete-expiry 5.714s |

---

## 🚧 局限

LGDO Knowledge Core 是一个内部知识库中台，不是面向终端客户的自动客服产品。

- **不直接面向客户自动回复。** 当前版本优先服务内部知识库试点，所有回答需要人工确认后才能对外。
- **企业 SSO 仍需生产加固。** 本地登录、账户角色、ACL 标签和内部 API 鉴权门禁已经可用；OIDC 映射、密码策略、登录限流和 token 哈希存储仍属于后续工作。
- **OCR 通道已预留但未完整接入。** 扫描件和图片型 PDF 当前会产生 warning，需要后续接入 PaddleOCR 或 Tesseract。
- **企业 IM 集成属于第二阶段。** 企业微信、钉钉、飞书的接入和客服系统写回尚未实现。
- **跨文档实体对齐仍有盲区。** 例如“部门总监”和“部门负责人”可能需要显式配置别名映射。
- **延迟和质量仍需按部署数据重测。** 历史 benchmark 依赖仓库外数据；当前 GBrain ON 基线没有证明质量增益，生产启用前必须在目标数据集上做 ON/OFF 与 P95 门禁。

---

## 📂 仓库结构

```text
.
├── app/                    # FastAPI 后端：解析、入库、RAG、问答、Wiki 编译
│   ├── main.py             # 应用入口
│   ├── routers/            # API 路由
│   ├── services/           # 核心服务（解析/标准化/RAG/Wiki/评测）
│   ├── models/             # Pydantic 数据模型
│   └── static/             # 旧版静态控制台回退资源
├── frontend/               # React + Vite 管理控制台
│   ├── src/                # TypeScript 源码
│   └── dist/               # 生产构建产物
├── tests/                  # 后端测试（pytest）
├── samples/                # 示例产品/客服知识资料
├── scripts/                # 评测和数据脚本
├── docs/                   # 方案设计和代码审查记录
├── gbrain/                 # 可选 GBrain 集成源码和依赖
├── .env.example            # 环境变量模板
├── README.md
├── README.zh-CN.md
└── LICENSE
```

### 运行时生成目录（不提交 Git）

| 路径 | 说明 |
| ---- | ---- |
| `data/` | SQLite 数据库、GBrain 本地数据 |
| `uploads/` | 用户上传的原始文件 |
| `vault/raw/` | 原始解析文本 |
| `vault/normalized/` | 标准化 Markdown |
| `vault/jsonl/` | 检索/评测用 JSONL chunks |
| `vault/wiki/` | 生成的 Obsidian 兼容知识页 |
| `vault/logs/` | 入库、问答、反馈和评测日志 |
| `output/` | 本地 benchmark 输出 |

---

## 🗄️ 部署说明

### 方案一：SQLite 单机试用

```bash
# .env
DATABASE_BACKEND=sqlite
DATABASE_PATH=data/lgdo.db
RAG_STORE_BACKEND=sqlite

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

零依赖秒起，适合本地开发和功能验证。

### 方案二：PostgreSQL + pgvector

```bash
# .env
DATABASE_BACKEND=postgres
RAG_STORE_BACKEND=postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=54322
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DATABASE=lgdo
```

系统自动创建所有业务表和 RAG chunk 表，并尝试启用 `pgvector` 扩展；扩展不可用时会回退到 JSONB embedding 存储。

### 方案三：DeepSeek 生成式回答

```bash
# .env
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

不配置时系统使用本地抽取式回答，适合验证入库和检索链路。

### 方案四：GBrain 知识图谱

```bash
# .env
GBRAIN_ENABLED=true
GBRAIN_ENDPOINT=http://127.0.0.1:8787/mcp
GBRAIN_QUERY_API_KEY=<只读 token>
GBRAIN_PROJECTION_API_KEY=<read/write token>
GBRAIN_MANAGED_SOURCE_ID=lgdo-managed
GBRAIN_SOURCE_ID=lgdo-managed
GBRAIN_IMPORT_ALLOWED_ROOT=<VAULT_PATH>/wiki
GBRAIN_HOME=data/gbrain
GBRAIN_REPO_PATH=gbrain
GBRAIN_IMPORT_ON_COMPILE=false
```

GBrain 默认关闭。查询与 projection 凭据必须分离；服务端还要把 canonical `<VAULT_PATH>/wiki` 注册为该 managed source 的允许 root。`GBRAIN_IMPORT_ON_COMPILE=true` 只表示编译事务写入异步 outbox，不会同步执行 600 秒导入。

---



### 常驻 GBrain HTTP MCP 与阿里云语义兜底

```powershell
# 首次创建 LGDO 专属 token（只显示一次，请写入根目录 .env）
$env:GBRAIN_HOME = "$PWD\data\gbrain"
Set-Location gbrain
bun run src/cli.ts auth create lgdo
Set-Location ..

# 启动或复用常驻 GBrain，然后启动 LGDO
powershell -ExecutionPolicy Bypass -File scripts/start-lgdo.ps1

# 只检查/启动 GBrain，不启动 FastAPI
powershell -ExecutionPolicy Bypass -File scripts/start-lgdo.ps1 -SkipApp
powershell -ExecutionPolicy Bypass -File scripts/status-gbrain.ps1
```

根目录 `.env` 使用 `GBRAIN_ENDPOINT=http://127.0.0.1:8787/mcp`。GBrain 异常、超时或无授权结果时，LGDO 可通过 `DASHSCOPE_EMBEDDING_ENABLED=true` 使用阿里云 `text-embedding-v3` 对本地候选块进行语义重排；阿里云也不可用时自动保留 BM25/关键词结果。百炼专属端点单批最多 10 条，代码会自动拆批。

### Obsidian 与前端开发服务器

启动完整本地工作流时，FastAPI 默认使用 `8000`，GBrain HTTP MCP 使用 `8787`，前端开发服务器使用 `8011`：

```powershell
Set-Location frontend
npm run dev -- --port 8011
```

打开 Obsidian 并将 Vault 指向 `.env` 的实际 `VAULT_PATH`（默认 `vault/`）。`resources/obsidian-vault/` 只是随项目发布的安装模板，不是运行 Vault。`vault/wiki/` 是可人工编辑的事实目录。应用启动时只补齐缺失的 `.obsidian/`、`templates/` 和 `indexes/` 资产；已有但漂移的文件会保留并在状态接口中报告。显式刷新会先写入 `.lgdo/obsidian-backups/` 再合并或替换受管资产：

```powershell
# 首次安装缺失资产
powershell -ExecutionPolicy Bypass -File scripts/install-obsidian-vault.ps1 -VaultPath vault

# 显式刷新已有资产；刷新前自动备份
powershell -ExecutionPolicy Bypass -File scripts/install-obsidian-vault.ps1 -VaultPath vault -Refresh

# 直接打开托管页面；VaultName 应与 OBSIDIAN_VAULT_NAME 或 Vault 文件夹名一致
powershell -ExecutionPolicy Bypass -File scripts/open-obsidian.ps1 -VaultName vault -PagePath 'wiki/product/faq/demo.md'
```

LGDO watcher 通过文件系统接收编辑，不需要插件即可同步；冲突审阅仍在 LGDO 控制台/API 中。GBrain 关闭时本地 revision、watcher、RAG/citation 工作流仍可运行。以下运维命令从本地登录响应中提取 Bearer token，不依赖开发身份回退：

```powershell
$login = Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8000/api/internal/auth/login' `
  -ContentType 'application/json' `
  -Body (@{ username = 'admin'; password = $env:LGDO_ADMIN_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.token)" }

Invoke-RestMethod -Headers $headers -Uri 'http://127.0.0.1:8000/api/internal/vault/status'
Invoke-RestMethod -Method Post -Headers $headers -Uri 'http://127.0.0.1:8000/api/internal/vault/reconcile'

# page_path 作为 path 参数时必须进行 URL 编码；响应的 url 可交给 Start-Process 打开
$pagePath = [Uri]::EscapeDataString('wiki/product/faq/demo.md').Replace('%2F', '/')
$link = Invoke-RestMethod -Headers $headers -Uri "http://127.0.0.1:8000/api/internal/wiki/pages/$pagePath/obsidian-link"
Start-Process $link.url
```

架构图：[技术栈结构图](docs/技术栈结构图.drawio) · [系统四层架构图](docs/系统四层架构图.drawio)。

## 🔌 常用接口

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/internal/auth/login` | 创建本地会话 token |
| `GET` | `/api/internal/auth/me` | 获取当前登录用户 |
| `POST` | `/api/internal/auth/logout` | 注销当前本地会话 |
| `GET` | `/api/internal/accounts` | 查看本地账户和 ACL 标签，仅管理员 |
| `POST` | `/api/internal/accounts` | 创建本地账户，仅管理员 |
| `PATCH` | `/api/internal/accounts/{user_id}` | 更新账户角色、状态、密码和 ACL 标签，仅管理员 |
| `POST` | `/api/internal/sources/scan` | 扫描本地目录资料 |
| `POST` | `/api/internal/sources/upload` | 上传并标准化资料 |
| `GET` | `/api/internal/sources` | 查看资料元数据 |
| `DELETE` | `/api/internal/sources/{id}` | 删除资料并清理索引 |
| `GET` | `/api/internal/sources/{id}/preview` | 标准化 Markdown 预览 |
| `POST` | `/api/internal/wiki/compile` | 编译 Markdown 知识页 |
| `GET` | `/api/internal/wiki/pages` | 知识页列表 |
| `PUT` | `/api/internal/wiki/pages/{path}` | 保存知识页内容 |
| `GET` | `/api/internal/wiki/pages/{path}/revisions` | 查看页面 revision 历史 |
| `GET` | `/api/internal/wiki/pages/{path}/conflicts` | 查看页面冲突 |
| `POST` | `/api/internal/wiki/conflicts/{review_id}/resolve` | 解决内容冲突 |
| `POST` | `/api/internal/wiki/write-intents/{intent_id}/release-backup` | 显式释放修复备份 |
| `GET` | `/api/internal/wiki/pages/{path}/obsidian-link` | 获取 Obsidian deep link |
| `GET` | `/api/internal/vault/status` | watcher、对账、Obsidian 资产与投影状态 |
| `POST` | `/api/internal/vault/reconcile` | 排队执行 Vault 对账 |
| `GET` | `/api/internal/vault/reconcile/{job_id}` | 查询对账结果 |
| `GET` | `/api/internal/projection-jobs` | 查看 RAG/GBrain projection outbox |
| `POST` | `/api/internal/projection-jobs/{job_id}/retry` | 重试失败的 projection job |
| `GET` | `/api/internal/reviews` | 审阅队列 |
| `GET` | `/api/internal/gaps` | 知识缺口列表 |
| `POST` | `/api/internal/ask` | 内部问答（带引用） |
| `POST` | `/api/internal/feedback` | 提交反馈（可生成缺口） |
| `POST` | `/api/internal/eval/questions` | 添加评测问题 |
| `POST` | `/api/internal/eval/run` | 运行评测 |
| `GET` | `/api/internal/rag/status` | RAG 和集成状态 |
| `POST` | `/api/internal/database/migrate-sqlite-to-postgres` | SQLite -> PostgreSQL 迁移 |

---

## 📋 TODO

- [x] 资料入库、标准化、Wiki 编译、引用问答
- [x] 人工审阅队列与知识缺口闭环
- [x] 四层评测体系（T1-T4）
- [x] PostgreSQL + pgvector 向量检索
- [x] GBrain 知识图谱的可选查询与异步 projection 集成
- [ ] GBrain 在目标数据集上的稳定正贡献与延迟门禁
- [x] 可配置 embedding provider + hybrid search（默认 `local-hash-v1`）
- [ ] BGE-M3 生产启用、重建索引与高维 pgvector schema
- [x] **本地登录与账户 ACL 管理**：显式 bootstrap 密码、会话 token 鉴权、角色、ACL 标签和账户管理界面
- [x] **ACL 感知的内部控制台保护**：内部 API 需要认证，管理写操作按 admin/editor 分层，QA 身份来自当前会话
- [x] **实体别名对齐**：实体别名表和别名管理 API，用于跨文档规范名称
- [ ] **企业 SSO 加固**：OIDC 角色/ACL 映射、token 哈希存储、密码策略、登录限流和账户审计筛选
- [ ] **OCR 引擎接入**：PaddleOCR 或 Tesseract，处理扫描件
- [ ] **企业 IM 集成**：企业微信、钉钉、飞书接入
- [ ] **延迟优化**：并行调用、查询缓存、reranker 精简
- [ ] **实时增量索引**：文件变更自动触发重新入库

---

## 🙏 致谢

- 基于 [FastAPI](https://fastapi.tiangolo.com/) 和 [Pydantic v2](https://docs.pydantic.dev/) 构建后端服务
- 向量检索基于 [pgvector](https://github.com/pgvector/pgvector)（PostgreSQL 向量扩展）
- 知识图谱基于 [GBrain](https://github.com/garrytan/gbrain)（MCP stdio adapter + PGLite）
- 前端管理控制台基于 [React 18](https://react.dev/) + [Vite](https://vitejs.dev/)
- 可选 embedding provider 支持 [BGE-M3](https://huggingface.co/BAAI/bge-m3)；默认 provider 为 `local-hash-v1`
- RAG 评测框架参考 [BEIR](https://github.com/beir-cellar/beir) 和 MTEB 的评测理念

---

## 📄 许可证

本项目基于 [Apache License 2.0](LICENSE) 开源。
