<p align="center">
  <h1 align="center">LGDO Knowledge Core</h1>
  <p align="center"><strong>🧠 An evaluable, traceable, closed-loop knowledge core for internal RAG systems</strong></p>
  <p align="center">
    Turn scattered product documents, customer-service FAQs, after-sales policies,<br>
    historical tickets, and internal rules into maintainable knowledge assets with citations, review, and gap tracking.
  </p>
</p>

<p align="center">
  <a href="#-quick-start"><b>🚀 Quick Start</b></a> |
  <a href="#-architecture"><b>🛠️ Architecture</b></a> |
  <a href="#-capabilities"><b>🧩 Capabilities</b></a> |
  <a href="#-evaluation"><b>📊 Evaluation</b></a> |
  <a href="更新说明.md"><b>📝 Updates</b></a> |
  <a href="#-limitations"><b>🚧 Limitations</b></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11">
  <img src="https://img.shields.io/badge/FastAPI-Backend-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/React-18-61DAFB?style=flat-square&logo=react&logoColor=white" alt="React 18">
  <img src="https://img.shields.io/badge/Vector_DB-pgvector-336791?style=flat-square&logo=postgresql&logoColor=white" alt="pgvector">
  <img src="https://img.shields.io/badge/Knowledge_Compile-LLMWiki-0f766e?style=flat-square" alt="LLMWiki">
  <img src="https://img.shields.io/badge/Graph_Memory-GBrain-6366f1?style=flat-square" alt="GBrain">
  <img src="https://img.shields.io/badge/Generation-DeepSeek-111827?style=flat-square" alt="DeepSeek">
  <img src="https://img.shields.io/badge/Wiki_Workflow-Obsidian-7c3aed?style=flat-square" alt="Obsidian">
  <img src="https://img.shields.io/badge/License-Apache_2.0-green?style=flat-square" alt="Apache 2.0">
</p>

<p align="center">
  <b>English</b> | <a href="README.zh-CN.md">简体中文</a>
</p>

## ✨ Overview

Internal knowledge usually fails in three places: it is scattered, it goes stale, and nobody knows how to repair wrong answers.

Most knowledge bases only solve document storage. LGDO Knowledge Core focuses on the engineering loop after storage: who can verify the answer, where the answer came from, which source text supports it, what knowledge is missing, and how a wrong answer becomes a fixable gap.

**LGDO Knowledge Core** is not just another document repository. It is built around a complete loop of **source material -> knowledge -> answer -> gap -> repair**. Raw materials keep their evidence chain, answers must carry citations back to source documents and source spans, wiki pages move through review states, feedback can generate knowledge gaps, and evaluation questions keep retrieval and answer quality measurable.

It is not an end-customer chatbot. It is an internal RAG platform for **knowledge managers, product teams, and operations teams** who need to know what the knowledge base contains, what it misses, whether it answers correctly, and where it fails.

### 🌟 Key Features

|     | Feature | Description |
| --- | ------- | ----------- |
| 🔗 | **Citation-first answers** | Every answer can trace back through source -> wiki page -> source span. |
| 🔍 | **Deep multi-format parsing** | Supports PDF, DOCX, PPTX, XLSX, Markdown, CSV, and JSON; PPT text is extracted across slides. |
| 📊 | **Quantified evaluation** | Four difficulty layers cover cross-document ambiguity, multi-hop reasoning, graph dependency, and negative/boundary cases. |
| 🧠 | **GBrain knowledge graph** | Entity traversal, cross-document dependency analysis, and role/permission aggregation beyond plain vector search. |
| 🔄 | **Knowledge gap loop** | Feedback can create gaps with priority, ownership, related pages, and status tracking. |
| ✅ | **Human review queue** | New and updated wiki pages enter review workflows instead of becoming unmanaged generated content. |
| 🔐 | **Local login and account ACL** | Built-in admin login, session tokens, account roles, ACL tags, and protected internal console APIs. |
| 🗄️ | **Dual storage backends** | SQLite for local trials; PostgreSQL + pgvector for production-oriented vector retrieval. |
| 🎛️ | **React admin console** | Dense internal console for sources, uploads, wiki pages, QA, reviews, and system status. |

---

## 🚀 Quick Start

```bash
git clone https://github.com/LWMEPOST/LGDO.git
cd LGDO

# Backend
python -m venv .venv
source .venv/bin/activate  # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env

# Set an explicit bootstrap password; there is no default admin/admin login.
# AUTH_BOOTSTRAP_ADMIN_PASSWORD=replace-with-a-strong-password

# Start backend
uvicorn app.main:app --reload

# Frontend, optional in development mode
cd frontend
npm install
npm run dev -- --port 8011
```

Open `http://127.0.0.1:8000/docs` for API documentation, or `/console` for the admin console.

The admin console uses local account login. A bootstrap administrator is created only when `AUTH_BOOTSTRAP_ADMIN_PASSWORD` is explicitly set; never enable `AUTH_DEV_FALLBACK_ENABLED` in production.

### Run the first cited answer in three calls

```bash
# First obtain a token from /api/internal/auth/login.
export TOKEN='<token returned by the login endpoint>'

# 1. Scan sample materials
curl -X POST http://127.0.0.1:8000/api/internal/sources/scan \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"root_path":"samples/product_service","domain":"product","owner":"admin","acl_tags":["internal"]}'

# 2. Compile wiki pages
curl -X POST http://127.0.0.1:8000/api/internal/wiki/compile \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"domain":"product"}'

# 3. Ask a citation-required question
curl -X POST http://127.0.0.1:8000/api/internal/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question":"灵创AI平台的核心价值主张是什么？","domain":"product","require_citations":true}'
```

For deployment options, including PostgreSQL + pgvector, see [Deployment](#-deployment).

---

## 🛠️ Architecture

LGDO Knowledge Core is centered on a five-layer data pipeline. Raw evidence is parsed, normalized, compiled into derived knowledge, and then used by retrieval, QA, feedback, and evaluation workflows.

```text
Local materials / Uploaded files
  -> Document parsing (PyMuPDF / python-docx / python-pptx / openpyxl)
  -> Normalization (canonical Markdown + JSONL chunks)
  -> RAG index (SQLite / PostgreSQL + pgvector)
  -> Wiki compilation + review queue
  -> Cited QA + feedback + knowledge gaps + evaluation regression
```

### Technical Route: LLMWiki + GBrain + DeepSeek + Obsidian

LGDO Knowledge Core uses **LLMWiki + GBrain + DeepSeek + Obsidian** as a modular route. The pipeline separates material processing, knowledge relations, answer generation, and human maintenance into replaceable parts.

| Technology | Role in LGDO |
| ---------- | ------------ |
| **LLMWiki** | Compiles raw materials into reviewable, citable, versionable Markdown wiki pages. It is the conversion layer from source material to knowledge asset. |
| **GBrain** | Acts as the MCP graph memory layer, covering entity relations, cross-document dependencies, and role/permission aggregation where pure vector retrieval is weak. |
| **DeepSeek** | Optional generation model used when retrieval evidence is sufficient. Without it, the system falls back to local extractive answers. |
| **Obsidian** | Shared-vault human maintenance endpoint: LGDO writes managed Markdown, the watcher ingests external edits into revision/conflict handling; there is no native Obsidian plugin yet. |

#### Obsidian integration boundary

The current integration is **shared vault + filesystem watcher + API deep links**, not the Obsidian Plugin API:

- Managed frontmatter writes both `id` and `lgdo_page_id`, both equal to the stable `page_id`.
- `VaultSyncService` observes, debounces, reconciles, and records Obsidian add/modify/rename/delete/restore events as immutable revisions.
- When generated and human content diverge, the human file is retained and a review/conflict plus recoverable backup is created.
- `/api/internal/wiki/pages/{path}/obsidian-link` returns an `obsidian://` deep link.
- The repository has no Obsidian plugin package, Obsidian plugin `manifest.json`, or plugin process; manifests belonging to other components are unrelated. A future plugin should wrap these APIs instead of duplicating revision logic.

### Data Layers

| Layer | Paths / Tables | Purpose |
| ----- | -------------- | ------- |
| 🗄️ **Raw evidence** | `uploads/`, `vault/raw/`, `sources` | Stores original files, parsed text, hashes, and source tags. |
| 📐 **Normalization** | `vault/normalized/`, `vault/jsonl/` | Stores canonical Markdown, retrieval chunks, and ingest reports. |
| 📝 **Derived knowledge** | `vault/wiki/`, `wiki_pages`, `wiki_page_revisions`, `review_items` | Shared-Vault facts, immutable revisions, and conflict review. |
| 🔍 **Retrieval/graph projections** | `document_chunks`, `wiki_chunks`, `gbrain_page_projections`, `gbrain_projection_protections`, `projection_state` | Rebuildable RAG/GBrain projections and generation watermarks. |
| 🔄 **Governance** | `query_logs`, `feedback`, `knowledge_gaps`, `audit_logs` | Records questions, feedback, gaps, and audit events. |

### Component Overview

| Component | Technology | Summary |
| --------- | ---------- | ------- |
| 🧠 **Retrieval and generation** | local-hash-v1 + keyword hybrid + DeepSeek API | Hybrid retrieval works without external embedding services; DeepSeek is optional for generated answers. |
| 🗂️ **Vector storage** | PostgreSQL + pgvector (ivfflat cosine) | Business metadata plus RAG chunk tables, with optional embedding vector columns. |
| 📄 **Document parsing** | PyMuPDF, python-docx, python-pptx, openpyxl | PDF, DOCX, PPTX, and XLSX parsing, including full slide text extraction. |
| 🔗 **Knowledge graph** | GBrain MCP adapter (bun + PGLite) | Entity traversal, cross-document dependency analysis, role aggregation, and gap reasoning. |
| 📊 **Evaluation engine** | pytest + four-layer difficulty framework | Cross-document ambiguity, multi-hop reasoning, graph dependency, and negative/boundary cases. |
| 🎛️ **Admin console** | React 18 + TypeScript + Vite | Source management, uploads, wiki editing, QA, review queue, and status overview. |

---

## 🧩 Capabilities

LGDO Knowledge Core is built around one idea: **do not just store documents; keep knowledge alive and repairable**.

### Knowledge Lifecycle

```text
Ingest           Normalize          Derive Wiki       Retrieve / QA      Govern
  |                 |                  |                 |                 |
  |- Directory scan |- Format unify     |- Wiki compile   |- Hybrid search  |- Feedback
  |- Multi-upload   |- JSONL chunking   |- State changes  |- Citations      |- Gap creation
  |- Hash dedupe    |- Quality reports  |- Human review   |- Confidence     |- Priority / owner
  `- Delete sync    `- Encoding checks  `- Obsidian ready `- Strategy tags  `- Repair tracking
```

### Four-Layer Evaluation Pyramid

Instead of only checking whether retrieval hit the right document, LGDO evaluates the types of reasoning that show up in real internal knowledge work.

| Layer | Name | Challenge | Example |
| :---: | ---- | --------- | ------- |
| **T1** | Cross-document ambiguity | The same entity appears in 3+ documents. Which source is authoritative? | "How should 429 errors be handled?" across error-code docs, tickets, and pricing docs. |
| **T2** | Multi-hop reasoning | The answer must be assembled from 2+ documents. | Incident flow and customer compensation require emergency procedures plus historical tickets. |
| **T3** | Entity relations | The answer depends on graph traversal. | Which systems depend on the review engine, and what does a V2.5 -> V3.0 upgrade affect? |
| **T4** | Negative and boundary cases | Negative retrieval, boundary conditions, and hallucination traps. | Unsupported image formats; refund eligibility after the 8th day. |

> GBrain is an optional relation-retrieval source, not an assumed quality win. The latest reproducible 31-question baseline records 16/31 with GBrain OFF and 14/31 with it ON, with roughly 949 ms higher P95 when ON. Keep it disabled by default until graph coverage and per-question contribution are proven; see `docs/方案设计.md`.

---

## 📊 Evaluation

The repository contains **31 upgraded evaluation questions** across four difficulty layers. The newer traceable baseline in `docs/方案设计.md` records 16/31 with GBrain OFF and 14/31 with it ON. Historical 91-document/334-file metrics depend on an external dataset path and are not reproducible from this repository alone.

### Parsing Performance

| Format | Files | Chars/s Range | Chunks/s Range | Warnings |
| ------ | :---: | :-----------: | :------------: | :------: |
| TXT / Markdown | 162 | 4,943 - 8,907 | 9.7 - 11.6 | 0 |
| DOCX | 81 | 4,324 - 7,458 | 8.7 - 8.9 | 0 |
| PDF | 81 | 4,391 - 7,341 | 9.1 - 10.3 | 0 |
| PPTX | 10 | 3,747 | 7.8 | 0 |

### Test Coverage

| Check | Result |
| ----- | :----: |
| Full Python suite | 771 passed, 9 skipped (2026-07-18) |
| Revision/watcher/API | 213 passed (2026-07-18) |
| PostgreSQL | 31 passed (2026-07-18) |
| GBrain TypeScript | 31 unit/contract + 13 PGLite E2E, typecheck passed (2026-07-18) |
| Frontend | 32 passed, production build passed (2026-07-18) |
| Real dynamic GBrain watcher SLA | add 1.935s, rename 1.665s, delete-expiry 5.714s |

---

## 🚧 Limitations

LGDO Knowledge Core is an internal knowledge platform, not an end-customer auto-reply product.

- **No direct customer auto-replies.** The current version is intended for internal pilots; external responses should be confirmed by humans.
- **Enterprise SSO still needs production hardening.** Local login, account roles, ACL tags, and internal API auth gates are available; OIDC mapping, password policy, login throttling, and token-hash storage remain future work.
- **OCR is reserved but not fully integrated.** Scanned documents and image-only PDFs produce warnings until PaddleOCR or Tesseract is connected.
- **Enterprise IM integrations are phase-two work.** WeCom, DingTalk, Lark, and customer-service writeback are not implemented yet.
- **Cross-document entity alignment has blind spots.** For example, "department director" and "department owner" may need explicit alias mapping.
- **Latency and quality need deployment-specific gates.** Historical benchmark data is external, and the current GBrain ON baseline does not prove a quality gain. Run ON/OFF and P95 gates on the target dataset before production enablement.

---

## 📂 Repository Structure

```text
.
├── app/                    # FastAPI backend: parsing, ingest, RAG, QA, wiki compilation
│   ├── main.py             # Application entry
│   ├── routers/            # API routes
│   ├── services/           # Core services: parsing, normalization, RAG, wiki, evaluation
│   ├── models/             # Pydantic models
│   └── static/             # Legacy static console fallback
├── frontend/               # React + Vite admin console
│   ├── src/                # TypeScript source
│   └── dist/               # Production build output
├── tests/                  # Backend tests with pytest
├── samples/                # Sample product and service knowledge materials
├── scripts/                # Evaluation and data scripts
├── docs/                   # Design notes and review records
├── gbrain/                 # Optional GBrain integration source and dependencies
├── .env.example            # Environment variable template
├── README.md
├── README.zh-CN.md
└── LICENSE
```

### Runtime Directories Not Committed to Git

| Path | Description |
| ---- | ----------- |
| `data/` | SQLite database and local GBrain data |
| `uploads/` | Uploaded source files |
| `vault/raw/` | Raw parsed text |
| `vault/normalized/` | Normalized Markdown |
| `vault/jsonl/` | JSONL chunks for retrieval and evaluation |
| `vault/wiki/` | Generated Obsidian-compatible wiki pages |
| `vault/logs/` | Ingest, QA, feedback, and evaluation logs |
| `output/` | Local benchmark outputs |

---

## 🗄️ Deployment

### Option 1: SQLite Single-Node Trial

```bash
# .env
DATABASE_BACKEND=sqlite
DATABASE_PATH=data/lgdo.db
RAG_STORE_BACKEND=sqlite

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

This starts quickly with minimal dependencies and is suitable for local development and feature validation.

### Option 2: PostgreSQL + pgvector

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

The system creates business tables and RAG chunk tables automatically. It also attempts to enable `pgvector`, falling back to JSONB embedding storage when the extension is unavailable.

### Option 3: DeepSeek Generated Answers

```bash
# .env
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

If DeepSeek is not configured, the system uses local extractive answers, which is useful for validating ingest and retrieval flows.

### Option 4: GBrain Knowledge Graph

```bash
# .env
GBRAIN_ENABLED=true
GBRAIN_ENDPOINT=http://127.0.0.1:8787/mcp
GBRAIN_QUERY_API_KEY=<read-only token>
GBRAIN_PROJECTION_API_KEY=<read/write token>
GBRAIN_MANAGED_SOURCE_ID=lgdo-managed
GBRAIN_SOURCE_ID=lgdo-managed
GBRAIN_IMPORT_ALLOWED_ROOT=<VAULT_PATH>/wiki
GBRAIN_HOME=data/gbrain
GBRAIN_REPO_PATH=gbrain
GBRAIN_IMPORT_ON_COMPILE=false
```

GBrain is disabled by default. Query and projection credentials must be separate, and the server must register canonical `<VAULT_PATH>/wiki` as an allowed root for the managed source. `GBRAIN_IMPORT_ON_COMPILE=true` only enqueues the asynchronous outbox; compile never performs a synchronous 600-second import.

### Obsidian and the frontend dev server

The complete local workflow uses FastAPI on `8000`, GBrain HTTP MCP on `8787`, and the frontend dev server on `8011`:

```powershell
Set-Location frontend
npm run dev -- --port 8011
```

Open Obsidian on the actual `.env` `VAULT_PATH` (default `vault/`). `resources/obsidian-vault/` is an installation template, not the runtime Vault. `vault/wiki/` is the human-editable fact directory. Application startup installs missing `.obsidian/`, `templates/`, and `indexes/` assets but preserves drifted files and reports them through the status API. An explicit refresh backs up the managed files under `.lgdo/obsidian-backups/` before merging or replacing them:

```powershell
# Install assets that are missing.
powershell -ExecutionPolicy Bypass -File scripts/install-obsidian-vault.ps1 -VaultPath vault

# Explicitly refresh managed assets, with an automatic backup first.
powershell -ExecutionPolicy Bypass -File scripts/install-obsidian-vault.ps1 -VaultPath vault -Refresh

# Open a managed page. VaultName must match OBSIDIAN_VAULT_NAME or the Vault folder name.
powershell -ExecutionPolicy Bypass -File scripts/open-obsidian.ps1 -VaultName vault -PagePath 'wiki/product/faq/demo.md'
```

The LGDO watcher ingests edits through the filesystem without a plugin; conflict review remains in the LGDO console/API. The local revision, watcher, RAG, and citation workflow still operates when GBrain is disabled. These operations obtain a Bearer token through local login instead of using the development identity fallback:

```powershell
$login = Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8000/api/internal/auth/login' `
  -ContentType 'application/json' `
  -Body (@{ username = 'admin'; password = $env:LGDO_ADMIN_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.token)" }

Invoke-RestMethod -Headers $headers -Uri 'http://127.0.0.1:8000/api/internal/vault/status'
Invoke-RestMethod -Method Post -Headers $headers -Uri 'http://127.0.0.1:8000/api/internal/vault/reconcile'

# URL-encode page_path when it is embedded in the route; open the returned URL with Obsidian.
$pagePath = [Uri]::EscapeDataString('wiki/product/faq/demo.md').Replace('%2F', '/')
$link = Invoke-RestMethod -Headers $headers -Uri "http://127.0.0.1:8000/api/internal/wiki/pages/$pagePath/obsidian-link"
Start-Process $link.url
```

Diagrams: [technology stack](docs/技术栈结构图.drawio) · [four-layer architecture](docs/系统四层架构图.drawio).

---

## 🔌 Common APIs

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/health` | Health check |
| `POST` | `/api/internal/auth/login` | Create a local session token |
| `GET` | `/api/internal/auth/me` | Read the current authenticated user |
| `POST` | `/api/internal/auth/logout` | Revoke the current local session |
| `GET` | `/api/internal/accounts` | List local accounts and ACL tags, admin only |
| `POST` | `/api/internal/accounts` | Create a local account, admin only |
| `PATCH` | `/api/internal/accounts/{user_id}` | Update account role, status, password, and ACL tags, admin only |
| `POST` | `/api/internal/sources/scan` | Scan local material directories |
| `POST` | `/api/internal/sources/upload` | Upload and normalize materials |
| `GET` | `/api/internal/sources` | List source metadata |
| `DELETE` | `/api/internal/sources/{id}` | Delete a source and clean related indexes |
| `GET` | `/api/internal/sources/{id}/preview` | Preview normalized Markdown |
| `POST` | `/api/internal/wiki/compile` | Compile Markdown wiki pages |
| `GET` | `/api/internal/wiki/pages` | List wiki pages |
| `PUT` | `/api/internal/wiki/pages/{path}` | Save wiki page content |
| `GET` | `/api/internal/wiki/pages/{path}/revisions` | List page revisions |
| `GET` | `/api/internal/wiki/pages/{path}/conflicts` | List page conflicts |
| `POST` | `/api/internal/wiki/conflicts/{review_id}/resolve` | Resolve a content conflict |
| `POST` | `/api/internal/wiki/write-intents/{intent_id}/release-backup` | Explicitly release a repair backup |
| `GET` | `/api/internal/wiki/pages/{path}/obsidian-link` | Build an Obsidian deep link |
| `GET` | `/api/internal/vault/status` | Watcher, reconcile, Obsidian asset, and projection status |
| `POST` | `/api/internal/vault/reconcile` | Queue Vault reconciliation |
| `GET` | `/api/internal/vault/reconcile/{job_id}` | Read reconciliation result |
| `GET` | `/api/internal/projection-jobs` | List the RAG/GBrain projection outbox |
| `POST` | `/api/internal/projection-jobs/{job_id}/retry` | Retry a failed projection job |
| `GET` | `/api/internal/reviews` | List review queue |
| `GET` | `/api/internal/gaps` | List knowledge gaps |
| `POST` | `/api/internal/ask` | Internal cited QA |
| `POST` | `/api/internal/feedback` | Submit feedback and optionally create gaps |
| `POST` | `/api/internal/eval/questions` | Add evaluation questions |
| `POST` | `/api/internal/eval/run` | Run evaluation |
| `GET` | `/api/internal/rag/status` | RAG and integration status |
| `POST` | `/api/internal/database/migrate-sqlite-to-postgres` | Migrate SQLite data to PostgreSQL |

---

## 📋 TODO

- [x] Source ingest, normalization, wiki compilation, and cited QA
- [x] Human review queue and knowledge gap loop
- [x] Four-layer evaluation framework (T1-T4)
- [x] PostgreSQL + pgvector retrieval
- [x] Optional GBrain query and asynchronous projection integration
- [ ] Stable positive GBrain contribution and latency gates on the target dataset
- [x] Configurable embedding provider + hybrid search (default `local-hash-v1`)
- [ ] Production BGE-M3 enablement, reindexing, and high-dimensional pgvector schema
- [x] **Local login and account ACL management**: explicit bootstrap password, session token auth, roles, ACL tags, and account management UI
- [x] **ACL-aware internal console protection**: authenticated internal APIs, admin/editor write gates, and QA identity display from the active session
- [x] **Entity alias alignment**: entity alias table and alias management APIs for cross-document canonical names
- [ ] **Enterprise SSO hardening**: OIDC role/ACL mapping, token-hash storage, password policy, login throttling, and account audit filters
- [ ] **OCR integration**: PaddleOCR or Tesseract for scanned documents
- [ ] **Enterprise IM integration**: WeCom, DingTalk, and Lark
- [ ] **Latency optimization**: parallel calls, query cache, and lighter reranking
- [ ] **Realtime incremental indexing**: automatically re-ingest changed files

---

## 🙏 Acknowledgements

- Backend service built with [FastAPI](https://fastapi.tiangolo.com/) and [Pydantic v2](https://docs.pydantic.dev/)
- Vector retrieval based on [pgvector](https://github.com/pgvector/pgvector), the PostgreSQL vector extension
- Knowledge graph based on [GBrain](https://github.com/garrytan/gbrain), MCP stdio adapter + PGLite
- Admin console built with [React 18](https://react.dev/) + [Vite](https://vitejs.dev/)
- Optional embedding provider supports [BGE-M3](https://huggingface.co/BAAI/bge-m3); the default provider is `local-hash-v1`
- RAG evaluation ideas inspired by [BEIR](https://github.com/beir-cellar/beir) and MTEB

---

## 📄 License

This project is open-sourced under the [Apache License 2.0](LICENSE).
