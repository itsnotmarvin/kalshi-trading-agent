# Intraday KXHIGH convergence after a physical temperature lock

Generated: `2026-08-13T06:59:03.085342Z`

## Methodology

For each settled Kalshi KXHIGH market in the selected lookback window, this study keeps only `strike_type=greater`. The market ticker supplies the station-local climate date. IEM's ASOS archive supplies hourly/METAR `tmpf` observations for the exact settlement station. A physical YES lock occurs at the first observation where that climate day's running observed maximum is at least the floor strike plus 1.0°F.

The lock timestamp is aligned to the end of its containing 60-minute Kalshi candle. Hour zero is that candle; midpoint convergence is the first usable two-sided candle with `(YES bid + YES ask) / 2 >= 0.95`. Missing quotes are never forward-filled. Snapshot P&L buys one YES contract at the candle's executable ask and subtracts the one-contract taker fee from `core.market_pricing.kalshi_trading_fee`; settlement pays $1.

## Coverage and drop accounting

Study window: **2026-06-11 through 2026-08-11**, using the latest settled target date returned by Kalshi as the endpoint. Excluded non-greater markets by strike type: `{'between': 1736, 'less': 434}`.

Candidate greater-strike markets in window: **434**. Fully analyzed physical-lock events: **9**.

| Outcome / primary drop reason | Markets |
|---|---:|
| `analyzed` | 9 |
| `candle_request_error` | 3 |
| `no_physical_yes_lock` | 422 |

### Events by settlement station

| Station | Greater-strike candidates | Analyzed locks | Converged | No physical lock |
|---|---:|---:|---:|---:|
| `KAUS` | 62 | 0 | 0 | 62 |
| `KDEN` | 62 | 3 | 3 | 59 |
| `KLAX` | 62 | 3 | 3 | 58 |
| `KMDW` | 62 | 0 | 0 | 62 |
| `KMIA` | 62 | 0 | 0 | 62 |
| `KNYC` | 62 | 1 | 1 | 60 |
| `KPHL` | 62 | 2 | 2 | 59 |

## Convergence time

Convergence was observed in **9 / 9** analyzed events. Median: **0.0h**; p75: **0.0h**; p90: **0.0h**.

| Aligned time to first midpoint >= 0.95 | Events |
|---|---:|
| 0h | 9 |
| 1h | 0 |
| 2h | 0 |
| 3-5h | 0 |
| 6h+ | 0 |
| not_observed | 0 |

## Executable gaps and hypothetical after-fee P&L

P&L is one YES contract per usable snapshot, including snapshots whose ask was already at or above 0.95. The average tradeable gap is conditional on `YES ask < 0.95` and equals `0.95 - ask`.

| Snapshot | Events | Usable asks | Ask < 0.95 | Avg ask | Avg tradeable gap | Fees | Net P&L | Avg P&L |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| lock+0h | 9 | 9 | 0 | 1.000 | n/a | $0.00 | $0.00 | $0.000 |
| lock+1h | 9 | 9 | 0 | 1.000 | n/a | $0.00 | $0.00 | $0.000 |
| lock+2h | 9 | 9 | 0 | 1.000 | n/a | $0.00 | $0.00 | $0.000 |

### Secondary snapshot missingness

No missing snapshot quotes.

## Interpretation and caveats

- **Hourly resolution makes every measured duration an upper bound on capturable edge duration.** A candle first showing convergence at hour N only establishes that convergence happened by that candle end; it can happen much earlier inside the interval.
- If most events converge in the aligned lock candle, the defensible verdict is **not measurable at this granularity, likely arbitraged**. This study cannot establish a minutes-scale trading window.
- Observation timestamps and candle end timestamps are different objects. Aligning a METAR timestamp to its containing candle end can include up to nearly one hour of market reaction before the recorded hour-zero close.
- IEM observations are hourly METAR reports. The settlement climatological maximum comes from a continuous sensor, so the observed lock is conservative and may occur later than the true physical lock. The 1.0°F clearance margin also protects against METAR conversion/rounding differences.
- Central Park is IEM `NYC` in `NY_ASOS` (Kalshi station `KNYC`), an important non-airport identifier caveat. Other mappings are MDW, MIA, AUS, DEN, PHL, and LAX.
- Hourly candlesticks expose candle-close bid/ask summaries, not guaranteed fills, queue depth, latency, or the intrahour quote path. The P&L is hypothetical and ignores slippage beyond the displayed ask.
- A missing, one-sided, crossed, or not-yet-open lock-hour candle is counted explicitly and never imputed. A physical lock that conflicts with settlement is dropped and surfaced as a data-integrity warning.

## Reproduce

```bash
python3 scripts/intraday_convergence.py
```
