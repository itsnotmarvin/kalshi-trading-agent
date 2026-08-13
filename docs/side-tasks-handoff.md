# Early Marks side-task handoff (2026-08-12)

Working brief for a follow-on agent conversation (T3 / Codex / Claude). The
main track — snapshot collection — is DONE and running; do not rework it.
This file lists the deferred side tasks, their constraints, and the decisions
already made. Read `docs/early-marks-v2-detection-spec.md` first; it is
binding.

## Context you must not re-litigate

- The snapshot collector was fixed on 2026-08-12: it walks cursor-paginated
  `/events` (full universe ~143K markets, ~29s/run), skips MVE parlay combos
  (counted in provenance), stores NULL for missing flow fields, persists
  `series_ticker`, refuses to record empty runs, and guards `last_seen`
  monotonicity. A launchd job (`com.marbin.kalshi-early-marks`) collects
  every 30 minutes while the Mac is awake. Do NOT modify, reload, or
  duplicate that job.
- Graph ML is SHELVED behind phase gates (deterministic peer-residual
  baseline → tabular learners → maybe a small GNN). Nothing model-shaped
  gets built until weeks of clean data exist and the funnel shows candidate
  quality is the bottleneck. Do not start it.
- Stage 1 is recall-first and must never output: direction, fair
  probability/value, expected future price, ROI, entry/exit, sizing, or any
  trade-recommendation status. Missing data is `unknown`, never evidence.
- Work in the real `kalshi-agent` checkout directly (NOT an isolated
  worktree copy — the live SQLite store and launchd job point at it). Run
  `.venv/bin/python -m pytest` before and after changes. Committing to git
  stays with David — leave changes uncommitted.

## Task 1 — Store book-depth fields (small code task, time-sensitive)

Kalshi's `liquidity_dollars` is a dead field (always 0). Real depth lives in
`yes_bid_size_fp` / `yes_ask_size_fp`, which collection currently discards —
and unstored history is unrecoverable, so every day this waits is a
permanent data hole.

- Add `yes_bid_size` / `yes_ask_size` columns to `observations` in
  `core/early_marks_snapshots.py` (schema + ALTER TABLE migration in
  `open_db`, mirroring `series_ticker`), parse via `_first_float`.
- Design decision to weigh (recommendation: start context-only): sizes are
  NOT in `_MATERIAL_FIELDS` at first — depth flickers constantly and would
  erode the dedup schema. Store them as context on rows that are written
  anyway. Flag in the code comment that a later depth-evidence function may
  need bucket-transition materiality (see the `_attention_bucket` precedent
  for `volume_24h`).
- Tests: missing sizes stay NULL; a genuine "0.00" stores as 0.0; roundtrip.

## Task 2 — Evaluation spec (discussion → doc, highest leverage)

Draft `docs/early-marks-evaluation-spec.md` BEFORE the data exists, so the
measurement rules are fixed while nobody can peek. Must define:

- Label: what counts as "a flagged market later repriced" — movement
  measured in log-odds (not flat cents; see the 3¢ critique), spread-aware,
  within what forward horizon, with a pre-close/settlement embargo.
- Metric: precision@K at a fixed Stage 2A research budget, plus recall of
  later-repricers; K matches the real research cadence (a handful every few
  days).
- Splits: forward-time only, whole events kept together, purge/embargo at
  boundaries; recurring-series performance reported separately from
  unseen-event generalization.
- Funnel instrumentation: the four numbers that locate the bottleneck —
  candidates surfaced → researched → "verified influence, not priced" →
  actually repriced later.

## Task 3 — Stage 2A verdict persistence (design, then small code)

Research verdicts currently have no durable, queryable home, which means the
funnel in Task 2 can never be measured. Design where adjudications land
(likely a small table or JSONL under `data/runtime/early_marks/`): ticker,
run reference, verdict (`verified_influence | no_relevant_influence |
already_priced | insufficient_data`), sources, timestamp, model used.
Stage 2A must be allowed to return "no relevant influence" — the schema
should make that the easiest thing to record, not the hardest.

## Task 4 — Stage 2A dry run (after ~1 day of data)

Pull a rough shortlist with the existing evidence functions (markets with
`observed_flat` + `low` attention) and run one manual research pass to find
workflow friction: where verdicts get written (Task 3), whether the format
survives contact, whether "no relevant influence" actually gets used.
Read-only market data; no orders, no paper positions.

## Dry-run findings (Task 4, run 2026-08-12)

All four side tasks are complete. Task 4 ran as a WORKFLOW rehearsal on 3
proxy markets (store had ~1.6h of history; a genuine `observed_flat` short-
list needs 6h+, and the evidence rules correctly returned 0 candidates).
Verdicts recorded under `dryrun-20260812-proxy` (excluded from evaluation):
COSTCOHOTDOG-27 → already_priced; CONTROLH-2028-D → insufficient_data;
BEYONCEGENRE-30-TRA → no_relevant_influence. Retrieval by gpt-5.6-sol via
the Codex bridge; adjudication by Fable; recorded via
`scripts/record_verdict.py` (the real paste path). Friction found:

1. **Ticker names mislead researchers.** The BEYONCEGENRE "TRA" outcome
   was guessed as "traditional"; it actually means Billboard **Top Rock
   Albums** chart placement. The store does not persist market titles, so
   the shortlist cannot supply them and the research template's CANDIDATE
   block starts from a guess. Consider persisting `title` in `markets` on a
   later pass (schema addition, mirroring `series_ticker`) — until then,
   Stage 2A must verify the real title/settlement rule first, which the
   template now mandates as research question 1.
2. **Kalshi rules PDFs are sometimes unfetchable** (S3-hosted contract
   terms). The market page is the reliable fallback; settlement rules
   corroborated through mirrors should be flagged in `notes`.
3. **The strict verdict paths work but demand real effort**:
   `already_priced` requiring a full influence record + sources is the
   right bar (it forced the Costco adjudication to name the actor, timing,
   and channel). `no_relevant_influence` at minimal payload was genuinely
   the easiest to record, as designed. `insufficient_data` requiring a
   notes reason produced the most useful text of the three.
4. **Format survived contact**: all three JSON blocks validated first try;
   the CLI's rejection messages (tested separately) name the exact missing
   field, which is what a T3 paste loop needs.

## Explicitly out of scope

Graph/ML code of any kind; changing evidence thresholds (the 3¢→log-odds
change is deliberately deferred to Task 2's spec); touching the launchd job;
live trading anything; committing to git.
