import { Item, List, Panel } from "../components/common";
import type { ReviewItem } from "../types";
import { translateReviewIssueType, translateReviewItemStatus } from "../utils/format";

export function ReviewsTask({
  reviews,
  updateReview,
  showToast,
}: {
  reviews: ReviewItem[];
  updateReview: (id: string, status: string) => Promise<void>;
  showToast: (message: string) => void;
}) {
  return (
    <Panel title="审阅队列" badge={reviews.length}>
      <List rows={reviews} empty="暂无待审阅项" render={(review) => (
        <Item title={review.page_path} meta={[`审阅 ID：${review.id}`, translateReviewIssueType(review.issue_type), translateReviewItemStatus(review.status)]}>
          <button onClick={() => updateReview(review.id, "approved").catch((error) => showToast(error.message))}>通过</button>
          <button className="danger" onClick={() => updateReview(review.id, "rejected").catch((error) => showToast(error.message))}>驳回</button>
        </Item>
      )} />
    </Panel>
  );
}
