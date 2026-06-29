from app.llm import build_prompt, extract_table_evidence


def test_build_prompt_adds_structured_table_evidence_for_pricing_questions():
    context_blocks = [
        (
            "标题：TBL-0063 TABLE 全平台定价对比表\n"
            "来源：TBL-0063\n"
            "资料正文：\n"
            "| 套餐 | 人数 | 年费 | 月积分(共享池) | API调用/月 |\n"
            "| 企业标准 | 21-50人 | ¥59,999 | 150,000 | 30,000 |\n"
            "| 企业旗舰 | 51-200人 | ¥199,999 | 600,000 | 150,000 |"
        )
    ]

    prompt = build_prompt(
        "一个日均 500 次文生图调用的电商团队，应该选择什么套餐组合？给出总成本和理由。",
        context_blocks,
        "detail",
    )

    assert "表格/数值依据" in prompt
    assert "TBL-0063 TABLE 全平台定价对比表 / TBL-0063: | 企业标准" in prompt
    assert "先引用「表格/数值依据」中的对应行" in prompt
    assert "30,000次/月 ÷ 30天 = 1,000次/天" in prompt
    assert "积分包价格、积分包单价" in prompt
    assert "无实质矛盾" in prompt


def test_extract_table_evidence_ignores_irrelevant_plain_text():
    evidence = extract_table_evidence(
        [
            "标题：普通说明\n来源：DOC-0001\n资料正文：这是一段没有表格的说明。",
            "标题：价格表\n来源：TBL-0001\n资料正文：\n| 套餐 | 年费 |\n| 企业标准 | ¥59,999 |",
        ],
        "企业标准套餐年费是多少？",
    )

    assert "企业标准" in evidence
    assert "普通说明" not in evidence
