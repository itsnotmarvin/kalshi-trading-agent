# Single-market replay diagnostic

This is a compact record of the repository's existing replay artifact. It is
included to make the current evidence boundary visible, not to claim strategy
validation.

## Setup

| Field | Value |
| --- | --- |
| Market | `KXFEDCHAIRNOM-29-KW` — Kevin Warsh nomination for Fed Chair |
| Category | Politics |
| Price history | 2024-12-19 through 2026-03-03 |
| Resolution | YES |
| Replay observations | 440 daily replay bars derived from 310 native bars |
| Nominal stake | $100 |
| Cost policy | Taker fees plus a 2-cent round-trip spread assumption |

## Result

| Metric | Result |
| --- | --- |
| Model-timing net P&L | **-$3.42** |
| Model-timing ROI | **-3.42%** |
| Replay Brier score | **0.457574** |
| Last directional lean | NO |
| Resolved outcome | YES |
| Direction matched | No |

The model-timing policy entered on 2025-09-06 and exited on 2025-10-06 at its
take-profit target. Its gross price move was positive, but modeled fees and
spread costs produced a net loss.

## Why this is not proof

- This is one market, so it is an anecdote rather than an out-of-sample strategy
  evaluation.
- The daily replay bars share one final outcome and are not independent resolved
  forecasts; the Brier score is diagnostic only.
- Historical order-book depth was unavailable, so exitability was approximated.
- The replay probability was largely market-price-derived, making calibration
  partly a measurement of the market itself.
- The raw replay artifact and source price-history CSV are not committed, so
  this compact historical summary is not independently reproducible from the
  public repository alone.

A credible next result requires many resolved markets, time-ordered holdouts,
locked pre-resolution forecasts, baseline comparisons, and trading results net
of executable costs.
