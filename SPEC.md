# Fundscout — Grant Data Pipeline Build Spec

> **For Claude Code:** This is the source of truth for the project. Build it milestone by milestone (see §12). After each milestone, stop, summarise what you built, show how to run/test it, and wait for approval before starting the next one. If something here is ambiguous or you need a decision (especially about sources), ask instead of guessing. Never invent grant sources or fake data outside of test fixtures.

---

## 1. Goal

**Naming:** Product name is **Fundscout**. This repo is `grant-pipeline` (the data layer only). Python package and CLI are both `fundscout`. Future repos will use the `fundscout-` prefix (e.g. `fundscout-web`, `fundscout-matching`).

Build the data foundation for a platform that helps organisations find grant funding. The system must:

1. Regularly check trusted public sources for grants
2. Collect raw pages/feeds/documents
3. Extract key grant details into a structured schema
4. Normalise values into consistent categories (themes, regions, org types, currency, dates)
5. Merge duplicates (same grant listed on multiple sites)
6. Store everything in a database, with every record linked to its original source(s)
7. Detect changes over time (new grants, deadline changes, closures) and keep history
8. Flag uncertain or conflicting records for human review

**Out of scope for now** (but design so they're easy later): organisation-to-grant matching, funder analytics, public-facing product UI, user accounts.

---

## 2. Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Package mgmt | `uv` with `pyproject.toml` |
| Database | PostgreSQL 16 |
| ORM / migrations | SQLAlchemy 2.x + Alembic |
| Validation | Pydantic v2 |
| HTTP | `httpx` (async), with retries via `tenacity` |
| JS-rendered pages | Playwright (only for sources that need it) |
| HTML parsing | `selectolax` or BeautifulSoup; `trafilatura` for main-content extraction |
| RSS | `feedparser` |
| PDF | `pdfplumber` |
| LLM extraction | Anthropic API or Groq, chosen by env var `LLM_PROVIDER` (model name from `EXTRACTION_MODEL`), using structured output. Groq added at the project owner's request (2026-09-21). |
| Fuzzy matching | `rapidfuzz` |
| Scheduling | APScheduler in a dedicated worker process |
| API | FastAPI |
| Admin UI | React + Vite + TypeScript (minimal, internal only) |
| Local dev | Docker Compose (Postgres + app + worker) |
| Tests / lint | pytest, ruff, mypy |

Config via environment variables (`.env`, loaded with `pydantic-settings`). Provide `.env.example`.

---

## 3. Repository layout

```
grant-pipeline/
  pyproject.toml
  docker-compose.yml
  .env.example
  alembic/
  src/fundscout/
    config.py
    cli.py                 # Typer CLI entry point
    db/                    # models.py, session.py
    taxonomy/
      data/                # themes.yaml, org_types.yaml, regions.yaml, grant_types.yaml
      loader.py            # seeds taxonomy tables from YAML
    sources/
      base.py              # SourceAdapter interface
      registry.py          # maps adapter names -> classes
      adapters/            # one module per source or source type
    fetch/
      http.py              # client, retries, conditional requests
      robots.py            # robots.txt checks
      ratelimit.py         # per-domain rate limiting
      storage.py           # raw snapshot storage (local FS now, S3-ready interface)
    extract/
      schema.py            # Pydantic extraction schema
      llm.py               # LLM extraction
      rules.py             # optional CSS/XPath rule-based extraction per source
      pdf.py
    normalise/             # currency, dates, amounts, countries, taxonomy mapping
    dedup/
    changes/               # diffing, versioning, status updates
    review/                # review queue logic
    pipeline/
      runner.py            # orchestrates one source run end to end
      scheduler.py         # worker process
    api/
      main.py
      routers/
  admin-ui/
  tests/
    fixtures/              # saved HTML/RSS/PDF/API responses per adapter
```

---

## 4. Pipeline stages

```
discover -> fetch -> store raw -> (unchanged? stop) -> extract -> normalise
         -> dedup/match -> upsert + record changes -> compute status -> flag for review
```

1. **Discover** — adapter returns candidate items (detail URLs, feed entries, API records).
2. **Fetch** — polite HTTP fetch (see §9). Use ETag / If-Modified-Since where supported.
3. **Store raw** — save the raw response with a SHA-256 content hash. Always keep raw snapshots so we can re-extract later without re-fetching.
4. **Skip if unchanged** — if the hash matches the last snapshot for that URL, update `last_checked_at` and stop. This keeps LLM costs down.
5. **Extract** — rule-based if the source has rules configured; otherwise LLM extraction into the schema in §6. Structured API sources map fields directly.
6. **Normalise** — map to taxonomy and standard formats (§7).
7. **Dedup / match** — find whether this is an existing grant (§8).
8. **Upsert + record changes** — write/update the grant, record field-level diffs in history.
9. **Status** — recompute status (§6.3).
10. **Review flags** — create review items where needed (§8.3).

Each stage must be independently testable. A failure on one item must not kill the whole run; log it and continue.

---

## 5. Data model

Use UUID primary keys and `created_at` / `updated_at` on every table.

**sources** — `name`, `adapter` (registry key), `base_url`, `source_type` (`html`, `rss`, `api`, `pdf`), `config` (JSONB: selectors, endpoints, pagination, etc.), `schedule` (cron string), `enabled`, `terms_reviewed` (bool), `notes`, `last_run_at`, `last_success_at`.

**source_runs** — `source_id`, `started_at`, `finished_at`, `status` (`running`, `success`, `partial`, `failed`), counts (`items_discovered`, `items_fetched`, `items_unchanged`, `grants_created`, `grants_updated`, `errors`), `error_log` (JSONB).

**raw_documents** — `source_id`, `url`, `fetched_at`, `http_status`, `content_type`, `content_hash`, `storage_path`, `etag`, `last_modified`.

**funders** — `name`, `normalised_name`, `website`, `country`, `funder_type` (foundation, government, multilateral, corporate, other).

**grants** — canonical record:
- `funder_id`, `title`, `normalised_title`, `description`
- `amount_min`, `amount_max` (NUMERIC), `currency` (ISO 4217), `amount_text` (raw)
- `opening_date`, `closing_date`, `deadline_type` (`fixed`, `rolling`, `multiple`, `unknown`), `deadline_text`
- `eligibility_text`, `application_url`, `funder_page_url`
- `status` (`upcoming`, `open`, `closed`, `rolling`, `unknown`)
- Phase 2 fields (nullable now): `requirements`, `documents_required` (JSONB list), `funder_priorities`, `contact_email`, `contact_url`
- `first_seen_at`, `last_seen_at`, `last_checked_at`, `needs_review` (bool), `fingerprint`

**grant_sources** — many-to-many between grants and where they were found: `grant_id`, `source_id`, `url`, `raw_document_id`, `is_primary` (official funder page = primary), `first_seen_at`, `last_seen_at`, `missing_count` (consecutive runs where it wasn't found).

**Taxonomy tables** — `themes`, `regions`, `org_types`, `grant_types`, each with `slug`, `label`, `parent_id` (optional hierarchy). Seeded from YAML.

**Join tables** — `grant_themes`, `grant_regions`, `grant_countries` (ISO 3166-1 alpha-2 code), `grant_org_types`, `grant_grant_types`.

**grant_changes** — `grant_id`, `source_run_id`, `field`, `old_value`, `new_value` (JSONB), `changed_at`.

**extraction_results** — `raw_document_id`, `method` (`llm`, `rules`, `api`), `model`, `output` (JSONB), `field_confidence` (JSONB), `created_at`. Keeps an audit trail of what was extracted and how.

**review_items** — `grant_id` (nullable), `raw_document_id` (nullable), `reason` (enum, §8.3), `details` (JSONB), `status` (`open`, `resolved`, `dismissed`), `resolved_by`, `resolved_at`, `resolution_note`.

---

## 6. Extraction

### 6.1 Schema (Pydantic)

```python
class ExtractedGrant(BaseModel):
    is_grant_opportunity: bool          # False for news posts, closed-program archives, etc.
    title: str | None
    funder_name: str | None
    funder_website: str | None
    description: str | None             # concise summary, max ~1000 chars
    amount_min: Decimal | None
    amount_max: Decimal | None
    currency: str | None                # ISO 4217
    amount_text: str | None             # exactly as written on the page
    opening_date: date | None
    closing_date: date | None
    deadline_type: Literal["fixed", "rolling", "multiple", "unknown"]
    deadline_text: str | None
    countries: list[str]                # ISO alpha-2
    regions: list[str]                  # taxonomy slugs
    org_types: list[str]                # taxonomy slugs
    themes: list[str]                   # taxonomy slugs
    grant_types: list[str]              # taxonomy slugs
    eligibility_text: str | None
    application_url: str | None
    # Phase 2 (extract if easily available, otherwise null)
    requirements: str | None
    documents_required: list[str]
    funder_priorities: str | None
    contact_email: str | None
    contact_url: str | None
    # Quality
    field_confidence: dict[str, float]  # 0-1 per populated field
    evidence: dict[str, str]            # short supporting snippet per key field
```

### 6.2 LLM extraction rules

- Send cleaned main content (not raw HTML) plus the page URL and today's date.
- Pass the allowed taxonomy slugs in the prompt; the model must only use those. Validate afterwards and drop anything not in the taxonomy.
- Instruct the model: never guess; use null when not stated. Dates must come from the text, not be inferred.
- Validate with Pydantic. On validation failure, retry once with the error message, then create a `extraction_failed` review item.
- Truncate very long pages sensibly (keep headings and sections mentioning deadlines, eligibility, amounts).
- Log token usage per run.

### 6.3 Status rules

- `closing_date` in the past → `closed`
- `opening_date` in the future → `upcoming`
- `deadline_type == rolling` → `rolling`
- otherwise, with a future `closing_date` → `open`
- no usable dates → `unknown`
- If a grant is missing from its primary source listing for 3 consecutive runs (`missing_count >= 3`), flag `possibly_removed` for review. Do not auto-close on disappearance alone.

---

## 7. Normalisation

- **Currency**: map symbols/words ("$", "USD", "US dollars", "€", "GH₵", "₦") to ISO 4217. `$` alone is ambiguous: default to USD only if the funder is US-based or the page context supports it, otherwise flag.
- **Amounts**: parse "up to $50,000", "£10k–£25k", "USD 1.5 million" into min/max. Keep `amount_text` as written.
- **Dates**: to ISO dates. Handle formats like "31st March 2027", "03/31/2027" vs "31/03/2027" (use source locale config to disambiguate).
- **Countries**: to ISO 3166-1 alpha-2. Regions ("Sub-Saharan Africa", "West Africa", "Global South") map to region slugs; regions have country lists in `regions.yaml`.
- **Funder names**: `normalised_name` = lowercased, punctuation stripped, common suffixes removed ("Foundation", "Inc", "Trust" kept in display name only).
- **Taxonomy**: seed files are the source of truth and must be easy to edit. Starting sets:
  - *themes*: education, health, climate-environment, agriculture-food-security, water-sanitation, gender-equality, youth, children, human-rights-governance, democracy-civic-space, economic-development-livelihoods, entrepreneurship, technology-innovation, arts-culture, research, humanitarian-emergency, peace-security, disability-inclusion, energy, media-journalism
  - *org_types*: nonprofit-ngo, community-based-org, social-enterprise, sme-for-profit, startup, university-research-institution, government-public-body, faith-based-org, network-coalition, individual
  - *grant_types*: project, core-operational, capacity-building, research, fellowship, prize-award, emergency-response, other
  - *regions*: continents plus sub-regions (West, East, Central, Southern, North Africa; etc.) with ISO country lists

---

## 8. Deduplication, change tracking, review

### 8.1 Matching

1. Exact match on `(source_id, url)` in `grant_sources` → same grant.
2. Else compute `fingerprint` = hash of `normalised_funder_name + normalised_title + closing_date`. Exact fingerprint match → same grant.
3. Else fuzzy: same normalised funder AND title similarity ≥ 90 (rapidfuzz `token_set_ratio`) AND compatible dates → same grant.
4. Similarity between 75 and 90 → create a new grant but add a `possible_duplicate` review item linking both.

Thresholds must be config values, not hard-coded.

### 8.2 Merging across sources

- The official funder page (`is_primary`) wins for conflicting values.
- If two sources disagree on closing date or amount and neither is primary, keep the most recent and create a `conflicting_values` review item.

### 8.3 Review reasons

`low_confidence` (any key field < 0.6), `missing_required_fields` (no title, funder, or application/source URL), `conflicting_values`, `possible_duplicate`, `deadline_changed`, `possibly_removed`, `extraction_failed`, `ambiguous_currency`, `fetch_failed_repeatedly` (3+ consecutive failures for a source).

### 8.4 Change history

On every update, diff the tracked fields and write rows to `grant_changes`. Deadline changes always also create a `deadline_changed` review item.

---

## 9. Responsible collection (required)

- Respect `robots.txt`. If disallowed, skip and log.
- Per-domain rate limit (default 1 request every 3 seconds, configurable per source).
- Identify with a clear User-Agent including a contact email from config.
- Use conditional requests and avoid re-fetching unchanged content.
- Sources have a `terms_reviewed` flag. The scheduler must not run sources where it is false, though manual CLI runs can with a `--force` flag and a warning.
- Prefer official APIs and RSS feeds over scraping when available.
- Only collect publicly available information. No logins, no bypassing paywalls or anti-bot protections.

---

## 10. Adapters

```python
class SourceAdapter(ABC):
    name: str
    async def discover(self, source: Source, ctx: RunContext) -> list[DiscoveredItem]: ...
    async def fetch(self, item: DiscoveredItem, ctx: RunContext) -> RawDocument: ...
    def extract(self, raw: RawDocument, ctx: RunContext) -> ExtractedGrant | list[ExtractedGrant]: ...
```

Provide generic, config-driven adapters so most new sources need config only, no new code:

- `generic_html_listing` — listing page URL + CSS selector for detail links + pagination config; detail pages go through LLM extraction
- `generic_rss` — feed URL; each entry's link is fetched and extracted
- `generic_pdf` — PDF URLs or listing of PDFs
- `generic_api` — for JSON APIs with a field-mapping config

Write custom adapters only when a source can't be handled by config.

**Initial sources:** TO BE CONFIRMED by the project owner. Before Milestone 2, ask which sources to start with. Build and test against at least one API/RSS source and one HTML source. Candidates to evaluate (verify current API availability and terms first): Grants.gov public search API, the EU Funding & Tenders Portal API, and a handful of foundation websites relevant to the target region.

---

## 11. Interfaces

### CLI (Typer)

```
fundscout db upgrade
fundscout taxonomy seed
fundscout sources list
fundscout sources add --file source.yaml
fundscout run --source <name> [--force] [--limit N]
fundscout run-all
fundscout reprocess --source <name>     # re-extract from stored raw, no fetching
fundscout worker                         # start scheduler
```

### API (FastAPI, internal)

- `GET /grants` — filters: status, theme, region, country, org_type, closing_before, closing_after, q (text search), pagination
- `GET /grants/{id}` — includes sources, taxonomy, change history
- `GET /changes?since=`
- `GET /sources`, `GET /sources/{id}/runs`, `POST /sources/{id}/run`
- `GET /review?status=open&reason=`, `POST /review/{id}/resolve`, `POST /review/{id}/dismiss`
- `PATCH /grants/{id}` — manual corrections (recorded in `grant_changes` with source "manual")
- `GET /health`

Simple API-key auth via header for now.

### Admin UI (minimal)

Three screens: **Review queue** (list + detail with side-by-side extracted data and source link, resolve/edit/dismiss), **Grants** (searchable table, detail with history), **Sources** (status, last run, errors, manual run button).

---

## 12. Milestones

Stop after each one for review.

**M0 — Scaffolding**
Repo layout, `pyproject.toml`, Docker Compose (Postgres), config, ruff/mypy/pytest, `.env.example`, README with setup steps.
*Done when:* `docker compose up` starts Postgres; `pytest` runs; lint passes.

**M1 — Data model + taxonomy**
SQLAlchemy models, Alembic migration, taxonomy YAML files and seed command.
*Done when:* migrations apply cleanly; `fundscout taxonomy seed` populates tables; model tests pass.

**M2 — Collection framework**
Adapter interface, registry, HTTP client with retries/conditional requests, robots.txt, rate limiting, raw storage, source runs logging, generic RSS and HTML listing adapters. Two real sources configured (after confirming with owner).
*Done when:* `fundscout run --source X` fetches and stores raw documents; unchanged pages are skipped on second run; tests use saved fixtures, not live requests.

**M3 — Extraction + normalisation**
Extraction schema, LLM extractor, PDF text extraction, normalisers, validation.
*Done when:* raw documents become validated `ExtractedGrant` objects; normaliser unit tests cover the tricky formats in §7; an evaluation script runs extraction on a small hand-labelled fixture set (10–20 pages) and reports per-field accuracy.

**M4 — Storage, dedup, changes, review**
Upsert logic, matching, merging, change history, status computation, review item creation.
*Done when:* running the same source twice creates no duplicates; a modified fixture (changed deadline) produces a `grant_changes` row and a review item; a grant listed in two sources is merged.

**M5 — Scheduling**
Worker process with APScheduler reading per-source cron; skips sources where `terms_reviewed` is false; alerts via log on repeated failures.
*Done when:* worker runs sources on schedule in Docker Compose.

**M6 — API + admin UI**
FastAPI endpoints and the three admin screens.
*Done when:* review items can be resolved from the UI and grants can be searched and filtered.

**M7 — Scale sources**
Add more sources using config only where possible; document the "how to add a source" process in the README.

---

## 13. Quality bar

- Type hints everywhere; mypy passes.
- Tests never hit the live internet or the real LLM API; use fixtures and a mocked LLM client. Provide one opt-in integration test marked `@pytest.mark.live`.
- Structured logging (JSON) with `source`, `run_id`, `url` on every log line.
- No secrets in the repo.
- README covers: setup, running locally, adding a source, running the eval, and environment variables.