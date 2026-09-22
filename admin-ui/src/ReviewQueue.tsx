import { useState } from "react";
import { api, formatDate, type GrantDetail, type ReviewDetail } from "./api";
import { display, ErrorBox, ExternalLink, Json, Loading, Pager, Status } from "./components";
import { EditGrant } from "./EditGrant";
import { navigate, useAsync } from "./hooks";

const REASONS = [
  "low_confidence",
  "missing_required_fields",
  "conflicting_values",
  "possible_duplicate",
  "deadline_changed",
  "possibly_removed",
  "extraction_failed",
  "ambiguous_currency",
  "fetch_failed_repeatedly",
];

export function ReviewList() {
  const [status, setStatus] = useState("open");
  const [reason, setReason] = useState("");
  const [page, setPage] = useState(1);
  const list = useAsync(() => api.review({ status, reason, page, page_size: 50 }), [status, reason, page]);

  return (
    <section>
      <h2>Review queue</h2>
      <div className="filters">
        <select value={status} onChange={(e) => (setStatus(e.target.value), setPage(1))}>
          {["open", "resolved", "dismissed"].map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <select value={reason} onChange={(e) => (setReason(e.target.value), setPage(1))}>
          <option value="">All reasons</option>
          {REASONS.map((r) => (
            <option key={r} value={r}>
              {r.replace(/_/g, " ")}
            </option>
          ))}
        </select>
      </div>
      <ErrorBox error={list.error} />
      <Loading when={list.loading} />
      {list.data && (
        <>
          <table>
            <thead>
              <tr>
                <th>Reason</th>
                <th>Grant / document</th>
                <th>Summary</th>
                <th>Opened</th>
              </tr>
            </thead>
            <tbody>
              {list.data.items.map((item) => (
                <tr key={item.id} className="clickable" onClick={() => navigate(`review/${item.id}`)}>
                  <td>
                    <Status value={item.reason} />
                  </td>
                  <td>{item.grant_title ?? String(item.details.url ?? item.details.source ?? "—")}</td>
                  <td className="muted small">{summarise(item.details)}</td>
                  <td className="nowrap">{formatDate(item.created_at)}</td>
                </tr>
              ))}
              {list.data.items.length === 0 && (
                <tr>
                  <td colSpan={4} className="muted">
                    Nothing here.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <Pager page={page} pageSize={50} total={list.data.total} onPage={setPage} />
        </>
      )}
    </section>
  );
}

function summarise(details: Record<string, unknown>): string {
  if (details.old !== undefined) return `${display(details.old)} → ${display(details.new)}`;
  if (details.other_title) return `Similar to “${details.other_title}” (${details.score})`;
  if (details.error) return String(details.error).slice(0, 120);
  if (Array.isArray(details.conflicts))
    return details.conflicts.map((c: Record<string, unknown>) => `${c.field}: ${c.stored} vs ${c.incoming}`).join("; ");
  if (Array.isArray(details.issues))
    return details.issues.map((i: Record<string, unknown>) => `${i.field ?? i.detail ?? ""}`).join(", ");
  if (details.missing_count) return `Missing from listing for ${details.missing_count} runs`;
  return "";
}

// Fields compared side by side: stored grant vs what was extracted from the document.
const COMPARE = [
  "title",
  "funder_name",
  "amount_min",
  "amount_max",
  "currency",
  "amount_text",
  "opening_date",
  "closing_date",
  "deadline_type",
  "deadline_text",
  "application_url",
  "themes",
  "regions",
  "countries",
  "org_types",
];

function storedValue(grant: GrantDetail, field: string): unknown {
  if (field === "funder_name") return grant.funder?.name;
  return (grant as unknown as Record<string, unknown>)[field];
}

function pickExtracted(detail: ReviewDetail): Record<string, unknown> | undefined {
  const grants = detail.extraction?.output?.grants ?? [];
  const title = detail.grant?.title?.toLowerCase();
  return grants.find((g) => String(g.title ?? "").toLowerCase() === title) ?? grants[0];
}

function same(a: unknown, b: unknown): boolean {
  const norm = (v: unknown) =>
    v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0)
      ? ""
      : Array.isArray(v)
        ? [...v].sort().join(",")
        : typeof v === "string" && !Number.isNaN(Number(v)) && v.trim() !== ""
          ? String(Number(v))
          : String(v);
  return norm(a) === norm(b);
}

export function ReviewDetailView({ id, reviewer }: { id: string; reviewer: string }) {
  const item = useAsync(() => api.reviewItem(id), [id]);
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string>();

  async function act(kind: "resolve" | "dismiss") {
    setBusy(true);
    setActionError(undefined);
    try {
      await (kind === "resolve" ? api.resolve(id, note, reviewer) : api.dismiss(id, note, reviewer));
      navigate("review");
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const detail = item.data;
  const extracted = detail ? pickExtracted(detail) : undefined;
  return (
    <section>
      <a href="#/review">← Review queue</a>
      <ErrorBox error={item.error} />
      <Loading when={item.loading && !detail} />
      {detail && (
        <>
          <h2>
            <Status value={detail.reason} /> {detail.grant?.title ?? "Document without a grant"}
          </h2>
          <p className="muted">
            Opened {formatDate(detail.created_at)} · status <Status value={detail.status} />
            {detail.resolved_by && ` · by ${detail.resolved_by}`}
            {detail.resolution_note && ` · “${detail.resolution_note}”`}
          </p>

          <div className="card">
            <h3>Why this needs review</h3>
            <Json value={detail.details} />
            {detail.related_grant && (
              <p>
                Possible duplicate of{" "}
                <a href={`#/grants/${detail.related_grant.id}`}>{detail.related_grant.title}</a> (closes{" "}
                {formatDate(detail.related_grant.closing_date)})
              </p>
            )}
          </div>

          <div className="card">
            <h3>Source document</h3>
            {detail.raw_document ? (
              <p>
                <ExternalLink href={detail.raw_document.url} /> · {detail.raw_document.source_name} · fetched{" "}
                {formatDate(detail.raw_document.fetched_at)}
                {detail.extraction && ` · extracted by ${detail.extraction.method}${detail.extraction.model ? ` (${detail.extraction.model})` : ""}`}
              </p>
            ) : (
              <p className="muted">No document linked.</p>
            )}
          </div>

          {(detail.grant || extracted) && (
            <div className="card">
              <h3>Stored grant vs extracted from the document</h3>
              <table className="compare">
                <thead>
                  <tr>
                    <th>Field</th>
                    <th>Stored</th>
                    <th>Extracted</th>
                  </tr>
                </thead>
                <tbody>
                  {COMPARE.map((field) => {
                    const stored = detail.grant ? storedValue(detail.grant, field) : undefined;
                    const fromDoc = extracted?.[field];
                    const differs = detail.grant && extracted && !same(stored, fromDoc);
                    return (
                      <tr key={field} className={differs ? "differs" : undefined}>
                        <td className="field-label">{field.replace(/_/g, " ")}</td>
                        <td>{detail.grant ? display(stored) : "—"}</td>
                        <td>{extracted ? display(fromDoc) : "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {detail.grant && (
                <p>
                  <a href={`#/grants/${detail.grant.id}`}>Open grant →</a>
                </p>
              )}
            </div>
          )}

          {editing && detail.grant && (
            <EditGrant
              grant={detail.grant}
              reviewer={reviewer}
              onCancel={() => setEditing(false)}
              onSaved={() => {
                setEditing(false);
                item.reload();
              }}
            />
          )}

          {detail.status === "open" && (
            <div className="card">
              <h3>Decision</h3>
              <textarea
                rows={2}
                placeholder="Note (optional)"
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
              <ErrorBox error={actionError} />
              <div className="actions">
                <button className="primary" disabled={busy} onClick={() => act("resolve")}>
                  Resolve
                </button>
                {detail.grant && !editing && <button onClick={() => setEditing(true)}>Edit grant…</button>}
                <button disabled={busy} onClick={() => act("dismiss")}>
                  Dismiss
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
