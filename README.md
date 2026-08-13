# Kalshi Research and Execution Agent

This project is an experimental system for researching Kalshi markets, turning
the research into structured trade proposals, and deciding whether those
proposals are safe enough to reach paper or live execution. It is designed to
make each stage inspectable: the market data used, the sources consulted, the
probability estimate, the executable quote, the fee-adjusted gap, the risk-gate
decision, and the resulting fill or rejection are recorded separately.

Paper mode is the default. It reads production market data but does not submit
orders. Live mode exists, uses real money, and requires an explicit typed
confirmation in the command-line flow. Nothing in this repository establishes
that the system is profitable or that its probability estimates beat the
market.

## What the system does

The general pipeline is:

```text
Kalshi market scan
        ↓
LLM selection and source-backed research
        ↓
structured probability and trade proposal
        ↓
deterministic pricing, liquidity, and portfolio checks
        ↓
paper record or idempotent live-order attempt
        ↓
fill verification, settlement, calibration, and postmortem data
```

The repository currently contains four related workflows:

- **General market agent:** scans Kalshi markets, lets the configured model
  select candidates, researches them with tool calls, and produces structured
  proposals. OpenAI, Anthropic, and a safe `mock` provider are supported.
- **Weather workflow:** parses supported weather contracts, requests explicit
  Open-Meteo GFS ensemble and HRRR model data, converts ensemble members into a
  probability distribution, and records every analyzed forecast so it can be
  scored after settlement. NWS products are used as additional context rather
  than silently substituted for the requested numerical model.
- **World Cup assistant:** evaluates markets through a separate real-data-only
  service. Missing required inputs cause a visible failure instead of a
  placeholder recommendation. Trade cards, quote-freshness checks, approval
  locks, and paper approvals are exposed through the dashboard API.
- **Early Marks:** a staged pipeline for finding markets that have not yet
  reacted to something that should move them. A credential-free collector
  snapshots the full market universe into SQLite (category-blind,
  coverage-audited, deduplicated to material changes); tri-state evidence
  functions turn that history into claims — response
  `observed_flat | moved | unknown`, attention `low | normal | high |
  unknown` — with missing data always `unknown`, never evidence; a shortlist
  script surfaces structural candidates; Stage 2A research verdicts are
  recorded append-only, with "no relevant influence" as a first-class
  answer; and a pre-registered evaluation spec grades the premise against
  matched controls with pre-committed kill dates. The premise itself is
  under evaluation until September 15, 2026, and nothing model-shaped gets
  built unless it passes. The binding contracts live in `docs/`.

The codebase also contains climate, sports, BTC-mode, calibration, postmortem,
and paper-analytics components. The Kalshi adapter is the developed execution
path. Polymarket and Manifold adapters remain experimental; Polymarket live mode
is explicitly rejected by configuration validation.

## How a proposal is gated

The language model can recommend a trade, but it cannot bypass the deterministic
risk layer. Before execution, the system checks the proposal against rules that
include:

- executable direction and quote availability;
- probability gap after confidence adjustment and estimated entry fees;
- minimum market volume, maximum spread, and minimum displayed order-book depth;
- per-trade, total-portfolio, category-exposure, and concurrent-position caps;
- daily loss, consecutive-loss, and persisted circuit-breaker state;
- weather-validation requirements for weather live mode;
- deterministic `client_order_id` generation so a retry of the same thesis does
  not create a second position; and
- adapter-reported fills, so an accepted but unfilled order is not counted as a
  completed trade.

These checks limit known execution and portfolio risks. They do not validate the
underlying forecast, guarantee a fill, or guarantee a positive return.

## Probability and pricing contract

Research factors are not added directly to a probability. Supported adjustments
are applied in log-odds space, or the deterministic model is rerun with one
sourced input changed:

```text
logit(p) = ln(p / (1 - p))
final_logit = base_logit + sum(sourced_factor_weights)
final_probability = 1 / (1 + exp(-final_logit))
```

The market midpoint and executable price serve different purposes. The midpoint
approximates the market's current belief; the relevant ask is the price that can
actually be crossed. The risk gate compares the model probability with the
market midpoint, scales the gap by confidence, and subtracts the estimated fee:

```text
probability_gap = model_side_probability - market_side_probability
confidence_adjusted_gap = probability_gap * confidence_multiplier
fee_adjusted_gap = confidence_adjusted_gap - estimated_entry_fee_fraction
```

The implementation models Kalshi's configured maker/taker fee formula and uses
the live quote for sizing. Kalshi's own order preview remains the authority for
the final cost, fee, payout, and order state.

Research citations are also gated. The agent records the URLs returned by its
search tools and requires cited URLs in the proposal to match that observed
source set. A citation that the tool did not return causes the proposal to fall
back to `HOLD` instead of being treated as verified research.

## Safe setup

Python 3.11 or newer is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Start with `LLM_PROVIDER=mock` and `TRADING_MODE=paper`. Mock mode makes no paid
model calls and only returns `HOLD` proposals. Reading authenticated portfolio
data or placing Kalshi orders requires a Kalshi API key ID and private key.

Keep the private key outside the repository and point to it by absolute path:

```text
KALSHI_API_KEY_ID=your_key_id
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi_private_key.pem
```

The project ignores `.env` files, common private-key extensions, runtime state,
JSONL logs, caches, and local virtual environments. `.env.example` contains
names and safe defaults only. Before sharing a branch, still inspect the staged
diff and run a secret scanner; ignore rules do not erase a secret that was
already committed.

## Running the project

Run one paper cycle against current market data:

```bash
python main.py --mode paper --once
```

Run the continuous paper loop:

```bash
python main.py --mode paper
```

Inspect stored state without starting the loop:

```bash
python main.py --status
python main.py --calibration
python main.py --weather-validation
```

Collect one Early Marks snapshot of the market universe. This requires no
API credentials — it reads public market data only:

```bash
python scripts/collect_snapshot.py
```

Inspect the current Early Marks shortlist, record a Stage 2A research
verdict, or generate the premise-check report:

```bash
python scripts/early_marks_shortlist.py
python scripts/record_verdict.py --help
python scripts/early_marks_premise_report.py
```

Scheduling is deliberately OS-level rather than part of the application:
run the collector every ~30 minutes and the premise report weekly with
launchd, cron, or any scheduler. Each run is a single idempotent process,
and gaps in uptime degrade coverage honestly (more `unknown` evidence)
rather than corrupting anything.

Start the local dashboard and API:

```bash
python server.py
```

Then open `http://127.0.0.1:8000`. The server also provides `/paper`,
`/world-cup`, and `/early-marks` views. Mutating API requests require the local
dashboard token injected into those pages. Set `DASHBOARD_API_TOKEN` when a
stable token is needed; otherwise the server creates a new one at startup.

Live mode is intentionally not presented as a quick-start step. If it is used,
review the current Kalshi rules and order preview, use restrictive limits, and
understand that software safety checks cannot remove financial risk.

## Repository layout

```text
adapters/               Exchange interfaces and Kalshi authentication/execution
config/                 Environment settings, paths, and model prompts
core/                   Research, forecasting, risk, execution, and evaluation
data/
  reference/            Reviewed inputs that are safe to version
  runtime/              Ignored logs, SQLite state, reports, and paper history
docs/                   Binding Early Marks contracts: the detection spec,
                        the pre-registered evaluation spec, and the Stage 2A
                        research template — read before touching Stage 1
  archive/              Clearly labeled historical project snapshots
  prompts/              Reusable review and research prompts
scripts/
  manual_probes/        One-off diagnostics kept outside automated test discovery
static/                 Shared dashboard styles
tests/                  Automated behavior and regression checks
web/                    Dashboard HTML/JavaScript views
main.py                 Command-line agent loop
server.py               FastAPI dashboard and background-loop API
```

Personal trading logs, learned rules, paper positions, generated reports, and
databases belong in `data/runtime/`. They are deliberately separated from the
project explanation and excluded from version control. `data/reference/` is for
small, reviewed inputs needed to reproduce a workflow.

## Verification: what was checked and what it means

The automated suite is intended to catch implementation regressions, not to
manufacture evidence of trading performance. A pass count is deliberately
not reported here — a count only says the code agrees with itself. What
matters is which behaviors are pinned down:

- **Pricing and risk:** fee math, sizing boundaries, persistent circuit
  breakers, retry idempotency, partial-fill accounting, and the
  source-lineage gate that blocks invented citations.
- **Forecasting:** weather parsing and model provenance, forecast logging,
  and post-settlement scoring.
- **Early Marks store:** snapshot dedup and history reconstruction; the
  missing-data contract (absent fields store as NULL and read as `unknown`,
  never as evidence of a lazy or unwatched market); attention-bucket
  transitions forcing stored rows so stale flow can never testify as
  current; monotonic `last_seen` under overlapping collectors; refusal to
  record zero-market collection runs as success.
- **Early Marks evaluation:** repricing labels judged in log-odds with a
  book-crossing rule, settlement embargo, coverage requirements, `pending`
  (never negative) unelapsed label windows, and verdict-store validation
  that makes "no relevant influence" the cheapest verdict to record.
- **Interfaces:** World Cup probability and API contracts, dashboard
  endpoints, and helper scripts.

Run it with:

```bash
python -m pytest
```

The suite uses controlled inputs and mocked HTTP responses where exact behavior
needs to be reproducible. A passing result means those inputs produced the
expected decisions—for example, an invented citation was blocked, a market with
missing flow data read `unknown` rather than `low` attention, a restarted
risk manager retained its halted state, and a retry reused the same client order
ID. It does **not** mean the estimates were accurate on future events or that a
strategy made money.

There are three separate levels of evidence:

1. **Implementation checks:** deterministic unit and API tests show that the
   code follows its stated contracts on controlled inputs.
2. **Integration checks:** live read-only probes can show that external APIs
   currently return the expected schemas and requested model families. These
   are environment- and network-dependent, so they are not presented as proof
   of forecast quality.
3. **Outcome validation:** forecasts must be locked before resolution, then
   joined to actual outcomes and contemporaneous market prices. Calibration,
   Brier/log scores, coverage, fill quality, fees, slippage, and net P&L need
   enough out-of-sample observations before any claim of predictive or trading
   edge is justified.

That last level is the one that answers whether the system works in reality.
Passing code checks is necessary, but it is not a substitute for it.

## Scope and responsibility

This is research software, not financial advice. Platform availability,
eligibility, contracts, fees, and API behavior can change. Verify current terms
with the platform and comply with the rules that apply to you before connecting
credentials or enabling live execution.
