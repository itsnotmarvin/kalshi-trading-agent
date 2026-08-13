# Early Marks V2 — Evaluation Spec (pre-registered)

Status: binding for all Early Marks measurement. Drafted 2026-08-12, BEFORE
meaningful snapshot history existed, so the grading rules are fixed while
nobody can peek. Companion to `early-marks-v2-detection-spec.md` (the
candidate-generation contract) and `side-tasks-handoff.md` (Task 2).

Freeze rule: this spec is frozen the moment the first eval report is
generated from real data. After that, any change requires a versioned
amendment at the bottom of this file stating what data had already been seen
when the change was made. Primary thresholds may never be changed
retroactively for a period already reported.

Scope guard: this spec measures DETECTION quality only — did Stage 1 surface
markets that later repriced, and did Stage 2A adjudicate them correctly. It
contains no P&L, ROI, sizing, or tradability metrics; those belong to Stage
2B and are explicitly out of scope here, matching the Stage 1 forbidden-
outputs contract.

## 1. Label — what counts as "repriced"

Labels are tri-state, like everything in this pipeline:
`repriced | not_repriced | unlabelable`. Missing data yields `unlabelable`,
never `not_repriced`.

All prices are the store's observed price: mid of a two-sided book, else
last trade (`_observed_price` in `core/early_marks_snapshots.py`). Eval and
detection must share these definitions by construction; the eval code
imports them, never re-implements them.

For a market flagged at run time `t0`:

- **Reference price** `p0` = observed price at `t0`. No priced observation
  at `t0` → `unlabelable`.
- **Label window** = `(t0, min(t0 + H, close_time − E, settle_time − E)]`
  where `settle_time` is the first observation whose status indicates
  determination/settlement. Empty window → `unlabelable`.
- **Horizon** `H = 14 days` (primary).
- **Settlement embargo** `E = 48 hours`. Drift into 0/1 near close is
  mechanical settlement, not early repricing; it must never count.
- **Movement is measured in log-odds**, not cents:
  `ΔL = |ln(p/(1−p)) − ln(p0/(1−p0))|`. Flat-cent thresholds are wrong at
  the extremes: 3¢ at 50¢ is noise, 3¢ from 4¢ nearly doubles the odds
  (the 3¢ critique this spec supersedes).
- **Primary threshold**: `ΔL ≥ 0.5` (odds ratio ≥ ~1.65×; e.g. 5%→8%,
  20%→29%, 50%→62%).
- **Spread-aware crossing rule**: the move must clear the book, not bounce
  inside it. Upward repricing requires some observation `t` in the window
  with `bid_t ≥ ask_t0` (you could have bought the flag-time ask and sold
  the later bid); downward mirrors with `ask_t ≤ bid_t0`. If either
  endpoint lacks a two-sided book, the pair cannot certify a crossing;
  a market with no certifying pair and no ΔL breach observed through a
  covered window is `not_repriced` only if coverage held (below),
  otherwise `unlabelable`.

`repriced` = ΔL threshold met AND crossing rule met, at any observation in
the label window. First qualifying observation timestamps the repricing;
one market flags at most one repricing episode per label window.

`not_repriced` requires coverage: the market was observed (stored row or
`last_seen` confirmation) through at least 80% of the label window and
neither condition was met. Anything less is `unlabelable`.

**Mandatory coverage line**: every report states the fraction of the
universe that was labelable. The crossing rule biases labels toward
readable books; that bias must be visible, not silent.

**Sensitivity rows (report-only, never primary)**: each report also shows
the headline numbers at `ΔL ≥ 0.35` and `ΔL ≥ 0.7`, and at `H = 7d` and
`H = 30d`, to demonstrate the result is not threshold-fragile. These rows
can never be promoted to primary within a reported period.

## 2. Metrics

### 2a. Premise check (whole-universe, no research required — first-class)

The core premise: markets that later reprice pass through the candidate
state (`observed_flat` + `low` attention) beforehand. This is computable
from snapshots alone, for the full universe, weeks before any research
budget is spent — it is the fastest honest kill-or-continue signal.

- Evaluated on one designated run per day (the run nearest 12:00 UTC) to
  avoid pseudo-replicating 48 autocorrelated scans per day.
- For each daily run: bucket every labelable market by its evidence state
  at that run (`candidate = observed_flat+low`, `flat_watched =
  observed_flat+normal/high`, `moved`, `unknown`), then compute the
  forward reprice rate per bucket.
- **Headline: lift** = candidate-bucket reprice rate ÷ matched-control
  reprice rate (matching below). Report both conditionals: P(reprice |
  candidate state) and P(candidate state before | repriced).

### 2b. Matched controls

Raw precision at small K is uninterpretable without a base rate. For each
flagged candidate at `t0`, draw controls at the same `t0` matched on:

- log-odds band of the observed price: `|L(p0)|` in bands
  `[0,0.85) [0.85,2.2) [2.2,∞)` (≈ 30–70%, 10–30%/70–90%, tails);
- attention bucket (`_attention_bucket`), status, and labelability.

Two control cohorts, both mandatory:

1. **Matched-random**: any matched market regardless of evidence state.
2. **Flat-but-watched**: matched markets in `observed_flat` +
   `normal/high` attention — the cohort that isolates whether LOW
   ATTENTION specifically (not mere flatness) carries the signal, which is
   the part of the thesis V1 never tested.

### 2c. Precision at the research budget

- **Budget**: `K = 10 researched candidates per calendar week` (matches
  the realistic 5–15/week T3 throughput). If fewer are actually
  researched, precision is reported over the actual count with the
  shortfall stated — never silently rescaled.
- **Surface precision@K**: fraction of the week's surfaced shortlist that
  later labels `repriced`. Computable for every shortlist with no
  research.
- **Verified precision** (the money number): fraction of
  `verified_influence` verdicts whose market later labels `repriced`.
- Both are always reported next to their matched-control base rates as
  lifts, never as bare percentages.

### 2d. Recall of later-repricers

Of all universe markets labeling `repriced` in a period: what fraction
were surfaced by Stage 1 (appeared on any shortlist), and what fraction
merely met the candidate condition at any designated run in the 14 days
before their repricing timestamp. The gap between those two numbers
separates "the definition misses repricers" from "the shortlist cutoff
misses them." Stage 1 is recall-first by contract; this is its grade.

### 2e. Funnel — the four numbers

Per period, the counts that locate the bottleneck:

1. candidates surfaced
2. researched (any verdict recorded)
3. `verified_influence` and not already priced
4. actually repriced within the label window

plus the three conversion rates between them. Every verdict row carries
`model`, retrieval provenance (evidence-pack source list once Exa packs
exist; `model_self_search` before that), and the Stage 2A template
version. Verdict taxonomy is Task 3's:
`verified_influence | no_relevant_influence | already_priced |
insufficient_data`.

Labels are tracked for ALL verdict classes, not just positives:

- `no_relevant_influence` markets that then reprice are research false
  negatives — a per-model research-quality metric.
- `already_priced` markets that then reprice in the SAME direction as the
  supposed pricing were, empirically, not priced — an adjudication error
  worth counting.

### 2f. Model comparison (T3 phase)

Per model: verdict distribution, verified precision, and false-negative
rate (2e). Comparisons are only apples-to-apples once evidence packs give
every model identical retrieval; before that, model numbers are
directional and reports must say so.

## 3. Splits and leakage rules

These govern any tuning or learning, including threshold adjustments to
the deterministic baseline. The phase-gated learners (shelved) inherit
them unchanged.

- **Forward-time only.** Anything tuned on data through time T is
  evaluated strictly on data after T. No shuffled or random splits, ever.
- **Whole events stay together.** The assignment unit is `event_ticker`:
  markets in one event are mechanically correlated (often mutually
  exclusive), and splitting them across a boundary leaks the outcome.
- **Purge and embargo at boundaries.** Events whose label window crosses
  a split boundary are dropped from both sides, and a 14-day embargo
  (one full horizon) separates tuning data from evaluation data.
- **Recurring vs unseen, reported separately.** A market whose
  `series_ticker` appeared before the split is "recurring"; otherwise
  "unseen-event." Headline numbers are reported for both cohorts
  separately and never blended into a single average: recurring-series
  skill (weather, weeklies) does not certify generalization to novel
  events, and V1's category biases hid in exactly this blend.
- **Threshold freeze.** The current evidence thresholds (MIN_OBSERVATIONS,
  MIN_WINDOW_HOURS, MOVED_PRICE_DELTA, attention cutoffs) are treated as
  tuned-on-nothing priors. Any future retuning must name its tuning
  window and be evaluated only forward of it.

## 4. Decision schedule and kill criteria (pre-committed)

Dates assume collection continuing from 2026-08-12 and are checkpoints,
not cliffs — but skipping one requires a written amendment, and reasons
must not be fitted to interim results.

- **Premise checkpoint — 2026-09-15** (~4 weeks of data, ~2 weeks of
  matured 14-day labels). Requires ≥200 labeled repricing episodes; if
  fewer, the checkpoint slides until reached and the report says so.
  KILL/REDESIGN trigger: candidate-state lift < 2× against matched-random
  controls AND no better against flat-but-watched. That outcome means
  structural flatness+inattention does not concentrate future repricers,
  and the Stage 1 definition needs redesign before another research hour
  is spent.
- **Funnel checkpoint — at 30 accumulated `verified_influence` verdicts
  or 2026-11-15, whichever comes first** (~30 positives distinguishes
  ~25% verified precision from a ~5% base rate at conventional power;
  at K=10/week with realistic verdict mixes this lands in 8–12 weeks).
  KILL/INVESTIGATE trigger: verified precision lift < 2× over
  matched-random controls — research verdicts are not adding signal over
  the raw shortlist, so either Stage 2A or the candidate feed is broken;
  find which before scaling anything.
- **Graph-ML gate (restates the handoff):** nothing model-shaped is
  considered until BOTH checkpoints pass AND the funnel shows candidate
  quality (stage 1→4 conversion) is the binding constraint rather than
  research throughput.

## 5. Reporting

- Premise check: weekly, automated from the store once labels mature.
- Funnel + precision/recall: monthly, and at each checkpoint.
- Every report carries: labelable-coverage fraction, actual vs budgeted
  K, sensitivity rows, both control cohorts, recurring/unseen split, and
  the spec version it was graded under.
- No report may present a bare precision without its matched-control
  lift on the same line.

## Amendments

(none — spec unfrozen until first real-data report)
