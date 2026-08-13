# Fable Prompt: Review Argentina 2026 World Cup Kalshi Market

Review this specific Kalshi market and produce a read-only market-move analysis:

https://kalshi.com/markets/kxmenworldcup/mens-world-cup-winner?utm_source=kalshiweb_eventpage

Focus specifically on the Argentina YES market inside the Men's World Cup Winner event.

The user has money on Argentina and wants to understand what is going on, why the odds have been moving, whether the move looks real or noisy, and what to watch before deciding whether to hold, sell, trim, or keep monitoring.

This is a research-only analysis. Do not place trades, cancel trades, approve trades, mutate positions, or create live/paper orders. Produce analysis and a monitoring plan only.

## Required Data Collection

Use the Kalshi page and downloaded market history data.

1. Open the market page.
2. Select or identify the Argentina YES contract.
3. Download the price-history CSV from Kalshi if available.
4. Parse the CSV instead of relying only on the screenshot/chart.
5. If the CSV cannot be downloaded, state that clearly and use the best available read-only market data, but do not pretend CSV analysis was performed.
6. Check current market price, bid/ask or executable price if available, spread, liquidity/exitability, and recent price movement.
7. Gather current source-backed context for Argentina's 2026 World Cup outlook:
   - tournament timing
   - draw/bracket context if available
   - injuries/roster news
   - manager/team form
   - sportsbook or expert odds if available
   - comparable market movement for other contenders such as France
   - any recent catalyst that could explain Argentina moving up or down

Use current sources with dates. Separate fresh facts from stale or historical context.

## Market-History Analysis

Use the downloaded CSV to identify:

- current price
- 24h / 7d / 30d change if data supports it
- major spikes and drops
- consolidation zones
- breakout or breakdown areas
- volatility
- spread/liquidity concerns
- whether the move appears catalyst-driven, low-liquidity, stale, or correlated with other teams
- whether Argentina is moving independently or as part of broader World Cup winner repricing

Do not rely only on technical chart patterns. Chart structure is one signal; prediction markets move because beliefs about real-world outcomes change.

## Probability And Price

Separate:

- market-implied probability using midpoint or a clearly stated pricing method
- executable YES price used for cost/EV
- estimated fair probability or fair range
- confidence level
- edge, if any

Do not call the ask alone "the market probability." If bid/ask are available, use midpoint or a clearly labeled method for market-implied probability, and use executable ask/sell price for cost or exit analysis.

If model/fair probability is weak or under-sourced, say so and use a range rather than false precision.

## Position-Aware Output

The user owns Argentina. If position details are visible or provided, use them. If not, ask for or note the missing pieces:

- contracts owned
- average entry price
- current sell price
- unrealized P&L

Still produce a useful monitoring plan even if exact position details are missing.

The position plan should include:

- current stance: hold / sell / trim / monitor / avoid adding
- why
- invalidation point
- likely upside/downside
- next catalyst date or event to watch
- sell-into-strength conditions
- hold-through-catalyst conditions
- what evidence would change the call
- what price/action would make selling attractive
- what price/action would make holding attractive

Do not present the stance as financial advice or an approved order. Keep it as research-only decision support.

## Required Report Format

```md
# Argentina World Cup Winner Market Review

## Quick Read
- Market:
- Contract:
- Current YES price:
- Bid/ask or executable sell/buy price:
- Recent move:
- Move type:
- Direction bias:
- Confidence:
- Research status: read-only

## What Changed
Explain the latest meaningful price movement in plain English.

## Price History Signals
- CSV used: yes/no
- 24h / 7d / 30d change:
- Major spike/drop points:
- Trend:
- Volatility:
- Liquidity / exitability:
- Related market movement:

## Likely Catalysts
1. Catalyst:
   Evidence:
   Source:
   Date:
   Probability impact:

## Fair Probability Check
- Market-implied probability / pricing method:
- Executable price used for EV or exit:
- Estimated fair probability range:
- Edge:
- Confidence:
- Main assumptions:

## Position Plan
- Current stance:
- Hold if:
- Sell/trim if:
- Reassess if:
- Next catalyst/date:
- Invalidation point:

## Risks
- What could break the thesis:
- What could make Argentina reverse lower:
- What could make Argentina continue higher:

## Sources
- Source title:
  URL:
  Published/fetched date:
  Used for:
```

## Quality Requirements

- Use the CSV if available and mention the file/path or download status.
- Cite sources with URLs and dates.
- Keep the probability reasoning transparent.
- Separate market price, model/fair probability, and research judgment.
- Do not hide assumptions.
- Avoid generic soccer commentary.
- Focus on why Argentina's Kalshi odds are moving and what may happen next.
- Finish with a completion report: what data was used, whether CSV download succeeded, and any true blockers.
