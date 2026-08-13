<!-- Authored by Codex gpt-5.6-sol (read-only recon, 2026-08-13); probe results
independently re-verified live the same night: previous-runs API returned
92/92 days non-null for previous_day1/3/7 at KDEN coordinates. -->

# BACKTEST DATA PLAN

## Probe Results (exact numbers)

Probe request:

```bash
curl -sS --get \
  'https://previous-runs-api.open-meteo.com/v1/forecast' \
  --data-urlencode 'latitude=39.847' \
  --data-urlencode 'longitude=-104.656' \
  --data-urlencode 'hourly=temperature_2m_previous_day1,temperature_2m_previous_day2,temperature_2m_previous_day3,temperature_2m_previous_day4,temperature_2m_previous_day5,temperature_2m_previous_day6,temperature_2m_previous_day7' \
  --data-urlencode 'past_days=92' \
  --data-urlencode 'forecast_days=1' \
  --data-urlencode 'timezone=UTC'
```

As of 2026-08-13, `past_days=92` requests 2026-05-13 through 2026-08-12, plus the current/forecast day 2026-08-13. Every lead populated the complete window:

| Variable | Earliest non-null date | Non-null past depth | Total daily groups |
|---|---:|---:|---:|
| `temperature_2m_previous_day1` | 2026-05-13 | 92/92 days | 93/93 |
| `temperature_2m_previous_day2` | 2026-05-13 | 92/92 days | 93/93 |
| `temperature_2m_previous_day3` | 2026-05-13 | 92/92 days | 93/93 |
| `temperature_2m_previous_day4` | 2026-05-13 | 92/92 days | 93/93 |
| `temperature_2m_previous_day5` | 2026-05-13 | 92/92 days | 93/93 |
| `temperature_2m_previous_day6` | 2026-05-13 | 92/92 days | 93/93 |
| `temperature_2m_previous_day7` | 2026-05-13 | 92/92 days | 93/93 |

Each series rendered 93 consecutive daily groups with no break. Open-Meteo defines `_previous_dayN` as the value forecast exactly `N × 24` hours before each valid timestamp. [Previous Runs documentation](https://open-meteo.com/en/docs/previous-runs-api)

## Assembly Spec

### 1. Market and settlement table

Use the seven series and station metadata in [core/settlement_stations.py](/Users/marbin/kalshi-trading-agent/core/settlement_stations.py:29), consistent with [docs/settlement-stations.md](/Users/marbin/kalshi-trading-agent/docs/settlement-stations.md:13).

Paginate:

```text
GET https://api.elections.kalshi.com/trade-api/v2/markets
    ?series_ticker={KXHIGH...}
    &status=settled
    &limit=100
    &cursor={cursor}
```

Retain at least:

```text
series_ticker, event_ticker, ticker, result,
strike_type, floor_strike, cap_strike,
open_time, close_time, expected_expiration_time
```

Derive `target_date` from the ticker’s `YYMMMDD` component, as in [scripts/archive_forecasts.py](/Users/marbin/kalshi-trading-agent/scripts/archive_forecasts.py:72). Do not derive it from the UTC date of `close_time`.

Primary market key: `market_ticker`.  
Event-day key: `(series_ticker, target_date)`.

### 2. Deterministic forecast table

Use one reproducible bulk request per station:

```text
GET https://previous-runs-api.open-meteo.com/v1/forecast
    ?latitude={station_lat}
    &longitude={station_lon}
    &hourly=temperature_2m_previous_day1,...,temperature_2m_previous_day7
    &models=gfs_seamless
    &temperature_unit=fahrenheit
    &timezone={station_IANA_timezone}
    &start_date={earliest_target_date}
    &end_date={latest_target_date}
```

Explicit `gfs_seamless` avoids a changing “best match” blend and guarantees a global model with all seven leads.

For every `(series_ticker, target_date=D, horizon_days=H)`:

1. Select hourly valid timestamps inside local climate day `[D 00:00, D+1 00:00)`.
2. Read `temperature_2m_previous_dayH`.
3. Assign each component an implied issue timestamp:

   ```text
   component_issue_ts = valid_ts_utc - H * 24 hours
   ```

4. Set:

   ```text
   forecast_cutoff_ts = max(component_issue_ts)
   forecast_daily_high_f = max(24 hourly forecast temperatures)
   ```

For a normal 24-hour day, the bundle cutoff is approximately local `D 23:00 - H days`. Thus the `day1` bundle becomes complete around 23:00 on the preceding local day.

Forecast join key:

```text
(series_ticker, target_date, horizon_days)
```

### 3. Matching market candle

For each market, request a window covering all selected cutoffs:

```text
GET https://api.elections.kalshi.com/trade-api/v2/series/{series_ticker}/markets/{market_ticker}/candlesticks
    ?period_interval=60
    &start_ts={unix_seconds_before_earliest_cutoff}
    &end_ts={unix_seconds_at_latest_cutoff}
```

This endpoint is already represented in [adapters/kalshi_adapter.py](/Users/marbin/kalshi-trading-agent/adapters/kalshi_adapter.py:244).

For each horizon:

1. Choose the candle with the greatest `end_period_ts` satisfying:

   ```text
   end_period_ts <= forecast_cutoff_ts
   ```

2. Require both two-sided closes.
3. Compute:

   ```text
   market_mid = (yes_bid.close + yes_ask.close) / 2
   ```

   Use `close_dollars` directly if supplied; otherwise divide cent fields by 100.

4. Never use a candle ending after the cutoff. Prefer an exact matching hour; if permitting carry-forward, predeclare a small staleness ceiling, such as two hours, record `price_age_minutes`, and discard older prices.

Candlestick join key:

```text
(series_ticker, market_ticker, candle_end_ts)
```

### 4. Strike and outcome normalization

Use the repository’s existing boundary conventions from [scripts/archive_forecasts.py](/Users/marbin/kalshi-trading-agent/scripts/archive_forecasts.py:111):

| `strike_type` | Forecast-implied YES |
|---|---|
| `greater` | `forecast_daily_high_f > floor_strike` |
| `less` | `forecast_daily_high_f < cap_strike` |
| `between` | `floor_strike <= forecast_daily_high_f <= cap_strike` |

Normalize settlement as `result_yes = 1` for `result=="yes"` and `0` for `result=="no"`. The Kalshi `result` remains authoritative; do not reconstruct settlement from weather observations.

Recommended final row grain:

```text
market_ticker × horizon_days
```

Fields:

```text
series_ticker, station_id, target_date, market_ticker,
horizon_days, forecast_cutoff_ts, forecast_daily_high_f,
candle_end_ts, price_age_minutes, yes_bid_close, yes_ask_close,
market_mid, strike_type, floor_strike, cap_strike,
forecast_implied_yes, result_yes
```

### Leakage guards

- Assert every forecast component satisfies `component_issue_ts <= forecast_cutoff_ts`.
- Assert `forecast_cutoff_ts < close_time` and precedes settlement/resolution.
- Never match a candle with `end_period_ts > forecast_cutoff_ts`.
- Never backfill from a later candle.
- Build forecasts and prices before attaching `result`.
- Use the ticker’s local climate date and station timezone, never a UTC calendar-day maximum.
- Keep one explicit model and parameter set across all cities and dates.

## Sample Size

Denver provides 402 markets over 68 calendar days. Using that as the per-city density estimate:

```text
Estimated markets = 402 × 7 cities = 2,814
```

| Horizons | Market × horizon rows |
|---|---:|
| One horizon | 2,814 |
| `H={1,3,7}` | 8,442 |
| `H={1,2,3,5,7}` | 14,070 |
| All `H=1..7` | 19,698 |

There are only approximately `68 × 7 = 476` distinct station-days. With all seven horizons, that is 3,332 distinct forecast vectors; multiple strike contracts on the same station-day share the same weather forecast and outcome driver.

## Risks/Unknowns

- The managed shell blocked DNS, so direct `curl` returned `Could not resolve host`. The exact request was executed through Open-Meteo’s API documentation transport. Re-run the command above in a normal networked shell if raw hourly null counts are required as an independent check.
- Previous Runs is a rolling fixed-lead product, not one internally consistent model run: the 24 daily components have different implied issue timestamps. The cutoff construction above prevents look-ahead, but a true single-run forecast should instead use `single-runs-api.open-meteo.com` with an explicit `run` no later than the decision timestamp.
- The API does not expose exact public-release/ingestion timestamps for each fixed-lead component. Record this assumption or impose a conservative availability buffer.
- Some horizons may predate market opening, and candles may be absent, stale, one-sided, or crossed. Such rows should be missing rather than filled from future information.
- Actual market totals will vary by city and strike count, so 2,814 is a density-based estimate, not an exact cross-series count.
- Hourly model maxima may miss a brief intrahour settlement peak from the continuous NWS sensor.

No files were changed.