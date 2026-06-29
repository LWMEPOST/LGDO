from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_internal_mvp_flow(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    vault_dir = tmp_path / "vault"
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "refund_policy.md").write_text(
        "# 退款政策\n\n用户 7 天内可以申请退款。客服需要记录工单摘要和引用政策。",
        encoding="utf-8",
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", data_dir / "test.db")
    monkeypatch.setattr(settings, "vault_path", vault_dir)
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")

    client = TestClient(app)

    console = client.get("/console")
    assert console.status_code == 200
    assert "LGDO 知识库核心" in console.text
    assert "/console-static/assets/" in console.text
    assert "type=\"module\"" in console.text
    asset_path = console.text.split('/console-static/assets/')[1].split('"')[0]
    console_asset = client.get(f"/console-static/assets/{asset_path}")
    assert console_asset.status_code == 200
    assert "知识库核心" in console_asset.text or "React" in console_asset.text
    console_js = client.get("/static/console.jsx").text
    assert "产品、客服与行政知识空间" in console_js
    assert "资料已删除" in console_js

    scan = client.post(
        "/api/internal/sources/scan",
        json={
            "root_path": str(sample_dir),
                "domain": "product",
                "owner": "tester",
                "acl_tags": ["internal", "product"],
                "metadata_defaults": {"source_system": "unit_test"},
            },
        )
    assert scan.status_code == 200
    assert scan.json()["new_files"] == 1

    upload = client.post(
        "/api/internal/sources/upload",
        data={
            "domain": "product",
            "owner": "tester",
            "acl_tags": "internal,upload",
            "metadata_defaults": '{"source_system":"upload_test"}',
        },
        files={"files": ("uploaded_faq.md", "# 上传 FAQ\n\n纸质材料可先扫描为 PDF 或图片后上传。".encode("utf-8"), "text/markdown")},
    )
    assert upload.status_code == 200
    assert upload.json()["saved_files"]

    compile_result = client.post("/api/internal/wiki/compile", json={"domain": "product"})
    assert compile_result.status_code == 200
    assert compile_result.json()["created_pages"] >= 2

    sources = client.get("/api/internal/sources?domain=product")
    assert sources.status_code == 200
    assert len(sources.json()) >= 2
    refund_source = next(source for source in sources.json() if source["title"] == "refund_policy")
    uploaded_source = next(source for source in sources.json() if source["title"] == "uploaded_faq")
    assert refund_source["metadata"]["acl_tags"] == ["internal", "product"]
    assert refund_source["metadata"]["parser"] == "text"
    assert refund_source["metadata"]["chunk_count"] >= 1
    assert refund_source["metadata"]["normalized_path"].startswith("normalized/product/")
    assert refund_source["metadata"]["jsonl_path"].startswith("jsonl/product/")
    source_id = refund_source["id"]

    reports = client.get(f"/api/internal/ingest/reports?source_id={source_id}")
    assert reports.status_code == 200
    assert len(reports.json()) == 1
    assert reports.json()[0]["parser"] == "text"
    assert reports.json()[0]["chunk_count"] >= 1

    preview = client.get(f"/api/internal/sources/{source_id}/preview")
    assert preview.status_code == 200
    assert preview.json()["title"] == "refund_policy"
    assert preview.json()["preview_path"].startswith("normalized/product/")
    assert "退款政策" in preview.json()["content"]

    normalized_path = vault_dir / refund_source["metadata"]["normalized_path"]
    jsonl_path = vault_dir / refund_source["metadata"]["jsonl_path"]
    assert normalized_path.exists()
    assert jsonl_path.exists()
    assert "source_id" in normalized_path.read_text(encoding="utf-8")
    assert jsonl_path.read_text(encoding="utf-8").strip()

    compile_again = client.post(
        "/api/internal/wiki/compile",
        json={"domain": "product", "source_ids": [source_id]},
    )
    assert compile_again.status_code == 200
    assert compile_again.json()["updated_pages"] == 1
    assert compile_again.json()["review_items"] == 0

    pages = client.get("/api/internal/wiki/pages?domain=product")
    assert pages.status_code == 200
    assert len(pages.json()) >= 2
    page_path = next(page for page in pages.json() if page["title"] == "refund_policy")["path"]

    page_content = client.get(f"/api/internal/wiki/pages/{page_path}")
    assert page_content.status_code == 200
    assert "# refund_policy" in page_content.json()["content"]
    assert "normalized_path" in page_content.json()["content"]

    edited_content = page_content.json()["content"] + "\n\n## 人工补充\n\n企业客户需要转财务审核。\n"
    save_page = client.put(
        f"/api/internal/wiki/pages/{page_path}",
        json={
            "content": edited_content,
            "review_status": "reviewed",
            "owner": "tester",
            "note": "补充企业客户规则",
        },
    )
    assert save_page.status_code == 200
    assert save_page.json()["review_status"] == "reviewed"

    page_content_after = client.get(f"/api/internal/wiki/pages/{page_path}")
    assert "企业客户需要转财务审核" in page_content_after.json()["content"]

    stale = client.patch(
        f"/api/internal/wiki/pages/{page_path}/status",
        json={"review_status": "stale", "note": "测试过期标记"},
    )
    assert stale.status_code == 200
    assert stale.json()["review_status"] == "stale"

    reviews = client.get("/api/internal/reviews?status=pending")
    assert reviews.status_code == 200
    assert len(reviews.json()) >= 1
    review_id = reviews.json()[0]["id"]

    review_update = client.patch(
        f"/api/internal/reviews/{review_id}",
        json={"status": "approved", "owner": "tester", "note": "样例通过"},
    )
    assert review_update.status_code == 200
    assert review_update.json()["status"] == "approved"

    ask = client.post("/api/internal/ask", json={"question": "用户如何申请退款？", "domain": "product"})
    assert ask.status_code == 200
    body = ask.json()
    assert body["citations"]
    assert "退款" in body["answer"]

    upload_ask = client.post("/api/internal/ask", json={"question": "纸质材料如何上传？", "domain": "product"})
    assert upload_ask.status_code == 200
    upload_body = upload_ask.json()
    assert upload_body["citations"]
    assert upload_body["citations"][0]["source_id"] == uploaded_source["id"]

    rag_before_delete = client.get("/api/internal/rag/status")
    assert rag_before_delete.status_code == 200
    chunk_count_before_delete = rag_before_delete.json()["chunk_count"]

    feedback = client.post(
        "/api/internal/feedback",
        json={
            "query_id": body["query_id"],
            "rating": "partial",
            "comment": "需要补充企业客户场景",
            "should_create_gap": True,
        },
    )
    assert feedback.status_code == 200
    assert feedback.json()["gap_created"] is True

    gaps = client.get("/api/internal/gaps?status=open")
    assert gaps.status_code == 200
    assert len(gaps.json()) == 1
    gap_id = gaps.json()[0]["id"]

    start_gap = client.patch(
        f"/api/internal/gaps/{gap_id}",
        json={"status": "in_progress", "priority": "high", "owner": "tester"},
    )
    assert start_gap.status_code == 200
    assert start_gap.json()["status"] == "in_progress"
    assert start_gap.json()["priority"] == "high"

    resolve_gap = client.patch(
        f"/api/internal/gaps/{gap_id}",
        json={"status": "resolved", "owner": "tester", "linked_page_path": page_path},
    )
    assert resolve_gap.status_code == 200
    assert resolve_gap.json()["status"] == "resolved"
    assert resolve_gap.json()["linked_page_path"] == page_path

    eval_add = client.post(
        "/api/internal/eval/questions",
        json={"question": "退款政策是什么？", "domain": "product"},
    )
    assert eval_add.status_code == 200

    eval_run = client.post("/api/internal/eval/run?domain=product")
    assert eval_run.status_code == 200
    assert eval_run.json()["total"] == 1

    delete_source = client.delete(f"/api/internal/sources/{uploaded_source['id']}?note=test")
    assert delete_source.status_code == 200
    assert delete_source.json()["status"] == "deleted"

    rag_after_delete = client.get("/api/internal/rag/status")
    assert rag_after_delete.status_code == 200
    assert rag_after_delete.json()["chunk_count"] < chunk_count_before_delete

    upload_ask_after_delete = client.post("/api/internal/ask", json={"question": "纸质材料如何上传？", "domain": "product"})
    assert upload_ask_after_delete.status_code == 200
    assert all(
        citation["source_id"] != uploaded_source["id"]
        for citation in upload_ask_after_delete.json()["citations"]
    )

    active_sources = client.get("/api/internal/sources?domain=product")
    assert active_sources.status_code == 200
    assert all(source["id"] != uploaded_source["id"] for source in active_sources.json())

    deleted_sources = client.get("/api/internal/sources?domain=product&include_deleted=true")
    assert deleted_sources.status_code == 200
    assert any(source["id"] == uploaded_source["id"] for source in deleted_sources.json())
