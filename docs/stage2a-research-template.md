# Stage 2A Research Template — v1 (frozen)

Version: `stage2a-v1`. Paste this template verbatim into every Stage 2A
research session (T3 or otherwise), filling only the CANDIDATE block. If
the template changes, bump the version and record it; verdicts from
different template versions are not directly comparable. Companion to
`early-marks-v2-detection-spec.md` (Stage 2A definition) and
`early-marks-evaluation-spec.md` (how verdicts are graded).

---

## Prompt (paste from here down)

You are performing Stage 2A research adjudication for a Kalshi prediction
market surfaced as a structural anomaly (unresponsive price history, low
observed attention). Your job is to determine whether a price-relevant
influence exists — NOT to recommend a trade. Web search is required; cite
direct URLs for every factual claim. Do not state a direction, fair value,
expected price, or any trade recommendation.

CANDIDATE
- ticker: {ticker}
- event_ticker: {event_ticker}
- series_ticker: {series_ticker}
- observed price / book: {price_context}
- attention: {attention_state} (24h volume {volume_24h})
- response: {response_state} over {window_hours}h
- shortlist_ref: {shortlist_ref}

Research these questions against live sources:
1. What does this market settle on, exactly? (Find the settlement rule.)
2. Is there an influence — announced OR predictable — that bears on that
   settlement rule? What is it, and when does it act?
3. What is the causal channel from the influence to the settlement outcome?
4. Is the influence already reflected in the price? What would you expect
   the market to look like if it were?

Decision rules — pick exactly one verdict:
- `verified_influence`: a concrete influence exists, you can state its
  timing and causal channel and why it appears NOT to be priced, and every
  element is backed by a cited source.
- `already_priced`: the influence exists (same evidentiary bar) but the
  market already reflects it.
- `no_relevant_influence`: you researched and found nothing that bears on
  the settlement rule within a relevant horizon. This is an expected,
  frequent, and fully acceptable answer — say it plainly.
- `insufficient_data`: timing, settlement relevance, or pricing status
  cannot be established from sources. Choose this over unsupported
  certainty, and give the reason in `notes`.

End your response with EXACTLY one JSON object in this shape (the influence
object is required for verified_influence and already_priced, forbidden
fields must not appear anywhere):

```json
{
  "ticker": "...",
  "shortlist_ref": "...",
  "verdict": "verified_influence | no_relevant_influence | already_priced | insufficient_data",
  "influence": {
    "influence": "what it is",
    "timing": "when it acts",
    "causal_channel": "how it reaches the settlement outcome",
    "settlement_relevance": "why it bears on the settlement rule",
    "pricing_assessment": "why it is / is not already priced"
  },
  "sources": ["https://..."],
  "model": "model name/version doing this research",
  "template_version": "stage2a-v1",
  "retrieval": "model_self_search",
  "notes": "optional; required reason for insufficient_data"
}
```

`retrieval` values: `model_self_search` (the model searched on its own),
`codex_research` (evidence brief from the Codex bridge), `exa_pack:v<N>`
(pre-assembled Exa evidence pack, once those exist).

---

## Recording the verdict

From kalshi-agent/, paste the JSON block:

    pbpaste | .venv/bin/python scripts/record_verdict.py

`researched_at` is stamped automatically. Rejections print the exact reason
and write nothing. Dry-run/rehearsal verdicts must use a `dryrun-*`
shortlist_ref so evaluation excludes them.
