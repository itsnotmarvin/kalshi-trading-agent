# Manual Probe Scripts

These files are live/manual diagnostics, not automated pytest coverage.

They may call Kalshi, Polymarket, local trade logs, or account endpoints, so they
were moved out of the project root to keep `pytest` focused on offline regression
tests. Run them from the project root when you explicitly want a probe, for
example:

```bash
python scripts/manual_probes/test_kalshi.py
```

For the World Cup route, this read-only probe fetches the public schedule,
scans live Kalshi Sports markets, links markets to the matchday teams, and
prints the discovered market/leg types such as winner/result, totals, both
teams to score, player props, win margin, and exact score when present:

```bash
python scripts/manual_probes/test_world_cup_markets.py
```

For weather model output without live or paper trade automation, use:

```bash
python scripts/manual_probes/run_weather_outputs.py --limit 25
```

That runner prints model diagnostics and optional JSON only. It does not place
orders, create paper positions, or write trade logs.

The local `sitecustomize.py` keeps project imports working after the move.
