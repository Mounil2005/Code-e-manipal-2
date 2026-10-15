/** Backend origin. Empty = same origin (the Vite dev proxy locally); on Vercel, set VITE_API_URL
 * to the Render service URL. */
export const API_BASE = (import.meta.env.VITE_API_URL ?? "").trim().replace(/\/$/, "");
export const apiUrl = (path: string) => `${API_BASE}${path}`;
export const wsUrl = (path: string) =>
  API_BASE ? `${API_BASE.replace(/^http/, "ws")}${path}` : `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;

export class ApiError extends Error {
  code: string;
  status: number;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(apiUrl(path), {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "NETWORK", API_BASE ? `Backend unreachable at ${API_BASE}` : "Backend unreachable — is the API server running on :8000?");
  }
  if (!res.ok) {
    let code = "HTTP_" + res.status;
    let message = res.statusText;
    try {
      const j = await res.json();
      code = j?.error?.code ?? (Array.isArray(j?.detail) ? "VALIDATION" : code);
      message = j?.error?.message ?? (Array.isArray(j?.detail) ? j.detail.map((d: any) => d.msg).join("; ") : message);
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, code, message);
  }
  return (await res.json()) as T;
}

export const api = {
  get: <T,>(p: string) => request<T>("GET", p),
  post: <T,>(p: string, b: unknown = {}) => request<T>("POST", p, b),
};
