import { useState } from "react";
import { api, type GrantDetail } from "./api";
import { ErrorBox } from "./components";

// Fields a reviewer can correct from the UI. Saved edits are recorded as manual changes
// and protected from later pipeline updates.
const FIELDS: { name: keyof GrantDetail; label: string; type: "text" | "date" | "number" | "textarea" | "deadline" }[] = [
  { name: "title", label: "Title", type: "text" },
  { name: "amount_min", label: "Amount min", type: "number" },
  { name: "amount_max", label: "Amount max", type: "number" },
  { name: "currency", label: "Currency (ISO 4217)", type: "text" },
  { name: "opening_date", label: "Opening date", type: "date" },
  { name: "closing_date", label: "Closing date", type: "date" },
  { name: "deadline_type", label: "Deadline type", type: "deadline" },
  { name: "application_url", label: "Application URL", type: "text" },
  { name: "eligibility_text", label: "Eligibility", type: "textarea" },
];

type Values = Record<string, string>;

function initial(grant: GrantDetail): Values {
  return Object.fromEntries(FIELDS.map((f) => [f.name, grant[f.name] == null ? "" : String(grant[f.name])]));
}

export function EditGrant({
  grant,
  reviewer,
  onSaved,
  onCancel,
}: {
  grant: GrantDetail;
  reviewer: string;
  onSaved: (grant: GrantDetail) => void;
  onCancel: () => void;
}) {
  const [values, setValues] = useState<Values>(() => initial(grant));
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);
  const start = initial(grant);

  async function save() {
    const body: Record<string, unknown> = {};
    for (const f of FIELDS) {
      if (values[f.name] !== start[f.name]) body[f.name] = values[f.name].trim() === "" ? null : values[f.name].trim();
    }
    if (Object.keys(body).length === 0) return onCancel();
    body.changed_by = reviewer || null;
    setSaving(true);
    setError(undefined);
    try {
      onSaved(await api.patchGrant(grant.id, body));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="card">
      <h3>Correct grant</h3>
      <div className="form-grid">
        {FIELDS.map((f) => (
          <label key={f.name} className={f.type === "textarea" ? "wide" : undefined}>
            {f.label}
            {f.type === "textarea" ? (
              <textarea
                rows={4}
                value={values[f.name]}
                onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
              />
            ) : f.type === "deadline" ? (
              <select value={values[f.name]} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}>
                {["fixed", "rolling", "multiple", "unknown"].map((d) => (
                  <option key={d}>{d}</option>
                ))}
              </select>
            ) : (
              <input
                type={f.type}
                value={values[f.name]}
                onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
              />
            )}
          </label>
        ))}
      </div>
      <ErrorBox error={error} />
      <div className="actions">
        <button className="primary" disabled={saving} onClick={save}>
          {saving ? "Saving…" : "Save correction"}
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
