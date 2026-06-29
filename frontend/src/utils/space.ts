import type { IngestReport, KnowledgeGap, SourceRecord, SpaceDirectoryGroup, SpaceFilter, WikiPage } from "../types";
import { translateDomain, translatePageType, translateReviewStatus } from "./format";

export const ALL_SPACE_FILTER: SpaceFilter = {
  id: "all",
  label: "全部空间",
  desc: "资料、知识页与缺口",
  kind: "all",
  targetSection: "overview",
  count: 0,
};

export function buildSpaceDirectory(
  sources: SourceRecord[],
  pages: WikiPage[],
  gaps: KnowledgeGap[],
): SpaceDirectoryGroup[] {
  const pageTypes = ["policy", "faq", "feature", "known_issue", "index"];
  const reviewStatuses = ["draft", "reviewed", "stale", "rejected"];
  const domains = collectDomains(sources, pages);
  const activeGapCount = gaps.filter((gap) => gap.status !== "resolved" && gap.status !== "rejected").length;

  return [
    {
      id: "scope",
      label: "知识空间",
      items: [
        { ...ALL_SPACE_FILTER, count: sources.length + pages.length + gaps.length },
        ...domains.map((domain) => ({
          id: `domain:${domain}`,
          label: translateDomain(domain),
          desc: "按业务域聚合",
          kind: "domain" as const,
          value: domain,
          targetSection: "overview" as const,
          count: countDomain(sources, pages, domain),
        })),
      ],
    },
    {
      id: "knowledge",
      label: "知识分类",
      items: pageTypes.map((pageType) => ({
        id: `page_type:${pageType}`,
        label: translatePageType(pageType),
        desc: "知识页类型",
        kind: "page_type" as const,
        value: pageType,
        targetSection: "wiki" as const,
        count: pages.filter((page) => page.page_type === pageType).length,
      })),
    },
    {
      id: "governance",
      label: "治理视图",
      items: [
        ...reviewStatuses.map((status) => ({
          id: `review_status:${status}`,
          label: translateReviewStatus(status),
          desc: "发布状态",
          kind: "review_status" as const,
          value: status,
          targetSection: "wiki" as const,
          count: pages.filter((page) => page.review_status === status).length,
        })),
        {
          id: "gaps:active",
          label: "待补充内容",
          desc: "未关闭缺口",
          kind: "gaps" as const,
          value: "active",
          targetSection: "gaps" as const,
          count: activeGapCount,
        },
      ],
    },
  ];
}

export function findSpaceFilter(groups: SpaceDirectoryGroup[], id: string): SpaceFilter {
  return groups.flatMap((group) => group.items).find((item) => item.id === id) || {
    ...ALL_SPACE_FILTER,
    count: groups[0]?.items[0]?.count || 0,
  };
}

export function filterSourcesBySpace(sources: SourceRecord[], pages: WikiPage[], filter: SpaceFilter): SourceRecord[] {
  if (filter.kind === "domain" && filter.value) {
    return sources.filter((source) => source.domain === filter.value);
  }
  if ((filter.kind === "page_type" || filter.kind === "review_status") && filter.value) {
    const sourceIds = new Set(
      pages
        .filter((page) => filter.kind === "page_type" ? page.page_type === filter.value : page.review_status === filter.value)
        .flatMap((page) => page.source_ids || []),
    );
    return sources.filter((source) => sourceIds.has(source.id));
  }
  return sources;
}

export function filterPagesBySpace(pages: WikiPage[], filter: SpaceFilter): WikiPage[] {
  if (filter.kind === "domain" && filter.value) {
    return pages.filter((page) => page.domain === filter.value);
  }
  if (filter.kind === "page_type" && filter.value) {
    return pages.filter((page) => page.page_type === filter.value);
  }
  if (filter.kind === "review_status" && filter.value) {
    return pages.filter((page) => page.review_status === filter.value);
  }
  return pages;
}

export function filterGapsBySpace(gaps: KnowledgeGap[], filter: SpaceFilter): KnowledgeGap[] {
  if (filter.kind === "gaps") {
    return gaps.filter((gap) => gap.status !== "resolved" && gap.status !== "rejected");
  }
  return gaps;
}

export function filterReportsBySources(reports: IngestReport[], sources: SourceRecord[]): IngestReport[] {
  const sourceIds = new Set(sources.map((source) => source.id));
  return reports.filter((report) => sourceIds.has(report.source_id));
}

export function sourceTypeBucket(source: SourceRecord): string {
  const type = source.source_type.toLowerCase();
  if (["pdf", "doc", "docx", "md", "markdown", "txt"].includes(type)) return "document";
  if (["csv", "xls", "xlsx", "json"].includes(type)) return "data";
  if (["ppt", "pptx"].includes(type)) return "deck";
  return "other";
}

export function countSourcesByBucket(sources: SourceRecord[]) {
  return {
    all: sources.length,
    document: sources.filter((source) => sourceTypeBucket(source) === "document").length,
    data: sources.filter((source) => sourceTypeBucket(source) === "data").length,
    deck: sources.filter((source) => sourceTypeBucket(source) === "deck").length,
    warning: sources.filter((source) => {
      const warnings = source.metadata?.warnings;
      return Array.isArray(warnings) && warnings.length > 0;
    }).length,
  };
}

export function countPagesByType(pages: WikiPage[]) {
  return {
    all: pages.length,
    policy: pages.filter((page) => page.page_type === "policy").length,
    faq: pages.filter((page) => page.page_type === "faq").length,
    feature: pages.filter((page) => page.page_type === "feature").length,
    known_issue: pages.filter((page) => page.page_type === "known_issue").length,
    stale: pages.filter((page) => page.review_status === "stale").length,
    draft: pages.filter((page) => page.review_status === "draft").length,
  };
}

function countDomain(sources: SourceRecord[], pages: WikiPage[], domain: string): number {
  return sources.filter((source) => source.domain === domain).length + pages.filter((page) => page.domain === domain).length;
}

function collectDomains(sources: SourceRecord[], pages: WikiPage[]): string[] {
  const preferred = ["product", "customer_service", "administration"];
  const seen = new Set<string>();
  [...sources.map((source) => source.domain), ...pages.map((page) => page.domain)]
    .filter(Boolean)
    .forEach((domain) => seen.add(domain));
  return [
    ...preferred.filter((domain) => seen.has(domain)),
    ...Array.from(seen).filter((domain) => !preferred.includes(domain)).sort(),
  ];
}
