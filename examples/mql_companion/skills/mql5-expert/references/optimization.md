# Optimization reports, criteria and `.set` files

Lookup tables for the Strategy Tester's *other* output: an optimization is not a
test report but a table of candidates, and the file that reproduces one candidate
is a `.set`. `tester_report.py` parses both; this is the vocabulary behind it.

> Verify anything critical against the MQL5 reference on your own machine
> (`jarvis memory index ./mql5-reference/`, then `knowledge_search`). Terminal
> builds differ, and the wording of MT5's own dialogs changes between them.

## The two files the tester writes

| Run | Report file | Contents |
|---|---|---|
| Single test (`Optimization=0`) | `<name>.htm` (or `.xml`) | One set of `ENUM_STATISTICS` metrics — parse with `parse_report` |
| Optimization (`Optimization=1/2/3`) | `<name>.xml` | A `<Table>` of passes — parse with `parse_optimization` |
| Forward run | `<name>.forward.htm` / `.forward.xml` | The out-of-sample half; the optimization table gains `Back Result` / `Forward Result` columns |

`Report=` in the ini takes a name **without** the extension — MT5 appends it, and
it appends a different one than you wrote. Relative paths resolve against the
platform's *install* directory, and MT5 will not create a missing subfolder. The
files are written in ANSI (cp1251 on a Russian build, cp1252 on an English one),
not UTF-8.

## The optimization table

Row 0 is the header. Ten columns are always there, then one per optimized input:

| Column | Meaning |
|---|---|
| `Pass` | Row number in the terminal's results tab — the id you quote |
| `Result` | The **criterion value** the search maximized, not profit |
| `Profit` | Net profit over the period |
| `Expected Payoff` | Profit per trade |
| `Profit Factor` | Gross profit / \|gross loss\| |
| `Recovery Factor` | Profit / max balance drawdown |
| `Sharpe Ratio` | As the terminal computes it |
| `Custom` | `OnTester()` return value — 0 unless the EA implements it |
| `Equity DD %` | Maximal *relative* equity drawdown |
| `Trades` | Number of trades (deals, in MT5's counting) |
| `<input name>` … | One column per optimized input, with the value used in that pass |

Doubles are written with full precision, so `30151.000000000004` is normal.
A localized terminal writes localized header names; the reader falls back to
column position for the ten fixed ones and keeps the input names as-is.

## `OptimizationCriterion` ↔ what MT5 calls it

`Result` is whatever criterion the run asked for, which is why the top row of the
table is often not the pass you want:

| ini value | MT5's name | Maximizes |
|---|---|---|
| 0 | Balance max | Final balance |
| 1 | Profit factor max | Balance × profit factor |
| 2 | Expected payoff max | Balance × expected payoff |
| 3 | Drawdown min | (100% − drawdown) × balance |
| 4 | Recovery factor max | Balance × recovery factor |
| 5 | Sharpe ratio max | Balance × Sharpe |
| 6 | Custom | `OnTester()` — the EA decides |
| 7 | Complex criterion max | An integral measure: it ranks progressively by number of deals, then expected payoff, recovery factor, drawdown and Sharpe |

MetaQuotes documents the criterion as "required only for the genetic algorithm"
— a slow complete run tests every combination regardless, so the criterion there
decides the `Result` column and the sort order, not which passes get run.
Criterion 7 is the "Complex Criterion max" the Optimization Types page
describes; the config-file documentation lists it as `7 — the maximum of complex
criterion`.

**The `Maximizes` column above is the config-file documentation's, and that page
has not kept up with the terminal.** Build 2530 changed the criteria that mixed
two variables: "Now, the criteria only take into account the second variable and
ignore the balance" — Balance + Maximum Profitability became Maximum
Profitability, and the same for expected payoff, drawdown, recovery factor and
Sharpe ratio (MetaQuotes' release notes for that build). So on any current build
the `Result` column *is* the metric — a profit factor of 1.4, not
balance × 1.4 — and the product form survives only in the older wording. Which
one a report follows shows in the numbers: a `Result` column in the same range as
`Profit Factor` is a post-2530 run, one in the thousands is not. Comparing
`Result` across two reports from builds either side of 2530 compares different
quantities.

## What the analysis checks, and what to do

| Warning fragment | It means | Right response |
|---|---|---|
| `the best pass traded N time(s)` | Below ~30 trades every ratio in that row is noise | Say the pass is unusable; do not quote its profit factor |
| `x the median of the next N passes` | A spike, not a plateau | Prefer a pass whose neighbours also work |
| `only K of M passes land within 10%` | The optimum is isolated | Widen the grid or question the strategy |
| `<input> sits at the start/stop of its tested range` | The real optimum is outside the grid that ran | Widen that range and re-run — never just take the pass |
| `every top pass uses <input>=<value>` | The other values never made the top | Confirm the input matters; drop it if it does not |
| `ranked by result the winner is A; ranked by <x> it is B` | Criterion mismatch | Ask which criterion the user optimizes for |
| `forward` + `median_degradation_pct` | Out-of-sample is worse than in-sample | Quote the forward numbers, not the back ones |
| `spearman_back_vs_forward` near 0 | In-sample rank does not predict out-of-sample rank | Selecting the best pass was close to selecting at random |

The default filters are the five MT5 offers in its own Optimization Results tab —
they arrived in build 2530, whose release notes list them as passes without
trades, loss-making passes, drawdown greater than 50%, recovery factor less than
1 and Sharpe ratio less than 0.5. A rule fires only on a value the file actually contains — a pass with no
trade count is not dropped for having a bad one.

## `.set` file format

One input per line, in MT5's five-field form:

```ini
InpStopLoss=30||20||5||60||Y
InpLots=0.10||0||0||0||N
InpUseTrailing=true||false||0||true||N
```

| Field | Meaning |
|---|---|
| value | The EA's default for this run |
| start / step / stop | The optimization range; `step=0` means "not swept" |
| optimize | `Y` = sweep this input, `N` = hold it fixed |

- **Booleans** use the literal words: `true||false||0||true||N`. The reader
  treats only `true`/`false` as boolean, because MT4-style files use `1`/`0` for
  integers and guessing misclassifies them.
- **Pseudo-entries** `_EA_IDENTIFIER` and `_EA_MAGIC_NUMBER` may appear; they
  are not inputs and are preserved untouched on a round-trip.
- **MT4's comma form** (`name=value,start,step,stop,Y`) is accepted when reading;
  writing always produces the `||` form MT5 expects.
- Unrecognized lines are preserved rather than dropped — a `.set` gets
  round-tripped into the terminal, so silently losing a line changes the EA.

**Where the file must live:** `ExpertParameters=` takes a *file name* that MT5
resolves inside `MQL5\Profiles\Tester\`. MetaQuotes' wording for that folder is
"the platform installation directory"; in portable mode the installation
directory *is* the data folder, and in the normal (main) mode the profiles are
editable files, which the same help page puts in the data folder. Rather than
reason about which mode a machine is in, open **File → Open Data Folder** in the
terminal and put the `.set` next to the ones already in `MQL5\Profiles\Tester`.
A full path does not work. If no `.set` is found, MT5 does not optimize at all — it loads the EA's
compiled defaults and reports "Optimization is not possible". With no
`ExpertParameters` at all it falls back to
`MQL5\Profiles\Tester\<EA name>.set`.

**Why `set_from_pass` needs the template:** a pass row lists only the inputs that
were *optimized*. Writing a `.set` from the row alone leaves every input the run
held fixed out of the file, so the terminal falls back to the EA's defaults and
the re-test measures a different strategy. Merging the original `.set` carries
those fixed inputs across; `--keep-ranges` leaves the grid intact for a second
optimization around the winner.

## ini keys for an optimization run

The values below are MetaQuotes' own, from the terminal help's start-up options
(`metatrader5.com/en/terminal/help/start_advanced/start`) and the optimization
pages it links to. `tester_report.py` keeps the same two tables as
`TESTER_MODELS` and `OPTIMIZATION_MODES` — if this page and that code disagree,
one of them has drifted.

| Key | Notes |
|---|---|
| `Expert`, `ExpertParameters` | EA name; `.set` **name** resolved in `Profiles\Tester` |
| `Symbol`, `Period`, `FromDate`, `ToDate` | Period as `M1`/`H1`/`D1`…; dates as `YYYY.MM.DD` |
| `Login` | An account number the EA can read through `AccountInfoInteger`. It does not log the terminal in |
| `Model` | 0 every tick · 1 one-minute OHLC · 2 open prices only · 3 math calculations · 4 every tick based on real ticks. MetaQuotes calls **0** "the most accurate but the slowest"; 4 replays recorded broker ticks and its *first* run on a symbol spends a long time downloading them. 3 downloads no history and calls only `OnInit`/`OnTester`/`OnDeinit` |
| `Optimization` | 0 = off · 1 = slow complete · 2 = fast genetic · 3 = all symbols in Market Watch. With 3 only the main symbol changes per pass — inputs are not swept, and the MQL5 Cloud Network is not used |
| `OptimizationCriterion` | 0–7; see the table above |
| `ExecutionMode` | 0 = no delay (what the help calls "ideal" conditions) · −1 = random delay · `>0` = fixed delay in ms (≤ 600000) |
| `ForwardMode`, `ForwardDate` | 0 = off · 1 = 1/2 of the period · 2 = 1/3 · 3 = 1/4 · 4 = a custom split that takes its start date from `ForwardDate`, which is read only when `ForwardMode=4` |
| `Report`, `ReplaceReport` | Name without extension; `ReplaceReport=1` overwrites |
| `ShutdownTerminal` | 1 = close MT5 when the run ends (the terminal exits *after* writing the report) |
| `Deposit`, `Currency`, `Leverage` | Account context |
| `UseLocal`, `UseRemote`, `UseCloud` | Where passes are computed |
| `ProfitInPips` | **Not a display setting.** Calculating profit in pips skips the conversion into the deposit currency — and with it swap and commission — and margin is not controlled. MetaQuotes: "only use it for quick and rough strategy estimation". A report from such a run is not comparable with one from a normal run |
| `Visual`, `Port` | UI and agent details |
| `Dates` | Seen in ini files the terminal wrote; not in MetaQuotes' list, so do not rely on it |

## Grid size

Multiply the number of steps per swept input (`--set grid.set` prints it). A few
thousand is a genetic run; over ~100k is a plan for next month, and the reader
says so. Cutting the grid is not cheating — an input that never changes the
outcome is an input to delete from the EA.

## Forward runs

`ForwardMode` splits the period: MT5 optimizes on the back half, then re-runs the
winner on the forward half — dates the search never saw. It is the only setting in
the tester that can answer "did this survive new data?" rather than "how well did
this fit?".

| Run | Files written |
|---|---|
| Single test with `ForwardMode` | `<name>.htm` and `<name>.forward.htm` |
| Optimization with `ForwardMode` | one `<name>.xml` whose table gains `Back Result` and `Forward Result` columns |

**Not every pass gets a forward number.** In a forward *optimization* MT5
optimizes on the first part of the period, then re-runs only the best **10%** of
passes (slow complete) or **25%** (genetic) on the forward part. So those two
columns are populated for a slice the platform picked *because it already won in
sample*, and `median_degradation_pct` and `spearman_back_vs_forward` describe
that slice rather than the run — a rank correlation over the top 10% has very
little in-sample spread left to correlate. `analyze_optimization` reports how
many passes it could pair (`forward.passes` against `forward.total_passes`) and
warns when that is a fraction of the table; quote the forward figures as a
statement about the winners, not about the strategy.

The forward file is matched **by name**: a `.forward.` report from another run in
the same folder is not this run's out-of-sample half, and pairing them would
compare two unrelated tests. `mt5_tester_forward_check` therefore returns
`available: false` when the companion is missing rather than reaching for the
newest forward file it can find. Passing the forward half as the report swaps in
its back companion, since the forward file is usually the newer of the two.

### Verdicts

| Verdict | Earned by |
|---|---|
| `holds_up` | No sign flip, and no gate ratio (profit factor, recovery factor, Sharpe) or profit-per-day down more than `max_degradation_pct` (50 by default) |
| `degrades` | Profit factor crossed 1, profit or expected payoff crossed zero, or a gate ratio fell past that threshold |
| `inconclusive` | A half traded fewer than `min_trades` (30), the two reports share no metric, or there is no forward file |

A `degrades` verdict wins over a thin sample — thinness is not an alibi for a loss
— but a thin sample on its own produces `inconclusive`, never `holds_up`.

### Normalization

The forward half is usually a fraction of the back half, so:

| Metric kind | Compared how |
|---|---|
| Ratios and per-trade figures (profit factor, recovery factor, Sharpe, expected payoff, win rate, drawdown %) | As written — they mean the same thing over a quarter as over a year |
| Money and counts (net profit, gross profit, trades) | Divided by each half's length in days, from the report's own from/to dates |
| Anything, when a report has no dates | Ratios only, with a warning that the money is not normalized |

Drawdown growth, history quality under 90%, and a forward half longer than the back
one are warnings rather than verdicts: they say the forward half was harder, not
that the parameters broke.

## The workflow that does not lie to the user

1. Optimize (`Optimization=1/2` + `OptimizationCriterion`), with a `.set` in
   `MQL5\Profiles\Tester`.
2. Read the ranked passes **and the warnings** — the warnings are the finding.
3. Take the `.set` for one pass (`--set-from-pass N`, or `set_from_pass` over
   MCP) and save it in `MQL5\Profiles\Tester`.
4. Run **one** test with `Optimization=0` and gate that report
   (`min_profit_factor`, `max_equity_drawdown_pct`, `min_trades`,
   `min_history_quality_pct`). A pass is what the optimizer measured once; the
   single test is the number you can act on.

A forward split (step 4 with `ForwardMode`) is the version of this that survives
contact with new data: pick on the back half, report the forward half.
