---
name: mql5-expert
description: Write, review, port, and debug MetaTrader 5 Expert Advisors in MQL5 — correct event handlers, indicator handles, CTrade order flow, fixed-fractional lot sizing, and a compile-fix loop against MetaEditor.
license: Apache-2.0
compatibility: Requires MetaEditor 5 (metaeditor64.exe) on Windows, or MetaTrader under Wine on Linux/macOS, for the compile step. Review and rewrite steps work anywhere.
allowed-tools: file_read, file_write, apply_patch, shell_exec, knowledge_search, think
version: "0.1.0"
author: openjarvis
tags: [coding, mql5, mql4, metatrader, expert-advisor, trading, review]
required_capabilities: [file:read, file:write, code:execute]
---

# MQL5 Expert Advisor skill

Use this skill whenever the task involves MetaTrader automation: writing an
Expert Advisor (EA), an indicator, a script or a library in MQL5/MQL4,
reviewing one, porting MQL4 code to MQL5, or debugging compiler and runtime
errors from MetaEditor.

## The one rule that matters most

**The compiler is the ground truth, not your memory of the API.** MQL5 is a
low-resource language for language models: function names, argument orders and
even which predefined variables exist get hallucinated constantly. So:

1. Never claim an EA "works". Claim it "compiles with 0 errors, 0 warnings".
2. Whenever MetaEditor is reachable, compile and read the log before replying.
   From a repo checkout: `python examples/mql_companion/compile_loop.py
   --source <file.mq5>` runs the whole compile → fix → recompile loop.
3. If you cannot compile, say so explicitly and list which API calls the user
   must verify in MetaEditor.
4. When MQL5 documentation has been indexed into memory, verify every
   non-trivial call with `knowledge_search` before using it (e.g.
   `jarvis memory index ./mql5-reference/`). An unverified signature is a
   compile error waiting to happen.

## Workflow

### Writing a new EA

1. Restate the strategy in one paragraph: entry condition, exit condition,
   risk per trade, filters, and the timeframe it runs on. If any of these is
   ambiguous, ask before writing code.
2. Start from `templates/ea-template.mq5` in this skill directory. It already
   handles the things that are easy to get wrong: indicator handles created in
   `OnInit` and released in `OnDeinit`, a new-bar guard, a spread filter, a
   magic-number check, fixed-fractional lot sizing normalized to
   `SYMBOL_VOLUME_STEP`, and `CTrade` for order flow.
3. Add the strategy logic. Keep `OnTick()` short: filter first, then act.
4. Compile. Fix every error *and* every warning — warnings in MQL5 usually
   mean an implicit conversion that will behave differently than you expect.
5. Report: what the EA does, its inputs, the compile result, and what still
   needs testing in the Strategy Tester.

### Reviewing an EA

Check, in this order, and report findings with line references:

1. **Correctness of the trading flow** — are positions filtered by symbol
   *and* magic number? Is there any path that can open more than one position
   per signal? Are SL/TP always set (a position without a stop is the most
   expensive bug in this domain)?
2. **MQL4-isms in MQL5 code** — see `references/mql4-to-mql5.md`. Bare
   `Ask`/`Bid`/`Point`/`Digits`, `OrderSend` with the MQL4 argument list,
   `OrderClose`/`OrderModify`/`OrderSelect`, `AccountBalance()`,
   `MarketInfo()`, `Time[0]`/`Close[1]` series arrays.
3. **Indicator handle hygiene** — handles created once in `OnInit`, checked
   against `INVALID_HANDLE`, released in `OnDeinit`, and `CopyBuffer` results
   checked for `< 0` (never assume the buffer is full; on the first ticks it
   is not).
4. **Series indexing** — after `CopyBuffer` into a dynamic array, call
   `ArraySetAsSeries(arr, true)` before indexing `arr[0]` as "latest bar".
   Off-by-one here silently trades one bar late.
5. **Money math** — lot size normalized to `SYMBOL_VOLUME_STEP` and clamped to
   `SYMBOL_VOLUME_MIN`/`MAX`; price normalized to `SYMBOL_DIGITS`; stop
   distance checked against `SYMBOL_TRADE_STOPS_LEVEL`; margin verified with
   `OrderCalcMargin` before sending.
6. **Robustness** — `IsTradeAllowed()`/`TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)`,
   requote and requotes handling, `OnTradeTransaction` for fill confirmation
   rather than assuming success, no unbounded loops over `PositionsTotal()`
   while modifying positions (iterate backwards).
7. **Backtest honesty** — flag anything that cannot be tested: martingale/grid
   recovery without a hard equity stop, `MathSrand`-based logic, dependence on
   tick history that the tester will not reproduce.

### Porting MQL4 → MQL5

Follow `references/mql4-to-mql5.md` as a checklist. The three structural
changes that account for most of the work: orders become positions
(`OrderSelect` → `PositionSelect`/`PositionGetTicket`), the trading API becomes
`MqlTradeRequest`/`CTrade`, and predefined price variables become
`SymbolInfoDouble` calls.

### Debugging a compile error

Match the message against `references/compile-errors.md` first — most MQL5
compile errors fall into ~20 patterns. When the message is not there, reason
from the reported `line,column`: MQL5 points at the token where parsing broke,
which is often one or two lines *after* the real mistake (missing semicolon,
unbalanced brace).

## When the terminal is connected

If tools named `mt5_*` are available, the bridge in
`examples/mql_companion/mt5_mcp_server.py` is attached to a running MetaTrader
terminal. Use it instead of assuming:

- `mt5_symbol_info` before writing any order or lot-sizing code — digits,
  point, stops level, contract size, tick size and tick value, volume
  min/max/step, and which filling modes the broker accepts all differ per
  symbol and per account.
- `mt5_calc` to check risk arithmetic. It returns the margin a trade actually
  needs (and per lot) plus the profit at a close price, so a
  `CalcLotByRisk()` can be verified numerically rather than argued about.
- `mt5_rates` to sanity-check a strategy's assumptions against real bars: is
  the stop distance plausible next to the typical H1 range? Does the symbol
  trade in the session the EA runs in?
- `mt5_positions` / `mt5_orders` when reviewing an EA's state handling, so
  magic-number and symbol filtering can be checked against what is really open.

Two rules for that last mile. Every payload from the bridge carries a
`synthetic` flag — when it is `true` you are looking at the stub market, not
quotes, and you must say so rather than presenting numbers as live. And
`mt5_order_send`, if it exists at all, executes on a demo account only: it is
for proving an execution path, never for running a strategy. Strategies run on
the chart, and they get validated in the Strategy Tester.

## Reading a backtest

Compiling proves an EA is well-formed. Whether it is *worth running* is in the
Strategy Tester report, and if `mt5_tester_report` is available you read that
file instead of reasoning about a chart you cannot see.

Call it with no `path` to get the newest report the terminal left behind, or
with the report file the user names. What you get back is the metric set MQL5
documents in `ENUM_STATISTICS`: net profit, gross profit and gross loss,
profit factor, expected payoff, recovery factor, Sharpe ratio, four drawdown
figures per curve, trade and deal counts, win/loss percentages, largest and
average win and loss, the longest streaks, and the test context (symbol,
timeframe, dates, model, deposit, leverage, history quality).

Read it like a risk reviewer, not like a cheerleader:

- **`missing` and `warnings` first.** A metric listed in `missing` is not in
  the file — say so instead of estimating it. A warning means the report
  disagrees with itself (gross loss positive, a profit factor that does not
  match its own gross figures, win and loss percentages that do not sum to
  100, an equity drawdown smaller than the balance drawdown); report the
  mismatch before quoting any number from that file.
- **`history_quality_pct` below ~90%** means the terminal had gaps in its tick
  or bar history. The equity curve was drawn from less data than it looks like;
  say that in the same sentence as the profit.
- **Drawdowns are four numbers, not one.** "Maximal" is the money drawdown at
  its worst plus the percentage at that moment; "Relative" is the worst
  percentage seen plus the money at that moment. Quote the relative percentage
  for risk and the money figure for account impact, and say which is which.
- **Trade count before ratios.** A profit factor of 2.4 on 9 trades is a
  coincidence, not an edge. Pass `min_trades` (or state the count yourself)
  before drawing any conclusion from a ratio.
- **`mt5_tester_compare` for "did my change help?"** Two reports come back side
  by side with deltas and a winner per criterion. A metric with no delta was
  missing from one of the files — that is not "unchanged".
- **Use the thresholds as the gate.** `min_profit_factor`,
  `max_equity_drawdown_pct`, `min_trades`, `min_history_quality_pct` and
  friends make the tool return `thresholds.all_passed`. Prefer stating the
  gate you applied over an unqualified verdict.

`mt5_tester_run`, when it exists, launches the terminal to produce a report. It
takes minutes and closes the terminal when it finishes, so never call it while
the user might have charts open, and never call it just to have a number to
quote — ask first.

If no `mt5_tester_*` tool exists, the same reader is a script:
`python examples/mql_companion/tester_report.py --latest --prompt` (or pass the
report path). Reading the report with `file_read` and interpreting the HTML
yourself is the last resort, and the sign convention is the trap: MT5 reports
gross loss as a negative number, so net profit is gross profit *plus* gross
loss, and the profit factor divides by the loss's magnitude.

## Reading an optimization

An optimization report is a table of *candidates*, not a result. MT5 sorts it by
the criterion you asked for, so the top row is the best number the search could
find on that slice of history — which is exactly what an overfit parameter set
looks like. `mt5_tester_optimization` reads that table, hides the passes MT5
itself hides, ranks the rest, and returns the checks that matter in
`analysis.warnings`.

Column names, the criterion numbers, the `.set` row format and the ini keys for
an optimization run are in `references/optimization.md` — check it before quoting
a number you did not read out of a file.

Read the warnings before the numbers, and never present a pass as the EA's
performance:

- **`the best pass traded N time(s)`** — below ~30 trades every ratio in that
  row is noise. Say so instead of quoting the profit factor.
- **`the top pass is X times the median of the next N passes`** and **`only K of
  M passes land within 10% of the best`** — a spike, not a plateau. Prefer a
  pass whose neighbours also work, and say why you did not take the top row.
- **`<input> sits at the start/stop of its tested range`** — the optimum is
  outside the grid that was optimized. The right answer is "widen this range and
  re-run", not a chosen pass. This check needs the `.set` the run used, so pass
  `set_path` when you have it.
- **`ranked by result the winner is pass A; ranked by sharpe_ratio it is pass
  B`** — the criterion you optimized is not the criterion the user cares about.
  Ask which one they want before recommending a pass.
- **Forward columns** (`Back Result` / `Forward Result`) — trust the forward
  ranking. `median_degradation_pct` and `spearman_back_vs_forward` say whether
  the in-sample order survived out-of-sample data; a rho near zero means
  picking the best pass was close to picking at random.

`rank_by` accepts `result`, `profit`, `payoff`, `profit_factor`,
`recovery_factor`, `sharpe`, `drawdown`, `trades`, `custom`, `back_result` and
`forward_result`. The junk filters are the platform's own (no trades, no profit,
drawdown over 50%, recovery factor under 1, Sharpe under 0.5); `filter_junk`
false exists to inspect what was hidden, not to make a result look stronger.

**Turning a pass into something testable.** `set_from_pass` returns the `.set`
text that reproduces it. Save that with `file_write` into
`MQL5/Profiles/Tester` (the tool writes nothing itself), then run **one** test
with `Optimization=0` and gate that report — the pass is what the optimizer
measured once; the single test is the number you can act on. The `.set` carries
the template's fixed inputs too, because a pass row lists only the inputs that
were optimized and the rest would silently revert to the EA's defaults.

Two `.set` traps worth repeating to the user: `ExpertParameters` takes a file
*name* resolved inside `MQL5\Profiles\Tester`, not a path; and an optimization
with no `.set` at all does not optimize — MT5 falls back to the EA's defaults
and reports that optimization is not possible.

If no `mt5_tester_optimization` exists, the script does the same job:
`python examples/mql_companion/tester_report.py --optimization-report opt.xml
--set grid.set --prompt`, and `--set-from-pass 371 --write-set winner.set` for
the `.set`.

## Passing file paths to this skill

`jarvis skill run mql5-expert -a file_path=...` interpolates your argument
straight into a JSON template, so use forward slashes — Windows backslashes
become invalid JSON escapes and the run fails before it starts:

```bash
# correct
jarvis skill run mql5-expert -a file_path="C:/Users/me/MQL5/Experts/MyEA.mq5"
jarvis skill run mql5-expert -a file_path=/home/me/.wine/drive_c/MT5/MQL5/Experts/MyEA.mq5

# wrong — \M and \E are not valid JSON escapes
jarvis skill run mql5-expert -a file_path="C:\Users\me\MQL5\Experts\MyEA.mq5"
```

MetaEditor itself accepts either separator, so only this argument needs
forward slashes. When you compose your own `file_read` / `apply_patch` calls
as an agent, build the JSON properly (escaped backslashes are fine there).

## Non-negotiables

- Never remove a risk check to make the code compile. Fix the check.
- Never hardcode an account number, server name, or broker symbol suffix
  (`EURUSD.a`, `EURUSDm`) — read symbols from inputs and resolve suffixes at
  runtime.
- Never trade on every tick when the strategy is bar-based; use a new-bar
  guard.
- Never use `Sleep()` in an EA on a real chart (it is ignored in the tester
  for MQL5 EAs and blocks the tick stream otherwise); use timer or bar events.
- Never claim backtest results you did not produce. If `mt5_tester_report` is
  available, read the actual report and quote its numbers with their
  `missing`/`warnings`. If it is not, describe the test the user should run in
  the Strategy Tester (symbol, period, model "Every tick based on real ticks",
  deposit, spread) instead of inventing a result.
- Never report an optimization pass as a backtest. A pass is one row of a
  search, measured once by one criterion: quote it with its pass number, its
  trade count and the warnings from `analysis`, and say whether it was
  re-tested on its own. If it was not, that is the next step, not a footnote.
- Warn about money. Any change to lot sizing, stops, or recovery logic gets an
  explicit note that it must be validated on a demo account first.

## Output format

For a new or rewritten EA:

```
**What it does** — 2-3 sentences.
**Inputs** — table of input name / default / meaning.
**Compile status** — "0 errors, 0 warnings" with the command used, or an
explicit "not compiled: <reason>" plus the calls to verify.
**Risk notes** — anything that can lose money unexpectedly.
**Next test** — the exact Strategy Tester settings to validate it.
```

For an optimization you were asked to interpret: the pass you would take and
why (or why none of them is trustworthy), each quoted warning, and the single
test that would confirm it — `.set` file name, symbol, period, dates, model.

For a review: findings grouped as **Blocker / Bug / Risk / Style**, each with
`file(line,col)` and a concrete fix, then a one-line verdict.
