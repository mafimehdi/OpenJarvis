# Changelog

All notable changes to OpenJarvis are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Added

**Apple Foundation Models (AFM 3)** — a new in-process `afm` engine drives
Apple's `apple-fm-sdk` directly, with no HTTP hop and no second process whose
CPU draw would land inside the same energy measurement window. Install with
`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer uv sync --extra afm`
(the SDK compiles Swift bindings, so a full Xcode is required), then
`jarvis ask --engine afm --model afm-3-core "..."`. Requires an Apple Silicon
Mac on macOS 26+ with Apple Intelligence enabled.

Token counts are real, via `SystemLanguageModel.token_count` (added in SDK
0.2.1, hence the floor). Because `token_count` rejects a value and
instructions together, both ends are measured against the session transcript:
`prompt_tokens = <transcript before> + <prompt alone>` and
`completion_tokens = <transcript after> - prompt_tokens`.

Two caveats worth reading before comparing AFM numbers to other backends:
`stream_response` yields cumulative snapshots batching roughly 8-10 tokens, so
`ttft` is time-to-first-*chunk* (~450 ms on an M1 Pro) and derived
inter-token latencies are inter-chunk; and the SDK exposes no variant
selector, so `afm-3` / `afm-3-core` / `afm-3-core-advanced` are run labels
only — the framework's dynamic profile chooses what actually executes.
Bench results record `metadata["engine_info"]` (SDK version, context size,
host chip) so a run can be attributed after the fact.

**Eval results record which rail did the work.** Result rows and summaries
previously carried only `energy_joules` and `power_watts`, so nothing in an
eval artifact distinguished a Neural Engine run from an idle GPU, or a
hardware measurement from a modelled estimate. Rows now include the per-rail
breakdown (`cpu`/`gpu`/`dram`/`ane`/`soc`), `energy_basis`, and
`energy_method`; summaries add `energy_joules_by_rail` alongside both. On a
ToolCall-15 run against AFM the ANE rail is the largest single component —
91.7 J of 223.7 J, against 3.0 J on the GPU — which is now visible in the
file rather than only through live instrumentation.

**ANE and whole-SoC energy** — `EnergySample.ane_energy_joules` existed but
died at the monitor boundary. Apple Neural Engine and a whole-SoC total now
flow through `TelemetryRecord`, the SQLite schema (with an `ADD COLUMN`
migration), the aggregator, `TelemetrySession`, eval `TurnTrace`/`QueryTrace`,
and bench metadata, alongside an `energy_basis` marker (`"soc"` on Apple
Silicon, `"gpu"` elsewhere) so downstream repeats the monitor's choice instead
of guessing. This matters because AFM runs predominantly on the ANE: measured
on an M1 Pro, one generation drew 0.98 J on the ANE against 0.014 J on the
GPU rail — 70x — where an MLX matmul on the same machine drew 8.99 J on the
GPU and nothing on the ANE.

**Vision input for `jarvis ask`** — attach images to a query with
`-i`/`--image` (repeatable) or capture the current screen with
`-S`/`--screen`, for vision-capable models such as `gemma3:4b`. Images flow
through `Message.images` into Ollama's `/api/chat` `images` field; text-only
requests are unaffected. A privacy guard warns before any image is sent to a
non-local engine, and the security guardrail now preserves images when it
sanitizes a flagged prompt. Screen capture uses the built-in Windows .NET
stack with `mss`/`Pillow` fallbacks on other platforms. Adds the
`JARVIS_NUM_CTX` environment variable to tune the Ollama context window
(default `16384`).

**MQL Companion — Expert Advisor development for MetaTrader.**
`examples/mql_companion/` adds a compile-in-the-loop fixer for MQL4/MQL5 sources,
an installable `mql5-expert` skill, an `mql-assistant` config preset, and
`mql-bench`, a structural benchmark for choosing a model that writes MQL5 instead
of MQL4. MQL5 is low-resource for LLMs and the characteristic failure of a small
local model is not a syntax slip but confidently emitting MQL4 — a bare `Ask`/`Bid`,
an eleven-argument `OrderSend`, `Close[1]` — which either fails to compile or,
worse, compiles and trades wrongly. So the loop ends where the authority is: the
compiler.

`metaeditor.py` drives MetaEditor's command line (`/compile` `/inc` `/log` `/s`),
the only MQL compiler MetaQuotes still ships. It locates the binary through
`METAEDITOR_PATH`, Program Files, broker install globs, and `~/.wine*`, adds the
`wine` prefix on non-Windows hosts, and decodes the log's UTF-16LE by sniffing the
BOM and then NUL bytes *before* trying UTF-8 — ASCII in UTF-16LE decodes as valid
UTF-8 with interleaved NULs, so the naive order returns corrupted lines rather than
raising. `compile_loop.py` compiles, feeds the parsed diagnostics
(`file(line,col) severity message`) to a `native_react` agent, snapshots the source
as `<file>.roundN.bak`, and repeats until the build is clean, the round budget runs
out, or the model returns an unchanged file (a loop guard, not an oversight). Exit
code is 0 only on a clean compile; `--compile-only` needs no model at all, and
`--json-out` writes a round-by-round report for CI or `jarvis scheduler`.

Four MetaEditor behaviours are handled explicitly because they lie. The process exit
code is reported inverted across builds, so success is decided by the parsed
`N errors, M warnings` summary and the exit code is advisory. A clean log is not proof
of a build either: zero errors with no `.ex5` artifact yields a `note` ("silent CLI
failure or stale artifact") rather than a false success, and the artifact must
post-date the run, not merely the source, so a previous build's `.ex5` is never
credited to a rebuild that wrote nothing. The summary may raise the error count — an
included file's diagnostics are counted without always being listed — but never clear
diagnostics the parser found; a log holding neither a summary nor one parseable
diagnostic is a toolchain failure (exit code 2, and the fix-up loop stops instead of
spending a model round), not a silent OK. And log decoding sniffs UTF-16 byte order
from NUL parity, because a mis-guess decodes to CJK glyphs rather than raising and
every count then reads as zero.

`mql-bench` (12 tasks — indicator handles, `CTrade` order flow, fixed-fractional lot
sizing, trailing stops, MQL4→MQL5 ports) scores deterministically and structurally,
since no MQL compiler runs in CI: required API surface 0.70, forbidden MQL4-isms
0.20, optional best practices 0.10 (renormalized to 0.78/0.22 when a task declares
no optional checks). A task is correct only when every required check passes, zero
MQL4-isms are found, and the answer is non-empty. Comments and string literals are
stripped with offsets preserved before scanning, so a doc comment mentioning
`OrderSend` does not register as an MQL4-ism, and `OrderSend` arity is detected by
counting top-level commas inside balanced parentheses so a model cannot dodge the
check by renaming variables. Every pattern also refuses a preceding `.`, because
`s.Bars` is a field on the author's own struct, not the MQL4 global — MQL4 never
spells those with a dot, while MQL5 member access always does. The mirror case is
accepted on purpose: a user type named `Point`, or a wrapper called
`AccountBalance()`, is still reported, since separating either from a genuine idiom
takes declaration parsing, and in this domain both readings say the same thing — the
model is still thinking in MQL4. Each decision is pinned by a test, and all twelve
reference answers pass their own benchmark.

The skill ships as a hybrid: `SKILL.md` carries the checklist an agent injects into
context, and `skill.toml` runs two deliberately JSON-safe steps (`file_read` →
`think`). `SkillExecutor` renders `arguments_template` with raw `{key}`
substitution, so interpolating a source file — or a Windows path with backslashes —
produces invalid JSON; `SKILL.md` documents the forward-slash requirement and
`tests/skills/test_mql5_expert_skill.py` pins both the limitation (xfail) and its
documentation. `templates/ea-template.mq5` is scanned by the benchmark's own MQL4
detector in that suite, so the starting point cannot teach the failure mode.

**MT5 bridge — a running MetaTrader 5 terminal as MCP tools.**
`examples/mql_companion/mt5_mcp_server.py` exposes live contract specs, quotes,
history, account state and margin maths to any agent, so an EA's digits, stops
level, tick value and filling modes get *read* instead of assumed — the
assumptions are exactly what lot-sizing and stop-placement code gets wrong. Nine
read-only market tools plus a gated `mt5_order_send`, over stdio for a terminal on
the same machine or `--http` for the common split of a Windows terminal and a Linux
VPS running OpenJarvis (token-protected, and it refuses a non-loopback bind
without one).

The official `MetaTrader5` package is Windows-only, so it is imported lazily and
`--stub` serves a deterministic synthetic market instead: five symbols, a seeded
walk whose bars are continuous (each opens at the previous close) and whose
volatility scales with sqrt(period), two open positions, a leverage-100 margin
model. That is how the tests in `tests/examples/test_mt5_mcp_server.py` run in
CI with no MetaTrader and no Windows. Every stub payload is marked
`"synthetic": true` so fiction cannot be mistaken for a quote, and
`--stub-trade-mode real` exercises the trading gate on any OS.

Trading is gated in layers, none reachable by accident. `mt5_order_send` is not
registered at all without `--allow-trading` — a tool the agent cannot see cannot
be hallucinated into a call. With that flag, `_assert_demo_account()` still
refuses any account whose `trade_mode` is not `demo`, and no flag lifts it.
Market orders without a stop loss are refused. Volume must be an exact multiple
of the lot step, because rounding a requested volume silently changes the risk of
the trade. SL/TP are checked for side and for the broker's stops level before
sending, prices are snapped to the tick grid, and a price far from the live quote
is rejected as invented rather than answered with retcode `10021 price_off` — a
clearer lesson for the model. The account password is read only from the
environment, never a flag, and stdout carries JSON-RPC and nothing else (one
stray `print` would break the transport, so a test pins it).

Every tool also declares the MCP annotation hints a client routes approvals by,
derived from the tool definition rather than repeated per tool: `readOnlyHint`,
`destructiveHint` and `idempotentHint` follow `read_only`, and `openWorldHint`
follows a new `open_world` field. Live reads are open-world — quotes, account
state and positions come from a broker's server through the terminal, which the
spec counts as reaching outside a closed domain — and only the four tools that
parse a report file on this machine declare `openWorldHint: false`
(`CLOSED_WORLD_TOOLS`). The bridge had been reporting every tool as closed-world,
which understated the reach of exactly the tools that touch a broker.
`requires_confirmation` stays unset on purpose: `ToolExecutor` refuses a tool
that declares it when no confirmation callback is plumbed and the MCP path has
none, so the flag would make the tool uncallable rather than add a human. What a
real per-call confirmation would take — a callback through `build_server`, or a
queue into `ApprovalStore` — is written up in `REVIEW-NOTES.md` as a proposal
instead of being assumed here.

One wiring note, now documented in the preset, the example README and the
tutorial because it is invisible until it bites: `jarvis ask` resolves its tool
set from `[tools] enabled` and then filters MCP tools *by those names*, so a
correctly configured bridge stays invisible unless its tools are listed there.
`mql-assistant.toml` lists them; unregistered names are skipped silently, so
they cost nothing while the bridge is off.

**Strategy Tester reports as numbers.**
`examples/mql_companion/tester_report.py` parses a tester report (`.htm`,
`.html`, `.xml`, or a tab-separated paste) into the metric set MQL5 documents in
`ENUM_STATISTICS`: net profit, gross profit and gross loss, profit factor,
expected payoff, recovery factor, Sharpe ratio, all four drawdown figures per
equity curve, trade and deal counts, win and loss percentages, largest and
average win/loss, longest streaks, plus the test context down to history
quality. The bridge serves the same data as `mt5_tester_report` and
`mt5_tester_compare`, and `mt5_tester_run` (only with `--allow-tester`) launches
the terminal to produce a report. 321 tests in
`tests/examples/test_tester_report.py` cover it with no MetaTrader and no
Windows.

The report is the kind of file a model misreads with confidence, so the parser
encodes the traps instead of trusting the reader. MT5 reports gross loss as a
*negative* number — net profit is gross profit *plus* gross loss, and the profit
factor divides by the loss's magnitude; one cell holds two figures
(`812.44 (8.01%)`, `131 (53.91%)`, `7 (310.50)`) and which is which depends on
the label; thousands separators are non-breaking spaces; "Maximal" and
"Relative" name four different drawdowns, not two; and a date like `2024.01.01`
or a filename like `MACD Sample.ex5` is digits and dots but is not a number
(the latter used to normalize to `0.5`, which is precisely the silent nonsense
this exists to prevent).

Nothing is invented and nothing passes quietly. A metric absent from the file is
listed in `missing`, and a CI gate on a missing metric *fails* — a check that
passes because it could not find the number is worse than no check. The reason fails
with it: `thresholds.results[].message` crosses the bridge as well as the CLI, so
`passed: false` tells an agent whether the metric was below the bar or absent from a
file nobody could read, instead of leaving `actual: null` to be guessed at. The
documented identities are enforced: `net = gross_profit + gross_loss`,
`profit_factor = gross_profit / abs(gross_loss)`,
`recovery_factor = net / balance_drawdown`; missing values are derived from them
(and `derived` says which), while a report that disagrees with itself — gross
loss positive, a profit factor that does not match its own gross figures, win
and loss percentages that do not sum to 100, trade counts that do not add up, an
equity drawdown smaller than the balance drawdown — produces a warning naming
the mismatch. History quality below 90% warns too, since gaps in the tick
history make the curve look better than the data deserves.

Layout is never assumed: labels are matched in a flattened cell stream, so a
two-column table, a `<br>`-separated column, XML elements, XML `name`/`value`
pairs, XML attributes and pasted text all yield the same metrics. The vocabulary
is English labels plus `STAT_*` identifiers, and `build_label_lookup()` raises at
import time if two aliases would claim one label — that collision is how one
metric silently takes another's value, and it caught two during development.
A report whose labels the vocabulary does not hold — a terminal set to another
language exports localized labels, and `decode_report_bytes` reads the Cyrillic bytes
faithfully only for every label to fall outside it — keeps the pairs it read in
`raw_labels` and warns that nothing matched. A long `missing` list alone is not the
honest answer it looks like: downstream it reads as a run with no drawdown and no
losses, and the forward check went further and called it `holds_up`, because a row
exists when *either* half carries a metric and no comparison rule can fire on a
number only one file contains. A verdict now needs a metric both halves carry, and a
half that parsed to nothing is named as the reason.

`--run` uses the mechanism the terminal documents, since the `MetaTrader5`
package cannot drive the tester: write a `[Tester]` ini and launch
`terminal64.exe /config:<ini>` (under Wine off Windows, detected from the
binary's name). It waits on the report file's mtime rather than the process, so
a stale report from a previous run is never mistaken for a fresh one, and a run
that produces nothing times out and says so. `ShutdownTerminal=1` closes the
terminal when the run ends, which is why `mt5_tester_run` is opt-in: MT5 ignores
`/config` for an already-running terminal, and closing someone's charts is not a
side effect an agent should trigger by accident.

The tester tools read files, not the market, so they work in `--stub` mode and on
a machine with no terminal. They appear only when `tester_report.py` sits next to
the bridge — copy that one file to a Windows box and the live-market tools work
exactly as before, with a warning on stderr explaining what is missing.

**Optimization passes, and the `.set` files that make them reproducible.**
`tester_report.py` now reads the *other* thing the Strategy Tester writes: an
optimization report is not a testing report but an XML table — `<Table>/<Row>/<Cell>`,
saved in ANSI, a header row naming ten fixed columns (`Pass`, `Result`, `Profit`,
`Expected Payoff`, `Profit Factor`, `Recovery Factor`, `Sharpe Ratio`, `Custom`,
`Equity DD %`, `Trades`) and then one column per optimized input. It is also the
most misleading file in the workflow, because the terminal sorts it by the chosen
criterion and the top row reads like the answer when it is usually the pass that
got lucky. `parse_any_report()` sniffs which of the two a file is, so a caller
cannot accidentally report one pass of a search as a backtest.

Ranking is the easy part; `analyze_optimization()` adds the checks a sorted table
hides. The default filters are the five MT5 offers in its own Optimization Results
tab (passes with no trades, no profit, drawdown over 50%, recovery factor under 1,
Sharpe under 0.5), and a rule fires only on a value the file contains — a pass with
no trade count is not dropped for having a bad one. Then: a best pass on fewer than
~30 trades, whose ratios are noise, and a best pass whose trade count the file does
not carry at all, which says the thin-sample rule could not run rather than letting an
absent number pass it; a spike rather than a plateau (top pass many
times the median of its neighbours, and how few passes land within 10% of it); an
input pinned at the start or stop of the range that was optimized, which means the
real optimum was never tested and the answer is to widen the grid; a criterion
mismatch, when the pass that wins on `Result` is not the one that wins on recovery
factor or Sharpe; and, for forward runs, out-of-sample degradation — median back
versus forward result, the forward rank of the in-sample winner, and a Spearman
correlation between the two orderings, because an in-sample ranking that does not
predict the out-of-sample one is a ranking of noise. That forward median needs at least
five passes carrying both halves before it becomes a sentence about the run; below that
the figures are still reported and the sample is named as too thin, since a median over
one pair describes one pass. And ranking has to be possible at all: when no pass carries
the metric it was asked to rank by, `best` is the first row in file order — for a
genetic run, the order the passes were tried, not the order they scored — and the
analysis says so instead of presenting that row as the winner.

`.set` files are read and written in MT5's `value||start||step||stop||optimize`
form (plain `name=value` and MT4-shaped rows are accepted on the way in, and
unrecognized lines are preserved rather than dropped, since a `.set` gets
round-tripped into the terminal). `total_combinations()` reports the grid size —
`26 x 9 x 17 x 2 = 7956` is a genetic run, a million is a plan for next month —
and `set_from_pass()` turns one pass back into a file you can re-test. It takes the
`.set` the optimization ran from, because a pass row lists only the *optimized*
inputs: without the template, every input the run held fixed would silently revert
to the EA's compiled defaults and the re-test would measure a different strategy.
`--keep-ranges` leaves the grid intact for a second optimization around the winner. A
pass value the template's range could not have produced is written as reported *and*
flagged: either the `.set` was edited after the optimization ran or that report column
is not that input, and under `--keep-ranges` the file would otherwise contradict itself
— `InpFastEMA=44||5||1||30||Y` searches a grid that cannot contain its own winner.

Two runner bugs surfaced while wiring this up, both of which failed as a timeout on
a run that had succeeded. `Report=` takes a name and MT5 appends the extension —
`.htm` for a test, `.xml` for an optimization, `.forward.*` for the forward half —
so waiting for exactly the requested name waited for a file that was never written;
`report_candidates()` now accepts any of them and reports which one appeared. A third
was subtler: the terminal writes the report *before* it exits, so reading the process's
return code the moment the file settled reported `exit_code: null` on a clean run —
`run_tester()` now gives the terminal a short grace window (`process_grace`, 5s) to
finish, which is also what makes a nonzero code from a slow shutdown reach the warning
it belongs in. And a report is not written atomically (an optimization with thousands
of passes takes seconds), so parsing on the first mtime change read a half-written file
and returned metrics as `missing`, which reads as "the EA never produced them"; the
runner now waits for size and mtime to stop moving. `tester_ini_warnings()` names the
ini mistakes that fail silently — optimizing with no `ExpertParameters` (MT5 falls back
to `MQL5\Profiles\Tester\<EA>.set` and, without it, cannot optimize at all), a
`.set` given as a path when MT5 resolves only a name inside that folder, a report
folder MT5 will not create, and the dates — a `FromDate` outside the documented
`YYYY.MM.DD`, where the risk is the fallback MT5 documents for a *missing* date (the
value still in the strategy tester's own field) reaching a merely unreadable one; an
inverted range, which tests nothing; a
`ForwardDate` without `ForwardMode=4`, the only mode that reads it; and a split
outside the range, which either holds nothing out from the optimizer or leaves the
forward half empty. `--run` prints them before launching anything, and
`verify_on_terminal.py` check 1 puts the format claim to a real terminal, since it
comes from MetaQuotes' documentation rather than from observation.
`build_tester_ini()` gained `ForwardMode`, `ForwardDate`, `UseRemote`, `UseCloud`
and `ProfitInPips`, and the report vocabulary gained the curve-shape metrics
(`Z-Score`, `AHPR`, `GHPR`, `LR Correlation`, `LR Standard Error`, MFE/MAE
correlations, position holding times, absolute drawdowns).

The bridge serves all of it as `mt5_tester_optimization`, read-only: ranked passes,
the warnings, an optional `.set` file's ranges, and — with `set_from_pass` — the
`.set` *text* for a chosen pass. It returns text rather than writing a file so the
read-only tools stay read-only and the agent decides whether to save it. The
`mql-assistant` preset lists the new tool, since a non-empty `[tools] enabled` list
filters MCP tools by name.

**Forward checks — the only question the tester can answer about new data.**
Every number in an optimization report was measured on the history that produced it,
so the table cannot tell a real edge from a good fit. `ForwardMode` is the setting
that asks a different question: MT5 splits the period, optimizes on the back half,
then re-runs the winner on the forward half — dates the search never saw — and writes
a second report beside the first (`<name>.forward.htm`; an optimization instead gains
`Back Result` and `Forward Result` columns in the same table). `check_forward()` reads
the pair and returns one of three verdicts.

`inconclusive` is the reason there are three. A forward check on nine trades cannot
support "it held up", so a thin half (under `min_trades`, 30), a missing companion and
two reports with no metric in common all say so instead of producing a number to quote;
a *degrades* verdict still wins over a thin sample, because thinness is not an alibi for
a loss. `degrades` is earned by a sign flip — profit factor crossing 1, or profit and
expected payoff crossing zero — or by decay past `max_degradation_pct` (50) on the gate
ratios. A metric that flipped is not then also reported as "degraded 54%": one failure,
one reason.

The comparison is normalized, because the trap here is arithmetic rather than
statistics: the forward half is usually a fraction of the back half, so 2 500 over a
quarter against 12 000 over a year reads as a 79% collapse when it is 14.79% down per
day. Ratios and per-trade figures mean the same thing over either span and are compared
as written; money and trade counts are divided by each half's length in days, taken from
the report's own from/to dates (`period_days()`), and when a report carries no dates the
check warns and compares ratios only rather than inventing a normalization. Drawdown
growth, history quality under 90% and a forward half *longer* than the back one are
warnings, not verdicts — they say the forward half was harder, not that the parameters
broke.

`run_tester()` collects both halves: `forward=None` reads `ForwardMode` out of the ini
text, `report` is now always the back half (waiting for "the newest file" returned the
forward one), and a `ForwardMode` run that produces a single file explains itself in
`forward_note` rather than timing out. The CLI gained `--forward-report`, `--min-
forward-trades` and `--max-degradation-pct`, and the bridge gained a read-only
`mt5_tester_forward_check` plus forward parameters on `mt5_tester_run`.

The MCP tool matches the companion **by name only**, which a first version did not: it
also accepted "the newest forward file under the search roots", and that happily paired
a report in a temp folder with an unrelated `TesterReport.forward.htm` sitting in the
repository root — two different runs reported as one forward check. A wrong answer that
looks like a right one is worse than `available: false`, so the fallback is gone and a
test pins that a forward report from another run is never paired. That same stray file
turned out to be committed: a CLI run had written the default report name into the repo
root and `git add -A` swept it in. Both files are removed and `.gitignore` now covers
`/TesterReport.*`.

**Verifying the assumptions a fake terminal cannot.**
`examples/mql_companion/verify_on_terminal.py` runs the experiments
`REVIEW-NOTES.md` lists, on a machine that has MetaTrader installed, and prints
what it observed plus a `paste this back` block for the pull request. Eleven
checks, numbered to match the notes: the `ForwardMode` integer→split mapping is
derived from the dates each half actually reports; the forward companion name is
compared with the names `forward_companion()` looks for, on disk rather than in
theory; `process_grace` is measured by running the same config with the grace at
0 and at 5 seconds, which is the only way to tell a real race from a machine that
simply exits quickly; a sampler thread watches the report grow during a run to
test `_file_is_stable`; and the decode, Wine, `ExpertParameters` and
report-extension checks run anywhere. Nothing launches without `--yes`, and
nothing can place an order: the only bridge tools it may call are the read-only
names in `READ_ONLY_TOOLS`, and `_guard_read_only` raises on any other name.

Its first run found a real bug. `decode_report_bytes` tried cp1251 and then
latin-1, and since cp1251 leaves one byte value undefined (0x98) where cp1252
leaves five, cp1251 won every contest: a French report came back as `Bйnйfice`,
which parses and reads as nonsense. It now switches to cp1252 only when the
cp1251 reading contains no Cyrillic *words* and every differing character is a
Cyrillic-block character where cp1252 has a Western accent — a decision per
document, not per character, because a lone `№` (byte 0xB9, cp1252's `™`) is
normal in a Russian report and one ambiguous byte is not evidence of a Western
page. Byte 0x98 is undefined in both tables and falls back to latin-1, which
keeps every offset aligned with the file.

### Fixed

**Apple Silicon energy was never measured, only modelled.**
`telemetry/energy_apple.py` imported `AppleSiliconMonitor` from
`zeus.device.soc.apple`; no such class has ever existed there. The import
raised on every machine, so the CPU-time fallback -- `wall_clock x TDP x 0.60`
split by four hardcoded ratios -- was the only path that ever ran. It read no
counters and no utilization, so a ten-second sleep and a ten-second generation
produced identical joules, and every Apple Silicon figure in the telemetry DB,
eval traces and `jarvis bench` came from it. Renaming the import would not
have helped: `zeus.device.soc` landed on zeus master after the last zeus
release, so no published `zeus-ml` contains it, and `zeus-ml` has no `apple`
extra either. `energy-apple` now installs `zeus-apple-silicon`, the IOReport
extension zeus itself wraps, which needs no root. Measured on an M1 Pro:
8.24 W under load against 3.72 W idle, where the previous code could not tell
the two windows apart.

`AppleEnergyMonitor.available()` now reports `False` when nothing is
measurable, instead of `True` on any Apple Silicon host, so the factory falls
through rather than letting a model masquerade as a measurement. The estimate
remains reachable through the new `telemetry.allow_energy_estimates` setting
and still reports `energy_method = "tdp_estimate"`.

`snapshot()` is implemented for the first time, so `TelemetrySession`'s
background sampler -- the path agentic evals use -- records real readings
rather than the ABC's empty default. Relatedly, `TelemetrySample` hardcoded
`cpu_power_w = 0.0`, which zeroed CPU power in every eval trace on every
platform.

**`apple_fm` shim reported zero token counts.** It hardcoded zeros with a note
that the SDK did not expose counts; SDK 0.2.1 added `token_count`. Zeroed
`completion_tokens` zeroes throughput, `energy_per_output_token_joules` and
`tokens_per_joule` for everything served through the shim. Streaming responses
now also carry `usage` on the final chunk. The install hint no longer tells
users to clone from GitHub -- `apple-fm-sdk` is on PyPI. System messages become
the session's `instructions` rather than being prefixed onto the prompt as
`[System] ...`, which discarded a distinction the SDK draws and inflated the
prompt-token count.

**`scripts/setup-energy-monitor.sh` did not exist**, though
`jarvis bench --setup-energy` built a path to it and offered to run it -- so
the offer silently did nothing.

### Security

**WebSocket API keys no longer appear in request URLs.** Browser clients now
send a marked, base64url-encoded credential through
`Sec-WebSocket-Protocol`; programmatic clients can continue to use an
`Authorization: Bearer <key>` handshake header. The encoding only makes the
credential safe for WebSocket protocol syntax and does not encrypt it, so use
`wss://` for remote connections.

The former `?token=<key>` WebSocket authentication path is no longer accepted.
Custom browser clients must migrate to the `openjarvis.auth.v1` subprotocol
format documented in the API server guide.

## [1.0.2] - 2026-05-24

A patch release that fixes a packaging bug which broke the v1.0.1
wheel on PyPI, silences a noisy startup warning, restores a working
install path while `openjarvis.ai` is down, improves desktop
first-boot diagnostics on Windows, and ships the RAM-detection fix
for Windows that missed the v1.0.1 cutoff.

### Fixed

**`openjarvis/traces/` missing from the v1.0.1 PyPI wheel** (#372).
The `.gitignore` carried an unanchored `traces/` pattern, which
hatchling honored at wheel-build time and matched the runtime module
`src/openjarvis/traces/` — silently dropping the whole package. Every
fresh `pip install openjarvis==1.0.1` then failed at import with
`ModuleNotFoundError: No module named 'openjarvis.traces'` on the
first `jarvis ask`, learning, or server call. Anchored the pattern to
`/traces/`. Verified: a clean `uv build` now produces a wheel
containing all four `traces/` files.

**`pynvml` deprecation `FutureWarning` on every command** (#389).
Switched the dependency from the legacy `pynvml` package to NVIDIA's
official `nvidia-ml-py` (same `pynvml` module name, no warning shim),
and added defensive `warnings.filterwarnings` at every `import pynvml`
site to suppress the warning even when `pynvml` is pulled in
transitively.

**Windows RAM detection returning `0.0 GB`** (#373). The Windows
branch of `_total_ram_gb()` (via `GlobalMemoryStatusEx`) landed after
the v1.0.1 cutoff, so v1.0.1 users still saw `0.0 GB` from `jarvis
init`. Now shipping in the wheel. A new `windows-latest` CI job runs
the real `GlobalMemoryStatusEx` path on every PR as a regression
guard.

**Desktop first-boot hung on "did not become healthy in time"**
(#331). The Tauri boot path ran `uv sync` with stderr discarded and
the exit code ignored, so a failed dependency install surfaced only
as a generic 600-second health-check timeout. Now captures stderr,
checks the exit status, and surfaces the actual `uv sync` error
(with the diagnostic tail) before the long wait. The error-formatting
logic is covered by unit tests.

### Changed

**Install URL moved to GitHub Pages** (#337, #352). The documented
`openjarvis.ai/install.sh` URL was failing with `sslv3 alert
handshake failure` (the domain is community-operated and had a broken
TLS config). The canonical installer is now served from the
project-controlled GitHub Pages site at
`https://open-jarvis.github.io/OpenJarvis/install.sh`, generated from
the same `scripts/install/install.sh` at docs-build time. The README
also documents the WSL2 path for Windows and the `uv` prerequisite
for the desktop binary, and the installer bails early with a clear
message when run under Git Bash / MSYS2 / Cygwin.

## [1.0.1] - 2026-05-17

A patch release that closes the auto-update gap so the analytics
module added in #351 actually reaches users on the desktop, adds
runtime opt-out for that analytics, fixes the misleading upgrade
hint the CLI was printing, and lands the ACE optimizer alongside
DSPy and GEPA.

### Added

**ACE agent optimizer** (`learning/agents/ace_optimizer.py`). Adds
[ACE](https://github.com/ace-agent/ace) as a third agent-learning
policy alongside DSPy and GEPA. Where DSPy bootstraps few-shot
examples and GEPA evolves prompt populations, ACE evolves a textual
*playbook* of strategies the agent reads at inference time, updated
by a Generator / Reflector / Curator triad. Pick via
`[learning.agent] policy = "ace"`. Setup is manual (ACE isn't on
PyPI and isn't a properly-packaged Python project as of v1.0.1) —
see `docs/learning/ace.md` for the install path and trace-adapter
behavior.

**`jarvis self-update`** subcommand. Detects how OpenJarvis was
installed (pip, uv tool, editable git checkout) by inspecting
`openjarvis.__file__`, then runs the right upgrade command. Supports
`--check` (print the command without running) and `-y` (skip the
confirmation prompt). The post-command "new version available" hint
now points users at this command instead of guessing at the right
flow.

**Desktop auto-update endpoint wired to the rolling
`desktop-latest` GitHub release.** The Tauri updater plugin was
configured on the build side (`createUpdaterArtifacts: true`,
`includeUpdaterJson: true`, signing key in `TAURI_SIGNING_PRIVATE_KEY`)
but inert on the runtime side (`active: false`, `endpoints: []`). The
installed desktop app would never check. Both are now fixed; the app
polls `releases/download/desktop-latest/latest.json` every 30 minutes
and signature-verifies downloads against the minisign pubkey baked
into the app. Full flow, key-rotation runbook, and dev escape hatch
(`OPENJARVIS_NO_UPDATER=1`) documented in `docs/desktop-auto-update.md`.

**Analytics env-var opt-out** (`DO_NOT_TRACK`, `OPENJARVIS_NO_ANALYTICS`).
Tanvir's analytics module (#351) only respected the
`[analytics] enabled` config-file setting. Both env vars are now
honored in `is_analytics_enabled()` and in the install.sh beacon
script. Any truthy value (`1`, `true`, `yes`, `on`) disables for
that process; env opt-out takes precedence over the config file.
Documented under a new "Opting out" section in `docs/telemetry.md`.

### Changed

**Version-check trigger widened.** The "new version available" hint
in `_version_check.py` used to fire only on `{ask, chat, serve}` and
hardcoded the wrong upgrade command (`git pull && uv sync` — only
correct for editable installs). Now fires on every interactive
command (`doctor`, `init`, `quickstart`, `model`, `agents`, `skill`,
`memory`, `bench`, `telemetry`, `config`, `eval`, `optimize`, plus
the original three) and uses install-detection to print the right
upgrade command. Honors `JARVIS_NO_UPDATE_CHECK=1` and `CI=true` to
stay silent in automation.

**Desktop app version bumped 0.1.0 → 1.0.1** across
`tauri.conf.json`, `frontend/package.json`, and
`frontend/src-tauri/Cargo.toml` so the Python and desktop release
streams are aligned and the auto-updater has a real version to
compare against.

### Migration from 1.0.0

- **Importing `is_analytics_enabled`?** Same signature; behavior now
  short-circuits on env opt-out before checking the config. Callers
  that want the raw "is the config flag set" semantic should read
  `cfg.enabled` directly.
- **Editable-git users running `jarvis self-update`** get the
  detected `git pull && uv sync` command pointed at their actual
  checkout, not `~/OpenJarvis`. If you'd come to rely on the
  hardcoded path, update your muscle memory.

## [1.0.0] - 2026-05-15

The five-primitive architecture (Intelligence, Engine, Agents,
Tools & Memory, Learning) is now stable, with efficiency and
on-device learning as first-class capabilities alongside accuracy.
Companion blog post:
[From Minions to OpenJarvis: A Retrospective on Two Years in Local AI](https://hazyresearch.stanford.edu/blog/2026-05-19-minions-to-openjarvis-retrospective).

### Highlights

**Five composable primitives.** Intelligence, Engine, Agents, Tools & Memory,
and Learning each sit behind a single typed interface — any slot is
substitutable without touching the rest. The composition layer is
`JarvisSystem` in `src/openjarvis/system.py`, driven by a TOML config.

**Built-in agents across three execution modes.** Eight agents spanning a
single-turn chat baseline, a deep-research agent with inline citations,
a CodeAct-style coder, and a continuous monitor with memory compression
for long-horizon workflows. Execution modes cover on-demand, scheduled,
and continuous.

**Starter presets.** Eight preset configs installable via
`jarvis init --preset <name>` bundle an agent with a hardware-appropriate
engine, connectors, and tools. Variants cover Apple Silicon, Linux GPU
servers, and CPU-only laptops, plus a quickstart for LLM-guided spec search.

**Inference engines.** Four first-class local engines (Ollama, vLLM, SGLang,
llama.cpp) and five cloud providers (OpenAI, Anthropic, Google Gemini,
OpenRouter, MiniMax) sit behind a single `Engine` interface. Discovery
in `engine/_discovery.py` picks a sensible default per host.

### Added — hybrid local-cloud capabilities

**Per-query routing via a query-complexity analyzer**
(`src/openjarvis/learning/routing/complexity.py`). Produces a 0.0–1.0
complexity score with code/math/reasoning signals and a suggested token
budget, populating `RoutingContext` so easy queries stay local and only
queries that need frontier capability escalate.

**LLM-guided spec search** (`src/openjarvis/learning/spec_search/`).
`SpecSearchOrchestrator` wires diagnose → plan → execute → gate into a
single learning session: a frontier model reads traces, proposes
coordinated edits across all five primitives, and a held-out benchmark
gate (`gate/benchmark_gate.py`, `gate/regression.py`, `gate/cold_start.py`)
accepts only non-regressing edits. Ships with the `spec-search-quickstart`
preset and a runnable tutorial at `examples/openjarvis/spec_search_quickstart.py`.

**Six hybrid coordination paradigms** in `src/openjarvis/agents/hybrid/`.
Each paradigm pairs a local student with a frontier cloud teacher under
a different orchestration shape, as `LocalCloudAgent` subclasses:

- `minions` — reactive single-local + single-cloud loop
- `conductor` — static DAG planner
- `advisors` — executor ↔ advisor loop
- `archon` — generate → rank → fuse
- `skillorchestra` — per-query router across local skills
- `toolorchestra` — RL'd local model with a tool pool

A runner CLI (`python -m openjarvis.agents.hybrid.runner --cell <name>`)
and a 35-cell experiment registry (one TOML per method × benchmark ×
model triple) let researchers run, score, and compare these on equal
footing. Includes a Modal-backed SWE-bench-Verified harness scorer
(`evals/scorers/swebench_harness.py`).

### Added — efficiency as a first-class constraint

**Hardware-agnostic energy telemetry at 50ms resolution** across NVIDIA
(`telemetry/energy_nvidia.py`), AMD (`telemetry/energy_amd.py`), Apple
Silicon (`telemetry/energy_apple.py`), and Intel RAPL
(`telemetry/energy_rapl.py`). Energy, dollar cost, FLOPs, and latency
are treated as evaluation targets alongside accuracy.

**Instrumentation for FLOPs, batch, steady-state, ITL, phase energy, and
vLLM-specific metrics.** Joined per-query by the aggregator
(`telemetry/aggregator.py`) so traces carry accuracy + efficiency together.

### Added — local learning loop

**Closed-loop optimization across the stack** — model weights via SFT
(`learning/intelligence/sft_trainer.py`) and GRPO
(`learning/intelligence/grpo_trainer.py` plus an orchestrator-specific
variant under `learning/intelligence/orchestrator/`), prompts via DSPy
(`learning/agents/dspy_optimizer.py`), agent logic via GEPA
(`learning/agents/gepa_optimizer.py`), and engine + stack configuration
via LLM-guided spec search. `LearningOrchestrator` coordinates triggers
and applies optimizer overlays at discovery time so improvements compound
across primitives.

### Added — cross-framework evaluation

**External agentic-framework evaluation via subprocess.** The
`evals/backends/external/` subpackage wraps Hermes Agent and OpenClaw as
one-shot subprocess backends behind the existing `InferenceBackend` ABC.
The `evals/comparison/` toolkit provides path + commit-pin enforcement
(`third_party.py`), config templating (`make_configs.py`), and LaTeX
table generation (`table_gen.py`).

Ships with a new optional extra `framework-comparison` (depends on
`polars`), a `live_external` pytest marker for integration tests
requiring real foreign-framework installations, and a `ToolOrchestra`
evaluation dataset (`evals/datasets/toolorchestra.py`) alongside the
existing 30+ benchmark suite.

### Added — Skills System (Plans 1, 2A, 2B)

- **Skills core** — every skill is a tool. Skills appear in a system prompt catalog, agents invoke them on demand, content (pipeline results, markdown instructions, or both) gets injected into context.
  - `SkillManifest` + `SkillStep` types with tags, depends, invocation flags, markdown content
  - `SkillManager` — discovery, precedence resolution, catalog XML generation, tool wrapping
  - `SkillTool(BaseTool)` — auto-extracts parameters from step argument templates
  - `SkillExecutor` — sequential pipeline execution with sub-skill delegation
  - Dependency graph with cycle detection, max depth enforcement, capability unions
  - Security: four trust tiers (bundled/indexed/unreviewed/workspace), capability-gated enforcement
  - Skill index module for git-backed registry search

- **agentskills.io spec adoption** — canonical `SKILL.md` format with YAML frontmatter following the [agentskills.io](https://agentskills.io/specification) open standard.
  - `SkillParser` with strict spec validation + tolerant field mapping via `FIELD_MAPPING` table
  - `ToolTranslator` for external tool name translation (Bash -> shell_exec, Read -> file_read, etc.)
  - Source resolvers: `HermesResolver`, `OpenClawResolver`, `GitHubResolver`
  - `SkillImporter` with provenance tracking (`.source` metadata files), optional script import
  - Sourced subdirectory layout (`~/.openjarvis/skills/<source>/<name>/`)

- **Skills learning loop** — trace tagging, pattern discovery, DSPy/GEPA optimization.
  - Trace metadata tagging: `skill`, `skill_source`, `skill_kind` flow through ToolExecutor -> TraceCollector -> TraceStep
  - `SkillDiscovery` wired into `SkillManager.discover_from_traces()` with kebab name normalization
  - `SkillOptimizer` — per-skill DSPy/GEPA wrapper that buckets traces and writes sidecar overlays
  - `SkillOverlay` — sidecar storage at `~/.openjarvis/learning/skills/<name>/optimized.toml`
  - `SkillManager._load_overlays()` applies optimized descriptions + few-shot examples at discovery time
  - `LearningOrchestrator._maybe_optimize_skills()` — opt-in auto-trigger

- **Skills benchmark harness** — 4-condition PinchBench evaluation.
  - I3 fix: `skill_few_shot_examples` wired through SystemBuilder -> `_run_agent` -> `ToolUsingAgent` -> `native_react.REACT_SYSTEM_PROMPT`
  - `SkillBenchmarkRunner` — 4-condition x N-seed x M-task sweep with markdown report
  - `JarvisAgentBackend` accepts `skills_enabled` and `overlay_dir` kwargs
  - Conditions: `no_skills`, `skills_on`, `skills_optimized_dspy`, `skills_optimized_gepa`

- **CLI commands:**
  - `jarvis skill list` / `info` / `run` / `install` / `sync` / `sources` / `update` / `remove` / `search`
  - `jarvis skill discover` — mine traces for recurring tool patterns
  - `jarvis skill show-overlay` — inspect optimization output
  - `jarvis optimize skills` — run DSPy/GEPA per-skill optimization
  - `jarvis bench skills` — run the PinchBench skills benchmark

- **Agent prompt improvement:**
  - `native_react.REACT_SYSTEM_PROMPT` now includes "Using Skills" guidance that teaches agents to distinguish executable vs. instructional skill responses
  - `{skill_examples}` placeholder for optimized few-shot example injection

- **Configuration:**
  - `[skills]` section: `enabled`, `skills_dir`, `active`, `auto_discover`, `auto_sync`, `max_depth`, `sandbox_dangerous`
  - `[[skills.sources]]` section: `source`, `url`, `filter`, `auto_update`
  - `[learning.skills]` section: `auto_optimize`, `optimizer`, `min_traces_per_skill`, `optimization_interval_seconds`, `overlay_dir`
  - `SkillSourceConfig` and `SkillsLearningConfig` dataclasses

- **Documentation:**
  - `docs/user-guide/skills.md` — comprehensive user guide
  - `docs/architecture/skills.md` — technical deep-dive
  - `docs/tutorials/skills-workflow.md` — end-to-end tutorial
  - `docs/getting-started/configuration.md` — expanded with skills config sections
  - `CLAUDE.md` — updated architecture section

### Examples & Tutorials

- `examples/openjarvis/spec_search_quickstart.py` — runnable end-to-end
  LLM-guided spec search session.
- `docs/user-guide/llm-guided-spec-search.md` — paper-aligned user guide.
- `docs/architecture/learning.md` — Learning primitive deep-dive covering
  routing, spec search, optimizers, and the orchestrator.
- `docs/tutorials/` — code-companion, deep-research, messaging-hub,
  scheduled-ops, and skills-workflow walkthroughs.
- `src/openjarvis/agents/hybrid/registry/*.toml` — 35-cell registry of
  paradigm × benchmark × model experiments.

### Migration from 0.x

- **`learning/distillation/` is now `learning/spec_search/`.** The
  subsystem was renamed to match the LLM-guided spec search semantics
  documented in the companion paper. Update any imports
  (`from openjarvis.learning.distillation.*` →
  `from openjarvis.learning.spec_search.*`). The `jarvis distillation`
  CLI command is removed; use `spec_search`-prefixed config keys instead.
- **`_third_party.toml` no longer ships default paths.** Set
  `HERMES_AGENT_PATH` and `OPENCLAW_PATH` env vars to point at your
  local checkouts before running the framework-comparison harness;
  missing or empty paths now raise `ThirdPartyNotFoundError` with an
  actionable hint.
- **Engine `generate_full` return shape extended.**
  `JarvisAgentBackend.generate_full` and `JarvisDirectBackend.generate_full`
  now return the spec §6.2 extended fields (`energy_joules`,
  `peak_power_w`, `tool_calls`, `turn_count`, `framework`,
  `framework_commit`, `error`). Existing callers that didn't read these
  fields are unaffected; new callers can rely on cross-framework parity.

### Fixed

- **Trace metadata flow** — `ToolResult.metadata` now propagates through `TOOL_CALL_END` event to `TraceStep.metadata` (was silently dropped at the event-bus boundary).
- **TaintSet JSON serialization** — `ToolExecutor._json_safe_metadata()` filters non-JSON-serializable values (like `TaintSet`) from event payloads before they reach `TraceStore`.
- **Non-dict YAML frontmatter** — source resolvers handle `yaml.safe_load()` returning a string instead of a dict (discovered on real OpenClaw imports).
- **OpenClaw category/name queries** — `jarvis skill install openclaw:owner/slug` now correctly splits into category + name match.
- **SkillDiscovery trace compatibility** — `_extract_tool_sequence` reads from `step.input["tool"]` (the actual `TraceStep` format), not the nonexistent `step.tool_name` attribute.
- **LearningOrchestrator skill trigger** — `_maybe_optimize_skills` runs BEFORE the SFT-data short-circuit (skills are tagged via trace metadata, not mined as SFT pairs).
- **PinchBenchScorer constructor** — `SkillBenchmarkRunner` constructs `PinchBenchScorer(judge_backend, model)` instead of no-args.
- **EvalRunner results access** — reads per-task data from `eval_runner.results` property, not nonexistent `summary.results`.
