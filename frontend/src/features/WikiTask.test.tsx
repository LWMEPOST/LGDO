import type { ComponentProps } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthUser, EditorState, VaultStatus, WikiPage } from "../types";
import { WikiTask } from "./WikiTask";

const page: WikiPage = {
  path: "wiki/product/faq/refund.md",
  page_id: "page-refund",
  domain: "product",
  page_type: "faq",
  title: "退款流程",
  source_ids: ["source-refund"],
  review_status: "reviewed",
  owner: "ops",
  current_revision_id: "rev-1",
  generated_revision_id: null,
  accepted_generated_revision_id: null,
  lifecycle_status: "active",
  projection_epoch: 1,
  pending_write_intent_id: null,
  write_in_progress: false,
  write_intent_id: null,
  sync_error: null,
};

const editor: EditorState = {
  path: "wiki/product/faq/selected.md",
  page_id: "page-selected",
  content: "# Selected",
  current_revision_id: "rev-selected",
  generated_revision_id: null,
  accepted_generated_revision_id: null,
  lifecycle_status: "active",
  projection_epoch: 1,
  write_in_progress: false,
  write_intent_id: null,
  review_status: "reviewed",
  owner: "ops",
};

const viewer: AuthUser = {
  user_id: "viewer-1",
  username: "viewer",
  role: "viewer",
  acl_tags: ["product"],
  auth_provider: "local",
};

const healthyVaultStatus: VaultStatus = {
  configured: true,
  running: false,
  clean: true,
  degraded: false,
  last_event_at: null,
  last_error: null,
  pending_occurrences: 0,
  failed_occurrences: 0,
  pending_deletes: 0,
  open_issues: 0,
  invalid_pages: 0,
  projection_backlog: 0,
  projection: {},
  obsidian: {},
  reconcile: null,
};

type WikiTaskProps = ComponentProps<typeof WikiTask>;

function renderWikiTask(overrides: Partial<WikiTaskProps> = {}) {
  const props: WikiTaskProps = {
    pages: [page],
    activeSpaceFilter: {
      id: "all",
      label: "全部空间",
      desc: "全部知识内容",
      kind: "all",
      count: 1,
    },
    clearSpaceFilter: vi.fn(),
    editor,
    setEditor: vi.fn(),
    loadPage: vi.fn(async () => undefined),
    savePage: vi.fn(async () => undefined),
    markPageStale: vi.fn(async () => undefined),
    openInObsidian: vi.fn(async () => undefined),
    requestReconcile: vi.fn(async () => undefined),
    vaultStatus: healthyVaultStatus,
    currentUser: viewer,
    showToast: vi.fn(),
    ...overrides,
  };

  return { props, ...render(<WikiTask {...props} />) };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("WikiTask Obsidian controls", () => {
  it("opens the canonical path from a page row for a viewer", () => {
    const { props } = renderWikiTask();
    const row = screen.getByText("退款流程").closest(".item");

    expect(row).not.toBeNull();
    const button = within(row as HTMLElement).getByRole("button", {
      name: /在 Obsidian 中打开.*退款流程.*wiki\/product\/faq\/refund\.md/,
    });
    expect(button).toHaveAttribute("title", "在 Obsidian 中打开");
    fireEvent.click(button);

    expect(props.openInObsidian).toHaveBeenCalledTimes(1);
    expect(props.openInObsidian).toHaveBeenCalledWith(page.path);
  });

  it("shows degraded sync counts without exposing reconcile to a viewer", () => {
    renderWikiTask({
      vaultStatus: {
        ...healthyVaultStatus,
        clean: false,
        degraded: true,
        pending_occurrences: 1,
        open_issues: 1,
      },
    });

    expect(screen.getByText("同步异常").closest(".sync-status")).toHaveClass("warn");
    expect(screen.getByText("1 个待处理事件")).toBeInTheDocument();
    expect(screen.getByText("1 个同步问题")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "立即对账" })).not.toBeInTheDocument();
  });

  it("announces sync changes politely", () => {
    renderWikiTask();

    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
  });

  it("renders missing vault status as neutral unknown", () => {
    renderWikiTask({ vaultStatus: null });

    const label = screen.getByText("同步状态未知");
    const status = label.closest(".sync-status");
    expect(status).toHaveClass("unknown");
    expect(status).not.toHaveClass("ok");
  });

  it("distinguishes an unconfigured vault", () => {
    renderWikiTask({ vaultStatus: { ...healthyVaultStatus, configured: false } });

    const status = screen.getByText("同步未启用").closest(".sync-status");
    expect(status).toHaveClass("disabled");
    expect(status).not.toHaveClass("ok");
  });

  it("requests one reconcile for an admin", () => {
    const requestReconcile = vi.fn(async () => undefined);
    renderWikiTask({
      currentUser: { ...viewer, role: "admin" },
      requestReconcile,
    });

    const button = screen.getByRole("button", { name: "立即对账" });
    expect(button).toHaveAttribute("title", "立即对账");
    fireEvent.click(button);

    expect(requestReconcile).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["queued", "对账排队中"],
    ["running", "对账进行中"],
  ])("shows active reconcile status %s and prevents duplicate requests", (status, label) => {
    const requestReconcile = vi.fn(async () => undefined);
    renderWikiTask({
      currentUser: { ...viewer, role: "admin" },
      requestReconcile,
      vaultStatus: {
        ...healthyVaultStatus,
        reconcile: { job_id: "reconcile-active", status },
      },
    });

    expect(screen.getByText(label)).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "立即对账" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    fireEvent.click(button);
    expect(requestReconcile).not.toHaveBeenCalled();
  });

  it("shows a failed reconcile summary as safe text and allows retry", () => {
    const errorSummary = '<img src=x onerror="alert(1)"> inventory failed';
    const { container } = renderWikiTask({
      currentUser: { ...viewer, role: "admin" },
      vaultStatus: {
        ...healthyVaultStatus,
        reconcile: {
          job_id: "reconcile-failed",
          status: "failed",
          error_summary: errorSummary,
        },
      },
    });

    expect(screen.getByText("对账失败")).toBeInTheDocument();
    expect(screen.getByText(errorSummary)).toBeInTheDocument();
    expect(container.querySelector("img")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "立即对账" })).toBeEnabled();
  });

  it.each([
    ["owner role", { ...viewer, role: "owner" }],
    ["wildcard ACL", { ...viewer, acl_tags: ["*"] }],
  ])("allows reconcile for %s", (_label, currentUser) => {
    renderWikiTask({ currentUser });

    expect(screen.getByRole("button", { name: "立即对账" })).toBeInTheDocument();
  });

  it("opens the selected editor path", () => {
    const openInObsidian = vi.fn(async () => undefined);
    renderWikiTask({ openInObsidian });
    const editorPanel = screen.getByRole("heading", { name: "Markdown 编辑区" }).closest(".panel");

    expect(editorPanel).not.toBeNull();
    const button = within(editorPanel as HTMLElement).getByRole("button", {
      name: /在 Obsidian 中打开.*wiki\/product\/faq\/selected\.md/,
    });
    expect(button).toHaveAttribute("title", "在 Obsidian 中打开");
    fireEvent.click(button);

    expect(openInObsidian).toHaveBeenCalledWith(editor.path);
  });

  it("disables the editor Obsidian control when no page is selected", () => {
    const openInObsidian = vi.fn(async () => undefined);
    renderWikiTask({ editor: { ...editor, path: "" }, openInObsidian });
    const editorPanel = screen.getByRole("heading", { name: "Markdown 编辑区" }).closest(".panel");

    const button = within(editorPanel as HTMLElement).getByRole("button", { name: /在 Obsidian 中打开/ });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(openInObsidian).not.toHaveBeenCalled();
  });
});
