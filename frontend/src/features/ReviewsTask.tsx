import { useEffect, useMemo, useState } from "react";

import { List, Panel } from "../components/common";
import type { ReviewItem } from "../types";
import { translateReviewIssueType, translateReviewItemStatus } from "../utils/format";

export function ReviewsTask({
  reviews,
  loadPage,
  updateReview,
  showToast,
}: {
  reviews: ReviewItem[];
  loadPage: (path: string) => Promise<void>;
  updateReview: (id: string, status: string) => Promise<void>;
  showToast: (message: string) => void;
}) {
  const [selectedReviewId, setSelectedReviewId] = useState<string | null>(reviews[0]?.id || null);
  const selectedReview = useMemo(
    () => reviews.find((review) => review.id === selectedReviewId) || reviews[0],
    [reviews, selectedReviewId],
  );

  useEffect(() => {
    if (!reviews.length) {
      if (selectedReviewId) setSelectedReviewId(null);
      return;
    }
    if (!selectedReviewId || !reviews.some((review) => review.id === selectedReviewId)) {
      setSelectedReviewId(reviews[0].id);
    }
  }, [reviews, selectedReviewId]);

  return (
    <section className="reviews-workspace">
      <Panel title="审阅队列" badge={reviews.length}>
        <List rows={reviews} empty="暂无待审阅项" render={(review) => (
          <button
            className={`review-row ${selectedReview?.id === review.id ? "active" : ""}`}
            type="button"
            onClick={() => setSelectedReviewId(review.id)}
          >
            <span className="item-title">{review.page_path}</span>
            <span className="meta">
              {[
                `审阅 ID：${review.id}`,
                translateReviewIssueType(review.issue_type),
                translateReviewItemStatus(review.status),
                review.owner || "未分配",
              ].map((value) => <span className="pill" key={value}>{value}</span>)}
            </span>
          </button>
        )} />
      </Panel>

      <section className="review-detail">
        {selectedReview ? (
          <Panel title="审阅详情" badge={translateReviewItemStatus(selectedReview.status)}>
            <div className="review-detail-hero">
              <p className="eyebrow">人工确认 / {translateReviewIssueType(selectedReview.issue_type)}</p>
              <h2>{selectedReview.page_path}</h2>
              <p>点击队列中的审阅项可在这里查看详情，再决定通过或驳回。</p>
            </div>
            <div className="detail-grid review-detail-grid">
              <div className="detail-item">
                <span>审阅 ID</span>
                <strong>{selectedReview.id}</strong>
              </div>
              <div className="detail-item">
                <span>问题类型</span>
                <strong>{translateReviewIssueType(selectedReview.issue_type)}</strong>
              </div>
              <div className="detail-item">
                <span>当前状态</span>
                <strong>{translateReviewItemStatus(selectedReview.status)}</strong>
              </div>
              <div className="detail-item">
                <span>负责人</span>
                <strong>{selectedReview.owner || "未分配"}</strong>
              </div>
            </div>
            <div className="review-actions">
              <button onClick={() => updateReview(selectedReview.id, "approved").catch((error) => showToast(error.message))}>通过审阅</button>
              <button className="danger" onClick={() => updateReview(selectedReview.id, "rejected").catch((error) => showToast(error.message))}>驳回审阅</button>
              <button className="secondary" onClick={() => loadPage(selectedReview.page_path).catch((error) => showToast(error.message))}>查看知识页</button>
            </div>
          </Panel>
        ) : (
          <Panel title="审阅详情" badge="未选择">
            <p>暂无可查看的审阅项。</p>
          </Panel>
        )}
      </section>
    </section>
  );
}
