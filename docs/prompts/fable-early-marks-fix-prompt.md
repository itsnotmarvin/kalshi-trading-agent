# Fable Prompt: Fix Early Marks Page And Workflow

Fix the Early Marks page/workflow in the Kalshi project. This is not a planning-only task. Inspect the existing code, implement a coherent first slice, run available validation, and finish with a completion report.

Do not end with suggestions. If the full scope is too large, complete the highest-impact slice that removes the current broken behavior.

## Context

Early Marks is meant to find early or recently opened markets that are still relatively uninfluenced, where current odds are low or spread across many outcomes, but future catalysts could create meaningful movement.

For V1, Early Marks should not pretend to support unbiased all-category discovery. The current behavior is unintentionally politics-heavy because politics is the best-developed category. Be honest in the UI and model flow.

## Current Bugs To Fix

1. Before pressing `Generate`, sections such as Ranked Marks, selected-card details, and model/catalyst text are already populated with stale or sample information.
2. After pressing `Generate`, the page shows raw probe/debug text instead of structured user-facing progress and results.
3. Cards do not include candidate/team/person images.
4. Raw Kalshi technical identifiers such as `KXPRESPERSON-28-JOSS` appear in the UI. These must not appear anywhere in the normal user-facing page.
5. The output is politics-only while the UI implies `All categories` or broad/general discovery.
6. `All categories` and generic `General` mode are available even though category-specific models do not exist yet.
7. The page can show `Complete` or old results in a way that makes it unclear whether the latest generation actually ran.
8. Early Marks does not visually match the desired direction for the app.
9. There is no clear API/service readiness display before running Generate.
10. Run History is too prominent and should be collapsed at the bottom.

## Required Behavior

### UI State Contract

Define and implement clear UI states instead of letting old data bleed between states:

- `Not run yet`: controls and service health only, with empty result panels.
- `Checking services`: API chips update and generation is not yet producing cards.
- `Generating`: staged loader is visible, old current-run cards are cleared or moved to history.
- `Complete`: only shown after the latest run has returned structured results.
- `Partial`: shown when some APIs fail but enough data exists to render a limited result.
- `Error`: shown when generation cannot produce trustworthy results.
- `Offline`: shown when core services are unavailable.

Each state should have a visible, human-readable message. Do not reuse previous successful results as the current result during loading, partial, error, or offline states.

### Empty Initial State

Before the first `Generate` click:

- Do not show pre-filled Ranked Marks counts.
- Do not show selected mark details.
- Do not show stale model/catalyst text.
- Do not show old cards as if they are current.
- Empty boxes are better than outdated information.
- Show controls, API/service health, category/model availability, and an honest empty state such as `No scan run yet`.

### Generate Flow

When the user presses `Generate`:

- Start a fresh run.
- Show staged progress instead of raw logs.
- Use a loading spinner similar to a segmented circular loader.
- Use clear steps such as:
  - `Connecting to Kalshi`
  - `Checking service health`
  - `Pulling markets`
  - `Checking category model`
  - `Ranking early marks`
  - `Enriching with research`
  - `Rendering cards`
- Only show `Complete` after the latest run has actually completed.
- Convert probe output into structured cards/summaries.
- Do not dump raw serialized probe/debug text into the visible page.

### API / Service Health

Automatically check service health on page load.

Show visible status chips before generation for:

- `Kalshi API`
- `LLM API`
- `OpenAI API` when configured
- `Research/Search API`
- `Local server`
- `Price history / data cache`

Each should show a clear state such as:

- `ONLINE`
- `OFFLINE`
- `API OK`
- `API ERROR`

The user should know before pressing `Generate` whether the system is likely to produce the best answer.

If a service is offline or degraded, the Generate button should either be disabled or clearly labeled as producing a limited result. Do not silently run a degraded scan as if everything is healthy.

### Category Rules For V1

For V1, do not allow `All categories` or generic `General` mode.

Instead:

- Show a category selector containing only categories with working category-specific Early Marks logic.
- If politics is the only reliable category today, show only `Politics`.
- Display category quality/win-rate when available, for example: `Politics — ##% win rate`.
- Be honest that other categories are not ready yet.
- Detect category concentration. If a run becomes unintentionally focused on one category or market family, either block broad output, diversify using category-specific models, or show a clear warning.

### Candidate Cards

Cards should be user-facing and clean.

Each card should show:

- candidate/team/person image from web/API when available
- polished category fallback visual when no image exists
- human-readable market name
- watched side/outcome
- current price
- price distribution context
- future catalyst path
- estimated runway
- liquidity/spread notes
- status: `Watch only`, `Catalyst setup`, `Too early`, or `Reject`
- reason it is or is not worth monitoring
- whether it is new, previously seen, updated, or suppressed as a duplicate

Do not show raw ticker strings or internal IDs in normal UI. Raw IDs should be completely hidden from normal users and kept only in internal data/code if needed.

Image rules:

- Try candidate/team/person images from available APIs or web-enriched data.
- Cache or store image URLs when practical so cards do not flicker between runs.
- If no image is available, use a category-specific fallback visual.
- Do not leave broken image icons, blank image slots, or layout gaps.
- If image lookup fails, the card should still look intentional.

Copy rules:

- Keep card copy short enough to scan.
- Move long catalyst reasoning into an expandable detail area.
- Do not show one giant paragraph of model text.
- Use labels and fields instead of raw prose dumps.
- Surface uncertainty honestly: `under-sourced`, `thin liquidity`, `watch only`, or `category model limited`.

### Run History

Run History should be all the way at the bottom of the page.

Behavior:

- Collapsed by default.
- Label it like `Run History ^` or a similar compact expandable control.
- When the user clicks the caret/control, show summarized prior runs.
- When the user presses `Generate` again, collapse the previous generation into Run History instead of mixing it into the current result list.
- Keep summaries compressed and human-readable.

Example summary:

```txt
On {date}, suggested watching {trade} for {target/outcome} because {reason}.
```

De-duplicate across generations by stable market identity such as market id plus side/outcome. If a new run finds an existing candidate, update or annotate the existing candidate rather than showing duplicate main cards.

## Visual Direction

Redesign the Early Marks page rather than lightly recoloring it.

Use this as visual inspiration, not something to copy exactly:

https://dribbble.com/shots/26372656-AI-Helper-for-Smarter-Crypto-Investing-and-Trading

Desired feel:

- polished AI trading assistant
- premium but readable
- clear data cards
- status-rich panels
- modern loading states
- compact enough for repeated analysis
- visually connected to the rest of the Kalshi app

Avoid:

- raw debug-looking blocks
- stale placeholder text
- generic AI dashboard styling
- unreadable decorative effects
- overusing one color
- hiding critical trade values

Preserve usability over decoration. If a visual effect makes prices, catalysts, status, or API health harder to read, remove the effect.

## Data And Privacy Contract

Keep a strict separation between internal data and user-facing display:

- Internal identifiers may exist in API calls, JSON, logs, and data attributes only when necessary.
- User-facing text must use human-readable names.
- Debug output must not render in normal UI.
- Developer diagnostics, if needed, should require an explicit developer-only mode or server log.
- Do not leak API key status details beyond simple connected/error indicators.

## Implementation Requirements

Inspect the relevant files before editing. Likely files include:

- `kalshi-agent/early_marks.html`
- `kalshi-agent/server.py`
- Early Marks probe/sweep scripts under `kalshi-agent/scripts/manual_probes/`
- JSON outputs under `kalshi-agent/data/`

Implement the fix in the actual codebase.

Do not modify live trading behavior.

Do not expose raw internal identifiers in the UI.

Do not make unrelated refactors.

## Validation

Run whatever validation is available and practical, such as:

- HTML/static sanity checks
- app server smoke test
- Early Marks endpoint/test call if available
- screenshot or browser check if available
- existing relevant tests

### Review The Early Marks Output

After implementing the UI/workflow fix, run or simulate an Early Marks generation and review the actual output, not just whether the page renders.

The review should check:

- Are the generated candidates actually early/uninfluenced markets with future catalyst paths?
- Does the output avoid pretending to be all-category discovery when only politics is reliable?
- Are stale/sample values gone before the first run?
- Are raw tickers/internal IDs hidden from normal UI?
- Are candidate cards human-readable without debug/probe text?
- Are images or polished fallbacks present?
- Is the status label justified: `Watch only`, `Catalyst setup`, `Too early`, or `Reject`?
- Is the catalyst explanation specific enough to be useful?
- Is liquidity/spread/exitability visible enough to avoid fake opportunities?
- Is the run history summary compressed and understandable?
- Does the output over-repeat the same market family, such as 2028 presidential candidates, without a warning or category constraint?

Include a short output-review section in the completion report:

- `Output quality: pass/fail`
- `Remaining output issues: ...`
- `Category concentration: ...`
- `Raw identifier leakage: yes/no`
- `Stale initial state: yes/no`

### Screenshot / State Review

If browser tooling is available, capture or inspect at least these states:

- before first Generate
- during Generate
- after a successful Generate
- Run History expanded
- degraded/offline service state if it can be simulated safely

Review each state for:

- no overlapping text
- no broken images
- no raw identifiers
- readable status chips
- clear current-vs-history separation
- mobile layout preserving critical values

Verify these acceptance criteria:

- Before Generate, the page shows no stale result cards/text.
- API/service status is visible on page load.
- Empty/loading/complete/error/partial states are distinct and cannot show stale current-run results.
- Generate shows staged progress.
- Results render as structured cards.
- Generated results pass the Early Marks output review or any remaining output quality issue is named clearly.
- Raw tickers/internal IDs do not appear in normal UI.
- Broken images or blank image slots do not appear in normal UI.
- `All categories`/`General` are unavailable in V1.
- Run History is collapsed at the bottom.
- Pressing Generate again moves old run into compressed history.
- Cards include images or polished fallbacks.
- The final answer is a completion report, not suggestions.
