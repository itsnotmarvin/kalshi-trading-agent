# Data directory

`wc2026_bracket.json` is the only committed reference fixture in this
directory. The application and research probes generate the other JSON/JSONL
files locally for paper positions, calibration, circuit-breaker state,
postmortems, and replay output; those files are intentionally ignored.

Generated evidence that is useful for review should be reduced to a small,
human-readable artifact under `examples/` with its assumptions and limitations
stated explicitly.
