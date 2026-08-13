# Fable Prompt: Kalshi Sports Prediction Skills

Create a set of reusable Codex/Fable skills for my Kalshi sports prediction project.

Do not merely suggest possible skills. Build the plan around the skill set below, then improve, refine, rename, merge, split, or add to them where useful. Keep the core intent intact, but critically review the architecture before building so the final skill set is cleaner than this first draft.

Execution expectation:

- Do the work, not just the planning.
- Do not end with a list of suggestions for what could be done later.
- If you have filesystem access, create or update the actual skill folders, `SKILL.md` files, shared references, scripts, test artifacts, and validation commands needed for the first useful implementation.
- If the full scope is too large for one pass, still complete a coherent first slice instead of stopping at recommendations.
- Final output should be a completion report: what was created or changed, what tests/validation were run, and any true blockers.
- Only leave follow-up items when they are genuinely blocked by missing data, credentials, unavailable tools, or a deliberate user decision.

The project should center on these skills:

1. `market-move-analyzer`
2. `position-exit-monitor`
3. `sports-catalyst-scanner`
4. `combo-correlation-checker`
5. `market-history-backtester`
6. `fair-probability-builder`
7. `trade-card-generator`
8. `news-to-market-impact`
9. `early-marks-scanner`

## UI Architecture

Design the app like a traditional trading workspace adapted to Kalshi prediction markets and transparent research. Do not cram every feature onto one page. Organize the interface by workflow.

The current UI direction should be redesigned rather than lightly recolored. The goal is a cleaner, more professional trading-desk feel: dense enough for repeated analysis, calm enough to scan, and clearly adapted to prediction markets rather than looking like a generic AI dashboard.

Visual direction:

- Use a restrained, practical palette rather than loud gradients or a one-color theme.
- Prefer neutral surfaces, strong readable text, and a small set of semantic colors: positive/edge, risk/loss, warning/stale, selected/action, muted/disabled.
- The preferred inspiration is the Dribbble shot "AI Helper for Smarter Crypto Investing and Trading" by Wladyslaw for Zajno: https://dribbble.com/shots/26372656-AI-Helper-for-Smarter-Crypto-Investing-and-Trading
- Translate that inspiration into Kalshi's domain: polished AI trading assistant, readable data cards, status-rich panels, modern loading states, and a more premium feel. Do not copy the crypto branding or make readability worse.
- Avoid generic glossy AI-app styling, oversized decorative elements, heavy glow, and purely cosmetic gradients.
- Cards and panels should feel like trading tools: compact, aligned, information-dense, and easy to compare.
- Buttons should make action hierarchy obvious: primary action, secondary action, destructive/risk action, disabled state, and research-only/paper-only state.
- Make market price, position status, payout, edge, next catalyst, and exitability visually scannable at a glance.
- Keep mobile layouts clean by stacking trading panels below the chart and preserving all critical values.
- Fable should execute a more cohesive design system if the existing UI looks messy or inconsistent, while still completing a coherent implementation slice rather than stopping at design suggestions.
- Include visible service health indicators before generation: `Kalshi API`, `LLM API`, `OpenAI API` when configured, `Research/Search API`, `Local server`, and `Price history / data cache`.
- Each service indicator should show `ONLINE` / `OFFLINE` or `API OK` / `API ERROR` with clear icons or chips.
- Check service health automatically on page load so the user knows whether pressing `Generate` can produce the best answer.
- Use staged loading when generating, with a spinner similar to the attached circular segmented loader and short steps such as: `Connecting to Kalshi`, `Pulling markets`, `Checking category model`, `Ranking early marks`, `Enriching with research`, and `Rendering cards`.

Primary pages or surfaces:

1. Home / Market Scanner: find markets worth attention.
2. Market Detail: analyze one market deeply.
3. Early Marks: manually generate early-stage, uninfluenced market candidates.
4. Position Monitor: manage owned positions after purchase.
5. Combo Builder: construct and review multi-leg ideas.
6. Backtest Lab: test historical prediction methods.
7. Optional Model Lab / Settings: manage model versions and trust status.

### Market Detail Layout

The Market Detail page should feel like a trading page, but with prediction-market research built in.

Top header:

- market title
- ticker
- category
- resolution or close date
- current YES / NO price
- last price change
- volume/liquidity
- status: open, closing soon, resolved, watch-only, owned, paper, or research-only

Main chart:

- price history
- volume underlay if available
- event/catalyst markers
- time range controls
- compare line for related outcomes when useful
- model fair-value overlay
- user entry-price line when the user owns the position

Right rail or stacked mobile panels:

- trade ticket or paper/research ticket
- position box if owned
- probability box
- order book / depth box
- alerts/watch rules

Tabs or secondary sections:

- `Overview`
- `Price Moves`
- `Research`
- `Position`
- `Related Markets`
- `Scenarios`
- `Backtest`

### Trading Page Components

Include these components where relevant:

- **Order book / depth:** YES bid/ask, NO bid/ask, spread, available size, exitability warning, and "can I actually sell this?" indicator.
- **Trade ticket:** buy YES, buy NO, sell owned YES/NO, contracts, estimated cost, max payout, breakeven probability, fees if available, paper/research/live status, and explicit approval/review state.
- **Position box:** contracts owned, average entry, current sell price, unrealized P&L, max payout, current stance, next catalyst, invalidation point, and hedge/sell/trim notes.
- **Probability box:** market-implied probability, model fair probability or range, edge, confidence, and whether the estimate is tradeable, research-only, or not scoreable.
- **Catalyst timeline:** past catalysts that moved price, upcoming catalysts, date/time, expected direction, confidence, and source link.
- **Research drawer:** sources, factor scores, bull case, bear case, stale-source warning, probability reasoning, and what would change the thesis.
- **Related markets:** other outcomes in the same event, same-team/player/candidate markets, opposing outcomes, correlated markets, and combo-relevant markets.
- **Activity / decision log:** when the market was scanned, what changed since the last scan, previous stance, current stance, and why the stance changed.
- **Alerts / watch rules:** price crosses, spread tightens, model edge disappears, catalyst approaches, source/news changes thesis, or owned position becomes a sell/trim candidate.

### Page Intent Rules

Keep each page focused on one user question:

- Scanner: What should I look at?
- Market Detail: What is happening and why?
- Early Marks: What early, uninfluenced, low-odds markets have future catalyst runway?
- Position Monitor: Should I hold, sell, trim, or hedge?
- Combo Builder: Do these legs make sense together?
- Backtest Lab: Did this method work historically?
- Model Lab: Which models are trusted, experimental, explanatory-only, or retired?

Do not put full research, combo building, scenario simulation, backtesting, and position monitoring all on one screen. Use compact summaries with drill-down paths.

## Critical Review Pass

Before building, review this plan critically.

Improve the skill plan where useful, but do not ignore the core skill list. You may rename, merge, split, or reorder skills if there is a clear reason. Explain why.

For any probability math or model logic, justify why it belongs in the code. Do not add math just because it sounds sophisticated. Every metric, score, or model component should answer one of these questions:

- Did this estimate beat the market price?
- Was this estimate calibrated?
- Did this signal help predict future price movement or final outcomes?
- Did this signal improve over a simpler baseline?
- Did this reduce bad hold/sell decisions?

Add tests that compare against simple baselines:

1. Market-only baseline: use the Kalshi price as the probability.
2. Naive baseline: use 50/50 for binary events, or equal probability across outcomes.
3. Closing-price baseline: compare the prediction to the final pre-event market price.
4. Optional external-odds baseline: use sportsbook/implied odds where available.

A model or probability feature should only stay if it improves out-of-sample results versus these baselines. If it does not improve results, label it as explanatory-only or remove it.

## First Skill: `market-move-analyzer`

This is the most important first skill.

Purpose:
Analyze a specific Kalshi market using downloaded price-history data, screenshots, market URLs, and optional position information. The goal is to explain why odds are moving, whether the move is likely to continue, and what a professional-style hold/sell/watch approach would be.

The skill should make probability reasoning transparent and combine:

1. Kalshi market prices/history
2. Statistical or domain model signal
3. Real-world research and sources
4. Position-aware hold/sell/hedge thinking
5. Clear trade-card style output

Inputs the skill should support:

- Kalshi market URL
- Downloaded price-history CSV
- Screenshot of the chart
- User's position, if available: side, average cost, contract count
- Optional market context: sport, team/player/country, event date, expiration date

Core workflow:

1. Parse market history
   - Identify current price
   - Recent change
   - Major spikes/drops
   - Volatility
   - Trend direction
   - Consolidation/breakout zones
   - Volume/liquidity if available
   - Key inflection points

2. Classify the move
   Label the move as one or more of:
   - catalyst repricing
   - slow trend
   - breakout after consolidation
   - low-liquidity spike
   - mean reversion risk
   - stale market update
   - correlated-market move
   - pre-event speculation

3. Explain likely causes
   Search for real-world catalysts:
   - injuries
   - roster changes
   - lineups
   - rankings
   - bracket/draw changes
   - odds movement
   - news
   - schedule dates
   - tournament structure
   - market comments
   - correlated market movement

4. Compare market price to fair probability
   Output:
   - current Kalshi market-implied probability using midpoint or a clearly stated pricing method
   - executable YES/NO price used for cost and expected value
   - independent estimated fair probability
   - confidence level
   - reason the estimate differs from market
   - factors pushing probability up
   - factors pushing probability down

5. Produce a position plan
   If I own the contract, output:
   - hold/sell/trim/monitor stance
   - invalidation point
   - likely upside/downside
   - next catalyst date
   - sell-into-strength conditions
   - hold-through-catalyst conditions
   - what evidence would change the call

6. Keep the output practical
   Avoid vague trading language. Tell me:
   - what changed
   - why it probably changed
   - what matters next
   - what price/action would make holding attractive
   - what price/action would make selling attractive

Required output format for `market-move-analyzer`:

```md
# Market Move Report

## Quick Read
- Market:
- Side:
- Current price:
- Move type:
- Direction bias:
- Confidence:

## What Changed
Brief explanation of the latest move.

## Price History Signals
- 24h / 7d / 30d change:
- Major spike/drop points:
- Trend:
- Volatility:
- Liquidity concerns:

## Likely Catalysts
1. Catalyst:
   Evidence:
   Source:
   Probability impact:

## Fair Probability Check
- Market implied probability / pricing method:
- Executable price used for EV:
- Estimated fair probability:
- Edge:
- Confidence:

## Hold / Sell / Watch Plan
- Current stance:
- Hold if:
- Sell/trim if:
- Reassess if:
- Next catalyst/date:

## Risks
- What could break the thesis:
- What could make the market reverse:
```

Important design note:

Do not rely only on stock-chart patterns like flags, breakouts, or trendlines. Those can be useful, but Kalshi markets move because beliefs about real-world outcomes change. Treat chart structure as one signal, then verify it against real catalysts, market liquidity, and probability.

## Self-Testing Framework

The skill should be able to test itself on historical Kalshi market data using a walk-forward method. It should simulate being in the past and only use information that would have been available at that time.

Testing method:

1. Load historical market price data.
2. Choose multiple cutoff dates before major catalysts.
3. For each cutoff date, hide all future price movement and all future news.
4. Ask the skill to produce a prediction using only information available up to that cutoff.
5. Lock the prediction.
6. Reveal the future outcome after the prediction is locked.
7. Score the prediction.
8. Repeat across multiple historical markets, not just one example.

For example, if testing a Germany vs Paraguay market:

- Use a cutoff date before the key catalyst or match.
- Hide the final result and future price movement.
- Have the skill estimate the likely winner and probability.
- Then reveal the actual outcome.
- Score whether the skill's probability was directionally useful and calibrated.

The goal is not to keep changing the method until it guesses Germany vs Paraguay correctly. That would overfit. The goal is to improve a general prediction process that performs well across many hidden historical examples.

Avoid overfitting by requiring:

- chronological train/test splits
- hidden future data
- multiple markets
- multiple sports or event types when possible
- no manual tuning to one game
- no changing rules after seeing the answer
- calibration scoring, not just win/loss accuracy
- written pre-analysis before revealing results

Use these test metrics:

- Brier score
- log loss, if enough samples exist
- directional accuracy
- calibration buckets
- average edge vs market
- whether the model beat closing market price
- whether the explanation identified the real catalyst before it happened
- false-positive catalyst rate

The skill may include scripts such as:

- `parse_kalshi_history.py`
- `make_walk_forward_splits.py`
- `score_predictions.py`
- `render_market_move_report.py`

## Probability Math Requirements

The project should not include math just because it looks professional. Probability math should earn its place through tests.

Required probability checks:

- Convert market prices to implied probabilities, while separating midpoint/market-belief estimates from executable ask prices used for cost.
- Compare model/fair probability against market-implied probability.
- Calculate expected value only when there is a stated price, payout, and probability.
- Use Brier score to test forecast accuracy when outcomes are known.
- Use calibration buckets to test whether repeated estimates behave honestly.
- Use log loss only when there are enough samples and the system can handle the punishment for confident wrong calls.
- Compare against market-only, naive, closing-price, and optional external-odds baselines.

Expected value formula for a YES contract that pays `$1`:

```txt
EV = (estimated_probability * 1.00) - executable_yes_cost
```

Do not call the ask alone "the market probability." The ask is the executable cost and includes spread. When bid/ask are available, use a midpoint or clearly labeled method for market-implied probability, and use the executable ask for EV.

Brier score formula:

```txt
Brier = (predicted_probability - actual_outcome)^2
```

Outcome is `1` if the event happens and `0` if it does not.

Keep probability outputs humble:

- Use ranges when evidence is weak.
- Do not report false precision like `18.734%` unless the data supports that precision.
- Separate market-price anchoring from independent evidence.
- State whether a probability estimate is tradeable, research-only, or not scoreable.
- Remove or downgrade any model feature that fails to improve out-of-sample results.

## Other Required Skills

### `position-exit-monitor`

Purpose:
Monitor owned single positions after purchase and help decide whether to hold, sell, trim, hedge, or wait.

Must output:

- current market
- side owned
- average entry price
- current sell price
- unrealized gain/loss
- remaining catalysts
- exit plan
- invalidation point
- hedge/sell conditions
- monitoring schedule

### `sports-catalyst-scanner`

Purpose:
Scan sports markets for upcoming catalysts that could reprice contracts.

Catalysts include:

- games
- injuries
- starting lineups
- bracket changes
- tournament draws
- suspensions
- rankings
- weather
- odds movement
- roster news
- schedule congestion

Must output:

- market
- catalyst
- expected timing
- likely direction
- affected contracts
- confidence
- source links

### `early-marks-scanner`

Purpose:
Find early or recently opened markets that are still relatively uninfluenced, where current odds are low or spread across many outcomes, but future catalysts could create meaningful movement.

It should ask:

```txt
What markets are early / recently opened and still very uninfluenced, where there will eventually be catalysts that move odds, but current odds are still low?
```

More specifically, Early Marks should look for markets that are in the pre-catalyst discovery phase:

- The market is new, recently opened, or still thinly interpreted by traders.
- Prices are still low, unconcentrated, or uncertain because no single outcome has been heavily validated yet.
- There is a credible future catalyst path that could force repricing later.
- The current price has enough room to move if the catalyst starts favoring one side or outcome.
- The market is not already dominated by one obvious favorite unless the low-priced alternatives have a clear catalyst path.
- The idea is usually a watchlist candidate first, not an immediate trade.

Example:

- A 2028 presidential election market where many candidates are under 30%.
- The market has lots of room for future catalysts such as announcements, polling, primaries, endorsements, legal/news events, debates, fundraising, or candidate withdrawals.
- The point is not that the market is already moving sharply. The point is that the market is early enough that future catalysts still have room to reshape the odds.
- Treat the 2028 presidential election as an example, not a default result. Do not over-select or repeatedly surface that same market just because it is a clear illustration.

Must evaluate:

- market age or early-stage status
- current price distribution across outcomes
- whether odds are broadly low or unconcentrated
- future catalyst runway
- catalyst types and likely timing
- liquidity and exitability
- spread quality
- whether the market is too directionless to be useful yet
- whether the idea is watch-only, catalyst setup, or reject
- duplicate status compared with prior generated Early Marks results
- category diversity so the shortlist does not become dominated by one example market
- whether the selected category has a working category-specific model
- whether the current run is unintentionally over-focused on one category or market family

Must output:

- market
- side or outcome being watched
- current price
- price distribution context
- future catalysts
- estimated runway
- liquidity/spread notes
- status: `Watch only`, `Catalyst setup`, `Too early`, or `Reject`
- reason it is or is not worth monitoring
- whether this candidate is new, previously seen, updated, or suppressed as a duplicate
- candidate/team/person image from web/API when available, with a clean category fallback if not available

UI requirement:

- Early Marks should have its own page or workflow.
- The page should not auto-load or auto-run the scan.
- Before the first `Generate` click, do not pre-fill ranked cards, mark counts, selected mark details, or model text with stale/outdated/sample data. Empty boxes are better than outdated information.
- Before generation, show only controls, service health, category/model availability, and an honest empty state such as `No scan run yet`.
- Include a clear `Generate` button that the user clicks to run or refresh the Early Marks scan.
- After generation, show the new shortlist as cards or rows.
- Each Early Marks candidate should link into the normal Market Detail page for deeper analysis.
- Keep Early Marks separate from the general Market Scanner: Early Marks is about early, uninfluenced, low-odds markets with future catalyst runway; the general scanner is about current movement and unusual activity.
- Do not show raw Kalshi ticker strings like `KXPRESPERSON-28-JOSS` anywhere in the user-facing Early Marks UI. Raw identifiers must be completely hidden from normal users, not just minimized. If needed internally, keep them in code/data only.
- Do not dump raw probe/debug text into the visible page after generation. Convert it into structured status, cards, and summaries. Put raw diagnostics only in developer logs, not in the normal UI.
- Candidate cards should include a candidate/team/person image from web/API when available. If no image exists, show a polished category-specific fallback visual.
- Add a category selector that only exposes categories with working category-specific Early Marks logic in V1.
- Do not allow `All categories` or generic `General` mode in V1 because current scoring is unintentionally biased toward the best-developed category.
- The category dropdown should show each available category plus a score/win-rate/quality indicator, for example `Politics — ##% win rate`, when such a metric exists.
- If only politics is reliable today, show only `Politics` as the working option and be honest that other category models are not ready yet.
- The system should detect category concentration. If a run becomes unintentionally focused on a single category or market family, either block general output, diversify using category-specific models, or show a clear warning.
- Keep a generated-results history on the Early Marks page.
- Run History should be all the way at the bottom of the page.
- Show it collapsed by default as `Run History ^` or a similar compact expandable control.
- When the user clicks the caret/control, show compressed summarized prior runs.
- When the user presses `Generate` again, collapse the previous generation into Run History instead of mixing everything into one flat list.
- Summaries should be short and human-readable, for example: `On {date}, suggested watching {trade} for {target/outcome} because {reason}.`
- De-duplicate candidates across generations by stable market identity, such as ticker plus side/outcome.
- If a newly generated candidate already exists in a previous run, update or annotate the existing candidate rather than showing a duplicate main card.
- Optionally show duplicates in a small `Already seen` or `Updated since last scan` section, but do not let duplicates crowd out new candidates.
- The first generation should remain accessible as an expandable historical run after later generations.

Known Early Marks V1 bugs to eliminate:

- Ranked Marks or selected-card panels showing stale text before the first run.
- A `Complete` state appearing before the latest generation has actually completed.
- Raw technical strings, tickers, or serialized debug output appearing in the normal UI.
- Cards missing candidate/team/person imagery or polished fallback visuals.
- The run producing only politics results while the UI claims to be using all categories.
- General/all-category search being available before category-specific models exist.
- Early Marks visual design feeling disconnected from the rest of the app.

### `combo-correlation-checker`

Purpose:
Review multi-leg combo/parlay-style Kalshi ideas and identify correlation, hidden duplicated risk, and payout logic.

Must output:

- each leg separately
- price/cost per leg
- combined cost
- max payout
- correlation notes
- whether legs are independent, mildly correlated, or strongly correlated
- what could make all legs fail together
- approval checklist before order placement

### `market-history-backtester`

Purpose:
Use downloaded Kalshi market history to test whether an entry/exit rule or analyst process would have worked historically.

Must support:

- walk-forward testing
- cutoff dates
- hidden future data
- Brier score
- log loss
- calibration
- closing-line comparison
- false-positive catalyst tracking

Must avoid:

- tuning rules to one market
- changing rules after seeing the answer
- treating one correct prediction as proof

### `fair-probability-builder`

Purpose:
Build transparent estimated fair probabilities for Kalshi sports markets.

Must combine:

- market-implied probability
- sports stats
- expert/market odds
- schedule context
- injuries/lineups
- model assumptions
- uncertainty bands

Must output:

- fair probability
- market price
- edge
- confidence
- factor scores
- main assumptions
- what would change the probability

### `trade-card-generator`

Purpose:
Turn a market idea or position into a clean trade card.

Trade cards must show:

- market
- side
- cost
- odds/price
- payout
- status
- confidence
- fair probability
- edge
- main catalyst
- source summary
- risk notes

Must support:

- single trades
- combo/parlay-style trade cards
- owned positions
- watch-only ideas

### `news-to-market-impact`

Purpose:
Take news, injury reports, odds movement, schedule updates, or other external information and identify which Kalshi markets might be affected.

Must output:

- news item
- affected market(s)
- likely direction
- magnitude estimate
- confidence
- timing
- source
- whether the market has already moved

## Final Execution Requirements

After defining and improving these skills, execute the implementation instead of stopping at a proposal.

Specifically:

- identify overlaps between skills, then resolve them through shared references, scripts, or merged responsibilities where appropriate
- create a clean folder/resource structure
- create test artifacts for each implemented skill or shared script
- choose a build order, then implement the first coherent slice
- make the skill descriptions trigger-friendly for Codex/Fable
- keep the skills practical for my Kalshi sports prediction assistant
- flag any math/modeling feature that may not add value, then either remove it, downgrade it to explanatory-only, or add a baseline test that can prove its usefulness
- explain how each implemented test would prove or disprove that the feature is useful
- run available validation commands
- finish with a completion report, not suggestions
