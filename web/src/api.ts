// Calls the Post-Vinyl API (docs/api.md). Types come from src/api-types.ts,
// generated from the app's /openapi.json with `npm run gen:api`.
import type { components } from "./api-types";

export type Schemas = components["schemas"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    detail: string,
    readonly retryAfter: string | null = null,
  ) {
    super(detail);
  }
}

// Called on any 401: the session is gone, show the login page.
let onLoggedOut = () => {};
export function whenLoggedOut(fn: () => void): void {
  onLoggedOut = fn;
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  // Same origin: the browser sends the cookie and the Origin header itself.
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 204) return undefined as T;
  const data: unknown = await res.json().catch(() => null);
  if (res.ok) return data as T;
  if (res.status === 401 && path !== "/api/auth/login") onLoggedOut();
  throw new ApiError(res.status, errorText(data, res.status), res.headers.get("Retry-After"));
}

function errorText(data: unknown, status: number): string {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d: { msg?: string }) => d.msg).join("; "); // 422
  return `request failed (${status})`;
}
