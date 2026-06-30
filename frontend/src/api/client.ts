let authToken = window.localStorage.getItem("lgdo.auth.token") || "";

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
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || response.statusText);
  return body as T;
}
