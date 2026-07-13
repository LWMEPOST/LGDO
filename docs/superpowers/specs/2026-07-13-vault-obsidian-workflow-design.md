# Vault Watcher 与 Obsidian 工作流设计

状态：书面规格已确认，进入 TDD 实施计划（2026-07-13）

## 目标

把当前“可由 Obsidian 打开的 Markdown 目录”升级为可验证的人工编辑工作流：Obsidian 中的新增、修改、重命名和删除能进入 revision、审计、RAG 与 GBrain 投影链路；Web 与编译器写回不会形成监听循环。

## 范围与边界

- Obsidian 可编辑区：`vault/wiki/**/*.md`。
- Obsidian 可查看区：`indexes/`、`reviews/`、`logs/`、`normalized/`、`jsonl/`。
- `raw/`、`normalized/`、`jsonl/`、`indexes/`、`logs/` 与生成的 conflict note 不是 watcher 的内容输入。
- 首版不开发自定义 Obsidian 插件；核心一致性全部在 LGDO 服务端。
- 提供稳定 Vault 配置、模板、wikilink/MOC、打开链接、状态页和冲突入口。

## 技术选型

| 类别 | 选择 | 理由 |
| --- | --- | --- |
| 文件监听 | `watchfiles.awatch` | 支持 asyncio、Windows、递归监听与事件归并 |
| Frontmatter | 受限 `ruamel.yaml` round-trip loader | 结构化校验并保留注释、键顺序和用户格式 |
| 生命周期 | FastAPI lifespan task | 与现有单服务部署一致，可在测试中注入与关闭 |
| 一致性 | revision service + persistent outbox | watcher 只适配事件，不复制业务规则 |

## Frontmatter 契约

每个受管 Wiki 页面包含：

```yaml
---
id: page_xxx
lgdo_page_id: page_xxx
lgdo_revision_id: wrev_xxx
lgdo_write_token: write_xxx
title: 页面标题
source_ids:
  - src_xxx
domain: product
page_type: feature
review_status: draft
owner: name
tags: []
aliases: []
---
```

- `lgdo_page_id` 是 rename 后仍保持不变的身份。
- `lgdo_revision_id` 和 `lgdo_write_token` 由 LGDO 写入，用户无需维护。
- `source_ids` 必须存在于 sources 表；未知来源使文件进入 sync error，而不是被导入。
- `review_status` 沿用现有枚举：`draft`、`reviewed`、`stale`、`rejected`，不引入与当前 API/UI 不兼容的新状态。
- `tags`、`aliases` 用于 Obsidian 搜索和 wikilink，保存进 revision metadata。
- YAML 大小、嵌套深度和正文大小有配置上限，防止异常文件耗尽内存。
- 禁止自定义 YAML tag、重复键和超限 alias；`id` 与 `lgdo_page_id` 同时存在时必须相同。

### 页面身份优先级

| 观察 | 处理 |
| --- | --- |
| path 已绑定 page A，frontmatter ID 缺失或等于 A | 按 A 的 external edit 处理 |
| path 已绑定 A，但 frontmatter 声明 B | 隔离该 path，A fail-closed并记录 identity conflict；绝不修改 B |
| 新 path 声明 active page B，且 B 的原 path 仍存在 | duplicate ID issue；新文件只保存 observation，不修改 B |
| 新 path 声明 B，B 原 path 缺失且事件窗口内有对应 delete | 作为 rename，锁 B 后更新 path |
| 新 path 声明 deleted page B | 路径无占用且内容合法时恢复 B |
| 新 path 无 ID | 分配新 page ID并写回受管字段 |

path-bound 页面优先于不可信 frontmatter ID。复制或伪造 ID 只能使当前观察文件进入隔离，不能把另一合法页面置 invalid、删除或撤销其投影。

### `pending_vault_deletes`

Delete 事件先持久化为 pending，不立即软删除：记录 page ID、旧 path、最后 file/semantic hash、detected/expire time 和状态。rename grace 不得小于 `max(5000 ms, debounce + stability timeout + safety margin)`；默认 debounce 750 ms、稳定检测 3 秒时使用 5000 ms。

- grace 内出现唯一匹配 page ID 的 add 时，在页面锁内成对提交 rename并取消 pending delete。
- 无 page ID 时仅在 hash 唯一匹配且无歧义时推断 rename；否则创建 review issue。
- grace 到期前已观察到 add、但候选文件仍在稳定检测或解析时，持久化标记 candidate-in-progress 并暂停相关 delete 的过期处理；候选完成身份分类后才恢复过期判断。由于稳定前无法可信读取 ID，过期 worker 必须先等待所有在原截止时间前到达且尚未分类的 add candidate 完成，不能先删后认领 rename。
- grace 到期后再次确认旧 path 缺失、无 active write intent、无匹配 add，才提交 soft delete。
- 服务重启后继续处理未到期记录；startup reconcile 先扫描全部 page ID，以“DB path 缺失 + 新 path 唯一同 ID”识别停机期间 rename，再处理真正 missing。

## `VaultSyncService`

### 启动

FastAPI lifespan 在数据库迁移和 Vault 初始化后：

1. 执行一次 startup reconcile。
2. 启动 projection worker。
3. 若 `VAULT_WATCH_ENABLED=true`，启动 watcher task。
4. shutdown 时停止接收事件、排空正在处理的单个事件、取消 watcher/worker 并释放租约。

测试默认关闭 watcher 和真实外部投影，通过 fixture 显式开启。

### 事件归并

- 只接收 `.md` 普通文件，拒绝 symlink 和越过 Vault 根目录的路径。
- `watchfiles` debounce 默认 750 ms；同一路径连续事件只处理最终状态。
- 写入中的文件需要两次 stat 大小和 mtime 一致后解析，最长等待 3 秒。
- Windows rename 可能表现为 delete + add：优先通过 `lgdo_page_id` 关联，其次通过已知 hash 在短时间窗口内关联。
- 事件处理按 `page_id` 串行，不同页面可并行，默认并发 4。
- path 有 active write intent 时，missing/delete 事件延迟到 intent terminal；`.lgdo/pending`、backup 和 temp 永久忽略。

### 写回循环抑制

LGDO 原子写文件时生成 `lgdo_write_token`，并把 token、revision ID、hash 写入数据库。watcher 收到事件后：

- 只有 token、revision 和 hash 全部匹配，且页面 `active`、path/observed hash clean、`sync_error` 为空、没有 recovery/pending issue 时才确认事件后忽略。
- token 匹配但 hash 不同，按 external edit 处理，不能忽略。
- token 不认识时，按 Obsidian/external edit 处理。
- invalid、deleted 或 recovery 状态即使 bytes 恰好等于历史 current，也必须走 restore/reconcile，不能被旧 token 抑制。

抑制依据是持久化 revision/hash，而不是只存在内存的时间窗口，因此重启后仍正确。

这里的 hash 分为两种：`file_hash` 覆盖包含受管字段的完整文件，用于漂移和循环抑制；`semantic_hash` 排除 revision ID、write token 和受管更新时间，用于判断业务内容是否相同。仅改变受管字段不会被识别为新的人工编辑。

### External ingest 与受管字段写回

Add/modify 事件先把原始 bytes 保存到 `wiki_file_observations`，再用受限 round-trip parser 验证并更新 LGDO 受管字段。service 分配 revision ID 与 write token，保留正文、注释和用户 frontmatter，计算两个 hash，通过 pending write intent 写回，再推进 current、审计和投影 outbox。紧接着的 watcher 事件因 revision、token 和 file hash 匹配而忽略。若写回期间文件再次变化，capture/no-replace 协议保留双方并让 human observation 优先，绝不覆盖未知 bytes。

## 事件语义

### Add

- 有合法 `lgdo_page_id` 且对应 deleted 页面：恢复页面并创建 external revision。
- 没有 page ID：分配新 ID；仅在 `source_ids`、domain、page_type 合法时创建页面。
- 新页面进入 `draft`，创建审阅项和 RAG/GBrain upsert 作业。

### Modify

- hash 未变化时忽略。
- 正文或业务 frontmatter 变化时创建 external revision并推进 current。
- 正文变化且原状态为 reviewed 或 rejected 时自动回到 draft；仅 owner/tags/aliases 变化不强制降级。
- `review_status` 的合法修改同步到数据库和审计。

### Rename

- 通过稳定 page ID 更新 `wiki_pages.path`，创建不推进 current/generated/accepted-generated 指针的 audit-only rename revision；投影引用原 current revision + 新 path/epoch。
- 更新受管 MOC/index 和相关数据库路径；不全库重写用户正文 wikilink。
- 创建 RAG rename 和 GBrain reconcile 作业。
- 目标路径已被另一 page ID 占用时记录冲突，不覆盖任何文件。

### Delete

- 先进入持久化 delete grace；窗口到期且未配对 rename 后才软删除页面，并保留 revision、审计和冲突历史。
- RAG 当前投影立即变为不可检索，GBrain delete/reconcile 作业异步执行。
- 文件在保留期内重新出现且 page ID 相同可以恢复。

### Invalid

YAML 无法解析、字段非法、来源不存在或文件超限时：

- 不推进 current revision，不更新投影。
- 不移动、不改写用户文件。
- 对已知页面更新 `wiki_pages.sync_error`，并统一写入 `vault_sync_issues`。
- 保存 `wiki_file_observations`：上限内保留原始 bytes；超限文件只保存 stat、流式 hash 和 64 KiB prefix。页面置为 invalid，立即清空 RAG 可见水位并撤销 GBrain mapping。
- 创建 `invalid_frontmatter` 审阅项并在状态 API 显示 degraded。

`vault_sync_issues` 保存 path、file hash、可选 page ID、issue type、错误摘要、首次/最近发生时间、状态和解决时间；新文件即使尚无 `wiki_pages` 行也能被追踪。相同 path、hash 和 issue type 的未解决问题幂等合并。

## 启动对账

startup reconcile 比较：

- 磁盘 page ID/path/hash。
- 数据库 active/deleted 页面与 current revision。
- RAG current projection revision/epoch。
- GBrain projection revision/epoch/generation watermark。

结果分为 clean、external_change、missing_file、duplicate_page_id、invalid、rag_stale、gbrain_stale。自动处理明确无歧义的 external change、missing 和 stale 投影；duplicate ID、无效 frontmatter 和路径占用进入审阅队列。

## Obsidian 交付物

- `resources/obsidian-vault/.obsidian/app.json`：受版本控制的稳定默认配置。
- `resources/obsidian-vault/.obsidian/core-plugins.json`：启用 backlinks、outgoing links、tags、templates 等核心插件。
- `resources/obsidian-vault/.obsidian/templates.json`：指向 `templates/`。
- `resources/obsidian-vault/templates/Wiki Page.md`：受管页面模板，不预填伪造 revision ID。
- `resources/obsidian-vault/README.md`：说明编辑区与只读生成区。
- `ensure_obsidian_vault` 在首次启动时把缺失模板物化到运行时 `vault/`；已存在的用户配置不自动覆盖，模板版本漂移通过状态接口报告，并由显式 refresh 命令合并更新。
- `scripts/install-obsidian-vault.ps1 -Refresh` 执行显式模板刷新；刷新前备份已有配置，只合并 LGDO 管理的键和核心插件集合。
- `vault/indexes/Home.md`：MOC，链接业务域、待审阅、冲突和同步状态。
- 运行时 `vault/` 继续整体 Git 忽略；受版本控制的模板中不包含 `.obsidian/workspace*.json`、缓存和第三方插件状态。
- `scripts/open-obsidian.ps1`：使用 `obsidian://open?vault=<name>&file=<path>` 打开指定页面；找不到 Obsidian 时输出明确提示。

Web 控制台为 Wiki 页面提供“在 Obsidian 中打开”命令和 sync/conflict 状态。该按钮只负责 deep link，不承担同步逻辑。

## 配置与状态 API

新增设置：

- `VAULT_WATCH_ENABLED`，开发默认 true，测试默认 false。
- `VAULT_WATCH_DEBOUNCE_MS=750`。
- `VAULT_WATCH_STABILITY_TIMEOUT_SECONDS=3`。
- `VAULT_WATCH_MAX_FILE_BYTES=5242880`。
- `VAULT_RENAME_GRACE_MS=5000`，启动时校验其不小于 debounce、稳定检测和安全余量之和。
- `PROJECTION_WORKER_ENABLED=true`，测试默认 false。
- `OBSIDIAN_VAULT_NAME`，默认使用 Vault 目录名。

新增接口：

- `GET /vault/status`：watcher running、last event/error、drift 分类、pending/failed jobs。
- `POST /vault/reconcile`：管理员触发对账，返回 job ID，不阻塞全量外部投影。
- `GET /wiki/pages/{path}/obsidian-link`：返回编码后的 deep link。

健康状态区分 configured、running、clean、degraded；不能再因 Vault 中存在 Markdown 文件就把 Obsidian 标为 `ok`。

## 测试与验收

- 使用确定性 watcher/fake projection worker 时，Add/modify/rename/delete 在 5 秒内更新 revision、数据库、审计和投影作业。
- Web/编译器写入只产生一次 revision，watcher 不回环。
- invalid/deleted 页面恢复为历史相同 bytes 时仍恢复 lifecycle、清 sync error并重新投影。
- capture 窗口的短暂 missing 不触发 soft delete，读取 API 返回数据库 current 与 write-in-progress 状态。
- watcher 重启、服务停机期间编辑和事件重复 replay 后状态一致。
- Windows 风格 delete+add rename 被 page ID 正确关联。
- delete grace 期间页面不软删；离线 rename 在 startup reconcile 中按稳定 ID 恢复。
- 慢写 add 在 grace 截止前到达但稳定检测晚于截止时，pending delete 保持暂停，文件稳定后仍按 page ID 关联 rename。
- duplicate page ID、非法 YAML、未知 source、超限文件和路径穿越均不会覆盖内容或进入检索。
- path/ID 冲突遵循身份矩阵，复制或伪造 ID 不影响另一合法页面。
- 非法 UTF-8、YAML 注释和 CRLF/LF 输入的 raw bytes、file hash 与 semantic hash 行为有回归测试。
- 稀疏超限文件测试证明读取内存和 observation BLOB 均有界。
- reviewed/rejected 页面正文修改后变 draft，纯标签修改不降级。
- startup reconcile 能发现并修复文件/DB/RAG/GBrain 水位漂移，歧义项进入审阅。
- Obsidian deep link 正确编码空格、中文和子目录。
- `.obsidian` 稳定配置存在，workspace 和插件缓存不进入 Git。
- 确定性 E2E 中，Obsidian 修改后 5 秒内完成本地 Wiki RAG projection，Ask 命中 current revision并返回有效 Citation。
- 真实 GBrain 集成使用条件轮询：incremental 目标 120 秒、reconcile 目标 600 秒；超时显示 degraded，不把固定 sleep 或外部服务时延作为普通 CI 的 5 秒断言。
- GBrain 不可用时内容修改仍成功，状态显示 projection degraded，恢复后作业可重放。
