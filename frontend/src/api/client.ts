let authToken = window.localStorage.getItem("lgdo.auth.token") || "";

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function parseResponseBody(bodyText: string): unknown {
  if (!bodyText) return {};
  try {
    return JSON.parse(bodyText);
  } catch {
    return bodyText;
  }
}

function responseDetail(body: unknown): unknown {
  if (typeof body === "object" && body !== null && "detail" in body) {
    return (body as { detail: unknown }).detail;
  }
  return body;
}

function errorMessage(status: number, statusText: string, detail: unknown): string {
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (typeof detail === "object" && detail !== null) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) return message.trim();
    const serialized = JSON.stringify(detail);
    if (serialized && serialized !== "{}") return serialized;
  }
  return statusText ? `${status} ${statusText}` : `请求失败（HTTP ${status}）`;
}

export function setAuthToken(token: string) {
  authToken = token;
  if (token) {
    window.localStorage.setItem("lgdo.auth.token", token);
  } else {
    window.localStorage.removeItem("lgdo.auth.token");
  }
}

export function getAuthToken() {
  return authToken;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = {
    "Content-Type": "application/json",
    ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
    ...(options.headers || {}),
  };
  const response = await fetch(path, {
    headers,
    ...options,
  });
  const body = parseResponseBody(await response.text());
  if (!response.ok) {
    const detail = responseDetail(body);
    throw new ApiError(response.status, detail, errorMessage(response.status, response.statusText, detail));
  }
  return body as T;
}
