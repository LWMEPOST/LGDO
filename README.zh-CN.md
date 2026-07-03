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

# 启动后端
uvicorn app.main:app --reload

# 前端，可选开发模式
cd frontend
npm install
npm run dev
```

然后在浏览器中打开 `http://127.0.0.1:8000/docs` 查看 API 文档，或访问 `/console` 打开管理控制台。

管理控制台默认启用本地账户登录。初始化管理员为 `admin / admin`，首次登录后建议在账户权限页修改密码。

### 三行命令跑通第一个引用问答

```bash
# 1. 扫描示例资料入库
curl -X POST http://127.0.0.1:8000/api/internal/sources/scan \
  -H 'Content-Type: application/json' \
  -d '{"root_path":"samples/product_service","domain":"product","owner":"admin","acl_tags":["internal"]}'

# 2. 编译知识页
curl -X POST http://127.0.0.1:8000/api/internal/wiki/compile \
  -H 'Content-Type: application/json' \
  -d '{"domain":"product"}'

# 3. 发起必须带引用的问答
curl -X POST http://127.0.0.1:8000/api/internal/ask \
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
| **Obsidian** | 作为 Markdown 知识库工作流的兼容出口，便于人工编辑、审阅、归档和长期维护。 |

### 数据分层

| 层级 | 路径/表 | 作用 |
| ---- | ------- | ---- |
| 🗄️ **原始证据层** | `uploads/`, `vault/raw/`, `sources` | 保存原始文件、解析文本、hash 指纹、来源标签。 |
| 📐 **标准化层** | `vault/normalized/`, `vault/jsonl/` | 保存 canonical Markdown、检索 chunks、采集报告。 |
| 📝 **知识衍生层** | `vault/wiki/`, `wiki_pages`, `review_items` | 生成可读、可审阅的 Markdown 知识页。 |
| 🔍 **检索层** | `document_chunks`, `rag_document_chunks` | 提供 pgvector 向量索引，支撑引用问答。 |
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

> T3 层是区分“会检索”和“懂关系”的分水岭。纯向量 RAG 在 T3 层准确率仅 12.5%，接入 GBrain 知识图谱后跃升至 75%。

---

## 📊 评测

我们在 **31 道升级版评测题**（覆盖四层难度）上测试 LGDO Knowledge Core，使用 PostgreSQL + pgvector + DeepSeek + GBrain，数据集为 91 份中文企业文档、334 个文件、763 万字符。

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
| 后端测试（pytest） | 35 passed |
| 前端生产构建 | 通过（JS 178.65 kB / CSS 17.22 kB） |
| 基础 QA（30 题） | 100% 正确 |
| 升级 QA（31 题） | 93.55% 正确 |

---

## 🚧 局限

LGDO Knowledge Core 是一个内部知识库中台，不是面向终端客户的自动客服产品。

- **不直接面向客户自动回复。** 当前版本优先服务内部知识库试点，所有回答需要人工确认后才能对外。
- **企业 SSO 仍需生产加固。** 本地登录、账户角色、ACL 标签和内部 API 鉴权门禁已经可用；OIDC 映射、密码策略、登录限流和 token 哈希存储仍属于后续工作。
- **OCR 通道已预留但未完整接入。** 扫描件和图片型 PDF 当前会产生 warning，需要后续接入 PaddleOCR 或 Tesseract。
- **企业 IM 集成属于第二阶段。** 企业微信、钉钉、飞书的接入和客服系统写回尚未实现。
- **跨文档实体对齐仍有盲区。** 例如“部门总监”和“部门负责人”可能需要显式配置别名映射。
- **延迟优化空间大。** 当前平均延迟约 23s（含 GBrain 图谱遍历），不适合直接用于低延迟 C 端场景。

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
GBRAIN_HOME=data/gbrain
GBRAIN_REPO_PATH=gbrain
```

GBrain 默认关闭。开启后提供实体关系遍历、跨文档依赖分析、缺口推理。

---

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
- [x] GBrain 知识图谱集成与贡献度量化
- [x] BGE-M3 embedding + hybrid search
- [x] **本地登录与账户 ACL 管理**：初始化管理员、会话 token 鉴权、角色、ACL 标签和账户管理界面
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
- embedding 模型使用 [BGE-M3](https://huggingface.co/BAAI/bge-m3)（BAAI 多语言向量模型）
- RAG 评测框架参考 [BEIR](https://github.com/beir-cellar/beir) 和 MTEB 的评测理念

---

## 📄 许可证

本项目基于 [Apache License 2.0](LICENSE) 开源。
