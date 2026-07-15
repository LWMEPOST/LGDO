import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setAuthToken } from "./api/client";
import { App } from "./App";

const backendObsidianUrl = "obsidian://open?vault=LGDO&file=wiki%2Fproduct%2Ffaq%2Frefund.md";

function jsonResponse(body: unknown, status = 200, statusText = "OK") {
  return new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch({ vaultUnavailable = false, role = "viewer" } = {}) {
  const requests: Array<{ path: string; method: string }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options: RequestInit = {}) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const method = options.method || "GET";
    requests.push({ path, method });

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
      return jsonResponse({ job_id: "reconcile-1", status: "queued", result: null, error_summary: null }, 202, "Accepted");
    }

    throw new Error(`Unexpected request: ${method} ${path}`);
  });

  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, requests };
}

afterEach(() => {
  cleanup();
  setAuthToken("");
  window.localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("App vault integration", () => {
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
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "在 Obsidian 中打开" }));

    await waitFor(() => expect(navigateTo).toHaveBeenCalledWith(backendObsidianUrl));
    expect(requests.some(({ path }) => path === "/api/internal/vault/status")).toBe(true);
    expect(requests.some(({ path }) => path === "/api/internal/reviews?status=pending")).toBe(true);
  });

  it("posts a reconcile job and surfaces its status for an admin", async () => {
    const { requests } = installFetch({ role: "admin" });
    setAuthToken("test-token");

    render(<App />);

    expect(await screen.findByText("LGDO Console")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("知识"));
    fireEvent.click(await screen.findByRole("button", { name: "立即对账" }));

    expect(await screen.findByText("Vault 对账已进入queued")).toBeInTheDocument();
    expect(requests).toContainEqual({ path: "/api/internal/vault/reconcile", method: "POST" });
  });
});
