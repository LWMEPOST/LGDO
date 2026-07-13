# GBrain 非阻塞导入与 Citation 契约设计

状态：书面规格已确认，进入 TDD 实施计划（2026-07-13）

## 目标

将 Wiki 编译和编辑请求中的同步 GBrain CLI 导入移出请求链路，并保证所有用于回答的 GBrain 命中都能映射为授权、可定位、版本可追踪的标准 Citation。

## 已确认根因

- `compile_wiki` 在返回前同步调用 `import_vault_to_gbrain`。
- `_run_gbrain_cli` 使用阻塞 `subprocess.run`，最长等待 600 秒。
- 当前导入固定使用 `--fresh`，但它只禁用 checkpoint 恢复，不能删除幽灵页面。
- HTTP MCP 目前只提供检索链路，没有受控写入/对账工具。
- 常驻服务使用 PGLite 独占文件锁，服务运行时第二个 CLI import/sync 进程无法连接同一数据库。
- GBrain `sync --full` 要求 source 目录是有 commit 的 Git 仓库；运行时 `vault/wiki` 不满足该前提。
- `GBrainHit` 缺少 LGDO 页面/revision 来源；normalizer 还可能用正文推断编码覆盖真实字段。
- `ask` 将 GBrain 上下文当成 Citation 的替代品，导致 `require_citations=true` 时仍可返回 `citations=[]`。

## 架构

内容事务只创建 `knowledge_projection_jobs`。FastAPI lifespan 启动本地 `ProjectionWorker`，通过数据库租约领取作业。RAG 作业按页执行；GBrain 将多个页面作业合并为一次受控 Vault sync，再按页面/revision 核对结果。

GBrain 查询继续使用常驻 HTTP MCP。项目在同一个 GBrain serve 进程内增加 source-scoped `lgdo_vault_sync` MCP 工具，复用已持有锁的 PGLite engine；正常 worker 不启动第二个 CLI 进程。工具是受鉴权写操作，服务端串行执行并限制允许的 source/root。

## GBrain 进程内同步工具

### MCP 契约

`lgdo_vault_sync` 输入：

- `source_id`：LGDO 专用 GBrain source namespace。
- `root`：必须等于该 source 注册的 canonical local root，并位于 `GBRAIN_IMPORT_ALLOWED_ROOTS`。
- `mode`：`incremental` 或 `reconcile`。
- `expected_pages`：批次开始时的 `page_id/revision_id/projection_epoch/path/file_hash` 快照。
- `protected_mappings`：本轮失败、superseded 或恢复中的 `(source_id, slug/source_path)`，reconcile 不得删除。
- `no_embed` 与 `idempotency_key`。

输出包含：

- 每页 `page_id/revision_id/projection_epoch/path/source_id/slug/source_path/raw_file_hash_before/raw_file_hash_after/content_hash/page_generation/status/error`；工具原样回显受信 manifest 的 epoch，不自行推断 LGDO 水位。
- reconcile 删除的 `(source_id, slug)` 列表。
- imported、skipped、deleted、errors、chunks 和耗时汇总。

incremental 与 reconcile 都只导入 `expected_pages` 中由 LGDO revision manifest 授权的路径。reconcile 的安全 walker 只生成全目录 presence/protected set供删除对账，不导入未列、未 revision 化或 invalid 的文件。两者拒绝 symlink 和越界路径，不受父仓库 `.gitignore` 影响。工具调用 GBrain 现有 import-file 核心逻辑，不重新连接数据库，并通过进程内 mutex 保证一次只有一个写操作。

每个 expected tuple 在导入前必须同时满足：canonical path 位于 root、是非 symlink 普通文件，frontmatter `id/lgdo_page_id` 等于 expected `page_id`，`lgdo_revision_id` 等于 expected `revision_id`，raw hash 等于 expected `file_hash`。任一不匹配返回逐页 error/superseded并保护可能的旧 mapping；不能仅凭调用者给出的 slug 或文件名猜测身份。

`reconcile` 在处理 expected pages 后，按专用 `source_id` 列出已有页面，只删除 `source_path` 不在 presence set 且不在 `protected_mappings` 的页面。当前仍存在但未授权、invalid 或解析失败的文件只保护同 source path，不被导入。逐页错误必须出现在结构化结果中；aggregate 成功不能掩盖单页失败。

每页导入前后都对原始 bytes 做流式 SHA-256，并要求两次 hash 等于 expected `file_hash`；变化时返回 `superseded`，不推进 LGDO mapping。工具使用 include-deleted lookup：同 ID/slug 的软删除页面在所有文件 CAS 和 embedding 准备完成后，于同一个 GBrain 数据库事务中调用 source-scoped `restorePage`、执行 `forceRechunk=true` 的 page/chunk 更新并核对持久化 row；任一步失败都回滚，因此旧 tombstone 不会在失败路径变成可查询页面。

同 ID、不同 slug/path 视为 rename：先调用 source-scoped `engine.updateSlug(old,new)`，再 force-reimport 更新 `source_path` 和内容元数据。PGLite/Postgres 的 `updateSlug` 契约改为返回 affected-row count/boolean；前向和补偿操作都必须核对 source-scoped external page ID、旧/新 slug 和影响行数，零行不能当成功。refresh、raw-hash CAS 或后续校验失败时必须补偿 `updateSlug(new,old)`；补偿或身份校验失败则返回 `recovery_required`，旧/新 mapping 都进入 protected set，LGDO mapping 保持 stale。任何失败分支都不能随后删除旧页。

工具每处理一个页面和每个删除批次后 cooperative yield，不在整个目录外包单一长事务。单次请求最多 100 个 expected pages、每页 5 MiB、请求体 1 MiB；reconcile 内部分批并持续响应 health/query。超限返回结构化错误。

### 安全

- LGDO 配置拆分 `GBRAIN_QUERY_API_KEY` 与 `GBRAIN_PROJECTION_API_KEY`；旧 `GBRAIN_API_KEY` 只作为 query 兼容别名，不启用 projection。
- 写工具只在 projection token 具备 write scope 时开放；token 绑定唯一 managed source，handler 强制 `input.source_id == auth_context.source_id`。
- source 必须标记为 LGDO managed，且 namespace 不与其他 GBrain 内容共享。
- root canonicalize 后必须与 source local root 完全一致，并位于 allowlist。
- 日志不输出正文、token、API key 或未截断的异常载荷。
- idempotency key 和 content hash 使 MCP 超时后的重试安全。

## Projection Worker

### 执行规则

- 同一时刻最多一个 GBrain sync batch。
- 仅有 upsert 时创建 `incremental_import` 批次，合并当前 pending upsert。
- 存在 rename/delete 时创建 `reconcile` 批次，并吸收批次开始前的 pending upsert。
- incremental 调用 `lgdo_vault_sync(mode=incremental)`；rename/delete 调用 `mode=reconcile`。
- worker 使用异步 HTTP MCP client，默认超时 120 秒，reconcile 最长 600 秒，并持续续租。
- 网络错误、MCP error、超时或无法解析结构化结果均使批次失败。
- 工具不支持或 source/root 校验失败时作业保持 failed/degraded，禁止在常驻 PGLite 服务旁回退启动 CLI。

单个 batch 可包含超过 100 个 retained pages，但 worker 必须按稳定的 `page_id` 顺序切成至多 100 页的 trusted segments，并为每段保存 segment index、expected-pages 快照、状态和派生 idempotency key。`incremental_import` 的每段都调用 `mode=incremental`。`reconcile` 的前 N-1 段只调用 `mode=incremental`，最后一段才调用一次 `mode=reconcile`，由该调用完成全目录 presence scan 和唯一一次删除阶段；因此分段不会重复执行 delete finalization。若 retained pages 为 0，worker 仍创建一个 `expected_pages=[]` 的 reconcile control segment；工具只在 reconcile mode 接受空 manifest，并执行 presence/protected scan 与删除，以支持删除最后一页或空 Vault 对账。

前段返回的 deterministic page errors、`superseded`、rename compensation 和 `recovery_required` 映射必须累积进最后一段的 `protected_mappings`。任一前段发生网络错误、超时、无法解析的歧义结果，或无法确定旧/新 mapping 是否存在时，worker 不调用最后的 reconcile/delete 阶段，整批按可重试失败处理。已成功的前段可凭 segment idempotency key 安全重放，但只有所有前段结果都可明确归因后才允许最终删除；`recovery_required` 的旧、新 mapping 在人工恢复完成前跨批次持续受保护。

### 租约与重试

- worker 使用随机 `lease_owner`，租约默认 180 秒并在运行时续租。
- 进程崩溃后，过期 running 作业重新变为 pending。
- 指数退避为 5、30、120、600 秒，最多 5 次；之后保持 failed，允许人工 retry。
- idempotency key 防止相同 revision 重复入队。
- 新作业到达运行中批次时保留 pending，由下一批处理。
- job、batch 和 segment 的 succeeded、failed、superseded、retry 更新都必须使用 `id/status=running/lease_owner` CAS 并检查影响行数；续租失败或 owner 不再匹配时，旧 worker 立即停止归因，丢弃晚到 MCP 结果且不得推进 mapping。接管者只从持久化 segment/result 状态恢复。

### 批次与作业归因

`gbrain_projection_batches` 保存 batch ID、批次开始水位、纳入的 job/revision/projection-epoch 快照、mode、状态、lease owner、结构化结果和时间戳。建批时按 `page_id` 只保留与页面最新 current revision 和 desired projection epoch 同时匹配的作业；同页更旧 revision 或 epoch 的 pending 作业标记 `superseded` 并关联替代 job。

MCP aggregate 成功不等于每个 revision 都成功投影。adapter 在仍持有 lease 的前提下，使用逐页结果和只读页面元数据核对后归因：revision 仍为 current、结果 epoch 等于 expected epoch、expected epoch 仍等于 `wiki_pages.projection_epoch`、raw hash 前后均等于 expected file hash且实际页面唯一匹配时 job succeeded；文件、current 或 desired epoch 变化时 job superseded，并确保新 current/epoch 已有 pending job；页面结果 error、缺失或映射失败时对应 job failed。mapping 更新与 job terminal 更新处于同一 LGDO 数据库事务，并以 `page_id/current_revision_id/projection_epoch + job_id/running/lease_owner` CAS。MCP 整体失败时 retained jobs 按退避重试。aggregate summary 不能伪造成旧 revision 或旧 epoch 的成功记录。

### 可观测性

- 作业保存 started/finished、耗时、imported/skipped/deleted/errors/chunks 和 MCP 错误摘要。
- `GET /projection-jobs` 支持 target/status/page_id 过滤。
- `POST /projection-jobs/{id}/retry` 仅重置 failed 作业。
- `/health` 区分 GBrain query availability 与 projection backlog/degraded。
- 编译响应只返回 queued 状态和 job ID，不宣称外部导入已完成。

## RAG Wiki 投影

新增 `wiki_chunks` 投影表，按 `page_id + revision_id + projection_epoch + chunk_index` 保存 Wiki revision 分块、embedding、`source_ids_json`、ACL 元数据和路径；epoch 是物理唯一键的一部分，不能只存在页面 watermark。检索将 `wiki_chunks` 与现有原始 `document_chunks` 融合；current Wiki 内容优先于同来源旧原始摘要，但原始块仍作为证据保留。

内容 current/path/lifecycle 改变时，同一事务递增 `projection_epoch` 并清空 RAG visible 水位，旧 Wiki 投影立即 fail-closed；原始 `document_chunks` 仍可用。worker 只向其 job epoch 的物理 rows 写入，并在仍持有 lease 后把新 revision 的全部 chunk upsert 完成；随后在一个事务中以 current revision/desired epoch/job owner CAS，把 `rag_visible_revision_id/rag_visible_epoch` 切换为该 revision/epoch。异步 cleanup 只能按明确的旧 `projection_epoch` 删除，绝不按 page/revision 的宽条件删除；检索必须要求 chunk revision/epoch 同时等于页面 visible revision/epoch。失租旧 worker 即使有晚到物理写也只能落入旧 epoch，不能覆盖或删除新 epoch；无 running job 引用后由 epoch-scoped GC 清理。

现有 `fallback_wiki_search` 不再直接扫描 Vault 文件。所有 Wiki 检索入口统一要求页面 `active`、`rag_visible_revision_id == current_revision_id`、全部 source active且当前用户对全部 source 有权限；否则页面不能进入上下文。有效多来源页面为每个来源创建 Citation，不能只取第一个或以“任一来源可读”放行。实现可检索 `wiki_chunks`，或在相同 gate 后检索数据库 current revision，但不得绕过 projection 水位。

## GBrain 页面投影水位

`gbrain_page_projections` 保存：

- `id` 主键、`page_id`、`revision_id`、`projection_epoch`、`page_path`、`file_hash`、`semantic_hash`。
- `gbrain_source_id`、实际 `slug`、实际 `source_path`；身份始终是 `(gbrain_source_id, slug)`。
- `gbrain_content_hash`、`gbrain_page_generation`、`status=current/stale/deleted`、`imported_at`、`invalidated_at` 与最后作业 ID。

表使用 `UNIQUE(gbrain_source_id, slug)`，并使用 `UNIQUE(gbrain_source_id, page_id) WHERE status='current'` partial index；数据库约束同一 source/page 只能有一个 current mapping。rename 先把旧 slug 标记 stale，再由 reconcile 验证删除并创建新 mapping。

生成的 Wiki frontmatter 同时写入 `id: <lgdo_page_id>` 和 `lgdo_page_id`；前者使用 GBrain 已支持的 external identity 去重，后者供 LGDO/Obsidian 识别。MCP 结果通过 frontmatter ID、实际 slug、source path 与 namespace 核对。只有唯一匹配、批次 revision 仍是 current 且批次 `projection_epoch == wiki_pages.projection_epoch` 时才推进水位；LGDO 不以猜测文件名产生 Citation。

## Cache Generation

GBrain 工具每次成功 import/updateSlug/delete 后必须推进 page generation并失效服务端查询缓存。逐页结果返回 GBrain content hash/generation。LGDO 的 `GBrainHit` 增加 source namespace、source path、content hash 和 page generation；Citation 映射要求它们与 current projection 记录一致。

LGDO 维护持久化 `gbrain_projection_generation`，mapping 成功、stale、rename 或 delete 时在同一事务递增。查询缓存 key 包含该 generation，多进程实例读取相同水位；不能只清当前进程内字典。这样同 slug 的旧缓存 snippet 不能被重新标记为 current revision。

## Citation 映射契约

标准 `Citation` 扩展可选字段：`page_id`、`revision_id`、`chunk_id`、`origin`。原有 `source_id`、`wiki_page` 和 `snippet` 保持兼容。

GBrain hit 进入回答上下文前必须满足：

1. `(source_id, slug)` 唯一映射到 current `gbrain_page_projections`，hit content hash/page generation 与 mapping 一致。
2. 投影 revision 仍是页面 current revision，且 mapping `projection_epoch == wiki_pages.projection_epoch`；仅 revision/hash 相同但 epoch 过期也必须拒绝。
3. 页面的全部 LGDO `source_id` 都存在且 active。
4. 当前用户对页面全部来源都有读取权限；多来源页面不能部分授权。
5. snippet 非空，chunk 或 page/revision 信息能够定位。

映射成功后，为页面每个来源创建 Citation，并按 citation key 去重。GBrain 返回的 namespace source ID 只作诊断，不能覆盖 LGDO 页面来源。正文编码推断只能用于日志诊断，不能产生可回答 Citation。

## `require_citations` 语义

- 回答依据只能来自已形成授权 Citation 的本地块或 GBrain hit。
- `gbrain_context_blocks` 不再绕过 Citation 检查。
- 过滤后 citation 为空时返回明确拒答，不调用 LLM 生成事实答案。
- LLM 返回后再次执行 Citation 授权过滤；过滤为空时降级为拒答。
- diagnostics 记录 mapped、stale、unauthorized、unmapped 数量。

历史问答 `memory_hits` 不能作为无 provenance 的事实依据。`require_citations=true` 时，memory 只用于查询扩展/排序，不把历史 answer snippet 放入 LLM factual context；最终上下文必须重新检索并形成 Citation。`require_citations=false` 时仍按当前用户重新执行 ACL 过滤，并在 diagnostics 标记 memory origin。

## 删除和重命名

delete/rename 作业调用 `lgdo_vault_sync(mode=reconcile)`，由专用 namespace 内的 source-path 对账删除幽灵页面，不依赖 Git。source 未注册、root 不在 allowlist 或 namespace 非专用时作业失败并显示明确配置错误。LGDO 立即撤销相关本地 projection watermark，因此残留 hit 会被判 stale，不能进入回答。

## 测试与验收

- 开启 `GBRAIN_IMPORT_ON_COMPILE` 时，compile 入队后立即返回，不直接调用 GBrain。
- 默认 pytest 不访问真实 GBrain/DeepSeek，且无 600 秒请求等待路径。
- worker 合并、租约续期、超时、崩溃回收和退避重试有确定性测试。
- 同页多个 revision 只有最新 current 进入 batch，旧作业 superseded；结果逐页归因。
- 同 revision/path/hash 在 invalid→restore 或 delete→restore 后进入新 `projection_epoch` 时，旧在途结果不能推进 mapping或形成 Citation，新 epoch 作业必须重新投影。
- worker 失租后晚返回的成功/失败结果不能修改 job、batch、segment 或 mapping；新 owner 的 CAS 状态保持不变。
- 构造逐页 error 但 aggregate 成功的响应，失败页面不得推进 mapping。
- 批次读取前后 raw file hash 变化时返回 superseded，不能把新 bytes 归因给旧 revision。
- incremental/reconcile 均通过常驻 HTTP MCP 进程内工具执行；PGLite serve 运行时不启动第二个 CLI。
- 真实 rename 先 source-scoped updateSlug，再 force-reimport 更新 source path；旧 slug 被清理且新 slug 可查询。
- 软删除页面再次出现时，在同一事务中执行 source-scoped `restorePage`、force-reimport 和 row 校验；成功后页面可查询且 mapping 推进到 current revision，任一失败保持 tombstone。
- rename refresh/raw-hash 校验失败时补偿恢复旧 slug；补偿失败返回 `recovery_required`，旧、新 mapping 均受保护且 LGDO 水位不前移。
- `updateSlug` 前向或补偿返回零行、错误 source 或错误 external ID 均视为失败，不能伪装成已完成 rename。
- 超过 100 页的 incremental/reconcile 按至多 100 页分段；reconcile 只有末段执行一次 presence scan/delete，任一前段网络或歧义失败时不执行删除。
- 删除最后一页和空 Vault reconcile 使用空 control segment，仍执行且只执行一次 delete finalization。
- `recovery_required` 和其他跨段 protected mappings 在后续 reconcile 中不会被幽灵页清理误删。
- GBrain-only 有效映射响应包含授权 source、Wiki page 和 revision Citation。
- unmapped、stale、inactive source、ACL 拒绝的 hit 不进入回答上下文。
- 只有无效 GBrain hit 或 memory hit时，不调用 LLM 生成事实答案。
- RAG 投影成功后 Ask 使用新 revision；失败时旧 Wiki chunks 不可检索，原始 chunks 仍可用。
- 同一 revision 在新 epoch 重投影时，失租旧 worker 的晚写不能覆盖新 epoch rows，旧 epoch cleanup 不能删除新 rows；查询只命中 visible epoch。
- direct-file Wiki fallback 不能绕过 active/current-visible gate；多来源页面缺少任一 source 权限时不可检索。
- 编译、Web 编辑和 watcher 编辑只产生幂等投影作业。
- 运行态可查询 backlog、失败原因、最近成功水位和 query 健康状态。
- 使用真实临时 PGLite serve 和专用 source 验证服务持续运行时可导入；rename/delete reconcile 清除旧 `(source_id, slug)`，查询端不引用旧 mapping。
- root allowlist、source namespace、symlink、父仓库 `.gitignore` 和逐页 error 输出均有 GBrain 侧测试。
- root 中存在格式合法但未列入 `expected_pages` 的 Markdown 时，incremental/reconcile 都不会导入它；伪造或不一致的 page ID、revision ID、path、frontmatter、hash tuple 被逐页拒绝并且不推进 mapping。
- query token 调用写工具被拒绝，projection token 只能写其绑定 source；legacy admin token 不被 LGDO query client 使用。
- 写入前缓存旧 snippet，投影后验证 GBrain 服务端与 LGDO 多进程 generation cache 都不返回旧内容。
- 真实 97 页 sync 期间 `/health` P95 小于 1 秒、查询 P95 小于 5 秒，且工具按页 yield、无单一目录长事务。
