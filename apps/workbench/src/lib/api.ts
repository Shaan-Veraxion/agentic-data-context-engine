// Client for the ADCE API gateway. The workbench never talks to stores or agents directly.

// Direct API URL for local development; in images the workbench proxies /api to the API gateway at runtime
// (src/app/api/[...path]/route.ts), so one image works in every environment.
export const API = process.env.NEXT_PUBLIC_ADCE_API ?? "/api";

export type Coverage = { total: number; understood: number; other: number; unresolved_variables: number; ratio: number };

export type AppSummary = { app_id: string; name: string; coverage: Coverage };

export type TableRow = {
  key: string;
  name: string;
  fields: string[];
  dropped: boolean;
  level: string | null;
  venue: string | null;
  approved: boolean;
  comparisons: string | null;
  checks_ok: boolean | null;
};

export type Overview = {
  app_id: string;
  coverage: Coverage;
  tables: TableRow[];
  issues: { code: string; message: string; statement: number | null }[];
  open_review_items: number;
  latest_pack: { run_id: string; files: string[]; prefix: string } | null;
};

export type ReviewItem = {
  id: string;
  app_id: string;
  mode: "ask" | "queue" | "gate" | "seed" | "correct";
  kind: string;
  subject: string;
  title: string;
  detail: Record<string, unknown>;
  options: string[];
  impact: number;
  uncertainty: number;
  score: number;
  status: string;
};

export type LineageNode = { id: string; type: "table" | "mapping" | "source" | "qvd"; label: string; level?: string | null; dropped?: boolean };
export type LineageEdge = { id: string; source: string; target: string; kind: string; via?: string | null };

function headers(json = false): HeadersInit {
  const h: Record<string, string> = {};
  if (json) h["content-type"] = "application/json";
  if (typeof window !== "undefined") {
    const token = window.sessionStorage.getItem("adce_token"); // set by the Keycloak login flow when auth is on
    if (token) h["authorization"] = `Bearer ${token}`;
  }
  return h;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, { cache: "no-store", ...init });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  apps: () => call<AppSummary[]>("/apps", { headers: headers() }),
  overview: (app: string) => call<Overview>(`/apps/${app}/overview`, { headers: headers() }),
  lineage: (app: string) => call<{ nodes: LineageNode[]; edges: LineageEdge[] }>(`/apps/${app}/lineage`, { headers: headers() }),
  review: (app: string) => call<ReviewItem[]>(`/review/items?app_id=${encodeURIComponent(app)}`, { headers: headers() }),
  refreshReview: (app: string) => call<{ new_items: number }>(`/review/apps/${app}/refresh`, { method: "POST", headers: headers() }),
  decide: (id: string, decision: string, payload: Record<string, unknown> = {}) =>
    call<ReviewItem>(`/review/items/${id}/decision`, { method: "POST", headers: headers(true), body: JSON.stringify({ decision, payload }) }),
  upload: (app: string, files: File[]) => {
    const body = new FormData();
    // keep sources/<path> for local source extracts (webkitRelativePath when a folder is chosen)
    for (const f of files) body.append("files", f, (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name);
    return call<{ files: { name: string }[] }>(`/apps/${app}/files`, { method: "POST", headers: headers(), body });
  },
  run: (app: string, appName?: string) =>
    call<{ run_id: string; status: string; levels: Record<string, string>; new_review_items: number }>(`/apps/${app}/runs`, {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify({ app_name: appName || null }),
    }),
  packFileUrl: (app: string, run: string, path: string) => `${API}/apps/${app}/packs/${run}/files/${path}`,
};

export const LEVELS = ["V0", "V1", "V2", "V3", "V4", "V5"];
