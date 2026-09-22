import { useState } from "react";
import { api, getApiKey, setApiKey } from "./api";
import { GrantDetailView, GrantList } from "./Grants";
import { useAsync, useHashRoute } from "./hooks";
import { ReviewDetailView, ReviewList } from "./ReviewQueue";
import { Sources } from "./Sources";

const REVIEWER_STORAGE = "fundscout.reviewer";

function stored(key: string): string {
  try {
    return localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

export function App() {
  const [key, setKey] = useState(getApiKey);
  const [keyDraft, setKeyDraft] = useState(key);
  const [reviewer, setReviewer] = useState(() => stored(REVIEWER_STORAGE));
  const [section, id] = useHashRoute();
  const taxonomy = useAsync(() => (key ? api.taxonomy() : Promise.resolve(undefined)), [key]);

  function saveKey(e: React.FormEvent) {
    e.preventDefault();
    setApiKey(keyDraft);
    setKey(keyDraft);
  }

  function saveReviewer(value: string) {
    setReviewer(value);
    try {
      localStorage.setItem(REVIEWER_STORAGE, value);
    } catch {
      /* ignore */
    }
  }

  const current = section ?? "review";
  const tab = (name: string, label: string) => (
    <a href={`#/${name}`} className={current === name ? "active" : undefined}>
      {label}
    </a>
  );

  return (
    <>
      <header>
        <strong>Fundscout admin</strong>
        <nav>
          {tab("review", "Review queue")}
          {tab("grants", "Grants")}
          {tab("sources", "Sources")}
        </nav>
        <form onSubmit={saveKey} className="key">
          <input
            placeholder="Your name (for review history)"
            value={reviewer}
            onChange={(e) => saveReviewer(e.target.value)}
          />
          <input
            type="password"
            placeholder="API key"
            value={keyDraft}
            onChange={(e) => setKeyDraft(e.target.value)}
          />
          <button type="submit">Save key</button>
        </form>
      </header>
      <main>
        {!key ? (
          <p>Enter the API key (the API_KEY setting) to continue.</p>
        ) : current === "grants" ? (
          id ? <GrantDetailView id={id} reviewer={reviewer} /> : <GrantList taxonomy={taxonomy.data} />
        ) : current === "sources" ? (
          <Sources />
        ) : id ? (
          <ReviewDetailView id={id} reviewer={reviewer} />
        ) : (
          <ReviewList />
        )}
      </main>
    </>
  );
}
