# Kalshi Prediction Market Research and Decision-Support Platform

An AI-assisted platform that scans live prediction markets, combines prices
and order books with weather, sports, and source-backed news, and ranks
opportunities by estimated probability, expected value, liquidity, and risk —
with paper/shadow validation and risk-gated execution. Provider-configurable:
supports **Kalshi** (recommended for US users 18+) and **Polymarket**.

## Platform Options

| Platform    | Age Req | Location | Real Money | API Access | Best For              |
|-------------|---------|----------|------------|------------|-----------------------|
| **Kalshi**  | 18+     | US (42+ states) | Yes | REST + WebSocket | Regulated, USD deposits |
| **Polymarket** | 18+  | Global (US waitlist) | Yes | REST + WebSocket | Highest volume, crypto |
| **Manifold**| 13+     | Global   | Play money | REST | Risk-free prototyping |

## Quick Start

```bash
# 1. Clone and setup
cd kalshi-agent
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install anthropic openai httpx python-dotenv kalshi-python

# 3. Configure credentials
# Edit .env with your API keys and model provider choice

# 4. Run in real-market paper/shadow mode (NO order placement)
python main.py --mode paper

# 5. After enough resolved real-market evidence, go live with tight limits
python main.py --mode live --max-daily-loss 25
```

Paper/shadow mode always points at production market data so validation is based
on real tickers, real prices, and real resolutions; it only skips live order
placement.

## LLM Provider Setup

The agent defaults to Claude and supports three provider modes:

```bash
# Free/dev mode: no paid model calls, safe HOLD-only proposals
LLM_PROVIDER=mock

# ChatGPT/OpenAI mode: requires OPENAI_API_KEY
LLM_PROVIDER=openai
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-5.4-mini
OPENAI_SCAN_MODEL=gpt-5.4-mini

# Existing Claude mode: requires ANTHROPIC_API_KEY
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=your_key_here
CLAUDE_MODEL=claude-opus-5
CLAUDE_SCAN_MODEL=claude-opus-5
```

If you want to build the dashboard and World Cup workflow before funding API
credits, use `LLM_PROVIDER=mock`. It will never recommend a real trade; it only
keeps the scan/research flow alive for UI and integration testing.

## World Cup Trading Assistant Decisions

For the World Cup route, evaluate every available market one by one. Do not
shortcut to only a top few markets just because the slate is large.

Support both true Kalshi combos/RFQs and basket-style groups of single trades.
Singles can be monitored for sell/hedge opportunities after purchase; combos
must show RFQ freshness, correlation risk, and explicit approval language.

The approval flow is: assistant researches, prepares an exact trade card, user
approves, then the bot places only that exact trade. Post-fill monitoring should
prepare sell/hedge cards for user approval.

The research breakdown should show source-backed probabilities, signed factor
impacts, confidence/reliability scores, the live Kalshi quote, and the gap
between the independent estimate and market-implied probability. Kalshi is the
source of truth for final fees, total cost, payout, and order-preview math.

## Probability Math Contract

The assistant must not invent additive probability adjustments. Probability
updates should happen in log-odds space, or by re-running the deterministic
model after one sourced input changes:

```text
logit(p) = ln(p / (1 - p))
final_logit = base_logit + sum(factor_weights)
final_probability = 1 / (1 + exp(-final_logit))
```

For user-facing factor impacts, report the measured probability difference
caused by perturbing one input:

```text
impact = P(model | changed input) - P(model | base input)
```

That impact is only valid if the changed input is sourced. For example, a
striker absence can change expected goals only through a rating, xG/xA
contribution, lineup model, market movement study, or another cited data source.
If a factor lacks source support, show it as `UNVERIFIED` and do not silently
fold it into the final probability.

Use Kalshi prices in two different ways:

- Mid-market estimate: closer to market belief, such as `(yes_bid + yes_ask)/2`.
- Executable price: the current quote for one YES or NO contract.

Do not call the ask alone "the market probability." The ask includes the cost
of crossing the spread. Compare the model with the market midpoint:

```text
probability_gap = model_side_probability - market_side_probability
confidence_adjusted_gap = probability_gap * confidence_multiplier
```

The app models the Kalshi trading fee — `ceil_to_cent(rate × C × P × (1 − P))`,
taker 7% / maker 1.75% — in both the decision gate and realized P&L, because an
"edge" smaller than the fee is not an edge:

```text
fee_adjusted_gap = confidence_adjusted_gap - fee_fraction(side_cost)
```

It still uses a configured fixed stake plus hard safety limits rather than
reproducing Kalshi's payout/breakeven/cash-out/Kelly checkout math, and asks
the user to verify final checkout numbers in Kalshi.

For grouped singles, show every leg separately and do not multiply leg
probabilities. Same-match or correlated legs require explicit correlation notes.
- True Kalshi combos/RFQs must display quote freshness and finality before
  approval.

Track calibration over time. Bin predictions by probability and compare
predicted frequency to observed frequency. Long-shot bins are especially
important, because tail probabilities create attractive payouts and the easiest
fake edges.

## Scoreboard Integrity

Paper validation is only as honest as the numbers feeding it, so the
measurement layer enforces five invariants (regression-tested in
`tests/test_scoreboard_integrity.py`):

- **Fees in every path** — the edge gate subtracts the entry fee from the
  probability gap, settlement P&L is net of the entry fee, and take-profit
  exits must clear round-trip fees before firing.
- **404s still settle** — Kalshi legitimately 404s expired/settled markets;
  the adapter falls back to the event endpoint so resolved positions book
  P&L instead of staying open at a stale mark.
- **Only fills count** — an accepted order is not a trade. Balance, daily
  trade counters, and alerts reflect the adapter-reported fill count, with
  partial fills counted for exactly the filled contracts.
- **Idempotency survives retries** — `client_order_id` is derived from the
  trade thesis (market + direction + price + day), not a per-cycle UUID, so
  a timeout-after-acceptance retry hits the 409 dedup path instead of
  double-positioning.
- **Circuit breakers survive restarts** — halt state, loss streaks, and
  daily P&L persist to disk and reload on startup, so a crash cannot clear
  a tripped breaker.

## Architecture

```
main.py                  ← Entry point, scheduling loop
core/
  agent.py               ← Provider-aware reasoning engine (Claude, ChatGPT, or mock)
  risk_manager.py        ← All safety checks & circuit breakers
  portfolio.py           ← Position tracking & P&L
  logger.py              ← Decision logging for review
adapters/
  base.py                ← Abstract interface all platforms implement
  kalshi_adapter.py      ← Kalshi API integration
  polymarket_adapter.py  ← Polymarket API integration (stub)
  manifold_adapter.py    ← Manifold play-money (for practice)
strategies/
  forecaster.py          ← Claude probability estimation
config/
  settings.py            ← All configurable parameters
  prompts.py             ← System prompts for LLM research
```

## Cost Estimates

- **Claude/OpenAI API**: configurable; use mock mode for $0 local testing
- **Kalshi**: Verify current fees and final totals in Kalshi's order preview
- **Total startup cost**: ~$50-100 (API credits + initial trading capital)
