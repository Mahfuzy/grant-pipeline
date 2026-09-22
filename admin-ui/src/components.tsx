import type { ReactNode } from "react";

export function Status({ value }: { value: string }) {
  return <span className={`pill pill-${value}`}>{value.replace(/_/g, " ")}</span>;
}

export function ErrorBox({ error }: { error?: string }) {
  if (!error) return null;
  return <div className="error">{error}</div>;
}

export function Loading({ when }: { when: boolean }) {
  return when ? <div className="muted">Loading…</div> : null;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="field">
      <div className="field-label">{label}</div>
      <div className="field-value">{children ?? "—"}</div>
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export function Pager({
  page,
  pageSize,
  total,
  onPage,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPage: (page: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="pager">
      <button disabled={page <= 1} onClick={() => onPage(page - 1)}>
        ← Prev
      </button>
      <span>
        Page {page} of {pages} · {total} total
      </span>
      <button disabled={page >= pages} onClick={() => onPage(page + 1)}>
        Next →
      </button>
    </div>
  );
}

export function ExternalLink({ href, children }: { href: string | null; children?: ReactNode }) {
  if (!href) return <>—</>;
  return (
    <a href={href} target="_blank" rel="noreferrer noopener">
      {children ?? href}
    </a>
  );
}

export function display(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
