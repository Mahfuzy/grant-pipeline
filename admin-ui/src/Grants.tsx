import { useState } from "react";
import { api, formatAmount, formatDate, type Taxonomy } from "./api";
import { display, ErrorBox, ExternalLink, Field, Loading, Pager, Status } from "./components";
import { EditGrant } from "./EditGrant";
import { navigate, useAsync } from "./hooks";

interface Filters {
  q: string;
  status: string;
  theme: string;
  region: string;
  country: string;
  org_type: string;
  closing_after: string;
  closing_before: string;
  needs_review: string;
}

const EMPTY: Filters = {
  q: "",
  status: "",
  theme: "",
  region: "",
  country: "",
  org_type: "",
  closing_after: "",
  closing_before: "",
  needs_review: "",
};

export function GrantList({ taxonomy }: { taxonomy?: Taxonomy }) {
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [page, setPage] = useState(1);
  const list = useAsync(() => api.grants({ ...filters, page, page_size: 50 }), [filters, page]);
  const set = (key: keyof Filters) => (e: { target: { value: string } }) => setDraft({ ...draft, [key]: e.target.value });

  function apply(e: React.FormEvent) {
    e.preventDefault();
    setFilters(draft);
    setPage(1);
  }

  const options = (kind: keyof Taxonomy) =>
    (taxonomy?.[kind] ?? []).map((t) => (
      <option key={t.slug} value={t.slug}>
        {t.label}
      </option>
    ));

  return (
    <section>
      <h2>Grants</h2>
      <form className="filters" onSubmit={apply}>
        <input placeholder="Search title and description" value={draft.q} onChange={set("q")} className="grow" />
        <select value={draft.status} onChange={set("status")}>
          <option value="">Any status</option>
          {["open", "upcoming", "rolling", "closed", "unknown"].map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <select value={draft.theme} onChange={set("theme")}>
          <option value="">Any theme</option>
          {options("themes")}
        </select>
        <select value={draft.region} onChange={set("region")}>
          <option value="">Any region</option>
          {options("regions")}
        </select>
        <select value={draft.org_type} onChange={set("org_type")}>
          <option value="">Any organisation type</option>
          {options("org_types")}
        </select>
        <input placeholder="Country (ISO, e.g. GH)" maxLength={2} size={8} value={draft.country} onChange={set("country")} />
        <label className="inline">
          Closing after <input type="date" value={draft.closing_after} onChange={set("closing_after")} />
        </label>
        <label className="inline">
          before <input type="date" value={draft.closing_before} onChange={set("closing_before")} />
        </label>
        <select value={draft.needs_review} onChange={set("needs_review")}>
          <option value="">Review: any</option>
          <option value="true">Needs review</option>
          <option value="false">No open review</option>
        </select>
        <button className="primary" type="submit">
          Search
        </button>
        <button type="button" onClick={() => (setDraft(EMPTY), setFilters(EMPTY), setPage(1))}>
          Clear
        </button>
      </form>
      <ErrorBox error={list.error} />
      <Loading when={list.loading} />
      {list.data && (
        <>
          <table>
            <thead>
              <tr>
                <th>Title</th>
                <th>Funder</th>
                <th>Amount</th>
                <th>Closes</th>
                <th>Status</th>
                <th>Themes</th>
              </tr>
            </thead>
            <tbody>
              {list.data.items.map((g) => (
                <tr key={g.id} className="clickable" onClick={() => navigate(`grants/${g.id}`)}>
                  <td>
                    {g.title}
                    {g.needs_review && <span className="flag" title="Open review items">●</span>}
                  </td>
                  <td>{g.funder?.name ?? "—"}</td>
                  <td className="nowrap">{formatAmount(g)}</td>
                  <td className="nowrap">{formatDate(g.closing_date)}</td>
                  <td>
                    <Status value={g.status} />
                  </td>
                  <td className="small">{display(g.themes)}</td>
                </tr>
              ))}
              {list.data.items.length === 0 && (
                <tr>
                  <td colSpan={6} className="muted">
                    No grants match.
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

export function GrantDetailView({ id, reviewer }: { id: string; reviewer: string }) {
  const grant = useAsync(() => api.grant(id), [id]);
  const [editing, setEditing] = useState(false);
  const g = grant.data;
  return (
    <section>
      <a href="#/grants">← Grants</a>
      <ErrorBox error={grant.error} />
      <Loading when={grant.loading && !g} />
      {g && (
        <>
          <h2>{g.title}</h2>
          <p>
            <Status value={g.status} /> {g.funder?.name ?? "Unknown funder"}
            {g.manual_overrides.length > 0 && (
              <span className="muted"> · manually corrected: {g.manual_overrides.join(", ")}</span>
            )}
          </p>
          {!editing && <button onClick={() => setEditing(true)}>Edit…</button>}
          {editing && (
            <EditGrant
              grant={g}
              reviewer={reviewer}
              onCancel={() => setEditing(false)}
              onSaved={() => {
                setEditing(false);
                grant.reload();
              }}
            />
          )}
          <div className="card grid2">
            <Field label="Amount">{formatAmount(g)}</Field>
            <Field label="Amount as written">{g.amount_text}</Field>
            <Field label="Opens">{formatDate(g.opening_date)}</Field>
            <Field label="Closes">
              {formatDate(g.closing_date)} ({g.deadline_type})
            </Field>
            <Field label="Deadline as written">{g.deadline_text}</Field>
            <Field label="Apply">
              <ExternalLink href={g.application_url} />
            </Field>
            <Field label="Themes">{display(g.themes)}</Field>
            <Field label="Regions">{display(g.regions)}</Field>
            <Field label="Countries">{display(g.countries)}</Field>
            <Field label="Organisation types">{display(g.org_types)}</Field>
            <Field label="Grant types">{display(g.grant_types)}</Field>
            <Field label="Contact">{g.contact_email ?? <ExternalLink href={g.contact_url} />}</Field>
            <Field label="First seen">{formatDate(g.first_seen_at)}</Field>
            <Field label="Last seen">{formatDate(g.last_seen_at)}</Field>
          </div>
          {g.description && (
            <div className="card">
              <h3>Description</h3>
              <p className="prewrap">{g.description}</p>
            </div>
          )}
          {g.eligibility_text && (
            <div className="card">
              <h3>Eligibility</h3>
              <p className="prewrap">{g.eligibility_text}</p>
            </div>
          )}
          <div className="card">
            <h3>Sources</h3>
            <table>
              <thead>
                <tr>
                  <th>Source</th>
                  <th>URL</th>
                  <th>Primary</th>
                  <th>Last seen</th>
                  <th>Missing runs</th>
                </tr>
              </thead>
              <tbody>
                {g.sources.map((s) => (
                  <tr key={`${s.source_id}${s.url}${s.item_key}`}>
                    <td>{s.source_name}</td>
                    <td className="break">
                      <ExternalLink href={s.url} />
                    </td>
                    <td>{s.is_primary ? "yes" : "no"}</td>
                    <td className="nowrap">{formatDate(s.last_seen_at)}</td>
                    <td>{s.missing_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="card">
            <h3>Review items</h3>
            {g.review_items.length === 0 && <p className="muted">None.</p>}
            <ul>
              {g.review_items.map((r) => (
                <li key={r.id}>
                  <a href={`#/review/${r.id}`}>
                    <Status value={r.reason} />
                  </a>{" "}
                  <Status value={r.status} /> {formatDate(r.created_at)}
                </li>
              ))}
            </ul>
          </div>
          <div className="card">
            <h3>History</h3>
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Field</th>
                  <th>Old</th>
                  <th>New</th>
                  <th>By</th>
                </tr>
              </thead>
              <tbody>
                {g.changes.map((c) => (
                  <tr key={c.id}>
                    <td className="nowrap">{formatDate(c.changed_at)}</td>
                    <td>{c.field}</td>
                    <td className="small break">{display(c.old_value)}</td>
                    <td className="small break">{display(c.new_value)}</td>
                    <td>{c.change_source === "manual" ? (c.changed_by ?? "manual") : "pipeline"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
