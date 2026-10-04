# MQL Companion

MetaTrader Expert Advisor development driven by a local model: a compile-in-the-loop
fixer that treats MetaEditor as ground truth, a reader for what the Strategy Tester
produces (backtest reports, optimization passes, `.set` input files), an MCP bridge
that gives an agent both, an installable `mql5-expert` skill, and an eval benchmark
(`mql-bench`) for choosing a model that writes MQL5 instead of MQL4.

## What This Demonstrates

MQL5 is a low-resource language for LLMs. The most common failure mode of a small
local model is not a syntax slip but confidently emitting MQL4 — a bare `Ask`/`Bid`,
an eleven-argument `OrderSend`, `AccountBalance()`, `Close[1]` — which either fails
to compile or, worse, compiles and trades wrongly. This example closes that loop with
the only authority that matters: the compiler.

| File | Purpose |
|---|---|
| `metaeditor.py` | MetaEditor CLI driver — discovery (Windows / Wine / env), UTF-16 log decode, diagnostic parsing, artifact detection |
| `mt5_mcp_server.py` | MCP bridge to a running MetaTrader 5 terminal — live quotes, contract specs, margin/profit maths, tester reports, and gated demo order execution |
| `tester_report.py` | Strategy Tester reader — parses `.htm`/`.xml` reports into metrics, checks CI thresholds, compares runs, ranks optimization passes with overfitting checks, reads and writes `.set` input files, checks a forward run against its out-of-sample half, and can launch a headless backtest or optimization |
| `compile_loop.py` | compile → fix → recompile loop built on the OpenJarvis SDK |
| `install_skill.py` | validate and install `skills/mql5-expert` into `~/.openjarvis/skills` |
| `verify_on_terminal.py` | runs the `REVIEW-NOTES.md` checks against a real terminal and prints what it observed |
| `REVIEW-NOTES.md` | the ten claims only a real terminal can settle, and how to settle each |
| `skills/mql5-expert/` | `SKILL.md` instructions, a 2-step `skill.toml` pipeline, 3 reference docs, an EA template |

Related pieces that live outside this directory:

| Path | Purpose |
|---|---|
| `configs/openjarvis/examples/mql-assistant.toml` | preset config (`jarvis init --preset mql-assistant`) |
| `src/openjarvis/evals/datasets/mql_bench.py` | `mql-bench` dataset — 12 MQL5 authoring and porting tasks |
| `src/openjarvis/evals/scorers/mql_bench.py` | structural scorer (required / optional / forbidden MQL4-isms) |
| `docs/tutorials/mql-companion.md` | full walkthrough |
| `docs/user-guide/mcp-external-servers.md` | how `[tools.mcp]` discovers and wraps external servers |

## Prerequisites

1. **Install OpenJarvis** (from the repository root):

   ```bash
   uv sync --extra dev
   ```

2. **An inference engine running** — Ollama locally (`ollama serve`), or a cloud API
   key in `.env`.

3. **MetaEditor 5** for anything that actually compiles:

   - Windows: a MetaTrader 5 install, e.g.
     `C:\Program Files\MetaTrader 5\metaeditor64.exe`
   - Linux/macOS: MetaTrader under Wine, e.g.
     `~/.wine/drive_c/Program Files/MetaTrader 5/metaeditor64.exe`
     (the `wine` prefix is detected automatically; `--wine` / `--no-wine` force it)
   - Or set `METAEDITOR_PATH` (also `METAEDITOR` / `MQL_EDITOR`) in the environment

Without MetaEditor, `compile_loop.py` exits with code 2 and tells you what to install.
The skill, the preset config, and `mql-bench` all work anywhere.

## Quick Start

### 1. Compile and report only (no model call)

```bash
python examples/mql_companion/compile_loop.py --source MyEA.mq5 --compile-only
```

Useful in CI or under `jarvis scheduler`: exit code 0 means zero errors.

### 2. The full fix loop

```bash
python examples/mql_companion/compile_loop.py \
    --source "<MT5 data folder>/MQL5/Experts/MyEA.mq5" \
    --max-rounds 5
```

Each round compiles, feeds the exact diagnostics back to the agent, snapshots the
file as `MyEA.mq5.round1.bak`, and rewrites it. Add `--json-out report.json` for a
machine-readable round-by-round report.

### 3. Let the agent patch the file itself

```bash
python examples/mql_companion/compile_loop.py --source MyEA.mq5 --mode agent-tools
```

`rewrite` (default) has the model return the whole file and the script writes it;
`agent-tools` gives the agent `file_read` / `apply_patch` / `shell_exec` and lets it
edit in place.

### 4. Install and use the skill

```bash
python examples/mql_companion/install_skill.py --dry-run     # validate first
python examples/mql_companion/install_skill.py               # install
jarvis skill list                                            # -> mql5-expert
jarvis skill run mql5-expert -a file_path=MyEA.mq5
```

> **Use forward slashes in `-a file_path=...`.** The pipeline interpolates your
> argument straight into a JSON template, so a Windows path with backslashes
> (`C:\Users\me\MyEA.mq5`) produces invalid JSON and the run fails before it
> starts. Pass `C:/Users/me/MyEA.mq5` instead. MetaEditor itself accepts either
> separator — only this argument is picky.

### 5. Pick a model with the benchmark

```bash
jarvis eval run -b mql-bench -m qwen3.5:35b --backend jarvis-direct
jarvis eval run -b mql-bench -m qwen3.5:9b -o results/mql_9b.jsonl
jarvis eval compare results/mql_35b.jsonl results/mql_9b.jsonl
```

Scoring is deterministic and structural (no compiler needed): `required` API surface
70%, forbidden MQL4-isms 20%, `optional` best practices 10%.

## How the Compile Loop Works

1. **Find the compiler** — `find_metaeditor()` checks `--metaeditor`, the three env
   vars, `Program Files`, broker-specific install globs, `APPDATA/MetaQuotes`,
   `~/.wine*`, and finally `PATH`.
2. **Build the command** — `metaeditor64.exe /compile:"<src>" /inc:"<MQL5>" /log:"<log>"`
   (plus `/s` for `--syntax-only`), prefixed with `wine` when needed.
3. **Run it** — `compile_source()` waits for the summary line to appear in the log,
   because MetaEditor can return before the file is flushed.
4. **Decode the log** — MetaEditor writes UTF-16LE. `decode_compile_log()` sniffs the
   BOM, then NUL bytes, then falls back to UTF-8 → cp1251 → latin-1. (Order matters:
   ASCII in UTF-16LE *decodes* as valid UTF-8 with interleaved NULs, which silently
   destroys every line.)
5. **Parse diagnostics** — `path(line,col) : severity code: message`, plus the
   tabular shape and the `N errors, M warnings` summary. Exit code is advisory only;
   the parsed summary decides.
6. **Check the artifact** — a clean log with no `.ex5` written gets a `note`
   ("silent CLI failure or stale artifact") instead of a false success. The
   artifact must post-date this run, not merely the source, so a previous
   build's `.ex5` is never credited to a rebuild that wrote nothing.
7. **Distrust a summary that contradicts the log** — it may raise the error
   count, never clear diagnostics the parser found. A log with neither a
   summary nor one parseable diagnostic is a toolchain failure (exit code 2),
   not a clean build.
7. **Fix** — the diagnostics go back to the agent with the current source; the answer
   is extracted from its ```mql5 fence and written back.
8. **Repeat** until zero errors, `--max-rounds` is hit, or the model returns an
   unchanged file (loop guard — it stops rather than burning rounds).

Exit codes: **0** clean compile, **1** still failing after the budget, **2** toolchain
problem (no MetaEditor, unreadable source).

## The `mql5-expert` Skill

`skills/mql5-expert/` is a hybrid skill: instructions plus a small deterministic
pipeline.

- **`SKILL.md`** — `allowed-tools: file_read, file_write, apply_patch, shell_exec,
  knowledge_search, think`; the checklist an agent injects into its context when it
  writes, reviews, ports, or debugs an EA.
- **`skill.toml`** — two steps, `file_read` → `think`, producing `source_code` and
  `review`. Only JSON-safe scalars are interpolated: a whole source file cannot go
  through the raw `{key}` template renderer without breaking JSON. The deep work
  happens on the instruction path, where the agent composes its own tool calls.
- **`references/`** — `compile-errors.md` (MetaEditor error codes → fix),
  `mql4-to-mql5.md` (porting table), `mql5-api-cheatsheet.md` (signatures).
- **`templates/ea-template.mq5`** — a starting EA that already handles indicator
  handle lifecycle, new-bar guard, spread filter, magic-number filtering,
  fixed-fractional lot sizing normalized to `SYMBOL_VOLUME_STEP`, and `CTrade` order
  flow. It contains zero MQL4-isms (the benchmark's own scanner is run over it in
  `tests/skills/test_mql5_expert_skill.py`).

`install_skill.py` validates with `load_skill_directory()` before writing anything,
then installs through `SkillImporter` and copies `skill.toml` manually (the importer
does not carry pipeline manifests). If the `.source` sidecar lists MQL identifiers
under `missing_tools`, that is the tool translator's CamelCase heuristic noticing
`OnInit`, `OrderSend`, and friends — informational, nothing is broken.

## Config Preset

```bash
jarvis init --preset mql-assistant --force
```

Installs `configs/openjarvis/examples/mql-assistant.toml`: the largest local code
model for `-m code`, temperature 0.2 (signatures must be reproduced exactly, not
invented), `max_tokens = 4096` (whole EA files), `orchestrator` with 14 turns,
`shell_exec` for the compiler, `knowledge_search` for verifying signatures against an
indexed MQL5 reference, and tight memory chunking for API docs.

Or point the loop at it without touching your global config:

```bash
python examples/mql_companion/compile_loop.py --source MyEA.mq5 \
    --config configs/openjarvis/examples/mql-assistant.toml
```

`--model` / `--engine` override the preset; omit them and the preset decides.

> **This preset enables `shell_exec`.** It runs as your user with no allowlist.
> Keep it on a machine or VPS that holds no live trading credentials, and validate
> every EA on a demo account before it touches real money.

## Live Market Data: the MT5 MCP Bridge

`mt5_mcp_server.py` turns a running terminal into tools an agent can call, so
it stops *assuming* a symbol's digits, stops level, contract size, tick value
and filling modes and starts reading them:

| Tool | What it gives the agent |
|---|---|
| `mt5_status` | Is the bridge attached? Which account, and is it demo or real? |
| `mt5_account` | Balance, equity, margin, free margin, margin level, leverage |
| `mt5_symbols` | Market watch rows with digits, point, spread, volume limits, stops level |
| `mt5_symbol_info` | Full contract spec: tick size/value, filling and expiration modes, swap, order types |
| `mt5_tick` | Latest bid/ask, spread in points |
| `mt5_rates` | OHLC history for any of MT5's 21 periods |
| `mt5_positions` / `mt5_orders` | Open positions (filterable by magic) and pending orders |
| `mt5_calc` | Margin required (and per lot) plus profit at a close price — the ground truth for lot-sizing code |
| `mt5_tester_report` | A Strategy Tester report as numbers: profit factor, all four drawdowns, win rate, streaks, history quality — with optional thresholds that turn it into a pass/fail gate |
| `mt5_tester_compare` | Two or more reports side by side, with deltas and a winner per criterion |
| `mt5_tester_optimization` | An optimization table ranked and filtered, with the overfitting checks and the `.set` text that reproduces one pass |
| `mt5_tester_forward_check` | A report against the forward half of its run: `holds_up`, `degrades` or `inconclusive`, with the money normalized per day |
| `mt5_order_send` | A market order. **Not registered unless you pass `--allow-trading`, and demo accounts only** |
| `mt5_tester_run` | Launches the terminal to run a backtest and returns its report. **Not registered unless you pass `--allow-tester`** |

```bash
# inspect what it exposes (works with no terminal at all)
python examples/mql_companion/mt5_mcp_server.py --stub --list-tools

# one-shot call — the fastest way to check the bridge
python examples/mql_companion/mt5_mcp_server.py --stub \
    --call mt5_calc --args '{"symbol":"EURUSD","side":"buy","volume":0.5,"price_close":1.09}'

# serve over stdio for [tools.mcp] on the same machine
python examples/mql_companion/mt5_mcp_server.py

# serve over HTTP when the terminal is on another machine (Windows box -> VPS)
python examples/mql_companion/mt5_mcp_server.py --http --host 0.0.0.0 \
    --port 8765 --token SECRET
```

Wire it into the preset by uncommenting its `[tools.mcp]` block, or point any
config at it:

```toml
[tools.mcp]
enabled = true
servers = '[{"name": "mt5", "command": "python", "args": ["examples/mql_companion/mt5_mcp_server.py"]}]'
```

`jarvis ask --agent native_react "Read EURUSD's stops level and tick value
with mt5_symbol_info, then tell me the minimum stop distance in price terms"`
now answers from the terminal instead of from a guess.

> **A non-empty `[tools] enabled` list also filters MCP tools by name.**
> `jarvis ask` resolves its tool set from that list and then keeps only the
> MCP tools whose names appear in it — so a bridge you configured correctly
> is still invisible unless `mt5_status`, `mt5_tick`, `mt5_calc` and friends
> are listed there too. The `mql-assistant` preset already lists them; names
> that are not registered (bridge off) are skipped silently. Add
> `mt5_order_send` to the list yourself if you also run with
> `--allow-trading`.

### Safety, in the order it bites

1. `mt5_order_send` does not exist unless the server was started with
   `--allow-trading`. An agent cannot call a tool it cannot see.
2. With the flag, orders still only execute on a **demo** account — the check
   is `_assert_demo_account()`, and no combination of flags lifts it. Trading
   a live account through an agent is a decision to make on purpose, by
   editing that function, not by accident.
3. A market order without a stop loss is refused (`--no-require-stops` lifts
   it; don't).
4. Volume must be an exact multiple of the lot step and inside the symbol's
   min/max — a hallucinated `0.113` becomes an error naming the nearest valid
   sizes, not a silently resized position.
5. SL/TP are checked for side and for the broker's stops level *before*
   anything is sent, and prices are snapped to the tick grid. A price far from
   the live quote is rejected as invented.
6. The account password is read only from the environment (`--password-env`,
   default `MT5_PASSWORD`), so it never reaches a process listing or shell
   history.
7. `--http` refuses to bind a non-loopback address without `--token`.

### What the annotations tell a client

Every tool also declares the MCP annotation hints a client routes approvals by,
derived from its definition rather than repeated by hand: `readOnlyHint`,
`destructiveHint` and `idempotentHint` follow `read_only`, and `openWorldHint`
follows `open_world`. Reads of live data are **open-world** — quotes, account
state and positions arrive from a broker's server through the terminal, which
the spec counts as reaching outside a closed domain — and only the four tools
that parse a report file on this machine declare `openWorldHint: false`
(`CLOSED_WORLD_TOOLS`). Calling a broker read closed-world would understate
exactly the reach a client's approval routing exists to catch.

`requires_confirmation` is deliberately left unset. `ToolExecutor` refuses a
tool that declares it when no confirmation callback is plumbed, and the MCP path
this bridge is built for has none — so the flag would make the tool uncallable
rather than put a human in the loop. `REVIEW-NOTES.md` writes up what a real
per-call confirmation would take.

### Developing without a terminal

`--stub` serves a deterministic synthetic market — five symbols, a seeded
price walk that is continuous across bars and scales volatility with
`sqrt(period`, two open positions, and a margin model. Every payload is marked
`"synthetic": true` so nothing can be mistaken for a quote. `--stub-trade-mode
real` exercises the trading gate on any OS. The bridge is a single file on
purpose: copy it to the Windows machine that runs the terminal and it works
with nothing but OpenJarvis installed. Copy the whole folder instead and it
picks up `tester_report.py`, which adds the `mt5_tester_*` tools; copy the
bridge alone and those tools are simply not registered (a warning on stderr
says why).

## Backtest Results: Reading Strategy Tester Reports

Compiling is the easy half. Whether an EA is *any good* lives in the Strategy
Tester report, and a model reading that report through `file_read` is reading
localized HTML with non-breaking-space thousands separators, cells that hold
two numbers at once, and a sign convention that inverts a metric if you get it
wrong. `tester_report.py` parses it into numbers instead:

```bash
# the newest report the terminal left behind, as a compact prompt-ready summary
python examples/mql_companion/tester_report.py --latest --prompt

# one report, with the numbers spelled out as JSON
python examples/mql_companion/tester_report.py reports/MyEA.htm

# a CI gate: exit 1 unless the EA clears every bar
python examples/mql_companion/tester_report.py reports/MyEA.htm \
    --min-profit-factor 1.3 --min-trades 50 --max-equity-drawdown-pct 15 \
    --min-history-quality-pct 90

# two versions of the same EA, with deltas and a winner per criterion
python examples/mql_companion/tester_report.py reports/v1.htm reports/v2.htm

# run the backtest yourself, then read what it produced
python examples/mql_companion/tester_report.py --run \
    --expert "Examples/MACD/MACD Sample" --symbol EURUSD --period H1 \
    --from-date 2024.01.01 --to-date 2024.06.30 --deposit 10000 --model 4

# see the [Tester] ini a run would use, without launching anything
python examples/mql_companion/tester_report.py --run --print-ini --expert MyEA --model 4
```

What comes back:

| Group | Metrics |
|---|---|
| Money | net profit, gross profit, gross loss (negative, as MT5 reports it), profit factor, expected payoff, recovery factor, Sharpe ratio |
| Drawdown | balance and equity, each as the money drawdown at its worst, the percentage at that moment, the worst percentage seen, and the money at that worst percentage |
| Trades | total trades and deals, wins and losses with their percentages, long/short split, largest and average win/loss, longest winning and losing streaks |
| Context | expert, symbol, timeframe, date range, model, deposit, currency, leverage, bars, ticks, **history quality**, minimum margin level |

Three things it refuses to do quietly:

1. **No invented numbers.** A metric the file does not contain is listed in
   `missing`, and a gate on a missing metric *fails* rather than passing.
2. **The identities are checked.** `net = gross_profit + gross_loss`,
   `profit_factor = gross_profit / abs(gross_loss)`,
   `recovery_factor = net / balance_drawdown`. A report that disagrees with
   itself — usually because a value was misread — produces a warning naming
   the mismatch. Missing values are derived from these, and `derived` says so.
3. **Layout is not assumed.** Labels are matched in a cell stream, so the
   two-column HTML table, a `<br>`-separated single column, XML elements, XML
   `name`/`value` pairs, XML attributes and tab-separated pastes all parse the
   same. English labels are the vocabulary; `STAT_*` identifiers from MQL5's
   `ENUM_STATISTICS` are accepted as aliases.

History quality is the one to watch: below ~90% the terminal had gaps in its
tick or bar history, and the results look better than the data deserves. The
parser warns about it, and `--min-history-quality-pct` makes CI care.

The agent reaches the same data through the bridge — `mt5_tester_report`,
`mt5_tester_compare`, `mt5_tester_optimization`, `mt5_tester_forward_check` and
(with `--allow-tester`) `mt5_tester_run` — which is what makes "is my EA
profitable?" a question it can answer with numbers:

```bash
python examples/mql_companion/mt5_mcp_server.py --stub --tester-dir ~/mt5-reports \
    --call mt5_tester_report --args '{"min_profit_factor": 1.3}'
```

> **Running a backtest launches the terminal.** `mt5_tester_run` writes a
> `[Tester]` ini, starts `terminal64.exe /config:<ini>` and waits for the
> report, and the ini sets `ShutdownTerminal=1` so the process exits — which
> closes the terminal. Do not run it on a machine where someone has charts
> open: MT5 ignores `/config` for an already-running terminal, and shutting it
> down would close their windows. That is why it needs `--allow-tester`.

## Optimization Results: Picking a Pass You Can Trust

An optimization writes a different file: an XML *table* — one `<Row>` per pass,
`<Cell>` per column, saved in ANSI — with ten metrics and then one column per
optimized input. It is also the most misleading file in the whole workflow,
because the terminal sorts it by the optimization criterion and the top row
looks like the answer. Usually it is the pass that got lucky.

`tester_report.py` reads it, hides what MT5 itself hides, ranks what is left,
and says what the ranking is worth:

```bash
# the best ten passes, with the checks that a sorted table cannot show
python examples/mql_companion/tester_report.py --optimization-report opt.xml --prompt

# rank by something else: result, profit, payoff, profit_factor,
# recovery_factor, sharpe, drawdown, trades, custom
python examples/mql_companion/tester_report.py opt.xml --rank-by recovery_factor --top 20

# see the passes MT5 hides too (no trades, no profit, drawdown over 50%,
# recovery factor under 1, Sharpe under 0.5)
python examples/mql_companion/tester_report.py opt.xml --no-filter

# what a .set actually asks the optimizer to do, including the grid size
python examples/mql_companion/tester_report.py --set grid.set

# turn one pass back into inputs, ready to re-test on its own
python examples/mql_companion/tester_report.py opt.xml --set grid.set \
    --set-from-pass 371 --write-set winner.set

# run the optimization, then read what it produced
python examples/mql_companion/tester_report.py --run --expert MyEA \
    --symbol EURUSD --period H1 --from-date 2024.01.01 --to-date 2024.06.30 \
    --optimization 2 --optimization-criterion 0 --expert-parameters MyEA.set \
    --model 4 --out-report reports/MyEA.xml
```

The checks, in the order they save you:

| Check | What it says | Why it matters |
|---|---|---|
| Thin report | `only 24 pass(es) in this report: too few to say anything about robustness` | A genetic optimization this small has barely searched the space, so nothing below is worth much yet |
| Thin best pass | `the best pass traded 12 time(s)`, `the best pass reports no trade count, so the thin-sample rule could not be applied` | Every ratio in that row — profit factor, Sharpe, recovery — is noise below ~30 trades. An *absent* count is named too: it is not a cleared check |
| Ranking nobody could rank | `no pass in this report carries 'sharpe_ratio', so nothing was ranked: "best" is pass 1 in file order` | Rank by a column the report does not carry and every pass sorts as "missing", so `best` is just the first row — for a genetic run, the order passes were *tried* |
| Spike vs plateau | `the top pass (14500) is 9.6x the median of the next 19 passes` | A robust setting has neighbours that also work. A lone peak is a lucky run |
| Lonely peak | `only 1 of 31 passes land within 10% of the best result` | Same signal from the other side: nothing near the winner means the market only has to move a little to lose it |
| Edge-pinned input | `InpStopLoss sits at the stop of its tested range (1000) in 100% of the top passes` | The optimum is *outside* the range you optimized. Widen the `.set` and re-run; do not trade this pass. Needs `--set` for the ranges |
| Criterion mismatch | `ranked by result the winner is pass 37; ranked by sharpe_ratio it is pass 5` | You optimized one thing and will be judged by another |
| Forward half too thin | `only 3 pass(es) carry both a back and a forward result — too few to say whether the in-sample ranking holds` | The figures are still reported, but a median over one pair describes one pass and not a run. Five pairs before it becomes a verdict |
| Forward degradation | `out of sample the median result falls from 14500 to 6950 (52% worse)`, `back-test and forward ranks barely agree (Spearman rho=-1.00)` | The only built-in measurement of overfitting. Needs a forward run (`--forward-mode`) |
| In-sample winner, out-of-sample nobody | `pass 361 ranked first in sample but 44 of 52 out of sample — do not trade it on the strength of this report` | The forward half's own verdict on the pass you were about to deploy |

Filters default to the five MT5 offers in the Optimization Results tab, and a
rule only fires on a value the file actually contains: a pass with no trade
count is not dropped for having a bad one.

### `.set` files

One line per input, `value||start||step||stop||optimize`:

```ini
InpFastEMA=12||5||1||30||Y
InpStopLoss=500||200||50||1000||Y
InpUseFilter=true||false||0||true||Y
InpLots=0.10||0||0||0||N
```

Three details that cause silent failures:

1. **MT5 resolves `ExpertParameters` inside `MQL5\Profiles\Tester`.** Pass a
   file *name*, not a path — a path is looked for under that folder and not
   found, and the run proceeds with the EA's compiled defaults.
2. **Optimizing without a `.set` does not optimize.** With no
   `ExpertParameters`, MT5 falls back to `MQL5\Profiles\Tester\<EA>.set`; if
   that is missing too it uses the defaults and, per the documentation,
   "optimization is not possible". `tester_ini_warnings()` says this before the
   terminal is launched, and `--run` prints those warnings. It also reads the
   dates: MT5 parses `YYYY.MM.DD` only and silently falls back to the tester's
   own field for anything else, an inverted range tests nothing, and
   `ForwardDate` is valid only with `ForwardMode=4`.
3. **A pass row lists only the optimized inputs.** `--set-from-pass` therefore
   takes the `.set` the optimization ran from (`--set`) and carries its fixed
   inputs into the file it writes; without them a re-test would run the EA on
   defaults for everything else and you would be gating a different strategy.
   Add `--keep-ranges` to keep the ranges instead of fixing every value, which
   is what you want for a second optimization around the winner. A pass value the
   template's range could not have produced (`InpFastEMA=44` from a grid declared
   `5||1||30`) is written as reported and flagged — with `--keep-ranges` the line
   would otherwise ask the terminal to search a grid that cannot contain the
   winner.

`--set` also reports the grid size, because `26 x 9 x 17 x 2 = 7956` passes is a
genetic run and `1001^3` is a plan for next month:

```text
grid_combinations: 7956
note: the .set grid has about 7956 combinations ...
```

### The workflow this is for

Optimization does not produce a result you can trust; it produces *candidates*.
The honest loop is:

1. Optimize (with `--forward-mode` if the period is long enough to split).
2. Read the ranked passes and their warnings; prefer a pass in a plateau over
   the single best row, and widen any input pinned at its range edge.
3. `--set-from-pass` that pass into a `.set`, put it in `MQL5\Profiles\Tester`.
4. Re-run **one** test with `Optimization=0` and gate *that* report with
   `--min-profit-factor`, `--min-trades`, `--max-equity-drawdown-pct`.

The agent does the same through the bridge, read-only:

```bash
python examples/mql_companion/mt5_mcp_server.py --stub --tester-dir ~/mt5-reports \
    --call mt5_tester_optimization \
    --args '{"path": "opt.xml", "set_path": "grid.set", "top": 5, "set_from_pass": 371}'
```

`mt5_tester_optimization` returns the ranked passes, the warnings, and — with
`set_from_pass` — the `.set` text for that pass. It writes nothing: the agent
saves the text with `file_write` if it wants the file, so a read-only bridge
stays read-only.

> **A pass is not a backtest.** The optimizer measured it once, on one slice of
> history, with the criterion you asked for. Everything above exists to stop
> that single row from being reported as if it were a result.

## Forward Checks: Did the Winner Survive New Data?

Every number in an optimization report was measured on the history that produced
it. A *forward* run is the one check in this workflow that can say something
different: MT5 splits the period, optimizes on the back half, then re-runs the
winner on the forward half — data the search never saw. It writes that second
half next to the first:

| Run | Files |
|---|---|
| Single test with `ForwardMode` | `<name>.htm` and `<name>.forward.htm` |
| Optimization with `ForwardMode` | one `<name>.xml`, whose table gains `Back Result` and `Forward Result` columns |

```bash
# Produce both halves (this launches the terminal; see the --run notes above).
python examples/mql_companion/tester_report.py --run \
    --expert "Examples/MACD/MACD Sample" --symbol EURUSD --period H1 \
    --from-date 2022.01.01 --to-date 2023.03.31 \
    --model 4 --forward-mode 1 --out-report reports/MACD.xml

# Read them back: the .forward.htm beside a report is found by name.
python examples/mql_companion/tester_report.py --report reports/MACD.htm --prompt

# Or name both halves yourself, e.g. files copied off another machine.
python examples/mql_companion/tester_report.py \
    --report reports/MACD.htm --forward-report reports/MACD.forward.htm --prompt
```

### The three verdicts

| Verdict | Earned by |
|---|---|
| `holds_up` | No sign flip, and no gate ratio (profit factor, recovery factor, Sharpe) or profit-per-day down more than `--max-degradation-pct` (50 by default) |
| `degrades` | Profit factor crossed 1, profit or expected payoff crossed zero, or a gate ratio fell past that threshold |
| `inconclusive` | A half traded fewer than `--min-forward-trades` (30), the two files share no metric, or there is no forward file at all |

`inconclusive` is the reason there are three verdicts and not two. A forward
check on nine trades cannot support "it held up", and reporting that would be
worse than reporting nothing — so a thin sample, a missing file and a pair of
reports with nothing in common all say so plainly instead of producing a number
to quote. A *degrades* verdict wins over a thin sample, though: thinness is not
an alibi for a loss.

### Why the money is divided by days

The forward half is usually a fraction of the back half, so raw profit is not a
comparison — 2 500 over a quarter against 12 000 over a year is 14.79% down per
day, not 79% down. Ratios and per-trade figures (profit factor, recovery factor,
Sharpe, expected payoff, win rate, drawdown percentages) mean the same thing over
either span and are compared as written; money and trade counts are divided by
each half's length in days, taken from the report's own from/to dates. When a
report carries no dates the check says so and compares ratios only, rather than
inventing a normalization.

### Warnings that are not verdicts

A drawdown that grew, history quality under 90% in either half, and a forward
half *longer* than the back one (MT5 splits the other way round by default, so
check `ForwardMode` and `ForwardDate`) come back as warnings. On their own they
say the forward half was harder, not that the parameters broke.

### What it looks like

Real output from the reader on a year in sample and a quarter out:

```text
forward check: degrades
back: reports/MACD.htm | forward: reports/MACD.forward.htm (364d back / 89d forward)
profit_factor: 1.8 -> 0.82 (54.44% worse)
recovery_factor: 2.5 -> 0.2 (92% worse)
sharpe_ratio: 1.4 -> -0.2 (114.29% worse)
expected_payoff: 4.2 -> -1.1 (126.19% worse)
equity_drawdown_pct: 12.0 -> 18.0 (50% worse)
net_profit/day: 32.967 -> -4.4944
total_trades/day: 1.3187 -> 1.573
reasons:
  - profit factor fell from 1.8 to 0.82: the back half cleared the bar and the
    forward half did not
  - expected payoff fell from 4.2 to -1.1: the back half cleared the bar and the
    forward half did not
  - net profit fell from 12000.0 to -400.0: the back half cleared the bar and the
    forward half did not
  - recovery_factor degraded 92% (2.5 -> 0.2), over the 50% this check allows
warnings:
  - forward half history quality is 88% — gaps in the tick or bar history make
    that curve look better than the data deserves
```

(The metric lines are wrapped here to fit the page; the tool prints one line per
metric. `net_profit/day` carries no percentage because the sign flip already
reported it — one failure, one reason.)

### Over MCP

`mt5_tester_forward_check` is the read-only version: pass the back half (or
nothing, for the newest report) and it finds the `.forward.` companion *by name*.
Only by name — a forward report from some other run in the same folder is not
this run's out-of-sample half, and pairing them would compare two unrelated
tests and call the result a forward check. Passing the forward half as `path`
swaps in its back companion, since the newest file after a forward run is usually
the forward one. An optimization table passed as the forward half is refused with
a pointer to the `Back Result` / `Forward Result` columns.

`mt5_tester_run` takes `forward_mode`, `forward_date`, `min_forward_trades` and
`max_degradation_pct`, so one call can run the split and return the verdict.

### What `holds_up` does not mean

It means one thing: on this split, with this symbol, spread and history, the
parameters did not break on data the search never saw. It is not a live-trading
forecast, and a single split is one sample — the same EA can fail on the next
quarter. Say the split you tested and treat the verdict as a gate that was
passed, not as a promise.

## Verifying on a Real Terminal

Everything above is tested with fixtures and a fake terminal, which proves the
readers are honest about what a file contains and proves nothing about
MetaTrader. `REVIEW-NOTES.md` lists the ten claims that rest on documentation
and inference instead, and `verify_on_terminal.py` runs the experiments:

```bash
python examples/mql_companion/verify_on_terminal.py --list          # what it checks
python examples/mql_companion/verify_on_terminal.py                 # plan only
python examples/mql_companion/verify_on_terminal.py --yes           # run it
python examples/mql_companion/verify_on_terminal.py --yes --json verify.json
python examples/mql_companion/verify_on_terminal.py --yes --with-model4 --with-grace
```

Nothing launches the terminal without `--yes`, and nothing in the script can
place an order: the only bridge tools it may call are the read-only names in
`READ_ONLY_TOOLS`, and asking for any other name raises. The report ends with a
`paste this back` block — one compact line per check — which is the part worth
putting in the pull request. Exit status is 0 when nothing contradicted an
assumption, 1 when something did, and 2 when no terminal was found.

The expensive checks are opt-in: `--with-model4` (a real-ticks run),
`--with-grace` (the same run twice, to measure the race `process_grace` covers),
`--with-stability` (a sampled write), `--with-optimization` and `--with-bridge`.

## Extending

- **New benchmark tasks** — append to `_TASKS` in
  `src/openjarvis/evals/datasets/mql_bench.py`. Keep `required` to what a correct
  answer cannot avoid, put nice-to-haves in `optional`, and use the `re:` prefix for
  a regex check.
- **New reference docs** — drop a markdown file into `skills/mql5-expert/references/`
  and mention it in `SKILL.md`; the importer copies that subdirectory.
- **Live market data** — `mt5_mcp_server.py` already wraps the official
  `MetaTrader5` package. Add a tool by appending a `ToolDef` to `build_tools()`:
  a name, a description the model can act on, a JSON schema, and a handler that
  calls the `Terminal` backend (implement it in both `MetaTraderTerminal` and
  `StubTerminal` so it stays testable off Windows).
- **Strategy Tester** — the `MetaTrader5` package cannot drive the tester, so
  `tester_report.py` does it the way the terminal itself documents: write a
  `[Tester]` ini and launch `terminal64.exe /config:<ini>`. Add a metric by
  extending `LABELS` (the alias list) and, if the cell holds two numbers,
  `PAIR_SPLIT`; `build_label_lookup()` raises at import time if two aliases
  would claim the same label, which is how one metric silently takes another's
  value. Add a gate by adding a `min_*`/`max_*` option and passing it through
  `_rules_from_options()`.

## SDK Pattern

`compile_loop.py` uses the SDK directly rather than the CLI, so it can drive many
rounds in one process:

```python
from openjarvis import Jarvis

jarvis = Jarvis(
    config_path=str(config) if config else None,  # None = the default config
    model=model,  # None = whatever the config says
    engine_key=engine_key,
)
try:
    answer = jarvis.ask(
        prompt,  # system rules prepended — ask() has no system_prompt
        agent="native_react",
        tools=["file_read", "apply_patch", "shell_exec", "think"],
    )
    source = extract_mql_source(answer)
finally:
    jarvis.close()
```

## See Also

- [Tutorial: MQL Companion](../../docs/tutorials/mql-companion.md) — walkthrough of every piece
- [User Guide: Evaluations](../../docs/user-guide/evaluations.md) — `mql-bench` in the registry, scoring methods, CLI options
- [Tutorial: Code Companion](../../docs/tutorials/code-companion.md) — the same `native_react` pattern for general code intelligence
- [Tutorial: Skills Workflow](../../docs/tutorials/skills-workflow.md) — the full skills lifecycle this skill plugs into
