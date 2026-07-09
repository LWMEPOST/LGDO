# GBrain 常驻 HTTP MCP 与阿里云检索兜底设计

## 目标

将 LGDO 当前按请求启动的本地 GBrain stdio 子进程替换为常驻 HTTP MCP 服务，降低冷启动和并发开销；当 GBrain 超时、失败、无结果或结果被 ACL 全部过滤时，使用阿里云百炼 `text-embedding-v3` 对 LGDO 文档块执行独立语义召回；阿里云不可用时保留 BM25/关键词检索。

## 已确认约束

- 使用 LGDO PowerShell 启动脚本确保 GBrain 已启动。
- GBrain 在 LGDO 退出后继续后台运行。
- GBrain 仅监听 `127.0.0.1`。
- 使用 `http://127.0.0.1:8787/mcp` 和 LGDO 专属 Bearer token。
- 阿里云 Embedding 是独立于 GBrain 的第二检索通道。
- 百炼专属端点单批最多 10 条，向量维度 1024。

## 架构

1. LGDO 启动脚本检查 GBrain `/health`，健康则复用；未监听则隐藏启动 `gbrain serve --http`。
2. 在线问答并行执行本地词法检索与 GBrain MCP 检索。
3. GBrain 正常返回时，将 GBrain hits 与本地候选融合。
4. GBrain 失败、超时、空结果或 ACL 后为空时，LGDO 调用阿里云生成查询与候选文档向量并执行语义重排。
5. 阿里云失败时继续使用现有 BM25、关键词、短语和规则评分，不中断问答。

## 检索融合

- 一级候选：现有 SQLite/PostgreSQL 文档块词法候选。
- 一级增强：GBrain 图谱/关系检索结果。
- 二级兜底：阿里云 query/document embedding 对候选文档块重排。
- 最终排序使用 RRF 融合词法名次和阿里语义名次，避免不同分数尺度直接相加。
- 硬编码规则暂不一次删除，后续通过评测逐步降低和移除。

## 缓存与熔断

- 保留 GBrain 查询缓存，默认 TTL 300 秒。
- 新增阿里云 embedding 内存 TTL/LRU 缓存，默认 600 秒、最多 256 条。
- GBrain 连续失败达到 3 次后熔断 30 秒；成功请求清零失败计数。
- 阿里云请求超时默认 3 秒，失败不影响 BM25 返回。

## 启动与运维

- `scripts/start-lgdo.ps1`：健康探测、端口占用检查、后台启动 GBrain、等待就绪、启动 Uvicorn。
- `scripts/status-gbrain.ps1`：输出健康状态、监听端口和 MCP 鉴权结果。
- `scripts/stop-gbrain.ps1`：仅停止命令行包含 GBrain serve 且监听目标端口的进程。
- 日志输出到 `output/gbrain-http.stdout.log` 和 `output/gbrain-http.stderr.log`。

## 性能目标

- 消除每次问答的 Bun/PGLite 冷启动。
- GBrain 健康时 MCP 检索 P95 小于 2 秒。
- GBrain 故障时 6 秒内切换到阿里云通道。
- 阿里云语义重排目标 P95 小于 2 秒。
- 缓存命中的 embedding 小于 10 毫秒。
- 任一外部通道故障时仍能通过 BM25 返回结果。

## 安全

- GBrain 仅绑定回环地址。
- Token 使用 `gbrain auth create lgdo` 生成，服务端哈希保存，原始 token 仅存在 Git 忽略的 `.env`。
- 脚本和日志不得打印完整 Token、DashScope API Key 或请求正文。
