// Typed client for the Fundscout internal API. The API key is kept in localStorage.

const KEY_STORAGE = "fundscout.apiKey";

export function getApiKey(): string {
  try {
    return localStorage.getItem(KEY_STORAGE) ?? "";
  } catch {
    return "";
  }
}

export function setApiKey(key: string): void {
  try {
    localStorage.setItem(KEY_STORAGE, key);
  } catch {
    /* storage unavailable: key lasts for this page only */
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

type Params = Record<string, string | number | boolean | undefined | null>;

async function request<T>(method: string, path: string, params?: Params, body?: unknown): Promise<T> {
  const query = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined && v !== null && v !== "") query.set(k, String(v));
  }
  const url = query.size ? `${path}?${query}` : path;
  const response = await fetch(url, {
    method,
    headers: {
      "X-API-Key": getApiKey(),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = await response.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface Funder {
  id: string;
  name: string;
  website: string | null;
  country: string | null;
}

export interface GrantSummary {
  id: string;
  title: string;
  funder: Funder | null;
  amount_min: string | null;
  amount_max: string | null;
  currency: string | null;
  opening_date: string | null;
  closing_date: string | null;
  deadline_type: string;
  status: string;
  needs_review: boolean;
  application_url: string | null;
  themes: string[];
  regions: string[];
  countries: string[];
  first_seen_at: string;
  last_seen_at: string;
}

export interface GrantSource {
  source_id: string;
  source_name: string;
  url: string;
  item_key: string;
  is_primary: boolean;
  first_seen_at: string;
  last_seen_at: string;
  missing_count: number;
}

export interface Change {
  id: string;
  grant_id: string;
  grant_title: string | null;
  change_source: string;
  changed_by: string | null;
  field: string;
  old_value: unknown;
  new_value: unknown;
  changed_at: string;
}

export interface ReviewItem {
  id: string;
  grant_id: string | null;
  grant_title: string | null;
  raw_document_id: string | null;
  reason: string;
  details: Record<string, unknown>;
  status: string;
  resolved_by: string | null;
  resolved_at: string | null;
  resolution_note: string | null;
  created_at: string;
}

export interface GrantDetail extends GrantSummary {
  description: string | null;
  amount_text: string | null;
  deadline_text: string | null;
  eligibility_text: string | null;
  funder_page_url: string | null;
  requirements: string | null;
  documents_required: string[] | null;
  funder_priorities: string | null;
  contact_email: string | null;
  contact_url: string | null;
  org_types: string[];
  grant_types: string[];
  manual_overrides: string[];
  sources: GrantSource[];
  changes: Change[];
  review_items: ReviewItem[];
}

export interface ReviewDetail extends ReviewItem {
  grant: GrantDetail | null;
  raw_document: {
    id: string;
    source_name: string;
    url: string;
    fetched_at: string;
    content_type: string | null;
  } | null;
  extraction: { method: string; model: string | null; output: ExtractionOutput; created_at: string } | null;
  related_grant: GrantSummary | null;
}

export interface ExtractionOutput {
  grants: Record<string, unknown>[];
  issues: Record<string, unknown>[][];
  notes: Record<string, unknown>;
}

export interface Run {
  id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  items_discovered: number;
  items_fetched: number;
  items_unchanged: number;
  grants_created: number;
  grants_updated: number;
  errors: number;
  error_log: Record<string, unknown>[];
}

export interface Source {
  id: string;
  name: string;
  adapter: string;
  base_url: string;
  schedule: string | null;
  enabled: boolean;
  terms_reviewed: boolean;
  notes: string | null;
  last_run_at: string | null;
  last_success_at: string | null;
  last_run: Run | null;
  grant_count: number;
}

export type Taxonomy = Record<"themes" | "regions" | "org_types" | "grant_types", { slug: string; label: string }[]>;

export const api = {
  grants: (params: Params) => request<Page<GrantSummary>>("GET", "/grants", params),
  grant: (id: string) => request<GrantDetail>("GET", `/grants/${id}`),
  patchGrant: (id: string, body: Record<string, unknown>) =>
    request<GrantDetail>("PATCH", `/grants/${id}`, undefined, body),
  review: (params: Params) => request<Page<ReviewItem>>("GET", "/review", params),
  reviewItem: (id: string) => request<ReviewDetail>("GET", `/review/${id}`),
  resolve: (id: string, note: string, by: string) =>
    request<ReviewDetail>("POST", `/review/${id}/resolve`, undefined, { note, resolved_by: by || null }),
  dismiss: (id: string, note: string, by: string) =>
    request<ReviewDetail>("POST", `/review/${id}/dismiss`, undefined, { note, resolved_by: by || null }),
  sources: () => request<Source[]>("GET", "/sources"),
  runs: (id: string) => request<Run[]>("GET", `/sources/${id}/runs`, { limit: 20 }),
  runSource: (id: string) => request<{ message: string }>("POST", `/sources/${id}/run`),
  taxonomy: () => request<Taxonomy>("GET", "/taxonomy"),
};

export function formatAmount(g: Pick<GrantSummary, "amount_min" | "amount_max" | "currency">): string {
  const fmt = (v: string) => Number(v).toLocaleString();
  const cur = g.currency ? `${g.currency} ` : "";
  if (g.amount_min && g.amount_max && g.amount_min !== g.amount_max)
    return `${cur}${fmt(g.amount_min)}–${fmt(g.amount_max)}`;
  const one = g.amount_max ?? g.amount_min;
  return one ? `${g.amount_min ? "" : "up to "}${cur}${fmt(one)}` : "—";
}

export function formatDate(value: string | null): string {
  if (!value) return "—";
  return value.length > 10 ? new Date(value).toLocaleString() : value;
}
