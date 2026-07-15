import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setAuthToken } from "./api/client";
import { App } from "./App";

const backendObsidianUrl = "obsidian://open?vault=LGDO&file=wiki%2Fproduct%2Ffaq%2Frefund.md";
const requiredWorkspacePaths = [
  "/api/internal/sources",
  "/api/internal/ingest/reports",
  "/api/internal/wiki/pages",
  "/api/internal/reviews?status=pending",
  "/api/internal/gaps",
  "/api/internal/rag/status",
] as const;

function jsonResponse(body: unknown, status = 200, statusText = "OK") {
  return new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

function wikiPage(title: string, path: string) {
  return {
    path,
    page_id: `page-${title}`,
    domain: "product",
    page_type: "faq",
    title,
    source_ids: [`source-${title}`],
    review_status: "reviewed",
    owner: "ops",
    current_revision_id: `rev-${title}`,
    generated_revision_id: null,
    accepted_generated_revision_id: null,
    lifecycle_status: "active",
    projection_epoch: 1,
    pending_write_intent_id: null,
    write_in_progress: false,
    write_intent_id: null,
    sync_error: null,
  };
}

function vaultStatus(overrides: Record<string, unknown> = {}) {
  return {
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
    ...overrides,
  };
}

function account(userId: string, username: string) {
  return {
    user_id: userId,
    username,
    role: "admin",
    acl_tags: ["*"],
    status: "active",
    auth_provider: "local",
    password_configured: true,
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T00:00:00Z",
    last_login_at: null,
  };
}

function requiredWorkspaceResponse(path: string, pageTitle: string) {
  if (path === "/api/internal/wiki/pages") {
    return jsonResponse([wikiPage(pageTitle, `wiki/product/faq/${pageTitle}.md`)]);
  }
  if (path === "/api/internal/rag/status") {
    return jsonResponse({
      database_backend: "sqlite",
      rag_store_backend: "memory",
      chunk_count: 0,
      source_count: 0,
      embedding_count: 0,
      embedding_model: "test",
      vector_count: 0,
      vector_backend: "memory",
      pgvector_enabled: false,
      domains: [],
      external_system_apis: {},
      postgres: { host: "localhost", port: 5432, database: "lgdo", user: "lgdo" },
    });
  }
  return jsonResponse([]);
}

function installFetch({
  vaultUnavailable = false,
  role = "viewer",
  vaultStatusResponse,
  reconcilePostResponse,
  reconcileJobs = [],
}: {
  vaultUnavailable?: boolean;
  role?: string;
  vaultStatusResponse?: Promise<Response>;
  reconcilePostResponse?: Promise<Response>;
  reconcileJobs?: Array<{ job_id: string; status: string; result: Record<string, number> | null; error_summary: string | null }>;
} = {}) {
  const requests: Array<{ path: string; method: string }> = [];
  const authorizations: Array<{ path: string; value: string | null }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options: RequestInit = {}) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const method = options.method || "GET";
    requests.push({ path, method });
    authorizations.push({ path, value: new Headers(options.headers).get("Authorization") });

    if (path === "/api/internal/auth/me") {
      return jsonResponse({
        user_id: `${role}-1`,
        username: role,
        role,
        acl_tags: role === "admin" ? ["*"] : ["product"],
        auth_provider: "local",
      });
    }
    if (path === "/api/internal/sources") return jsonResponse([]);
    if (path === "/api/internal/ingest/reports") return jsonResponse([]);
    if (path === "/api/internal/wiki/pages") {
      return jsonResponse([{
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
      }]);
    }
    if (path === "/api/internal/reviews?status=pending") return jsonResponse([]);
    if (path === "/api/internal/gaps") return jsonResponse([]);
    if (path === "/api/internal/rag/status") {
      return jsonResponse({
        database_backend: "sqlite",
        rag_store_backend: "memory",
        chunk_count: 0,
        source_count: 0,
        embedding_count: 0,
        embedding_model: "test",
        vector_count: 0,
        vector_backend: "memory",
        pgvector_enabled: false,
        domains: [],
        external_system_apis: {},
        postgres: { host: "localhost", port: 5432, database: "lgdo", user: "lgdo" },
      });
    }
    if (path === "/api/internal/vault/status") {
      if (vaultStatusResponse) return vaultStatusResponse;
      if (vaultUnavailable) return jsonResponse({ detail: "vault unavailable" }, 503, "Service Unavailable");
      return jsonResponse({
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
      });
    }
    if (path === "/api/internal/accounts") return jsonResponse([]);
    if (path === "/api/internal/wiki/pages/wiki/product/faq/refund.md/obsidian-link") {
      return jsonResponse({ url: backendObsidianUrl });
    }
    if (path === "/api/internal/vault/reconcile" && method === "POST") {
      if (reconcilePostResponse) return reconcilePostResponse;
      return jsonResponse({ job_id: "reconcile-1", status: "queued", result: null, error_summary: null }, 202, "Accepted");
    }
    if (path === "/api/internal/vault/reconcile/reconcile-1" && method === "GET" && reconcileJobs.length) {
      return jsonResponse(reconcileJobs.shift());
    }

    throw new Error(`Unexpected request: ${method} ${path}`);
  });

  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, requests, authorizations };
}

afterEach(() => {
  cleanup();
  vi.clearAllTimers();
  vi.useRealTimers();
  setAuthToken("");
  window.localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("App vault integration", () => {
  it("renders required workspace data before optional vault status resolves", async () => {
    const pendingVaultStatus = deferred<Response>();
    installFetch({ vaultStatusResponse: pendingVaultStatus.promise });
    setAuthToken("test-token");

    render(<App />);

    expect(await screen.findByText("LGDO Console")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("知识"));
    expect(await screen.findByText("退款流程", {}, { timeout: 400 })).toBeInTheDocument();
    expect(screen.getByText("同步状态未知")).toBeInTheDocument();

    pendingVaultStatus.resolve(jsonResponse({
      configured: true,
      running: false,
      clean: true,
      degraded: false,
      pending_occurrences: 0,
      failed_occurrences: 0,
      pending_deletes: 0,
      open_issues: 0,
      invalid_pages: 0,
      projection_backlog: 0,
      projection: {},
      obsidian: {},
      reconcile: null,
    }));
    expect(await screen.findByText("同步已停止")).toBeInTheDocument();
  });

  it("ignores required data and vault status from a logged-out session", async () => {
    const oldWorkspace = new Map(requiredWorkspacePaths.map((path) => [path, deferred<Response>()]));
    const oldVaultStatus = deferred<Response>();
    const endpointCalls = new Map<string, number>();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, options: RequestInit = {}) => {
      const path = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const call = (endpointCalls.get(path) || 0) + 1;
      endpointCalls.set(path, call);

      if (path === "/api/internal/auth/me") {
        return jsonResponse({ user_id: "old-admin", username: "old-admin", role: "admin", acl_tags: ["*"], auth_provider: "local" });
      }
      if (path === "/api/internal/auth/logout" && options.method === "POST") return jsonResponse({});
      if (path === "/api/internal/auth/login" && options.method === "POST") {
        return jsonResponse({ token: "new-token", user: account("new-admin", "new-admin") });
      }
      if (requiredWorkspacePaths.includes(path as typeof requiredWorkspacePaths[number])) {
        if (call === 1) return oldWorkspace.get(path as typeof requiredWorkspacePaths[number])!.promise;
        return requiredWorkspaceResponse(path, "新会话页面");
      }
      if (path === "/api/internal/vault/status") {
        if (call === 1) return oldVaultStatus.promise;
        return jsonResponse(vaultStatus({ configured: false }));
      }
      if (path === "/api/internal/accounts") return jsonResponse([account("new-account", "新账号")]);
      throw new Error(`Unexpected request: ${options.method || "GET"} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    setAuthToken("old-token");
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "退出" }));
    fireEvent.click(await screen.findByRole("button", { name: "登录" }));
    expect(await screen.findByText("new-admin · admin")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("知识"));
    expect(await screen.findByText("新会话页面")).toBeInTheDocument();
    expect(await screen.findByText("同步未启用")).toBeInTheDocument();

    await act(async () => {
      for (const [path, pending] of oldWorkspace) {
        pending.resolve(requiredWorkspaceResponse(path, "旧会话页面"));
      }
      oldVaultStatus.resolve(jsonResponse(vaultStatus({ clean: false, degraded: true, open_issues: 3 })));
      await Promise.all([...oldWorkspace.values()].map(({ promise }) => promise));
      await oldVaultStatus.promise;
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText("新会话页面")).toBeInTheDocument();
    expect(screen.queryByText("旧会话页面")).not.toBeInTheDocument();
    expect(screen.getByText("同步未启用")).toBeInTheDocument();
    expect(screen.queryByText("同步异常")).not.toBeInTheDocument();
  });

  it("ignores an account response that resolves after logout", async () => {
    const oldAccounts = deferred<Response>();
    const endpointCalls = new Map<string, number>();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, options: RequestInit = {}) => {
      const path = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const call = (endpointCalls.get(path) || 0) + 1;
      endpointCalls.set(path, call);

      if (path === "/api/internal/auth/me") {
        return jsonResponse({ user_id: "old-admin", username: "old-admin", role: "admin", acl_tags: ["*"], auth_provider: "local" });
      }
      if (path === "/api/internal/auth/logout" && options.method === "POST") return jsonResponse({});
      if (path === "/api/internal/auth/login" && options.method === "POST") {
        return jsonResponse({ token: "new-token", user: account("new-admin", "new-admin") });
      }
      if (requiredWorkspacePaths.includes(path as typeof requiredWorkspacePaths[number])) {
        return requiredWorkspaceResponse(path, call === 1 ? "旧会话页面" : "新会话页面");
      }
      if (path === "/api/internal/vault/status") {
        return jsonResponse(vaultStatus({ configured: call !== 2 }));
      }
      if (path === "/api/internal/accounts") {
        if (call === 1) return oldAccounts.promise;
        return jsonResponse([account("new-account", "新账号")]);
      }
      throw new Error(`Unexpected request: ${options.method || "GET"} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    setAuthToken("old-token");
    render(<App />);

    await waitFor(() => expect(endpointCalls.get("/api/internal/accounts")).toBe(1));
    fireEvent.click(screen.getByRole("button", { name: "退出" }));
    fireEvent.click(await screen.findByRole("button", { name: "登录" }));
    expect(await screen.findByText("new-admin · admin")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("账户"));
    expect(await screen.findByText("新账号")).toBeInTheDocument();

    await act(async () => {
      oldAccounts.resolve(jsonResponse([account("old-account", "旧账号")]));
      await oldAccounts.promise;
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText("新账号")).toBeInTheDocument();
    expect(screen.queryByText("旧账号")).not.toBeInTheDocument();
  });

  it("keeps required workspace data when vault status fails and navigates with the backend URL", async () => {
    const { requests } = installFetch({ vaultUnavailable: true });
    const navigateTo = vi.fn();
    setAuthToken("test-token");

    render(<App navigateTo={navigateTo} />);

    expect(await screen.findByText("LGDO Console")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "登录" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByTitle("知识"));
    const row = (await screen.findByText("退款流程")).closest(".item");
    expect(row).not.toBeNull();
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: /在 Obsidian 中打开/ }));

    await waitFor(() => expect(navigateTo).toHaveBeenCalledWith(backendObsidianUrl));
    expect(requests.some(({ path }) => path === "/api/internal/vault/status")).toBe(true);
    expect(requests.some(({ path }) => path === "/api/internal/reviews?status=pending")).toBe(true);
  });

  it.each([
    ["succeeded", "对账已完成", null],
    ["failed", "对账失败", '<img src=x onerror="alert(1)"> reconcile failed'],
  ])("polls the exact reconcile job until %s and refreshes vault status", async (terminalStatus, terminalLabel, errorSummary) => {
    const { requests } = installFetch({
      role: "admin",
      reconcileJobs: [
        { job_id: "reconcile-1", status: "running", result: null, error_summary: null },
        {
          job_id: "reconcile-1",
          status: terminalStatus,
          result: terminalStatus === "succeeded" ? { repaired: 1 } : null,
          error_summary: errorSummary,
        },
      ],
    });
    setAuthToken("test-token");

    render(<App />);

    expect(await screen.findByText("LGDO Console")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("知识"));
    const button = await screen.findByRole("button", { name: "立即对账" });
    vi.useFakeTimers();

    await act(async () => {
      fireEvent.click(button);
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByText("对账排队中")).toBeInTheDocument();
    expect(button).toBeDisabled();
    expect(requests.filter(({ path }) => path === "/api/internal/vault/reconcile/reconcile-1")).toHaveLength(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(screen.getByText("对账进行中")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });

    expect(screen.getByText(terminalLabel)).toBeInTheDocument();
    if (errorSummary) {
      expect(screen.getByText(errorSummary)).toBeInTheDocument();
      expect(document.querySelector("img")).not.toBeInTheDocument();
    }
    expect(button).toBeEnabled();
    expect(requests).toContainEqual({ path: "/api/internal/vault/reconcile", method: "POST" });
    expect(requests.filter(({ path }) => path === "/api/internal/vault/reconcile/reconcile-1"))
      .toEqual([
        { path: "/api/internal/vault/reconcile/reconcile-1", method: "GET" },
        { path: "/api/internal/vault/reconcile/reconcile-1", method: "GET" },
      ]);
    expect(requests.filter(({ path }) => path === "/api/internal/vault/status")).toHaveLength(2);
  });

  it("sends only one reconcile POST while the first request is pending", async () => {
    const pendingPost = deferred<Response>();
    const { requests } = installFetch({ role: "admin", reconcilePostResponse: pendingPost.promise });
    setAuthToken("test-token");

    render(<App />);

    expect(await screen.findByText("LGDO Console")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("知识"));
    const button = await screen.findByRole("button", { name: "立即对账" });

    fireEvent.click(button);
    fireEvent.click(button);

    expect(requests.filter(({ path, method }) => path === "/api/internal/vault/reconcile" && method === "POST"))
      .toHaveLength(1);
  });

  it("cancels a scheduled reconcile poll on logout", async () => {
    const { requests, authorizations } = installFetch({ role: "admin" });
    setAuthToken("test-token");

    render(<App />);

    expect(await screen.findByText("LGDO Console")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("知识"));
    const reconcileButton = await screen.findByRole("button", { name: "立即对账" });
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(reconcileButton);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("对账排队中")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "退出" }));
    expect(screen.getByRole("button", { name: "登录" })).toBeInTheDocument();
    expect(authorizations.find(({ path }) => path === "/api/internal/auth/logout")?.value)
      .toBe("Bearer test-token");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });

    expect(requests.filter(({ path }) => path === "/api/internal/vault/reconcile/reconcile-1"))
      .toHaveLength(0);
  });
});
