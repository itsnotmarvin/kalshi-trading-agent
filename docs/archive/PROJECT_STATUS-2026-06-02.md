# Archived Project Status — June 2, 2026

> Historical snapshot only. Counts, paths, open issues, and implementation
> claims below may no longer describe the current repository. See the active
> project `README.md` and current code for the maintained description.

# PROJECT STATUS — Kalshi Prediction Market Trading Agent

> **Last updated:** 2026-06-02<br>
> **Codebase root:** `kalshi/`<br>
> **Branch:** `main`<br>
> **Test suite:** 32 collected, 32 passing
> **Focus:** Kalshi weather markets — paper validation phase

---

## WHAT THIS IS

An **autonomous prediction-market trading bot** powered by Claude (Anthropic), built specifically for **Kalshi**. It scans for mispriced contracts, uses Claude for probability estimation + web research, passes proposals through a 22-check risk manager, and executes paper or live trades. Runs as a FastAPI web server with a dashboard UI.

**Current operational mode:** `weather_paper` — collecting resolved real Kalshi weather trades to validate the strategy before going live. Paper/shadow mode uses production Kalshi market data and skips order placement.

```
┌─────────────────────────────────────────────────────────┐
│                    server.py (FastAPI)                   │
│  ┌──────────┐   ┌──────────────┐   ┌─────────────────┐ │
│  │ Dashboard │   │ REST API     │   │ BotState        │ │
│  │ (HTML)    │   │ /api/start   │   │ (shared state)  │ │
│  │           │   │ /api/status  │   │                 │ │
│  └──────────┘   └──────┬───────┘   └────────┬────────┘ │
│                        │                     │          │
│  ┌─────────────────────▼─────────────────────▼────────┐ │
│  │             trading_loop.py (background)            │ │
│  │  scan → research → propose → risk-check → execute  │ │
│  └────┬──────────┬────────────────────┬───────────────┘ │
│       │          │                    │                  │
│  ┌────▼──┐  ┌────▼─────┐  ┌──────────▼──────────┐      │
│  │Agent  │  │Risk Mgr  │  │ Kalshi Adapter       │      │
│  │(Claude│  │(22 checks│  │ (primary, production)│      │
│  │ brain)│  │ Kelly,   │  │                      │      │
│  │       │  │ R-Score) │  │ Polymarket (read-only│      │
│  └───┬───┘  └──────────┘  │ reference only)      │      │
│      │                    │ Manifold (play money) │      │
│  ┌───▼────────────────┐   └──────────────────────┘      │
│  │ Data Engines       │                                  │
│  │ • WeatherEngine    │ ← primary focus                  │
│  │ • SportsEngine     │                                  │
│  │ • ClimateEngine    │                                  │
│  │ • Forecaster       │                                  │
│  └────────────────────┘                                  │
└─────────────────────────────────────────────────────────┘
```

---

## WHAT'S WORKING ✅

These features are complete, tested where noted, and functional:

### Core Pipeline
- **Full Kalshi trading pipeline** — real-market paper/shadow + live modes, weather-first strategy
- **Claude-powered scanning** — cheaper model for initial screening, deep research with extended thinking + web search (Tavily/DuckDuckGo fallback), up to 15 agentic iterations
- **22-check risk manager** — circuit breakers, Kelly sizing, R-score, fee kill zone, parlay blocking, category caps
- **Real orderbook-based spread check** — `attach_orderbook_metrics()` derives implied asks from Kalshi bid-only books, computes side-specific spread + depth (replaced the old dead `abs(0)` check)
- **Side-correct fee estimation** — `get_side_cost()` returns `(1 - market_price)` for BUY_NO, fixing the old bug where BUY_NO fees were underestimated by up to 4x
- **Kalshi order idempotency** — `client_order_id` support + 409-duplicate reconciliation via `find_order_by_client_id()`
- **`post_only` limit orders** — maker fee rate (0.0175 vs 0.07 taker) applied when `post_only=True`

### Weather Engine
- **GFS 31-member ensemble** probability computation
- **HRRR deterministic** cross-validation
- **NWS forecaster sentiment** analysis (Claude extracts ±15% modifier from Area Forecast Discussion text)
- **Weather validation gate** — stays `PAPER_ONLY` until 50+ resolved trades, `LIVE_REVIEW` at 50, `SCALE_REVIEW` at 100

### Dashboard & UI
- **Main dashboard** (`dashboard.html`) — defaults to `weather_paper`, shows `weather_live` as gated
- **Paper dashboard** (`paper.html`) — restricted to `weather_paper` only, client-side guard blocks other modes
- **Weather validation panel** in the Analyze tab — calls `/api/weather-validation` endpoint
- **Real-time status** — positions, proposals, P&L, balance, logs

### Reporting & Learning
- **Calibration report** — buckets predictions by decile, computes Brier score, breaks down by market family (rain/snow/wind/high_temp/low_temp) and timing (same_day/tomorrow/over_24h), includes ROI after fees
- **Weather validation report** — Kalshi-specific evidence gate with family/timing breakdown
- **Order attempt audit log** — `log_order_attempt()` records client_order_id, status, errors to JSONL for retry auditing
- **Self-improving analyzer** — 5 randomized lenses, anti-hallucination gates, macro-lesson lifecycle
- **Memory system** — micro + macro lessons with persistence
- **Postmortem engine** — learns from settled trades
- **Telegram notifications**

### Backend Safety
- **`weather_live` server gate** — `weather_live_validation_error()` blocks live mode unless validation reaches `LIVE_REVIEW` or `ALLOW_WEATHER_LIVE_BEFORE_VALIDATION=true`
- **Paper trade persistence** — positions saved to disk, survive restarts
- **Paper settlement** — auto-settles resolved positions, books P&L

---

## CURRENT VERIFICATION ✅

### Test Suite

```
32 collected — 32 passed
```

`pyproject.toml` now scopes pytest collection to the automated offline regression files. The older manual/live probe scripts were moved from the project root to `scripts/manual_probes/` so the root only contains real automated tests.

The risk math tests were **not** restored as a separate `test_risk_math.py` file. That coverage was consolidated into `test_review_fixes.py` to reduce top-level test-file sprawl while preserving the checks for R-score, Kelly sizing, Kalshi fee math, side-specific NO pricing, orderbook spread/depth, and WAIT_FOR_ENTRY paper-side recording.

Weather parser/probability/analysis coverage was also kept in the existing `test_weather_learning.py` file rather than split into another test file. It now covers ensemble member counting, below-threshold low-temperature markets, fractional rainfall thresholds, ticker-derived city codes, sports-market rejection, the GFS+HRRR agreement path, and the HRRR veto path.

Weather validation and settlement semantics are covered in existing files too: `test_logger_report.py` verifies that only YES/NO weather settlements count toward validation, that SOLD rows are ignored, that Sports rows stay out of weather validation, and that low-temp/snow/wind/timing buckets are populated correctly. `test_review_fixes.py` verifies settlement P&L for both YES and NO positions.

---

## EXTERNAL RESEARCH — KALSHI-FIRST

The useful outside signal is mostly Kalshi-specific now. Polymarket remains useful only as a microstructure/reference comparison, not as the target architecture.

| Source | What others are doing | What we should take from it |
|--------|------------------------|------------------------------|
| [OctagonAI Kalshi Trading Bot CLI](https://github.com/OctagonAI/kalshi-trading-bot-cli) | Uses independent probability estimates, live orderbook edge, Kelly sizing, and a multi-gate risk engine before execution. | Confirms the direction of our orderbook-aware edge, fee-aware sizing, and risk gate hardening. |
| [Bot for Kalshi backtesting guide](https://www.botforkalshi.com/blog/kalshi-bot-backtest) | Emphasizes historical Kalshi trade data, fee/slippage modeling, partial fills, and spread dynamics before live deployment. | Supports keeping weather in `weather_paper` until resolved-trade evidence exists; our validation gate should stay strict. |
| [Bot for Kalshi product notes](https://www.botforkalshi.com/) | Uses daily loss caps, per-trade stop-losses, position limits, paper trading, and outage-aware pausing. | Our risk manager already covers several of these; outage-aware pausing and clearer stop-loss/take-profit telemetry are good next steps. |
| [PredictionMarketBench](https://arxiv.org/abs/2602.00133) | Evaluates agents with replayed Kalshi orderbooks, trades, lifecycle, settlement, maker/taker fees, and reproducible trajectories. | Reinforces logging order attempts, settlement outcomes, maker/taker fee assumptions, and replayable trade history. |
| [Kalshi weather bot discussion](https://www.reddit.com/r/SideProject/comments/1t8l155/shipped_v21_of_my_kalshi_trading_bot_its_a_twobot/) | Weather bot started with GFS ensemble data, then improved by requiring agreement across multiple forecast families before trading. | Our current GFS + HRRR + NWS sentiment stack is directionally right; next improvement is stricter multi-model agreement rather than more trade volume. |
| [Kalshi weather threshold discussion](https://www.reddit.com/r/Kalshi/comments/1sn15bt/kalshi_weather_bot/) | Other builders are getting tripped up by Kalshi weather API thresholds versus UI bucket labels. | Direct parser/probability/validation tests now cover low-temperature, precipitation, ticker-threshold, sports-rejection, YES/NO-only settlement scoring, and weather-family buckets; UI label fixtures are still useful before scaling size. |

### Known Bugs Still Open

| Bug | File | Status |
|-----|------|--------|
| **`Market.spread` property is tautologically zero** | `adapters/base.py:42-44` | Low impact — property exists but isn't used in risk decisions anymore (the real spread comes from `attach_orderbook_metrics` now) |
| **`evaluate_exits()` wrong side price in CLI mode** | `risk_manager.py:498` | Medium — `main.py` positions from the adapter may not have side-correct `current_price`. The server's `trading_loop.py` handles this correctly. |
| **Silent exception swallowing** | Throughout codebase | High for operability — many `except Exception: pass` blocks hide failures. Locations: `risk_manager.py:88,98`, `kalshi_adapter.py:170`, `memory_manager.py:133`, `postmortem_engine.py:53`, `trading_loop.py:406,444`, `sports_engine.py:446` |
| **`main.py` diverges from `trading_loop.py`** | `main.py` vs `core/trading_loop.py` | The CLI `run_trading_cycle()` is simpler — no weather/sports modes, no settlement, no exploratory sizing, no exit evaluation. Running via CLI gives a degraded experience. |

### Recent Hardening Work

These are the main changes in the current hardening pass:

**Modified files (M):**
- `adapters/base.py`, `kalshi_adapter.py`, `manifold_adapter.py`, `polymarket_adapter.py`
- `config/settings.py` — added `allow_weather_live_before_validation`
- `core/logger.py` — expanded from 172→474 lines (calibration report, weather validation, order attempt logging, stricter weather-only validation filter)
- `core/risk_manager.py` — expanded from 629→759 lines (real spread check, orderbook metrics, `get_side_cost()`, `post_only`/maker fees)
- `core/trading_loop.py` — expanded from 1000→1142 lines
- `dashboard.html` — weather validation panel, mode defaults
- `paper.html` — restricted to `weather_paper` only
- `main.py` — expanded from 474→590 lines (`--weather-validation` flag)
- `server.py` — expanded from 762→794 lines (`/api/weather-validation`, weather-live gate)
- `pyproject.toml` — scopes pytest to automated offline regression tests
- `test_review_fixes.py` — now includes the consolidated risk math/orderbook/paper-side regression coverage
- `test_sizing_engine.py` — compatibility update for the hardened risk interface
- `scripts/manual_probes/` — moved 18 live/manual diagnostic scripts out of the automated test root; includes README and `sitecustomize.py` import bootstrap

**New test files:**
- `test_logger_report.py` (134 lines) — tests calibration report + weather validation gates
- `test_order_idempotency.py` (78 lines) — tests 409-duplicate handling + order attempt logging
- `test_weather_live_gate.py` (60 lines) — tests weather-live evidence gates

---

## TRADE HISTORY

```
Total JSONL rows:     158
├── Scans:            22
├── Analyses:         86
├── Trades:           43  (6 approved, 37 rejected)
├── Resolutions:      7   (all outcome=SOLD, $0 P&L)
└── Order attempts:   0
```

**All 43 trades are Kalshi weather markets** (KX-prefix tickers: KXHIGH, KXLOW, etc.)

**Approved trades (6):**
| Market | Direction | Price | Size | Edge |
|--------|-----------|-------|------|------|
| KXHIGHMIA-26MAR24-T85 | BUY_YES | $0.13 | $5 | 12% |
| KXHIGHDEN-26MAR24-T80 | BUY_YES | $0.23 | $5 | 50% |
| KXHIGHCHI-26MAR26-T77 | BUY_NO | $0.11 | $5 | 18% |
| KXHIGHCHI-26MAR25-T73 | BUY_YES | $0.10 | $5 | 17% |
| KXHIGHMIA-26MAR26-T84 | BUY_YES | $0.11 | $5 | 11% |
| (1 more) | | | | |

**Resolutions (7):** All marked `outcome=SOLD` with `$0.00` P&L — no YES/NO resolutions scored yet, so the **weather validation gate reads 0 resolved weather trades**.

**What this means:** The bot is placing weather paper trades, but none have resolved to YES/NO outcomes yet. The calibration report and weather validation gate are both waiting for market settlements to produce usable data.

---

## MODULE MAP (Current Line Counts)

### Entry Points

| File | Lines | What Changed | Status |
|------|-------|-------------|--------|
| `server.py` | 794 | Added `/api/weather-validation`, weather-live gate | ✅ Complete |
| `main.py` | 590 | Added `--weather-validation` flag | ⚠️ Still diverges from `trading_loop.py` |
| `dashboard.html` | ~115KB | Weather validation panel, mode defaults | ✅ Complete |
| `paper.html` | ~123KB | Restricted to real-market `weather_paper` only | ✅ Complete |

### Core Modules (`core/`)

| File | Lines | What Changed | Status |
|------|-------|-------------|--------|
| `agent.py` | 628 | No changes | ✅ Complete |
| `risk_manager.py` | 759 | Fixed BUG 2 (side-correct fees), real orderbook spread check, `get_side_cost()`, `attach_orderbook_metrics()`, `post_only`/maker fees, `client_order_id` on proposals | ✅ Significantly hardened |
| `trading_loop.py` | 1142 | Audited live-order helpers | ✅ Complete |
| `logger.py` | 474 | Calibration report with Brier/family/timing, weather validation report, weather-only validation filtering, `log_order_attempt()` | ✅ Major expansion |
| `weather_engine.py` | 686 | Exact ticker city-code fallback; safer target-date parsing so threshold phrases like `above 70` are not mistaken for dates; mocked `analyze_market()` agreement/veto tests | ✅ Complete |
| `sports_engine.py` | 484 | No changes | ✅ Complete |
| `climate_engine.py` | 67 | No changes | ✅ Complete |
| `forecaster.py` | 160 | No changes | ✅ Complete |
| `analyzer.py` | 566 | No changes | ✅ Complete |
| `memory_manager.py` | 172 | No changes | ✅ Complete |
| `postmortem_engine.py` | 119 | No changes | ✅ Complete |
| `paper_sizer.py` | 36 | Exploratory paper/shadow sizing | ✅ Complete |
| `trade_utils.py` | 29 | No changes | ✅ Complete |
| `notifier.py` | 23 | No changes | ✅ Complete |

### Adapters (`adapters/`)

| File | Lines | What Changed | Status |
|------|-------|-------------|--------|
| `base.py` | 168 | Minor | ✅ Complete |
| `kalshi_adapter.py` | 620 | `client_order_id` support, 409-duplicate reconciliation, `find_order_by_client_id()`, `_parse_order()` refactor, `post_only` param | ✅ Production-hardened |
| `polymarket_adapter.py` | 237 | No changes | Read-only reference only |
| `manifold_adapter.py` | 203 | No changes | Play money only |

### Configuration (`config/`)

| File | Lines | What Changed |
|------|-------|-------------|
| `settings.py` | 103 | Added `allow_weather_live_before_validation` |
| `prompts.py` | 258 | No changes |

---

## TEST COVERAGE

### Passing Tests (32/32)

| Test File | Tests | What It Covers |
|-----------|-------|---------------|
| `test_agent_init.py` | 1 | Agent construction |
| `test_fix_verification.py` | 1 | Duplicate-market filtering and paper position recording |
| `test_logger_report.py` | 3 | Calibration report with Brier/fees, weather validation 49/50-trade gates, YES/NO-only weather settlements, SOLD exclusion, sports exclusion, weather-family/timing buckets |
| `test_order_idempotency.py` | 2 | 409-duplicate reconciliation, order attempt audit logging |
| `test_review_fixes.py` | 12 | WAIT_FOR_ENTRY mapping, daily loss breaker, category exposure cap, Polymarket live block, R-score, Kalshi fees, Kelly sizing, side-specific NO cost, orderbook metrics, missing metrics rejection, paper-side recording, YES/NO settlement P&L |
| `test_sizing_engine.py` | 2 | Log-uniform distribution bounds, risk manager bypass |
| `test_weather_learning.py` | 8 | Weather humility-buffer math, ensemble probability counting, Kalshi weather-market parsing, low-temperature below direction, fractional rainfall thresholds, exact ticker-city fallback, sports-market rejection, GFS+HRRR agreement and HRRR veto paths |
| `test_weather_live_gate.py` | 3 | Weather-live blocks at PAPER_ONLY, allows at LIVE_REVIEW, allows with explicit override |

### Still Untested

| Module | Priority |
|--------|----------|
| `SportsEngine` | Low (not current focus) |
| `ClimateEngine` | Low |
| `Forecaster` | Medium |
| `Analyzer` (self-improving) | Low |
| `PostmortemEngine` | Low |
| `MemoryManager` | Medium |
| `server.py` endpoints | Medium |
| `trading_loop.py` | Medium |
| `Kalshi weather UI bucket labels` | Medium — parser, mocked `analyze_market()`, weather validation buckets, and settlement scoring are covered; direct fixtures for UI bucket language remain useful before scaling size |

---

## WHAT'S NEXT (Prioritized)

### 🟡 Soon — Validation Phase

1. **Run `weather_paper` and accumulate resolved trades** — the validation gate needs 50+ resolved weather trades to unlock `LIVE_REVIEW`. Currently at 0 resolved. This is the actual next step for the project.
2. **Address silent exception swallowing** — add `logging` module so suppressed exceptions are captured. This will matter a lot when running paper trades unattended.
3. **Add UI bucket-label fixtures for Kalshi weather markets** — verify API strike thresholds and UI bucket language before increasing weather trade size.

### 🟢 Later — Before Going Live

4. **Consolidate `main.py` and `trading_loop.py`** — make CLI call into the trading loop instead of reimplementing it
5. **Clean up `Market.spread` property** — either remove it or make it use `attach_orderbook_metrics` data
6. **Add `MemoryManager` tests** — lifecycle transitions, prompt injection, legacy migration
7. **Rename project directory** — `kalshi` with a space is fragile for scripts and CI

### 🔵 Not Planned (Reference Only)

- **Polymarket adapter** — read-only reference for microstructure research. Not an implementation target.
- **Manifold adapter** — play money only, useful for risk-free testing.

---

## BUGS FIXED IN RECENT HARDENING

These were documented in the old status doc and have been **fixed**:

| Original Bug | Fix Applied |
|-------------|------------|
| **BUG 1: Dead spread check** — `abs(1-x-(1-x))` was always 0 | Replaced with real `attach_orderbook_metrics()` that derives implied asks from Kalshi's bid-only orderbook. CHECK 14 now compares actual `side_spread` against `max_orderbook_spread` threshold, and checks `side_depth_usd` against `min_orderbook_depth_usd`. |
| **BUG 2: Fee-adjusted edge wrong for BUY_NO** — divided by `market_price` instead of `(1-market_price)` | Fixed via `get_side_cost()` which returns the correct per-contract cost for each side. CHECK 20 now uses `side_cost` for contract count estimation. |
| **No order idempotency** — retries could place duplicate orders | Added `client_order_id` to `TradeProposal` and `place_order()`. 409-duplicate responses are reconciled by looking up the existing order via `find_order_by_client_id()`. |
| **No order audit trail** — couldn't trace what actually hit Kalshi | Added `log_order_attempt()` to JSONL logger with client_order_id, status, error fields. |
| **No maker fee support** — always assumed taker rate | `post_only` parameter flows from proposal through to `place_order()`. `calculate_kalshi_fee()` uses 0.0175 multiplier for makers vs 0.07 for takers. |
| **Weather ticker city fallback used substring matching** — `KXSNOWSLC` could be read as New Orleans because `NO` appears inside `SNOW` | Parser now extracts the exact city code after `KXHIGH`/`KXLOW`/`KXRAIN`/`KXSNOW`/`KXWIND` and tests Salt Lake City snow parsing directly. |
| **Weather date parser could treat thresholds as dates** — a phrase like `above 70` could be parsed as a date, which prevented the HRRR veto window from running | Date parsing now accepts `today`, `tomorrow`, or explicit month-name dates only; `analyze_market()` tests cover both HRRR agreement and veto behavior. |
| **Weather validation could include non-weather resolved trades** — a Sports trade family like `sports` was not `other`, so it could slip into the weather validation summary | Weather validation now counts only explicit Weather category, `model_source=weather`, or known weather-family rows; tests prove Sports and SOLD rows are excluded. |
