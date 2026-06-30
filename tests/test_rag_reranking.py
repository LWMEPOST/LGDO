from app.aliases import build_alias_context
from app.rag import diversify_ranked_rows, embed_text, rank_search_rows, tokenize


def make_row(row_id: str, title: str, text: str, domain: str = "product") -> dict:
    return {
        "id": f"{row_id}_chunk_0000",
        "source_id": row_id,
        "domain": domain,
        "title": title,
        "chunk_index": 0,
        "text": text,
        "metadata": {
            "title": title,
            "domain": domain,
            "embedding": embed_text(f"{title}\n{text}"),
        },
        "embedding": embed_text(f"{title}\n{text}"),
    }


def test_reranker_prefers_exact_plan_row_over_generic_api_docs():
    rows = [
        make_row(
            "api_error",
            "API 错误码完整参考",
            "401 missing_api_key 缺少 API Key。429 rate_limited 频率限制。API 调用限制请查看套餐文档。",
        ),
        make_row(
            "faq_02",
            "灵创AI创作平台 常见问题解答 FAQ",
            "Q16: API调用频率限制是多少？\n- 免费套餐：10次/分钟\n- 基础套餐：60次/分钟\n- 专业套餐：300次/分钟\n- 企业套餐：可定制",
        ),
    ]

    ranked = rank_search_rows(rows, "专业套餐的API调用频率限制是多少？")

    assert ranked[0]["source_id"] == "faq_02"
    assert ranked[0]["phrase_boost"] > ranked[1]["phrase_boost"]


def test_reranker_prefers_historical_ticket_for_specific_401_question():
    rows = [
        make_row(
            "api_0014",
            "API 错误码完整参考",
            "401 invalid_api_key API Key无效，检查或重新生成。expired_api_key API Key已过期。",
        ),
        make_row(
            "tickets_04",
            "历史客服工单",
            "工单 #TK-2025-06104\n用户问题：我在调用文生图API时一直返回401错误，但我的API Key是刚创建的，确认没有过期。\n处理记录：客服请确认请求头中Authorization字段格式是否为\"Bearer <API_KEY>\"，注意Bearer后面有一个空格。",
        ),
    ]

    ranked = rank_search_rows(rows, "文生图API返回401但API Key没过期，客服建议检查什么？")

    assert ranked[0]["source_id"] == "tickets_04"
    assert ranked[0]["context_boost"] > 0


def test_reranker_does_not_treat_registration_fact_as_ticket_intent():
    rows = [
        make_row(
            "tickets_04",
            "历史客服工单",
            "工单记录。用户充值积分异常，处理记录为补回200积分并额外补偿50积分。",
        ),
        make_row(
            "faq_02",
            "常见问题解答 FAQ",
            "Q: 新用户注册后有什么权益？\nA: 新用户注册后赠送50创作积分。",
        ),
    ]

    ranked = rank_search_rows(rows, "新用户注册后会赠送多少创作积分？")

    assert ranked[0]["source_id"] == "faq_02"


def test_reranker_prefers_recharge_ticket_over_generic_solution_table():
    rows = [
        make_row(
            "sup_0074",
            "常见生成失败原因及解决方案",
            "错误类型速查表 | 错误信息 | 常见原因 | 解决方案 | 恢复时间 |\n生成失败 | GPU节点瞬时故障 | 直接点重试 | 即时",
        ),
        make_row(
            "tickets_04",
            "历史客服工单",
            "工单 #TK-2025-06182\n用户问题：我刚充值了200元买了1000积分，但账户显示只有800积分。\n处理记录：客服核查支付回调，补回缺失的200积分，并额外补偿50积分。",
        ),
    ]

    ranked = rank_search_rows(rows, "充值200元但少了200积分的工单是怎么解决的？")

    assert ranked[0]["source_id"] == "tickets_04"


def test_reranker_prefers_faq_for_system_failure_credit_policy():
    rows = [
        make_row(
            "tickets_04",
            "历史客服工单",
            "工单记录。系统故障后客服处理记录：系统恢复正常并补偿100积分。",
        ),
        make_row(
            "faq_02",
            "常见问题解答 FAQ",
            "故障与售后\nQ18: 系统故障导致积分扣了但没出图怎么办？\nA: 系统会自动检测失败任务，并在24小时内自动退还积分。",
        ),
    ]

    ranked = rank_search_rows(rows, "系统故障导致积分扣了但没出图，平台怎么处理？")

    assert ranked[0]["source_id"] == "faq_02"


def test_reranker_prefers_troubleshooting_doc_for_common_failure_question():
    rows = [
        make_row(
            "tickets_04",
            "历史客服工单",
            "工单记录。用户生成图片失败，处理记录由客服协助排查。",
        ),
        make_row(
            "sup_0074",
            "常见生成失败原因及解决方案",
            "错误类型速查表\n| 错误信息 | 常见原因 | 解决方案 |\n| 生成失败，请重试 | GPU节点瞬时故障 | 直接点重试，系统已自动切换节点 |",
        ),
    ]

    ranked = rank_search_rows(rows, "生成失败请重试通常是什么原因，解决方案是什么？")

    assert ranked[0]["source_id"] == "sup_0074"


def test_reranker_prefers_credit_guide_over_generic_ticket_for_credit_topup():
    ticket = make_row(
        "tickets_04",
        "历史客服工单",
        "工单记录。用户生成图片失败，处理记录由客服协助排查，并在故障后补偿100积分。",
    )
    guide = make_row(
        "kb_0045",
        "KB-0045 KB 积分系统完全指南",
        "积分获取方式\n| 方式 | 数量 | 说明 |\n| 邀请好友 | 30积分/人 | 好友注册并使用后到账 |\n| 每日签到 | 5积分/天 | 连续7天额外+20 |\n| 作品被精选 | 100积分 | 由编辑评选 |\n| 付费购买 | 见套餐 | 永久有效 |",
    )

    ranked = rank_search_rows([ticket, guide], "专业套餐的用户生成图片时积分不够了，有哪些补充积分的办法？")

    assert ranked[0]["source_id"] == "kb_0045"
    assert ranked[0]["doc_code_boost"] > 0


def test_reranker_boosts_explicit_document_code_matches():
    generic = make_row(
        "tickets_04",
        "历史客服工单",
        "API 调用失败后客服建议稍后重试。",
    )
    api_reference = make_row(
        "api_0014",
        "API-0014 API 错误码完整参考",
        "429 rate_limited 频率限制，建议等待后重试或指数退避，并降低并发。",
    )

    ranked = rank_search_rows([generic, api_reference], "API-0014 里 429 错误后怎么处理？")

    assert ranked[0]["source_id"] == "api_0014"
    assert ranked[0]["doc_code_boost"] > 0


def test_reranker_boosts_table_rows_for_policy_and_pricing_questions():
    policy = make_row(
        "policy_06",
        "行政管理制度",
        "| 假别 | 天数/年 | 薪资 | 审批人 |\n| 婚假 | 10天 | 全额 | 直属上级 + HR |",
        "customer_service",
    )
    purchase = make_row(
        "purchase_08",
        "采购管理制度",
        "| 采购类型 | 金额范围 | 采购方式 | 审批人 |\n| 常规采购 | 10000-50000 | 至少3家比价 | 直属上级 |",
        "customer_service",
    )
    ranked_policy = rank_search_rows([purchase, policy], "婚假有多少天，审批人是谁？")
    assert ranked_policy[0]["source_id"] == "policy_06"
    assert ranked_policy[0]["table_boost"] > 0

    pricing = make_row(
        "tbl_0063",
        "TBL-0063 TABLE 全平台定价对比表",
        "| 套餐 | 人数 | 年费 | 月积分(共享池) | API调用/月 |\n| 企业标准 | 21-50人 | ¥59,999 | 150,000 | 30,000 |",
    )
    api = make_row(
        "api_0014",
        "API 错误码完整参考",
        "API 调用失败可能返回 429 rate_limited，表示频率限制，需要等待后重试。",
    )
    ranked_pricing = rank_search_rows([api, pricing], "企业标准套餐的年费、月积分共享池和API调用/月是多少？")
    assert ranked_pricing[0]["source_id"] == "tbl_0063"
    assert ranked_pricing[0]["table_boost"] > 0


def test_reranker_prefers_pricing_table_for_daily_image_api_cost_question():
    case_study = make_row(
        "ppt_0053",
        "企业客户成功案例集",
        "某电商团队通过内容生产流程优化，将文生图工作流接入运营活动，日常产出效率提升。",
    )
    pricing = make_row(
        "tbl_0063",
        "TBL-0063 TABLE 全平台定价对比表",
        "| 套餐 | 人数 | 年费 | 月积分(共享池) | API调用/月 |\n| 企业标准 | 21-50人 | ¥59,999 | 150,000 | 30,000 |",
    )
    guide = make_row(
        "kb_0045",
        "KB-0045 KB 积分系统完全指南",
        "文生图 API 按次消耗积分；积分不够时可通过积分包或升级套餐补充。",
    )

    ranked = rank_search_rows(
        [case_study, guide, pricing],
        "一个日均 500 次文生图调用的电商团队，应该选择什么套餐组合？给出总成本和理由。",
        alias_context=build_alias_context(
            "一个日均 500 次文生图调用的电商团队，应该选择什么套餐组合？给出总成本和理由。",
            [
                {
                    "canonical_name": "全平台定价对比表",
                    "canonical_key": "全平台定价对比表",
                    "alias": "套餐组合",
                    "alias_key": "套餐组合",
                    "entity_type": "pricing_table",
                    "metadata_json": '{"terms":["企业标准","API调用/月","总成本"]}',
                }
            ],
        ),
    )

    assert ranked[0]["source_id"] == "tbl_0063"
    assert ranked[0]["alias_boost"] > 0


def test_reranker_boosts_content_safety_dependency_documents():
    content_safety = make_row(
        "prd_0007",
        "PRD-0007 内容安全审核引擎",
        "内容安全审核引擎会输出内容审核结果，并通过 Webhook 通知依赖方。",
    )
    image_api = make_row(
        "api_0010",
        "API-0010 文生图 API",
        "文生图 API 在任务完成前需要读取内容审核结果，未通过时返回申诉入口。",
    )
    generic = make_row(
        "prd_0003",
        "PRD-0003 用户中心",
        "用户中心管理账号资料和登录态。",
    )

    ranked = rank_search_rows([generic, image_api, content_safety], "哪些子系统或 API 直接依赖内容安全审核引擎？")

    assert ranked[0]["source_id"] in {"prd_0007", "api_0010"}
    assert ranked[1]["source_id"] in {"prd_0007", "api_0010"}
    assert ranked[0]["context_boost"] > 0


def test_diversify_ranked_rows_deduplicates_import_copies_by_document_code():
    duplicate_a = make_row(
        "src_a",
        "TBL-0063 TABLE 全平台定价对比表",
        "| 套餐 | 年费 | API调用/月 |\n| 企业标准 | ¥59,999 | 30,000 |",
    )
    duplicate_b = make_row(
        "src_b",
        "全平台定价对比表 (2025年7月)",
        "| 企业标准 | ¥59,999 | 30,000 |",
    )
    guide = make_row(
        "kb_0045",
        "KB-0045 KB 积分系统完全指南",
        "文生图标准质量消耗 2 积分/张。",
    )

    ranked = rank_search_rows(
        [duplicate_a, duplicate_b, guide],
        "一个日均 500 次文生图调用的电商团队，应该选择什么套餐组合？",
    )
    diversified = diversify_ranked_rows(ranked, 2)

    assert [row["source_id"] for row in diversified] != ["src_a", "src_b"]
    assert any(row["source_id"] == "kb_0045" for row in diversified)


def test_reranker_boosts_privacy_audit_sources():
    competitor = make_row(
        "kb_0051",
        "竞品对比 灵创AI vs DALL-E 3",
        "| 数据隐私 | 国内服务器 | 海外服务器 |",
    )
    retention = make_row(
        "api_0018",
        "API-0018 API 数据保留与隐私白皮书",
        "数据处理原则：数据仅用于声明的目的，不用于模型训练。生成作品90天，上传素材30天，AES-256加密存储。",
    )
    policy = make_row(
        "policy_06",
        "行政管理制度",
        "数据安全：禁止存储在个人网盘，敏感文件传输需使用加密通道。",
        "customer_service",
    )

    ranked = rank_search_rows([competitor, policy, retention], "API-0018 数据隐私合规审计需要检查哪些平台政策条款？")

    assert ranked[0]["source_id"] == "api_0018"
    assert ranked[0]["doc_code_boost"] > 0


def test_reranker_boosts_prd_priority_dependency_sources():
    roadmap = make_row(
        "ppt_0060",
        "产品 Roadmap",
        "中文文字渲染优化 | P0 | 2025-08 | 60%",
    )
    canvas = make_row(
        "prd_0001",
        "PRD-0001 PRD 智能画布编辑器 V2.0",
        "| CAN-001 | 拖拽式布局 | P0 | 2周 |\n| CAN-002 | 实时预览 | P0 | 3周 |",
    )
    rtc = make_row(
        "prd_0006",
        "PRD-0006 PRD 实时协作引擎 RTC 1.0",
        "实时同步 Yjs + WebSocket，支持多人在同一个画布上实时协作。",
    )

    ranked = rank_search_rows([roadmap, rtc, canvas], "从 PRD-0001 和 PRD-0006 文档中提取所有明确标注了优先级（P0/P1/P2）的功能需求，并分析依赖关系。")

    assert ranked[0]["source_id"] in {"prd_0001", "prd_0006"}
    assert ranked[0]["doc_code_boost"] > 0
    assert ranked[0]["phrase_boost"] + ranked[0]["doc_code_boost"] > ranked[-1]["phrase_boost"] + ranked[-1]["doc_code_boost"]


def test_tokenize_splits_mixed_english_and_chinese_terms():
    tokens = tokenize("文生图API返回401但API Key没过期")

    assert "api" in tokens
    assert "key" in tokens
    assert "文生" in tokens


def test_reranker_uses_alias_context_without_benchmark_intent_rules(monkeypatch):
    import app.rag as rag
    from app.aliases import build_alias_context

    monkeypatch.setattr(rag, "embed_text", lambda _: [1.0, 0.0])
    rows = [
        make_row("generic", "普通制度", "审批流程说明。"),
        make_row("role", "费用报销制度", "部门负责人审批报销、采购、招待等事项。"),
    ]
    context = build_alias_context(
        "部门总监有哪些权限？",
        [
            {
                "canonical_name": "部门负责人",
                "canonical_key": "部门负责人",
                "alias": "部门总监",
                "alias_key": "部门总监",
                "entity_type": "role",
                "metadata_json": '{"terms":["报销","采购","招待"]}',
            }
        ],
    )

    ranked = rag.rank_search_rows(rows, "部门总监有哪些权限？", alias_context=context)

    assert ranked[0]["id"] == "role_chunk_0000"
    assert ranked[0]["alias_boost"] > 0
    assert ranked[0]["context_boost"] < 100
    removed_helper_names = [
        "_".join(parts)
        for parts in [
            ("pricing", "calculation", "intent"),
            ("content", "safety", "dependency", "intent"),
            ("privacy", "audit", "intent"),
            ("time", "window", "policy", "intent"),
            ("prd", "priority", "dependency", "intent"),
            ("api", "failure", "checklist", "intent"),
        ]
    ]
    for name in removed_helper_names:
        assert not hasattr(rag, name)


def test_reranker_does_not_keep_benchmark_phrase_boost_branches():
    import inspect
    import app.rag as rag

    source = "\n".join(
        [
            inspect.getsource(rag.contextual_boost),
            inspect.getsource(rag.document_code_boost),
        ]
    )

    forbidden_literals = [
        "内容安全审核引擎",
        "透明通道",
        "积分获取方式",
        "退款窗口期",
        "隐私白皮书",
        "数据保留",
        "不用于模型训练",
        "aes256",
        "生成失败请重试",
        "错误类型速查表",
        "常见生成失败原因",
    ]
    for literal in forbidden_literals:
        assert literal not in source
