# Early Marks V1 — Page & Workflow Contract

Status: binding for `web/early_marks.html`, the Early Marks endpoints in
`server.py`, and `scripts/manual_probes/test_early_marks.py` stage emission.
Design system: `static/desk.css` tokens per
`/Users/marbin/kalshi/.agents/skills/_shared/ui-design-system.md`.

## Honesty rules (V2 — category-blind discovery)

- An early mark is defined by **lifecycle structure, never by topic**: listed
  recently (little of the market's own life elapsed), few trades, a mark
  still near its opening price, a concrete catalyst before resolution, and a
  book that plausibly allows an exit after the repricing. Scans default to
  **all categories**; the ranking must not gate or boost on category.
  (V1's politics-only allowlist and 30-day runway floor encoded the surface
  features of one example market and structurally excluded fast-cadence
  categories — that was the politics-skew bug, not a feature.)
- Category remains a *display filter* and a *research lens*: domain-specific
  question lists and cadence floors (e.g. tournament schedules) are allowed
  at the explanation layer, but never as eligibility gates.
- Category quality shown from `data/runtime/category_stats.json` honestly, as a
  per-category track-record summary with sample sizes; when there is no
  track record, say so and state that ranking is structural.
- Concentration warning: if ≥60% of current-run candidates share one ticker
  series prefix (text before first `-`), show a warning chip on the results
  header: "Concentrated: mostly one market family".

## UI state machine (single source of truth)

Client keeps one `uiState` ∈ `idle | checking | generating | complete |
partial | error | offline`. The current-results area renders ONLY from the
latest run's response. The saved payload on disk is never shown as current.

| State | Trigger | Visible |
|---|---|---|
| idle | page load, no run this session | controls, health chips, category panel, empty results panel: "No scan run yet." |
| checking | health checks in flight | chips show "checking…", Generate disabled |
| generating | Generate pressed | segmented circular loader + staged step list; previous current-run cards moved into Run History, results area cleared |
| complete | latest run returned structured results | cards + summary; timestamp of THIS run |
| partial | run finished but some services degraded/failed and results are limited | cards + amber banner naming what was limited |
| error | run failed / untrustworthy output | red panel with human message, no cards |
| offline | core services (Kalshi API or local server) unavailable | grey panel, Generate disabled |

Rules: no state ever reuses a previous run's results as current. Every state
has a human-readable message. "Complete" appears only after the just-launched
run returns.

## Page load (before first Generate)

Fetch ONLY: `GET /api/early-marks/health` and `GET /api/early-marks/runs`
(to seed collapsed Run History). Do NOT fetch `/api/early-marks` on load; do
not render ranked counts, selected-mark details, model/catalyst text, or old
cards. Empty panels with "No scan run yet" are correct.

## Service health

`GET /api/early-marks/health` (new) returns simple states only — no key
material, no error internals:

```json
{"services": [
  {"id": "kalshi",   "label": "Kalshi API",        "state": "online|offline|error"},
  {"id": "llm",      "label": "Claude API",         "state": "configured|not_configured"},
  {"id": "openai",   "label": "OpenAI API",         "state": "configured|not_configured", "optional": true},
  {"id": "research", "label": "Research API",       "state": "configured|not_configured"},
  {"id": "server",   "label": "Local server",       "state": "online"},
  {"id": "cache",    "label": "Data cache",         "state": "fresh|stale|empty"}
]}
```

- kalshi: one lightweight authenticated or public read with short timeout.
- llm/openai/research: configuration presence only (connected/error level,
  never key details). openai chip hidden entirely when not configured.
- cache: existence + age of `data/runtime/early_marks_probe.json` / runs file.
- Client renders chips (ONLINE / OFFLINE / API OK / API ERROR / NOT SET /
  STALE) on load, before Generate.
- Kalshi offline → `offline` state, Generate disabled. Research or LLM not
  configured → Generate enabled but labeled "Generate (limited)" and the run
  lands in `partial`, with the banner naming the missing service. Never
  silently run degraded as if healthy.

## Generate flow & staged progress

- Probe script (`test_early_marks.py`) additionally writes
  `data/runtime/early_marks_status.json` at stage boundaries:
  `{"run_id", "stage", "stages_done": [...], "updated_at"}`. Stages, in order:
  `connect` (Connecting to Kalshi) → `health` (Checking service health) →
  `pull_markets` (Pulling markets) → `category_model` (Checking category
  model) → `rank` (Ranking early marks) → `enrich` (Enriching with research)
  → `render` (Rendering cards). Keep the probe change additive — do not
  alter scoring logic; `test_early_marks.py` root tests must stay green.
- `GET /api/early-marks/run/status` (new): returns that file plus a
  server-side `running` flag toggled around the subprocess.
- Client: POST run as today; while awaiting, poll status every ~2s and render
  a **segmented circular loader** (CSS conic-gradient segments, rotating) with
  the step list, each step ticking to done. If status file lags, degrade
  gracefully (show current stage as active, never raw text).
- On response: render structured cards from the response payload only.
  `claude_raw_text`, probe stdout, JSON dumps, `claude_parse_error` never
  render in normal UI (server logs keep them). No developer output unless
  `?dev=1` query param — and even then, in a clearly separate collapsed
  "Developer" drawer.

## Candidate cards

Human-facing, scannable, desk.css tokens. Each card:

- Image header: `/api/early-marks/image?...` (existing endpoint, already
  falls back to SVG initials). Fixed aspect box (no layout shift), `onerror`
  hides img and shows the category fallback block. Cache image URLs in run
  records so cards don't flicker between runs. No broken-image icons, ever.
- Fields: human-readable market name (`display_title`/`display_name`) ·
  watched side/outcome · current price · price-distribution context · future
  catalyst path (short) · estimated runway · liquidity/spread note (with
  `.exit-warn` when thin/wide) · status chip: `Watch only / Catalyst setup /
  Too early / Reject` (map internal: Pre-open watch→Too early, Too
  directionless→Reject, Market knows something→Reject) · one-line "why
  monitor" reason · dedupe annotation: new / previously seen / updated /
  suppressed duplicate.
- **No raw tickers or internal IDs anywhere in visible text** (e.g.,
  KXPRESPERSON-28-JOSS). Tickers live only in `data-*` attributes and URLs
  (`href` to kalshi.com is fine). Audit every render path including research
  panel, watchlist, summaries, tooltips, and history.
- Copy: short labeled fields; long catalyst reasoning goes into an
  expandable detail area, never a giant paragraph. Uncertainty stated
  honestly: under-sourced, thin liquidity, watch only, category model
  limited.

## Run History

- At the very bottom of the page, collapsed by default, compact control
  ("Run History ⌃" pattern).
- Expanding shows summarized prior runs, newest first, compressed
  human-readable summaries: "On {date}, suggested watching {market} for
  {side/outcome} because {reason}." — one line per candidate, no raw fields.
- Pressing Generate again collapses the previous generation into Run History
  (it must leave the current-results area).
- Dedupe across generations by ticker+side (already implemented server-side
  in `/api/early-marks/runs` annotations): duplicates are annotated on the
  existing candidate, not shown as duplicate main cards.

## Visual direction

Premium AI-trading-assistant feel (inspiration: dense status-rich crypto
assistant dashboards), built strictly from desk.css tokens — dark neutral
surfaces, one accent, semantic colors only for meaning. Add to desk.css:
`.loader-ring` (segmented conic-gradient circular loader), `.stage-list`
(steps with pending/active/done ticks), `.health-chip` variants, `.mark-card`
(image header + labeled field grid), `.banner-warn/.banner-error`. No glow,
no gradients-as-decoration, no debug-looking blocks. Usability over
decoration: prices, catalysts, status, health always readable. Mobile:
single column, critical values preserved.

## Data & privacy contract

Internal identifiers in API calls/JSON/data-attributes only. User-facing
text uses human names. Debug output only in server logs or `?dev=1` drawer.
API key status limited to configured/error level. No live-trading behavior
changes anywhere.
