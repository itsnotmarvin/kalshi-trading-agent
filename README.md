# Forecast archive

Append-only, point-in-time snapshots written by the `Forecast archive`
GitHub Actions cron (`.github/workflows/forecast-archive.yml` on `main`),
every four hours at fixed UTC times.

Each snapshot (`archive/YYYY/MM/DD/HHMMZ.json.gz`, schema
`forecast-archive/v1`) contains the raw GFS ensemble members at each
settlement station, the station's own NWS observation, the full top-of-book
state of every open KXHIGH market, and a method-labeled ensemble probability
per market.

**Verification:** do not trust git timestamps — commit dates are
author-settable. Every snapshot embeds the `GITHUB_RUN_ID` and run URL of
the Actions run that produced it; GitHub's server-side run history is the
tamper-evident record that the forecast existed before resolution.
