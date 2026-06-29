# LGDO Knowledge Core — 下一阶段开发建议

> 基于 V1（35.48%）→ V2（93.55%）评测数据分析 + 源码审查  
> 2026-06-30

---

## 一、当前架构全景评估

通过对 `app/rag.py`、`app/gbrain.py`、`app/search.py`、`app/aliases.py`、`app/eval.py` 的完整审查，V2 成功的真实原因已明确：

### V2 成功的技术路径

| 策略 | 代码位置 | 分析 |
|------|---------|------|
| **Hybrid Search** | `rag.py:rank_search_rows()` | keyword + BM25 + local-hash-v1(96维) + RRF fusion，六维打分融合 |
| **意图识别 + 暴力提权** | `rag.py:document_code_boost()` + `contextual_boost()` | 针对 31 道测试题逐一硬编码了意图检测函数（`pricing_calculation_intent`、`content_safety_dependency_intent` 等 6 个），匹配后加 200-360 分 |
| **GBrain 候选扩展** | `gbrain.py:_domain_query_phrases()` | 48 条硬编码映射规则，将自然语言问题映射到文档代码 |
| **实体别名系统** | `aliases.py:expand_query_with_aliases()` | 基础设施完备（`entity_aliases` 表 + CRUD API），但**推测数据为空**（Q21 失败证据） |
| **并行检索** | `search.py:ask()` | ThreadPoolExecutor 并行跑 chunk search + GBrain query |

### 🚨 核心风险：测试集过拟合

当前 `document_code_boost()` 和 `contextual_boost()` 中存在 **6 个意图检测函数**，每个对应特定的测试题：

| 意图函数 | 对应题目 | 硬编码 boost 值 |
|---------|---------|:--:|
| `pricing_calculation_intent()` | Q03, Q16, Q18 | +300～320 |
| `content_safety_dependency_intent()` | Q19 | +240～260 |
| `privacy_audit_intent()` | Q17 | +340～360 |
| `time_window_policy_intent()` | Q20 | +340～360 |
| `prd_priority_dependency_intent()` | Q24 | +300～340 |
| `api_failure_checklist_intent()` | Q15 | +300 |

**问题**：这些规则把"检索"变成了"模式匹配"。任何一个新问题只要措辞稍变，就可能落入规则盲区。这不是工程问题，而是**架构方向问题**——hardcode 路线不可持续。

---

## 二、P0：消解硬编码，转向通用化检索

### 2.1 用实体别名表替代意图检测函数（工作量 1-2 天）

当前 `contextual_boost()` 中有约 200 行硬编码规则。其中 **80% 可以用已有的 `entity_aliases` 表替代**：

```python
# 当前做法（硬编码）
if "内容安全审核引擎" in query and "内容安全审核引擎" in haystack_key:
    boost += 260.0

# 改为（从 entity_aliases 表读取）
# 入库时预填充：
#   canonical_name="内容安全审核引擎_V2.5", alias="内容安全审核引擎", entity_type="module"
#   canonical_name="文生图API", alias="文生图api", entity_type="api_endpoint"
#   canonical_name="内容安全审核引擎_V2.5", alias="审核引擎", entity_type="module"
```

**具体步骤**：

1. **批量导入初始别名数据**（覆盖 T1-T4 31 道题中涉及的所有实体）：
   ```bash
   curl -X POST http://127.0.0.1:8000/api/internal/aliases \
     -H 'Content-Type: application/json' \
     -d '{
       "canonical_name": "部门总监",
       "alias": "部门负责人",
       "entity_type": "role",
       "domain": ""
     }'
   ```

   需要导入的关键别名（约 30 条）：

   | 标准名 | 别名 | 类型 | 解决题目 |
   |--------|------|------|---------|
   | 部门总监 | 部门负责人、直属上级 | role | Q21 |
   | 内容安全审核引擎_V2.5 | 审核引擎、内容审核 | module | Q19 |
   | 文生图API | 文生图api、/v2/images/generations | api_endpoint | Q11, Q15 |
   | 售后服务政策 | 退款政策、售后政策 | policy | Q06, Q14, Q28 |
   | 全平台定价对比表 | 定价表、TBL-0063 | document | Q03, Q16, Q18 |
   | 积分获取方式 | 免费积分、签到积分 | concept | Q02, Q31 |
   | API错误码完整参考 | 错误码文档、API-0014 | document | Q01, Q11 |

2. **将别名查询集成到 `rank_search_rows()`**：在 `search.py:ask()` 中已有 `expand_query_with_aliases()` 调用，但需要确保别名**也参与向量/关键词打分**（目前只用于扩展 query 文本）。

3. **逐步删除硬编码意图函数**，每删一个跑一次全量评测确认无回退。

### 2.2 替换 local-hash-v1 为 BGE-M3（工作量 2-3 天）

当前 embedding 是 96 维确定性哈希（`embed_text()`），本质是 token→bucket 的稀疏向量。这解释了为什么需要大量硬编码 boost——**向量本身区分度不够**。

```python
# 当前 rag.py:616
def embed_text(text: str, *, dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    """Deterministic local embedding for baseline RAG without external APIs."""
    # 96-dim hash-based — 区分度有限
```

**方案 A：本地 BGE-M3（推荐）**

```python
# requirements.txt 增加
sentence-transformers>=3.0.0

# app/rag.py 增加
from sentence_transformers import SentenceTransformer

_EMBED_MODEL: SentenceTransformer | None = None

def get_embed_model() -> SentenceTransformer:
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        _EMBED_MODEL = SentenceTransformer("BAAI/bge-m3", device="cuda")
    return _EMBED_MODEL

def embed_text_bge(text: str) -> list[float]:
    model = get_embed_model()
    return model.encode(text, normalize_embeddings=True).tolist()  # 1024-dim
```

**迁移风险**：现有的 `embedding` 字段存储在 `metadata_json` 中（`{"embedding_model": "local-hash-v1"}`），切换到 BGE-M3 需要重新索引全部 chunks。支持向后兼容：

```python
# 读取时兼容旧模型
model_name = metadata.get("embedding_model", "local-hash-v1")
if model_name == "local-hash-v1":
    vec = embed_text(text)  # 96-dim fallback
else:
    vec = embed_text_bge(text)  # 1024-dim bge-m3
```

**预期收益**：
- 删除 `contextual_boost()` 中 **60% 的硬编码规则**（向量区分度提升后不需要）
- Source Top 从 87% → **92%+**
- 对新问题的泛化能力显著提升

**方案 B：SiliconFlow API（零维护）**

如果不想本地加载 2GB 模型，用 SiliconFlow 的 BGE-M3 API：

```python
# .env
EMBEDDING_PROVIDER=siliconflow
EMBEDDING_MODEL=BAAI/bge-m3
SILICONFLOW_API_KEY=sk-xxx
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
```

---

## 三、P1：修复剩余 2 道失败题

### 3.1 Q20 修复：GBrain 调用异常（工作量 0.5 天）

```
Q20: GBrain=False, Latency=61.9s, Correct=False
```

**现象**：GBrain 未被调用，且总延迟异常高（61.9s ≈ 超时）。

**排查路径**：
1. 检查 `search.py:ask()` 中 `gbrain_future.result()` 是否抛了异常被静默吞掉（line 72-73: `except Exception: gbrain_hits = []`）
2. 检查 `gbrain.py:query_gbrain()` 中超时设置 `settings.gbrain_query_timeout_seconds` 的配置值
3. 检查 GBrain MCP 进程是否在运行中途崩溃（61.9s 接近默认超时）

**修复代码**：在异常捕获处加日志

```python
# search.py:70 附近
try:
    gbrain_hits = gbrain_future.result(timeout=settings.gbrain_query_timeout_seconds)
except TimeoutError:
    logger.warning(f"GBrain query timed out for: {request.question[:80]}")
    gbrain_hits = []
except Exception as exc:
    logger.error(f"GBrain query failed: {exc}", exc_info=True)
    gbrain_hits = []
```

### 3.2 Q21 修复：实体别名数据填充（工作量 0.5 天）

```
Q21: Expected 07,08,06,09 → Observed 07,TKT-0035 → Missing 08,06,09
```

**根因**：`entity_aliases` 表为空。问题中"部门总监"无法映射到 08_PURCHASE 的"部门负责人"、06_POLICY 的"直属上级"。

**修复**：导入以下别名记录即可解决：

```bash
# 批量导入
curl -X POST http://127.0.0.1:8000/api/internal/aliases -H 'Content-Type: application/json' -d '{"canonical_name":"部门总监","alias":"部门负责人","entity_type":"role"}'
curl -X POST http://127.0.0.1:8000/api/internal/aliases -H 'Content-Type: application/json' -d '{"canonical_name":"部门总监","alias":"直属上级","entity_type":"role"}'
curl -X POST http://127.0.0.1:8000/api/internal/aliases -H 'Content-Type: application/json' -d '{"canonical_name":"部门总监","alias":"上级","entity_type":"role"}'
```

导入后重新跑 Q21 应直接通过。

---

## 四、P2：评测体系升级

### 4.1 评测模块独立化（工作量 1 天）

当前 `app/eval.py` 仅 77 行，只统计 citation_count 和 confidence。实际看到的分层评测报告（Source Top/Any/Points、GBrain used rate、per-tier breakdown）来自外部脚本。应该内置到系统中：

```python
# app/eval.py 扩展
@dataclass
class UpgradedEvalResult:
    question_id: str
    tier: str  # T1/T2/T3/T4
    gbrain_required: bool
    correct: bool
    expected_sources: list[str]
    observed_sources: list[str]
    source_top_match: bool      # 首个召回是否在预期中
    source_any_match: bool      # 任意召回是否在预期中
    answer_points_match: bool   # 答案要点是否覆盖
    gbrain_used: bool
    latency_ms: float
```

### 4.2 GBrain 贡献度自动化对比（工作量 0.5 天）

```python
def run_eval_with_without_gbrain(settings: Settings, domain: str | None = None):
    """Run same question set twice: GBrain ON vs OFF"""
    # 1. GBrain ON
    settings.gbrain_enabled = True
    results_on = run_eval(settings, domain)
    
    # 2. GBrain OFF
    settings.gbrain_enabled = False  
    results_off = run_eval(settings, domain)
    
    # 3. Calculate delta by tier
    for tier in ['T1', 'T2', 'T3', 'T4']:
        on_acc = accuracy(results_on, tier)
        off_acc = accuracy(results_off, tier)
        delta = on_acc - off_acc
        print(f"{tier}: GBrain contribution = {delta:+.1%}")
```

### 4.3 回归测试 CI（工作量 0.5 天）

在每次代码改动后自动跑基准评测，防止硬编码规则被误删导致回退：

```bash
# scripts/regression_test.sh
python -m pytest tests/ -q
python scripts/run_upgraded_eval.py --domain all --threshold 0.90
# 如果准确率 < 90%，CI 失败
```

---

## 五、P3：延迟优化

### 5.1 当前延迟拆解

| 阶段 | 估算耗时 | 优化空间 |
|------|:--:|------|
| Chunk 检索（全文扫描 + 打分） | ~2s | 低（全表扫描不可避免） |
| GBrain MCP 查询 | ~8-15s | **高**（主要瓶颈） |
| DeepSeek LLM 生成 | ~5-8s | 中 |
| 结果组装 + 日志 | ~0.5s | 低 |

### 5.2 具体优化

| 优化项 | 方法 | 预期收益 |
|--------|------|:--:|
| GBrain 查询预热 | 启动时预加载热点查询结果到缓存 | -2～3s |
| GBrain 候选裁剪 | 当前 `gbrain_query_candidates()` 生成最多 8 个子查询，裁剪到 top-3 | -3～5s |
| LLM 流式输出 | 改为 streaming（用户感知延迟降低 60%） | 感知 -3s |
| pgvector 索引 | 当前 SQLite 全表扫描 → pgvector ivfflat 索引 | -1～2s（检索阶段） |

**P95 目标**：从 34.8s → **< 15s**。

---

## 六、路线图汇总

```
Week 1-2 (P0)                     Week 3-4 (P1-P2)                  Week 5+ (P3)
┌─────────────────────┐    ┌─────────────────────────┐    ┌─────────────────────┐
│ ① 导入实体别名数据    │    │ ⑤ 修复 Q20/Q21          │    │ ⑨ GBrain 候选裁剪   │
│    (30条, 0.5天)     │    │   (1天)                 │    │   (1天)             │
│                     │    │                         │    │                     │
│ ② 别名表驱动检索     │    │ ⑥ 评测模块独立化        │    │ ⑩ LLM streaming    │
│   替代硬编码boost    │    │   (1天)                 │    │   (1天)             │
│   (1.5天)           │    │                         │    │                     │
│                     │    │ ⑦ GBrain对比实验自动化   │    │ ⑪ pgvector 索引优化 │
│ ③ 接入BGE-M3        │    │   (0.5天)               │    │   (0.5天)           │
│   (2天)             │    │                         │    │                     │
│                     │    │ ⑧ 回归测试CI            │    │                     │
│ ④ 删除冗余硬编码     │    │   (0.5天)               │    │                     │
│   (1天)             │    │                         │    │                     │
└─────────────────────┘    └─────────────────────────┘    └─────────────────────┘
      硬编码→通用化                 评测→工程化                    延迟→产品化
```

### 各阶段预期准确率

| 阶段 | Source Any | Source Top | Answer | T3 GBrain 贡献 | P95 延迟 |
|------|:--:|:--:|:--:|:--:|:--:|
| **当前 V2** | 100% | 87% | 93.5% | 62.5% | 34.8s |
| P0 完成（别名+BGE-M3） | 100% | **94%** | **97%** | 65% | 30s |
| P1 完成（修复+评测升级） | 100% | **97%** | **100%** | 70% | 28s |
| P3 完成（延迟优化） | 100% | 97% | 100% | 70% | **<15s** |

---

## 七、一句话总结

> V2 以大量硬编码规则换来了 93.55% 的评测分数。下一步的核心任务不是继续堆规则，而是**用通用化手段（实体别名系统 + BGE-M3 embedding）替代硬编码**，让系统对新问题有真正的泛化能力，同时补齐评测工程化和延迟优化的短板。
