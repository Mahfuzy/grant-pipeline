import { useState } from "react";
import { api, formatDate, type Source } from "./api";
import { ErrorBox, ExternalLink, Json, Loading, Status } from "./components";
import { useAsync } from "./hooks";

export function Sources() {
  const sources = useAsync(() => api.sources(), []);
  const [open, setOpen] = useState<string>();
  const [message, setMessage] = useState<string>();
  const [error, setError] = useState<string>();

  async function run(source: Source) {
    setMessage(undefined);
    setError(undefined);
    try {
      await api.runSource(source.id);
      setMessage(`Run of ${source.name} started. Refresh in a while to see the result.`);
      setTimeout(sources.reload, 1500);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <section>
      <h2>Sources</h2>
      <div className="actions">
        <button onClick={sources.reload}>Refresh</button>
      </div>
      {message && <div className="notice">{message}</div>}
      <ErrorBox error={error ?? sources.error} />
      <Loading when={sources.loading && !sources.data} />
      {sources.data && (
        <table>
          <thead>
            <tr>
              <th>Source</th>
              <th>Status</th>
              <th>Schedule (UTC)</th>
              <th>Last run</th>
              <th>Last success</th>
              <th>Grants</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sources.data.map((s) => (
              <SourceRow
                key={s.id}
                source={s}
                open={open === s.id}
                onToggle={() => setOpen(open === s.id ? undefined : s.id)}
                onRun={() => run(s)}
              />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function SourceRow({
  source: s,
  open,
  onToggle,
  onRun,
}: {
  source: Source;
  open: boolean;
  onToggle: () => void;
  onRun: () => void;
}) {
  const runnable = s.enabled && s.terms_reviewed;
  const last = s.last_run;
  return (
    <>
      <tr>
        <td>
          <button className="link" onClick={onToggle}>
            {open ? "▾" : "▸"} {s.name}
          </button>
          <div className="muted small">
            {s.adapter} · <ExternalLink href={s.base_url} />
          </div>
        </td>
        <td>
          {!s.enabled && <Status value="disabled" />}
          {!s.terms_reviewed && <Status value="terms_not_reviewed" />}
          {runnable && <Status value="enabled" />}
        </td>
        <td>{s.schedule ?? "manual only"}</td>
        <td>
          {last ? (
            <>
              <Status value={last.status} /> {formatDate(last.started_at)}
              <div className="muted small">
                {last.items_discovered} found · {last.items_fetched} fetched · {last.grants_created} new ·{" "}
                {last.grants_updated} updated · {last.errors} errors
              </div>
            </>
          ) : (
            "never"
          )}
        </td>
        <td className="nowrap">{formatDate(s.last_success_at)}</td>
        <td>{s.grant_count}</td>
        <td>
          <button className="primary" disabled={!runnable || last?.status === "running"} onClick={onRun}
            title={runnable ? "Run now" : "Disabled or terms not reviewed"}>
            Run now
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={7}>
            <Runs source={s} />
          </td>
        </tr>
      )}
    </>
  );
}

function Runs({ source }: { source: Source }) {
  const runs = useAsync(() => api.runs(source.id), [source.id]);
  return (
    <div className="card">
      {source.notes && <p className="muted small prewrap">{source.notes}</p>}
      <ErrorBox error={runs.error} />
      <Loading when={runs.loading} />
      {runs.data && (
        <table>
          <thead>
            <tr>
              <th>Started</th>
              <th>Status</th>
              <th>Found</th>
              <th>Fetched</th>
              <th>Unchanged</th>
              <th>New</th>
              <th>Updated</th>
              <th>Errors</th>
            </tr>
          </thead>
          <tbody>
            {runs.data.map((r) => (
              <tr key={r.id}>
                <td className="nowrap">{formatDate(r.started_at)}</td>
                <td>
                  <Status value={r.status} />
                </td>
                <td>{r.items_discovered}</td>
                <td>{r.items_fetched}</td>
                <td>{r.items_unchanged}</td>
                <td>{r.grants_created}</td>
                <td>{r.grants_updated}</td>
                <td>
                  {r.errors}
                  {r.error_log.length > 0 && (
                    <details>
                      <summary>errors</summary>
                      <Json value={r.error_log} />
                    </details>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
