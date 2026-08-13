# Kalshi temperature-market settlement stations

Kalshi daily high-temperature markets settle on the **NWS Climatological
Report (Daily)** for one specific observing station. Forecasting or observing
anywhere else measures the wrong quantity. This table is the source of truth
for `core/settlement_stations.py`; both the ensemble forecast coordinates and
the observation-lock override use it.

Station identities were read from each live market's `rules_primary` text via
the Kalshi API on **2026-08-13**. Coordinates are the observing stations
themselves, not city centers.

| City | Series | Settlement location (per rules) | Station | Lat, Lon | Local climate day |
| --- | --- | --- | --- | --- | --- |
| New York | `KXHIGHNY` | Central Park, New York | `KNYC` | 40.779, -73.969 | America/New_York |
| Chicago | `KXHIGHCHI` | Chicago Midway, IL | `KMDW` | 41.786, -87.752 | America/Chicago |
| Miami | `KXHIGHMIA` | Miami International Airport | `KMIA` | 25.791, -80.316 | America/New_York |
| Austin | `KXHIGHAUS` | Austin Bergstrom | `KAUS` | 30.195, -97.670 | America/Chicago |
| Denver | `KXHIGHDEN` | "Denver, CO" (see caveat) | `KDEN` | 39.847, -104.656 | America/Denver |
| Philadelphia | `KXHIGHPHIL` | Philadelphia International Airport | `KPHL` | 39.868, -75.231 | America/New_York |
| Los Angeles | `KXHIGHLAX` | Los Angeles Airport, CA | `KLAX` | 33.938, -118.389 | America/Los_Angeles |

## Quoted rules (retrieved 2026-08-13)

> **KXHIGHNY**: "If the highest temperature recorded in **Central Park, New
> York** … as reported by the National Weather Service's Climatological Report
> (Daily), is greater than X°, then the market resolves to Yes."
>
> **KXHIGHCHI**: "… recorded at **Chicago Midway, IL** … according to the
> National Weather Service's Climatological Report (Daily) …"
>
> **KXHIGHMIA**: "… recorded at **Miami International Airport** …"
>
> **KXHIGHAUS**: "… recorded in **Austin Bergstrom** …"
>
> **KXHIGHDEN**: "… recorded in **Denver, CO** …"
>
> **KXHIGHPHIL**: "… recorded at **Philadelphia International Airport** …"
>
> **KXHIGHLAX**: "… recorded in **Los Angeles Airport, CA** …"

## Caveats and traps

- **Denver:** the rules name only "Denver, CO". The NWS Denver daily
  climatological report is issued for **Denver International Airport**, which
  sits ~30 km northeast of downtown — DIA regularly differs from the
  city-center grid point by several °F. If Kalshi ever clarifies a different
  station, update `core/settlement_stations.py` and this table together.
- **Austin settles at Bergstrom (`KAUS`), not Camp Mabry (`KATT`).** Camp
  Mabry is the station most Austin climate trivia refers to; assuming it here
  would silently misprice every Austin market.
- **The settlement max can exceed hourly observations.** The Climatological
  Report's daily max comes from a continuous sensor; METAR-derived hourly
  observations arrive in °C and round through unit conversion. This is why
  observation locks in `core/weather_engine.py` must clear the threshold by
  `LOCK_MARGIN_F` (1.0°F) instead of comparing exactly.
- **The climate day is the station's local calendar day**, not a UTC window.
  Market tickers encode it exactly (`KXHIGHNY-26AUG13-…` → 2026-08-13); a
  market `end_date` converted to UTC can fall on the following calendar day.
