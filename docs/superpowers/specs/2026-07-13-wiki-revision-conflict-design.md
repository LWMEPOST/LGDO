# Wiki Revision 与冲突模型设计

状态：书面规格已确认，进入 TDD 实施计划（2026-07-13）

## 目标

让 `vault/wiki` 成为人工可编辑的知识内容事实源，同时保证编译、Web 编辑和 Obsidian 编辑都不会静默覆盖人工内容。所有内容变化保存为不可变 revision；数据库、RAG 和 GBrain 只保存可重建投影。

## 范围

- 为 Wiki 页面增加稳定 `page_id`、当前 revision 和最近生成 revision。
- 保存 generated、manual、external、rename、merge 等来源的完整修订正文。
- 编译时区分可安全自动推进与需要人工解决的冲突。
- Web 保存、审阅状态修改、重命名和冲突解决统一经过 revision 服务。
- 使用持久化投影作业驱动 RAG 和 GBrain 更新。
- 为旧 SQLite/PostgreSQL 数据库提供幂等迁移和保守的首次快照。

本阶段不实现 watcher 进程和 Obsidian 配置；它们消费本设计定义的 revision API 与投影事件。

## 核心不变量

1. `raw/` 是不可变证据；编译只读取 `normalized/` 或 `raw/`。
2. `vault/wiki` 中的文件是用户看到和编辑的当前内容；若文件无效，系统保存 observation 并 fail-closed，最近有效 current revision 只用于历史与修复，不继续投影。
3. revision 一旦创建就不修改正文、hash、来源和父 revision。
4. 有效稳定状态下 `wiki_pages.current_revision_id` 必须指向文件当前内容对应的 revision；短暂写入窗口必须有 `pending_write_intent_id`。无效状态下磁盘 file hash 必须对应 `wiki_file_observations`，并设置 `sync_error`。
5. `wiki_pages.generated_revision_id` 只表示最近一次编译结果，不因人工编辑而推进。
6. 新页面可直接采用首个 generated revision；已有页面只有 `current_revision_id == generated_revision_id` 且磁盘 hash 一致时，编译才可自动替换当前文件。
7. 其他情况一律保留当前文件，保存 generated candidate；未被明确处理过的新 candidate 才创建冲突审阅项。
8. RAG/GBrain 更新失败不回滚当前内容；失败留在可重试的持久化作业中。
9. 任意有效的未知磁盘内容先保存为 external revision；无效内容保存 invalid observation并撤销投影，不伪装成有效 revision。

## 数据模型

### `wiki_pages` 增量字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `page_id` | TEXT | 稳定页面身份；唯一索引，路径变化时不变 |
| `current_revision_id` | TEXT NULL | 当前 Vault 内容 revision |
| `generated_revision_id` | TEXT NULL | 最近编译生成 revision |
| `accepted_generated_revision_id` | TEXT NULL | 最近明确保留、接受或合并处理过的 generated candidate |
| `revision_number` | INTEGER NOT NULL DEFAULT 0 | 页面内单调递增序号；旧行先回填 0 |
| `file_hash` | TEXT NULL | 当前完整 Markdown 字节的 SHA-256 |
| `semantic_hash` | TEXT NULL | 排除 LGDO 受管字段后的规范化内容 hash |
| `last_write_token` | TEXT NULL | 最近一次 LGDO 原子写入 token |
| `rag_visible_revision_id` | TEXT NULL | 当前可用于 Wiki RAG 的 revision；current 改变时先清空 |
| `projection_epoch` | INTEGER NOT NULL DEFAULT 0 | current/path/lifecycle 每次需要重投影时单调递增；旧行先回填 0 |
| `rag_visible_epoch` | INTEGER NULL | RAG 已完成的 projection epoch |
| `lifecycle_status` | TEXT | `active`、`invalid`、`deleted`，默认 `active` |
| `deleted_at` | TEXT NULL | Vault 删除时间 |
| `sync_error` | TEXT NULL | 最近一次解析或同步错误 |
| `observed_file_hash` | TEXT NULL | watcher 最近观察到的磁盘字节 hash，包括 invalid 文件 |
| `pending_write_intent_id` | TEXT NULL | 正在应用的持久化 Vault 写入意图 |

现有 `path` 主键保留，以兼容当前 API。`page_id` 是 revision、rename 和投影使用的稳定身份。

### `wiki_page_revisions`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT PK | `wrev_<uuid>` |
| `page_id` | TEXT | 稳定页面身份 |
| `page_path` | TEXT | 创建 revision 时的路径快照 |
| `revision_number` | INTEGER | 页面内序号 |
| `file_hash` | TEXT | 包含受管 frontmatter 的完整 UTF-8 正文 SHA-256 |
| `semantic_hash` | TEXT | 排除 `lgdo_revision_id`、`lgdo_write_token` 和受管更新时间后的规范化 hash |
| `content` | TEXT | 完整 Markdown 正文 |
| `origin` | TEXT | `generated`、`manual`、`external`、`rename`、`merge`、`legacy` |
| `base_revision_id` | TEXT NULL | 生成或编辑所基于的 revision |
| `source_ids_json` | TEXT | 解析后的来源 ID |
| `actor` | TEXT NULL | 用户、`compiler`、`vault-watcher` 等 |
| `note` | TEXT NULL | 编辑或解决说明 |
| `metadata_json` | TEXT | 解析后的 frontmatter 与扩展元数据 |
| `idempotency_key` | TEXT | 编译、API 请求或 watcher 事件的稳定去重键 |
| `created_at` | TEXT | ISO-8601 时间 |

约束和索引：

- `UNIQUE(page_id, revision_number)`。
- `UNIQUE(idempotency_key)`。
- `INDEX(page_id, created_at)`。
- `INDEX(semantic_hash)`。
- revision 不因相同 hash 全局复用；rename、merge 等事件即使正文相同也保留独立审计。只有同一 `idempotency_key` 的事件重放复用原 revision。

### `vault_write_intents`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT PK | `wint_<uuid>` |
| `page_id` | TEXT | 页面身份 |
| `revision_id` | TEXT | 将要写入的 revision |
| `expected_revision_id` | TEXT NULL | 写入开始时的 current revision |
| `expected_file_hash` | TEXT NULL | 写入前完整文件 hash；新页面为空 |
| `target_path` | TEXT | Vault 相对路径 |
| `write_token` | TEXT | 写回循环抑制 token |
| `backup_path` | TEXT NULL | capture-before-replace 保存的原文件路径 |
| `captured_file_hash` | TEXT NULL | 已捕获原文件 hash |
| `backup_last_observed_hash` | TEXT NULL | recovery monitor 最近观察到的 backup bytes |
| `backup_retention_status` | TEXT | `none`、`retained`、`change_detected`、`released`；默认不自动释放 |
| `status` | TEXT | `pending`、`captured`、`installed`、`recovery_required`、`applied`、`superseded`、`aborted`、`failed` |
| `executor_owner` | TEXT NULL | 当前执行器 ID |
| `lease_expires_at` | TEXT NULL | 执行租约 |
| `attempts` | INTEGER | 执行次数 |
| `last_error` | TEXT NULL | 安全截断错误 |
| `created_at` / `updated_at` | TEXT | 时间戳 |

创建 intent 的事务同时设置 `wiki_pages.pending_write_intent_id`。同一页面最多有一个 pending intent；后续写入在前一个 intent 完成前返回冲突或由 reconcile 接管。

### `wiki_file_observations`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT PK | 磁盘 observation ID |
| `page_id` | TEXT NULL | 能从路径或受管字段识别时填写 |
| `page_path` | TEXT | Vault 相对路径 |
| `file_hash` | TEXT | 原始磁盘字节 hash |
| `size_bytes` / `mtime_ns` | SQLite `INTEGER` / PostgreSQL `BIGINT` | 文件 stat 元数据；纳秒时间和大文件大小不得落入 PostgreSQL 32 位 `INTEGER` |
| `content_bytes` | BLOB/BYTEA NULL | 上限内的未改写原始字节 |
| `content_prefix` | BLOB/BYTEA NULL | 超限文件最多 64 KiB 前缀 |
| `content_truncated` | INTEGER/BOOLEAN | 是否只保存有界前缀 |
| `parse_status` | TEXT | `valid` 或 `invalid` |
| `error_code` / `error_message` | TEXT NULL | 安全分类和截断说明 |
| `observed_at` | TEXT | 观察时间 |

`UNIQUE(page_path, file_hash)` 只去重原始 bytes 工件，不代表一次状态转移。上限内文件保存完整 bytes；超限文件使用固定缓冲区流式计算 hash，只保存 stat 和最多 64 KiB prefix，原文件留在 Vault，不整文件读入内存或数据库。valid external 先保存 observation，再由具体 change event 关联生成的 revision；invalid observation 不作为 current revision，也不进入检索。对应页面立即清空 RAG 可见水位并撤销 GBrain 映射。文件修复后按 external ingest 创建有效 revision、恢复 `active` 并关闭 sync issue。

### `vault_change_events`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT PK | watcher 归并后的一次事件 occurrence |
| `kind` | TEXT | add、modify、rename、delete |
| `page_path` / `old_page_path` | TEXT | 观察路径 |
| `observation_id` | TEXT NULL | 原始 bytes 工件 |
| `expected_state_json` | TEXT | current/generated/accepted/lifecycle/path/file hash 基线 |
| `status` | TEXT | pending、applied、ignored、failed |
| `result_revision_id` | TEXT NULL | 本次转移结果 |
| `detected_at` / `updated_at` | TEXT | 时间戳 |

同一 event ID replay 返回原结果；A→B→A 是三个 occurrence，即使最后复用 A 的 observation blob，也必须按当时 expected state 执行新的转移。

### `review_items` 增量字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `page_id` | TEXT NULL | 稳定页面身份 |
| `base_revision_id` | TEXT NULL | 冲突开始时的当前 revision |
| `candidate_revision_id` | TEXT NULL | generated 或 concurrent-write 冲突候选 revision |
| `resolution_revision_id` | TEXT NULL | 最终采用的 revision |
| `expected_state_json` | TEXT | 创建冲突时的 current/generated/lifecycle/path 基线 |
| `resolved_at` | TEXT NULL | 解决时间 |

`issue_type=content_conflict` 每页最多一个 pending 项，使用 partial unique index 约束。新 generated candidate 到来时，在页面锁内把旧 pending 项标记 `superseded` 并创建新项；历史项保留。冲突解决必须对 review status、current 与 generated 三者做 CAS。

冲突分两类：

- `content_conflict`：candidate origin 为 generated；accept/keep/merge 按 generated 状态机更新 accepted-generated。
- `concurrent_write_conflict`：manual/external/status intent 与另一人工写入竞争；accept candidate 或 merge 只推进 current，绝不修改 generated/accepted-generated。

两类分别最多一个 pending。API 响应必须包含 issue type，resolve service 按类型执行，不能把 manual candidate 当 generated candidate。

任何 current、generated、lifecycle 或 path 变化时，协调器先在同一页面锁内 supersede `expected_state_json` 不再匹配的两类 pending review，再按新的 state vector 重建仍成立的冲突或关闭它。不能让旧 base review 永久占用 partial unique index。

### `knowledge_projection_jobs`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT PK | 作业 ID |
| `idempotency_key` | TEXT UNIQUE | `target:operation:page_id:revision_id:projection_epoch` |
| `target` | TEXT | `rag` 或 `gbrain` |
| `operation` | TEXT | `upsert`、`delete`、`rename`、`reconcile` |
| `page_id` | TEXT NULL | 页面身份 |
| `revision_id` | TEXT NULL | 目标 revision |
| `projection_epoch` | INTEGER | desired projection 水位 |
| `payload_json` | TEXT | 路径、旧路径等参数 |
| `status` | TEXT | `pending`、`running`、`succeeded`、`failed`、`superseded` |
| `attempts` | INTEGER | 尝试次数 |
| `available_at` | TEXT | 下次可执行时间 |
| `lease_owner` | TEXT NULL | worker 身份 |
| `lease_expires_at` | TEXT NULL | 租约过期时间 |
| `last_error` | TEXT NULL | 截断后的错误信息 |
| `created_at` / `updated_at` | TEXT | 时间戳 |

## 迁移策略

- SQLite 使用 `PRAGMA table_info` 检查后逐列 `ALTER TABLE ADD COLUMN`。
- PostgreSQL 使用 `ADD COLUMN IF NOT EXISTS`，再创建索引。
- `revision_number` 与 `projection_epoch` 在两种数据库都以 `NOT NULL DEFAULT 0` 增加。PostgreSQL 兼容中间库时先回填 NULL，再 `SET DEFAULT 0/SET NOT NULL`。SQLite 缺列时可直接 `ADD COLUMN ... NOT NULL DEFAULT 0`；若检测到列已存在但 nullable/default 不正确，则在 `BEGIN IMMEDIATE` 下创建完整目标结构的 `wiki_pages__new`，用 `COALESCE(revision_number, 0)`、`COALESCE(projection_epoch, 0)` 复制全部行，核对行数与 page/path 唯一性后原子 drop/rename，并重建该表全部索引和触发器。提交前以 `PRAGMA table_info`、`PRAGMA index_list` 和 `PRAGMA foreign_key_check` 验证；失败整笔回滚。所有 `+ 1` 更新前均保证非 NULL。
- 同步更新 `MAIN_TABLES`、`TABLE_PRIMARY_KEYS` 和迁移列白名单。
- 旧页面首次 read/save/compile/reconcile 时分配 `page_id` 并保存 `legacy` revision。
- 旧页面没有可证明的 generated 基线，因此首次快照只设置 `current_revision_id`，`generated_revision_id` 保持 NULL。下一次显式编译创建冲突候选，避免误覆盖历史人工修改。
- 旧 SQLite 到新 PostgreSQL 迁移允许源库缺少新增列，目标库使用默认值。

## 组件边界

### `WikiRevisionService`

负责 file/semantic hash、frontmatter 解析、revision 创建、当前指针推进、漂移接纳、冲突创建和解决。编译器、catalog API 与 watcher 不直接写 Wiki 文件或 revision 表。

所有 external 文件先按原始 bytes 保存 observation。UTF-8 解码和受限 round-trip YAML 解析成功后，service 只更新受管字段，保留正文、注释、键顺序和用户格式；旧的 `id/lgdo_page_id/lgdo_revision_id/lgdo_write_token` 按身份规则验证或替换。分配新 revision/token 后计算 file/semantic hash并通过 write intent 写回。由此产生的第二个文件事件可以按 revision、token 和 file hash精确抑制。

Hash 算法固定为：

- `file_hash = SHA-256(raw file bytes)`，不做编码或换行转换。
- `semantic_hash = SHA-256(UTF-8(canonical JSON of parsed frontmatter excluding id、lgdo_* 和受管更新时间) + LF + body with CRLF normalized to LF)`。
- canonical JSON 使用 UTF-8、键排序和稳定分隔符；数组顺序保留。
- YAML 注释/排版变化会改变 file hash并留 observation，但不改变 semantic hash。

### `PageMutationCoordinator`

所有会创建 revision、分配 `revision_number` 或更新 current/generated/accepted/path/lifecycle 的流程都经过同一协调器：compile candidate、manual save、external ingest、rename、delete、status update 和 conflict resolve 均不例外。

- PostgreSQL 在事务中 `SELECT ... FOR UPDATE` 页面行；SQLite 使用 `BEGIN IMMEDIATE`。
- 新页面在同一事务中插入唯一 path/page ID；唯一冲突使整笔 revision/intent 事务回滚，不留下孤立 revision。
- revision number 在锁内从 `wiki_pages.revision_number + 1` 分配并同步更新。
- 指针更新使用 expected current/generated/pending-intent CAS，并检查影响行数。
- revision transition key 包含 command/event ID 和 expected state vector：current、generated、accepted-generated、lifecycle、path、file hash 与 pending conflict。
- compile 使用客户端/调度器 `compile_job_id`；API save/status 使用 `request_id`；watcher 使用持久化 `vault_change_events.id`。
- 同一命令和同一 expected state replay 返回原结果；相同 source/hash 在人工编辑后再次 compile，或 A→B→A 的新 event，因 command/state 不同而重新执行正确转移。
- compile 在锁内发现最新 generated semantic hash、source hash 与 compiler version 都未变化时复用 generated artifact，不新建 revision；但仍必须按最新 state vector 执行 conflict supersede/create/refresh。只有 artifact 和状态转移都已满足时才整次 no-op。

### `AtomicVaultWriter`

只负责把已创建 intent 的 revision 内容安全安装到目标路径，并且永不以 replace 覆盖一个仍可能被 Obsidian 改写的目标：

1. 在目标同文件系统的 `.lgdo/pending/<intent_id>/` 写入并 fsync 新文件。
2. 目标存在时，原子移动到 intent 唯一的持久 backup，状态改为 captured；打开的编辑器句柄后续写入也落到 backup。
3. 等待 captured backup 稳定，核对其 hash 等于 `expected_file_hash`。不匹配时停止安装、保留 target/backup并标记 recovery-required，由协调器原子 handoff 到 human observation successor。
4. 只有目标路径仍不存在时，使用同文件系统 hard-link/create-if-absent 或平台 no-replace rename 安装新文件；目标已存在则绝不覆盖并 abort。
5. 核对目标 hash 后把 intent 标记 installed，revision service 才能 finalize。backup 在 finalize 后仍保持有路径的持久文件并标记 `retained`，不得因短暂 post-write stability 通过而自动 unlink。

它不直接修改 revision 指针。所有 backup/temp 都位于受管隐藏目录，普通 watcher 不把它们当 Wiki 输入；专用 recovery monitor 在启动和周期对账时检查 retained backup。backup hash 发生任何后续变化都先保存完整 observation并创建 recovery/conflict，再更新 retention 状态。系统不尝试用 advisory lock 推断 Obsidian 已关闭旧句柄。

retained backup 默认无限期保存。自动 GC 禁止删除它；只有显式管理员释放操作在展示 backup/current hash、关联 observation 和 intent 后才能执行，并记录审计。若平台未来提供可证明的 deny-write/exclusive capture，可在单独批准的实现中增加自动释放，但本阶段不把“等待若干秒未变化”视为安全证明。

### `IntentExecutor`

多个 Uvicorn 进程或重复 startup reconcile 不能同时执行同一 intent。执行器先以数据库 CAS 领取 `executor_owner/lease_expires_at`，再持有 `.lgdo/pending/<intent_id>/executor.lock` 的跨进程 OS advisory lock；lease 运行中续期。每个 capture/install/finalize/abort phase 都以 `intent_id + expected_status + executor_owner` CAS 更新并检查一行受影响。未持有 owner/OS lock 的进程只观察，不得根据文件状态 abort、清 pending 或推进 current。进程崩溃后 OS lock 自动释放，租约过期方可接管。

### `ProjectionOutbox`

与 revision 指针更新处于同一数据库事务，幂等创建 RAG/GBrain 作业。投影 worker 不拥有内容真源写权限。

每次 current、path 或 lifecycle 发生需要下游重放的转移，`projection_epoch` 加一并清空 RAG visible 水位；作业 key 包含 epoch。即使历史 revision 再次成为 current，restore/delete 或路径往返，也会创建新 epoch 作业，不被旧 succeeded job 阻挡。投影只有在 revision 与 epoch 都匹配当前 desired state 时才能推进水位。

## 主要流程

### 编译

1. 读取数据库页面与磁盘文件。
2. 若磁盘 file hash 与 current revision 不同，先按 external ingest 状态机处理；解析无效时保存 invalid observation并停止该页编译，绝不以 generated 内容覆盖无效但未知的用户文件。
3. 记住编译前的 current 和 generated 指针，生成 Markdown并保存 generated revision。
4. 新页面或可安全自动推进的已有页面创建 write intent，原子写入后完成 intent并推进 current，审阅状态改为 `draft`。
5. 每次 generated 指针改变前，先 supersede 所有 candidate 不等于新 generated 的 pending `content_conflict`。若当前与 generated 分叉，且新 candidate semantic hash 与 `accepted_generated_revision_id` 所指 revision 不同，再创建唯一新项；已处理过的 semantic candidate 不创建替代项。
6. 无论自动采用还是发生冲突，`generated_revision_id` 都推进到本次 generated revision。
7. 只有 current 真正变化时才创建 RAG/GBrain upsert 作业。
8. 编译 API 返回 created、updated、conflicted 与 projection job 计数，不等待外部导入。

### Web/API 保存

1. 先验证页面元数据存在、期望 revision 与当前 revision 相同。
2. 解析 frontmatter、校验来源和状态。
3. 在数据库事务中插入 manual revision、创建 pending write intent并提交；current 尚未改变。
4. capture/no-replace 安装完成后，用条件更新完成 intent、推进 current并创建投影作业；generated 指针不变。
5. 并发版本不一致返回 HTTP 409，响应包含最新 revision ID。

### 审阅状态更新

审阅状态是 Wiki frontmatter 的一部分。状态 API 读取当前正文、更新 frontmatter、创建 manual revision并走相同写入流程，避免只改数据库。

### 冲突解决

以下三种 generated/accepted 指针语义用于 `content_conflict`：

- `keep_current`：保留 current，关闭冲突，把 candidate 记为 `accepted_generated_revision_id`，不创建新正文 revision。
- `accept_candidate`：直接通过 write intent 采用 candidate revision，使 current、generated 和 accepted-generated 指针相同。
- `merged_content`：以提交的合并正文创建 merge revision并推进 current，同时把冲突 candidate 记为 accepted-generated；current 与 generated 保持分叉，后续不同 candidate 仍需冲突审阅。

解决请求必须携带 `expected_current_revision_id` 和 `expected_generated_revision_id`；review 仍为 pending、candidate 仍等于当前 generated 时才执行，否则返回 409。由此旧 conflict 不能回退 generated 指针。所有解决方式记录 actor、note、resolution revision 和审计事件。接受 candidate 或 merged content 后创建投影作业。

上述 candidate==generated 条件只适用于 `content_conflict`。`concurrent_write_conflict` 改为校验 candidate 仍是未被替代的写入 revision和 expected current/generated 都未变化；accept/merge 后 generated 与 accepted-generated 保持原值。

`concurrent_write_conflict` 也提供 keep-current、accept-candidate 和 merged-content，但 `generated_revision_id` 与 `accepted_generated_revision_id` 保持不变。

下一次编译状态转移（content conflict）：

| 当前状态 | 新 generated 与已处理 candidate | 行为 |
| --- | --- | --- |
| current == generated | 任意新版本 | 自动推进 |
| keep-current 或 merged 后仍分叉 | semantic hash 相同 | 不重复创建冲突 |
| keep-current 或 merged 后仍分叉 | semantic hash 不同 | 创建新冲突 |
| accept-candidate 后未再人工编辑 | 任意新版本 | current == generated，自动推进 |
| accept-candidate 后又人工编辑 | 任意不同版本 | 创建冲突 |

### 删除与重命名

- rename 根据 `page_id` 更新 `wiki_pages.path` 并递增 `projection_epoch`，创建一条 `origin=rename/base_revision_id=<current>` 的 audit-only revision；该 revision 保存新路径和当时正文用于审计，但不推进 current/generated/accepted-generated 指针，因此不会把原本 `current == generated` 的页面制造成内容分叉。rename 投影作业引用原 `current_revision_id`、新 path 和新 epoch，历史 revision 的路径快照不改写。
- delete 标记 `lifecycle_status=deleted`，保留 revision，创建 delete 投影作业；文件重新出现时可恢复。

## API 契约

- `GET /wiki/pages/{path}/revisions`：分页返回 revision 元数据，不默认返回全文。
- `GET /wiki/revisions/{revision_id}`：返回指定 revision 全文。
- `GET /wiki/pages/{path}/conflicts`：返回 pending 冲突及 base/candidate 摘要。
- `POST /wiki/conflicts/{review_id}/resolve`：提交 `keep_current`、`accept_candidate` 或 `merged_content`，并携带 expected current/generated revision。
- `PUT /wiki/pages/{path}`：请求必须包含 `expected_revision_id`；响应返回新 revision 与投影状态。

Web 客户端和 watcher 必须显式传递 `expected_revision_id`。缺失返回 HTTP 428，版本不一致或页面有 pending intent 返回 HTTP 409，避免把基于旧正文的保存误当成基于最新版本。

## 崩溃与错误恢复

文件系统和数据库无法组成单一原子提交，因此采用 write intent 协议：第一笔事务保存 revision 和 pending intent，但不推进 current；随后执行 capture-before-replace 安装；第二笔事务以 intent、期望 current 和目标 file hash 为条件完成指针推进、创建 outbox并标记 applied。worker 只能看到完成 intent 后创建的作业。

prepare 阶段 PostgreSQL 使用 `SELECT ... FOR UPDATE` 锁页面行；SQLite 使用 `BEGIN IMMEDIATE` 串行化写事务。两者都在同一事务中检查 expected revision、pending intent 为空和 expected file hash，然后设置 pending intent。finalize 再次锁行并使用 `pending_write_intent_id + expected_revision_id` 条件更新，检查影响行数为 1；任何 CAS 失败都交给 reconcile，不继续覆盖。进程内 page lock 只用于减少竞争，正确性依赖数据库锁与 CAS。

启动 reconcile 同时检查 intent phase、target、backup 和 temp：expected 内容仍在 target 或 backup 时继续安装；target 已是 revision hash 时完成 intent；target/backup 任一出现未知 hash 时分别保存 external/invalid observation，保留双方并进入 recovery handoff。初始数据库事务失败时没有 intent，文件保持原样；捕获或安装失败时 current 不变；完成事务失败时 installed intent 可由 reconcile 完成。applied intent 的 retained backup 仍由 recovery monitor 在每次启动和周期任务中对账；后续任意时刻发生变化都保存为 observation/conflict，且 backup 只经显式审计释放，不存在按稳定时长自动清理的分支。

终态矩阵：

| target | backup | 动作与最终 current |
| --- | --- | --- |
| expected old | 无 | 继续 capture；current 保持 expected |
| 缺失 | expected old | 继续 no-replace 安装；current 保持 expected 到 finalize |
| intended revision | expected old | finalize intended，清 pending，current 推进 intended |
| external unknown | expected old | human wins：保存 target observation，原子 handoff 到 external successor intent；intended 保留为未采用 revision/conflict candidate |
| intended revision | external unknown | late writer wins：保留 intended，原子 handoff 到基于 backup observation 的 external successor；intended 进入对应类型冲突 |
| external unknown | external unknown | 两份 bytes 都保存；path-visible target 作为 successor 内容，backup 为 secondary conflict，投影 fail-closed |
| 缺失 | external unknown | 原子 handoff 到基于 backup observation 的 successor，恢复 human 内容 |
| 缺失 | 无 | failed/missing issue；current 保持最近有效 revision，投影 fail-closed |

`applied`、`aborted` 和 `failed` 都必须在页面锁内以 intent ID CAS 清除 `pending_write_intent_id`；CAS 失败由 reconcile 重试，不能让页面永久 409。human-wins 恢复使用新的 intent，不直接覆盖 target。

human-wins handoff 不允许“先清 pending、后建新 intent”。协调器在同一页面锁事务中创建 external observation/revision/successor intent，把旧 intent 标记 `superseded`，并将 `pending_write_intent_id` 从旧 ID 直接 CAS 为 successor ID。如果 successor 不能完整准备，旧 intent 保持 `recovery_required` 且仍被 pending 指针引用，页面继续 fail-closed；不存在可被其他 writer 插入的 null-pointer 窗口。

watcher 在任何路径存在 active intent 时延迟 missing/delete 事件，直到 intent terminal；隐藏 backup/temp 永远忽略。启动顺序先恢复 intents，再做普通文件对账，最后启动 watcher。读取 API 在 captured/installed 窗口从数据库 current revision 返回稳定正文，并附 `write_in_progress` 与 intent ID，不因目标路径短暂缺失返回 404。

无效 frontmatter 不推进 current revision：保存不可变 invalid observation 和 `sync_error`，把页面置为 invalid、清空 RAG 可见水位、撤销 GBrain projection mapping，并创建 `invalid_frontmatter` 审阅项。用户文件保持原样。

## 测试与验收

- 人工保存后显式 recompile 不覆盖正文，并产生唯一冲突候选。
- Obsidian/磁盘直接修改后 recompile 同样不覆盖。
- current 等于 generated 时 recompile 自动推进 revision。
- 同一 command/event replay 不产生重复 revision 或冲突；相同 bytes 在不同 expected state 下仍执行新转移。
- generated artifact 未变但 current 已人工修改时，compile 复用 artifact并创建/刷新冲突，不做整次 no-op。
- keep-current、accept-candidate、merged-content 三种解决后再次编译符合状态转移表。
- 新 candidate supersede 旧 conflict；对旧 review 或旧 expected-generated 的 resolve 返回 409，generated 指针不回退。
- current/lifecycle/path 变化会 supersede base state 过期的两类 review，并按新 state 重建。
- rename 可在正文 hash 不变时创建独立审计 revision，事件 replay 依赖 idempotency key 去重。
- current==generated 的页面 rename 后保持两指针相等；随后相同内容 recompile 不产生伪 `content_conflict`，rename 投影使用 current revision + 新 path/epoch。
- 两个并发 writer 在 SQLite 和 PostgreSQL 下只有一个能 prepare intent，另一个得到 409。
- 两个 Uvicorn/reconcile 进程竞争同一 intent 时，DB lease、OS lock 和 phase-owner CAS 保证只有一个执行器推进状态。
- manual/status 与 external edit 竞争时创建 concurrent-write conflict，解决它不改变 generated/accepted-generated。
- 同一 revision 在 delete/restore 或版本往返后再次 current 时 projection epoch 递增，RAG/GBrain 必须重新投影。
- DB 元数据缺失、版本冲突或创建 intent 的 DB 写失败时，原文件保持不变。
- 状态 API 同时更新 frontmatter、revision 和数据库元数据。
- valid/invalid external 都保存原始 bytes observation；非法 UTF-8/YAML 撤销投影且不推进 current，修复后恢复 active revision。
- 稀疏超大文件通过固定内存流式 hash，只保存有界 prefix，不把完整内容写入 observation 表。
- rename/delete 保留历史并创建正确投影作业。
- 进程在 intent 创建、文件替换、指针推进三个位置中断后均可 reconcile。
- Obsidian 在 capture 前、capture 后和安装前并发写入时，未知字节始终保留在 target 或 backup，LGDO 不做覆盖式 replace。
- 编辑器长期持有 capture 前句柄并在 finalize/重启后才写入时，retained backup 仍有目录项；recovery monitor 保存迟到 bytes 为 observation/conflict，自动 GC 不会让它们静默消失。
- 旧 SQLite、旧 PostgreSQL 和 SQLite 到 PostgreSQL 迁移均通过。
- 人工构造已存在 nullable `revision_number/projection_epoch` 且含 NULL 的半迁移 SQLite fixture，升级后两列均为 `NOT NULL DEFAULT 0`、数据/索引/外键完整且可正常递增。
- 每个内容变化都有 audit log，且可从页面追溯到 current/generated/conflict revision。
