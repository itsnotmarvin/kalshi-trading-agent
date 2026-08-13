# Weather Backtest Results

Generated: `2026-08-13T06:13:58.603705Z`

Series/cities: Austin Bergstrom, Chicago Midway, IL, Denver International Airport, Los Angeles Airport, CA, Miami International Airport, Central Park, New York, Philadelphia International Airport.

## Methodology

Settled KXHIGH markets were paginated by series. Target dates came from each ticker, and station-local GFS `gfs_seamless` fixed-lead hourly temperatures were reduced to daily maxima. A quote was usable only when its hourly candle ended at or before the forecast bundle cutoff, had both closes, and was no more than 120 minutes old. Market outcomes were attached only after forecast/price matching.

Deterministic forecasts were converted to probabilities with a zero-bias Gaussian error model. Sigma was fitted separately per horizon by binary likelihood on expanding training folds created with `core.forecast_scoring.make_walk_forward_splits`; each row was scored only out of sample. Brier skill is midpoint Brier minus model Brier. Confidence intervals use 1000 cluster resamples by `(station_id, target_date)`. Paper P&L buys one contract at the executable YES ask or implied NO ask only when absolute edge exceeds `0.12`, and subtracts taker fees.

## Coverage

| Horizon | Candidates | Scoreable joins | Walk-forward scored | Warm-up | Dropped | Drop reasons |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 2814 | 2365 | 1584 | 781 | 449 | candle_request_error: 446; candle_stale: 3 |
| 2 | 2814 | 0 | 0 | 0 | 2814 | candle_after_cutoff_only: 2368; candle_request_error: 446 |
| 3 | 2814 | 0 | 0 | 0 | 2814 | candle_after_cutoff_only: 2368; candle_request_error: 446 |
| 5 | 2814 | 0 | 0 | 0 | 2814 | candle_after_cutoff_only: 2368; candle_request_error: 446 |
| 7 | 2814 | 0 | 0 | 0 | 2814 | candle_after_cutoff_only: 2368; candle_request_error: 446 |

## Scoring results

| Horizon | N | Indicator Brier | Gaussian Brier | Midpoint Brier | Skill (95% CI) | Gaussian log loss | Net P&L | Trades |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1584 | 0.2247 | 0.1492 | 0.0986 | -0.0506 (-0.0588, -0.0428) | 0.4954 | $-18.47 | 715 |
| pooled | 1584 | 0.2247 | 0.1492 | 0.0986 | -0.0506 (-0.0586, -0.0426) | 0.4954 | $-18.47 | 715 |

### Calibration buckets

#### 1

| Bucket | N | Mean prediction | Observed YES rate |
|---|---:|---:|---:|
| 0.0-0.1 | 823 | 0.0439 | 0.1057 |
| 0.1-0.2 | 589 | 0.1327 | 0.2581 |
| 0.2-0.3 | 52 | 0.2472 | 0.0577 |
| 0.3-0.4 | 41 | 0.3463 | 0.0488 |
| 0.4-0.5 | 22 | 0.4371 | 0.0909 |
| 0.5-0.6 | 21 | 0.5395 | 0.0952 |
| 0.6-0.7 | 13 | 0.6465 | 0.1538 |
| 0.7-0.8 | 6 | 0.7650 | 0.3333 |
| 0.8-0.9 | 10 | 0.8423 | 0.5000 |
| 0.9-1.0 | 7 | 0.9492 | 0.5714 |

#### pooled

| Bucket | N | Mean prediction | Observed YES rate |
|---|---:|---:|---:|
| 0.0-0.1 | 823 | 0.0439 | 0.1057 |
| 0.1-0.2 | 589 | 0.1327 | 0.2581 |
| 0.2-0.3 | 52 | 0.2472 | 0.0577 |
| 0.3-0.4 | 41 | 0.3463 | 0.0488 |
| 0.4-0.5 | 22 | 0.4371 | 0.0909 |
| 0.5-0.6 | 21 | 0.5395 | 0.0952 |
| 0.6-0.7 | 13 | 0.6465 | 0.1538 |
| 0.7-0.8 | 6 | 0.7650 | 0.3333 |
| 0.8-0.9 | 10 | 0.8423 | 0.5000 |
| 0.9-1.0 | 7 | 0.9492 | 0.5714 |

## Interpretation

At the only testable horizon (~24h), the market midpoint beats this model
decisively: Brier skill −0.0506 with a 95% CI entirely below zero, and the
0.12-edge paper policy lost $18.47 over 715 trades. The calibration table
shows why — mid-to-high model probabilities are grossly overconfident
(e.g., bucket 0.5–0.6 resolved YES 9.5% of the time).

Two scope limits on that conclusion:

1. This evaluates a fixed-lead **deterministic** GFS forecast under a
   Gaussian error model — a proxy, because member-level ensemble history
   does not exist beyond ~5 days. The production ensemble route is not
   directly tested here; judging it requires the forward archive
   (`forecast-archive` branch) to accumulate. The proxy result sets a
   strong prior against it, not a verdict on it.
2. Horizons ≥48h produced zero scoreable rows because markets have no
   quotes that early (`candle_after_cutoff_only`). That is a fact about
   market listing times, not missing data: the only edge that could exist
   in these markets lives inside the final ~24 hours.

## Caveats

- Previous Runs is a rolling fixed-lead product, not one internally consistent model run; the 24 hourly components have different implied issue times.
- Hourly model maxima can miss a brief intrahour peak measured by the continuous settlement sensor.
- Open-Meteo does not expose exact public-release/ingestion timestamps for each fixed-lead component. The implied issue timestamp is therefore an availability assumption, without an added publication buffer.
- Missing, stale, one-sided, crossed, or post-cutoff candles remain missing. No later quote is backfilled.
- Multiple strikes on a station-day share a forecast and weather driver; the bootstrap clusters them together.
