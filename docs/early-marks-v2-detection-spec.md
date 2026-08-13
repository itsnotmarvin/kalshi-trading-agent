# Early Marks V2 — Candidate Generation Contract

Status: binding for the V2 detection layer (`core/early_marks_snapshots.py`
and successors). Supersedes the discovery semantics of
`early-marks-v1-spec.md`; the V1 page/workflow contract remains in force for
the UI until the V2 pipeline replaces it.

## Definition

An **early mark** is a position taken in a market *before the thing that will
move it starts moving it*. It is NOT defined by listing age, horizon length,
or category — those are proxies that have each produced systematic bias
(politics-only output, 30-day cliffs, lifecycle scores punishing dormant
markets with approaching influences).

A market only *becomes* an early mark after research verifies an influence.
The pipeline therefore has three stages with opposite loss functions:

- **Stage 1 — Candidate generation** (this spec): broad, cheap, frequent,
  recall-first. Surfaces *structural anomalies*: markets whose observed
  history shows no response and low attention. Minimizes false negatives.
- **Stage 2A — Research adjudication**: verifies a price-relevant influence
  (announced OR predictable), its timing, causal channel, relevance to the
  settlement rule, and whether it is already priced. MUST be allowed to
  return "no relevant influence."
- **Stage 2B — Position planning**: fair value, side, entry, exit, costs,
  sizing — and explicit refusal when liquidity or confidence is inadequate.

## Evidence rules (Stage 1)

1. **Response is tri-state: `observed_flat | moved | unknown`.**
   Non-response is a claim about history and requires history: at least two
   reliable observations spanning a minimum window. One stale quote or an
   untraded book is `unknown`, never `flat`. Missing data must not score as
   evidence of a lazy mark (the V1/V2.0 missing-data-as-flat trap).
2. **Attention is tri-state: `low | normal | high | unknown`**, from observed
   flow (trades, volume, quote activity) — never from category, and stated
   honestly: public data cannot reveal watchers or participant identity.
3. **Silence is disambiguated by attention**: unresponsive + watched means
   "no underreaction evidence" (not "market knows something" — that label is
   reserved for unexplained MOVEMENT before a public explanation);
   unresponsive + unwatched is the candidate condition.
4. **Influence hints are priors, not gates.** Keyword/calendar hints may
   prioritize research budget via retrieval lanes but must never gate
   admission. Retrieval runs in lanes: (a) top structural anomalies,
   (b) top calendar/influence-hint candidates, (c) a small exploration
   sample outside both — so hint blindness cannot redefine the concept.
5. **Category never changes structural eligibility or rank.** Domain
   knowledge lives in Stage 2A as research lenses only.

## Forbidden outputs (Stage 1)

Candidate generation must NOT emit: direction (YES/NO), fair probability or
fair value, expected future price, ROI, entry limit, exit target, stake or
contract count, verified catalyst identity, or any trade-recommendation
status ("Catalyst setup", "actionable"). Those are Stage 2 outputs. A
contract test must enforce this list.

## Snapshot store (the prerequisite)

Non-response cannot be measured from a single scan. The snapshot store
persists point-in-time observations of the full market universe in
`data/runtime/early_marks/observations.db`:

- Each collection receives a run record plus one observation row per market.
- Observation rows contain identity, status, bid/ask/last, volume, 24h volume,
  liquidity, open interest when available, and `observed_at`.
- Run-level provenance records the pages fetched and batch diagnostics needed
  to audit coverage.
- Collection is category-blind and coverage-audited: the run records how many
  markets were fetched and any pagination truncation, because
  category-neutral scoring cannot fix category-biased input coverage.
- Timestamps are injected (`observed_at` parameter), never read from wall
  clock inside scoring functions, so evidence functions are deterministic
  and testable with synthetic histories.

## Known V1/V2.0 defects this contract supersedes

- Lifecycle-age "earliness" as a definition (age is at best a tiebreaker).
- Missing last price treated as maximal mark-laziness.
- `days_to_resolution` conflating time-to-open with time-to-resolution for
  pre-open markets (open and resolution timestamps must be separate fields).
- Domain profiles changing rank weights and scenario prices by category.
- "Market knows something" applied to silence instead of movement.
