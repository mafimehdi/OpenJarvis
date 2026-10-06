# Review notes — what only a real terminal can settle

Everything in `examples/mql_companion/` is covered by tests (509 in the suite at the
time of writing), but those tests run against **fixtures and a fake terminal**. A fake
terminal can prove the reader is honest about what a file does and does not contain; it
cannot prove anything about MetaTrader 5 itself.

This file lists what is still an assumption, where that assumption lives in code and
docs, and the shortest way to settle it on Windows (or Wine). Ordered by how much the
answer would change behaviour.

Nothing here is a known defect. It is a list of claims we could not check from
Linux — with two exceptions. Item 6 was settled by `verify_on_terminal.py` on its
first run and is kept here because the fix still wants one real report from a
non-English terminal. Item 1 turned out to be documented by MetaQuotes after all,
so its check now compares a build against the documentation instead of trying to
discover the mapping; one narrower assumption inside it is still open.

## One command instead of ten experiments

`verify_on_terminal.py` runs these checks on a machine that has the terminal
installed and prints what it observed:

```bash
python examples/mql_companion/verify_on_terminal.py --list
python examples/mql_companion/verify_on_terminal.py            # plan only
python examples/mql_companion/verify_on_terminal.py --yes      # run it
python examples/mql_companion/verify_on_terminal.py --yes --json verify.json
```

The check numbers below are the script's numbers. Nothing launches without
`--yes`, the expensive checks are opt-in (`--with-model4`, `--with-grace`,
`--with-stability`, `--with-optimization`, `--with-bridge`, `--with-forward-opt`), and
nothing in the
script can place an order — the only bridge tools it may call are the read-only
names in `READ_ONLY_TOOLS`, and any other name raises. The report ends with a
`paste this back` block; send that and these notes can be turned into answers.

---

## 1. `ForwardMode` integer ↔ split mapping — documented, so the check compares

**Documented.** MetaQuotes' start-up options list the integers outright:
"ForwardMode — forward testing mode (0 — off, 1 — 1/2 of the testing period, 2 — 1/3 of
the testing period, 3 — 1/4 of the testing period, 4 — custom interval specified using
the ForwardDate parameter)". The Strategy Optimization page says the same thing from
the UI side ("a half, one third, one fourth or a custom period") and adds the part that
changes how a report reads: after optimizing on the first part of the period, **10% of
the best runs (full search) or 25% (genetic)** are re-tested on the forward part. So in
a forward optimization the `Forward Result` column is populated for a slice the platform
selected *because those passes already won in sample*.

This note used to say the exact integers were not documented and that nothing here
guessed them. That was wrong, and the repo already relied on the documented meaning:
`tester_ini_warnings` warns that `ForwardDate` is read only with `ForwardMode=4`, which
is the same sentence quoted above.

**Where it lives.**

- `skills/mql5-expert/references/optimization.md` — the ini-key table carries the
  mapping, and the forward section states the 10%/25% rule and what it does to
  `median_degradation_pct` and `spearman_back_vs_forward`.
- `tester_report.py` — the `build_tester_ini` docstring, `--forward-mode`'s help and the
  MCP `forward_mode` description all name the integers; `analyze_optimization` reports
  `forward.passes` against `forward.total_passes` and warns when that is a fraction of
  the table. The integer itself is still passed through untouched: no code branches on
  1, 2 or 3.
- `verify_on_terminal.py` — `DOCUMENTED_FORWARD_SHARE` holds the three shares and
  `_split_line` labels each observed split `matches` or `differs from` the documented
  one, so the check compares instead of only collecting. Mode 4 is the value that
  comparison cannot judge — there is no documented share to hold it against, and
  `--modes` does not include it — so check 12 asks for a *date* instead.

**How to check it on a machine.** `verify_on_terminal.py --yes --only 1` runs a single
test per mode over a period whose midpoint is known and prints, for each mode, the dates
each half actually reports beside the documented share.

```bash
python examples/mql_companion/verify_on_terminal.py --yes --only 12 --with-custom-split
```

Check 12 covers mode 4 and the two `ForwardDate` rules `tester_ini_warnings` ships. It
computes the date to ask for from the range (`CUSTOM_FORWARD_SHARE` = 40% forward, which
on the default range is 45 days from the 1/2 point, 31 from 1/3 and 68 from 1/4, so a
build that ignored the date and fell back to a documented share could not look like one
that honoured it), then runs three single tests: `ForwardMode=4` with that date,
`ForwardMode=1` with the same date — the warning says other modes *ignore* it — and
`ForwardMode=4` with no date at all. PASS means all three behaved as documented; FAIL
names the claim that broke and the dates that broke it.

**If a build disagrees.** The observed dates win for that build: paste the `mode=` lines
into the pull request and keep both readings with the build that produced each. Nothing
else moves, because no code depends on the integer beyond `4` — and what that `4` does
is exactly what check 12 asks the terminal.

**Still an assumption — empty cells versus zeros.** A `Forward Result` cell MT5 leaves
empty parses as `None` and is excluded, so partial coverage is safe as written. But if a
build writes `0` into the cells of passes it never re-ran, those zeros are
indistinguishable from results: every pass would appear to lose its whole back result
out of sample, and `median_degradation_pct` would read 100%. `analyze_optimization`
cannot tell the two apart from the file alone, so it warns when *every* forward value in
the table is exactly 0.

Settling that one is check 11: an optimization with `ForwardMode=1` and `Optimization=1`
that classifies every pass's `Forward Result` cell against the share MetaQuotes documents
as forwarded (10% of a slow complete search).

```bash
python examples/mql_companion/verify_on_terminal.py --yes --only 11 \
    --with-forward-opt --set-file MyEA.set
```

- **PASS** — some passes carry no cell at all, or an empty one. None-is-missing is then the
  right reading, partial coverage is safe as written, and the all-zero warning is a signal
  rather than the only defence. The assumption above is retired.
- **FAIL** — every row carries a cell and some hold exactly `0`, although only about a
  tenth of the passes could have been re-run. The terminal's Forward Results tab is then the
  tie-breaker: a row the tab shows blank while the XML holds 0 means `analyze_optimization`
  has to treat 0 as missing whenever coverage is partial.
- **UNKNOWN** — the table has no Back/Forward Result columns (so the run never split the
  period), or every pass carries a non-zero forward value (so this run re-ran them all and
  the question never arose). Re-run with a `.set` that sweeps several times the share.

Check 9 still optimizes without a forward split; the two answer different questions —
whether a pass table is written at all, and what that table holds for the passes the
forward stage skipped.

---

## 2. Report file names for a single test with a forward half

**Assumption.** A single test (not an optimization) with `ForwardMode` set writes
`<name>.htm` plus `<name>.forward.htm`. Forward pairing is **by name only**: the
companion is the same stem with `.forward` inserted. There is no fallback that scans
the report roots for "the newest forward-looking file", because that paired unrelated
runs during development and was removed.

**Where it lives.**

- `examples/mql_companion/README.md` and `references/optimization.md` — the
  "what MT5 writes" tables state this as fact.
- `tester_report.py` — companion resolution, and the note emitted when only one half is
  found (it now says the companion is missing and where to look, rather than guessing
  that `ForwardMode` was off).

**How to settle.** Run a single test with `ForwardMode` set, then list the report
directory. Check both the extension (`.htm` vs `.xml`) and the exact position of
`.forward`.

**If wrong.** The pairing rule plus the two doc tables. A wrong name surfaces loudly
today — `forward-check` reports the missing companion instead of silently returning an
empty verdict — but it still fails to do its job.

---

## 3. `Model=4` (real ticks) freezes the terminal UI

**Assumption.** Real-ticks testing starts the tester minimized and blocks the
terminal's UI thread for the duration of the run. Documented as a caveat, not observed.

**How to settle.** Run it once. If the terminal stays responsive, soften the wording in
`README.md` and `references/optimization.md`; if it hangs harder than described (e.g.
needs killing), say so and mention the timeout flags.

---

## 4. `process_grace` is long enough

**Assumption.** 5 seconds is enough to wait for the terminal to exit after the report
appears. This exists because MT5 writes the report **before** it exits, so a reader that
returns as soon as the file is stable can race the exit code.

**Where it lives.** `tester_report.py` — `run_test(..., process_grace: float = 5.0)`.

**How to settle.** Run on a slow machine or with a large tick history and watch for a
report that is read while the process is still alive. Raise the default if it happens;
the parameter is already exposed, so a caller can override without a code change.

---

## 5. `_file_is_stable` on a slow or network filesystem

**Assumption.** Size + mtime unchanged across one poll interval means the write is
finished.

**Where it lives.** `tester_report.py` — `_file_is_stable(path, poll_interval)`.

**How to settle.** Point `Report=` at a network share or an antivirus-heavy folder and
check that a partially written report is never parsed. A truncated parse today shows up
as missing metrics rather than a crash, which is the failure mode we want, but it is
still a wrong answer.

---

## 6. ANSI code page: cp1251 or cp1252 — settled, one report still wanted

**What it was.** MT5 writes reports in ANSI, in the terminal's own code page, so
the same byte is `é` on a French install and `й` on a Russian one.
`decode_report_bytes` tried cp1251 and then latin-1; since cp1251 leaves one byte
value undefined (0x98) where cp1252 leaves five, cp1251 won every contest and a
Western report came back as `Bйnйfice` — which parses, and reads as nonsense.
Found by running check 6 of `verify_on_terminal.py`, not by reading the code.

**What it does now.** cp1252 is preferred only when both halves of the evidence
agree: the cp1251 reading contains no run of two or more Cyrillic letters (real
Cyrillic *words* mean a Cyrillic terminal), and every character where the two
readings differ is a Cyrillic-block character on one side and a Western accent on
the other. The decision is per document, not per character, because a lone `№`
(byte 0xB9, cp1252's `™`) is normal in a Russian report and one ambiguous byte is
not evidence of a Western page. Byte 0x98 is undefined in both tables and falls
back to latin-1, which keeps every offset aligned with the file.

**Where it lives.** `tester_report.py` — `decode_report_bytes` and
`_western_reading_is_better`; check 6 of `verify_on_terminal.py` replays six
samples including the ambiguous ones, and
`tests/examples/test_tester_report.py::TestDecodeReportBytesCodePages` pins the
behaviour.

**Still worth doing.** Read one report from a non-English terminal — French or
Russian — and check the accents and letters come back as written. The heuristic
is tested against synthetic bytes; a real report is the only thing that can show
a case neither table describes.

## 7. Wine discovery on Linux

**Assumption.** `~/.wine/drive_c/Program Files{, (x86)}` plus every `~/.wine*` prefix
are enough to find `metaeditor64.exe` / `terminal64.exe`.

**Where it lives.** `metaeditor.py` — the Wine root list.

**How to settle.** Install MT5 under a non-default prefix and run the compile CLI
without pointing it at an explicit path. The CLI accepts an explicit executable path, so
this is a convenience issue, not a blocker.

---

## 8. `ExpertParameters` must be a bare `.set` name

**Assumption.** The value is a file name resolved against `<mt5-data>/MQL5/Profiles/Tester/`,
so anything containing a path separator is rejected with a warning.

**Where it lives.** `tester_report.py` — the ini warnings (`contains a path separator`,
`does not end in .set`, and the "optimization without ExpertParameters" case).

**How to settle.** Try `Tester\My.set` and a subpath on a real terminal. If MT5 accepts
them, downgrade the rejection to a note. The warning exists because a missing `.set`
produces the terminal's unhelpful *"Optimization is not possible"*.

**What the page documents, and what it does not.** MetaQuotes lists a chain: the
`ExpertParameters` file; if that setup "is not available", `<EA>.set` in the same folder,
which the terminal rewrites with the last inputs it used; and only if *that* is missing,
the compiled defaults (where "Optimization is not possible"). So an unresolvable name
may land on **stale last-used inputs, ranges included**, not on defaults — an earlier
version of this note, the README and the warning all said "defaults". Whether an
unresolvable name counts as "not available" is not stated; the warning now names the
`<EA>.set` fallback as a possibility instead of asserting either outcome. To settle it:
save an `<EA>.set` with a recognisable input value, run a single test with
`ExpertParameters=Sub\missing.set`, and read the `Inputs` line of the report — the
recognisable value means the chain falls through, the compiled default means it jumps.

---

## 9. Optimization report extension when `Report=` has no extension

**Assumption.** No extension means `.htm` for a single test and `.xml` for an
optimization.

**Risk: low.** The readers sniff the content (`parse_any_report`) rather than trusting
the extension, so a surprise here costs a doc line, not a wrong verdict.

---

## 10. A date MT5 cannot parse, and what it tests instead

**Assumption.** `FromDate`, `ToDate` and `ForwardDate` are read as `YYYY.MM.DD`. The
format is documented; what is *inferred* is the consequence — the documented fallback
for a **missing** parameter is the date still sitting in the strategy tester's own
field, and `tester_ini_warnings()` assumes an **unreadable** one lands in the same
place, so the run measures a period nobody asked for and reports it as a success.

**Risk: medium.** If MT5 reads `2022-01-01` after all, the warning is noise and should
be softened. If it does something else — refuses the run, or tests a default range —
the warning understates it.

**Settled inside check 1**, which launches the terminal with `FromDate=2022-01-01` and
prints the period the resulting report actually covers. The line reads one of two ways:
`the terminal tested exactly that range` (soften the warning) or `the terminal tested
from <date> instead` (the fallback is real). It needs `--yes`, like every launch.

The `ForwardDate` half of the same assumption is **check 12**
(`--only 12 --with-custom-split`), which asks for a custom split rather than a broken
date: `ForwardMode=4` with a `ForwardDate` far from every documented share, the same
date under `ForwardMode=1`, and `ForwardMode=4` with no date. Its verdict can contradict
three warnings this repo already ships, which is the point of it — this note used to say
check 1 was the only one that could, and check 11 and check 12 have since made that
false.

Still documentation-derived after both: the two out-of-range rules (a `ForwardDate` at or
before `FromDate`, or at or after `ToDate`), and what a date MT5 cannot parse does to
`ForwardDate` in particular, since check 1 probes `FromDate`.

---

## 11. A `.set` with non-ASCII text

**Assumption.** `write_set_file` writes pure-ASCII content as ASCII and anything else as
UTF-16LE with a BOM, because MT5 writes its own `.set` files that way and the terminal
is reported not to load a UTF-8 one that contains non-ASCII text.

**Where it lives.** `tester_report.py::write_set_file`.

**Evidence, and how thin it is.** One forum report (mql5.com/en/forum/312820): a `.set`
containing Arabic characters, written as UTF-8, would not load; written as UTF-16 it did.
MetaQuotes' build 1525 note that "ANSI, UTF-8 and UTF-16 are supported" is about the file
*functions* (`FileOpen`), not preset loading, so it does not settle this. Nothing here
is a vendor statement about `.set` encodings.

**How to settle.** Take an EA with a `string` input, write a `.set` with a non-ASCII value
using `--write-set`, load it from the tester's **Inputs → Load**, and read the value back.
If a UTF-8 file loads fine on current builds, the UTF-16 branch is harmless but
unnecessary; if the UTF-16 one fails, switch the non-ASCII branch to whatever loads.

---

## What is pinned by tests, and what is not

Pinned (will fail loudly if it regresses):

- Every reader refuses a report of the wrong shape and says which tool to use instead —
  `report`, `compare`, `forward_check` and `run` all detect an optimization table, and
  the optimization reader detects a testing report.
- Forward verdicts and their precedence: `degrades` beats `inconclusive`, `inconclusive`
  beats a thin sample; per-day normalization when both halves carry dates; warnings for
  a missing companion and for a pair with no trade counts.
- ini generation, including `ForwardMode`, `ForwardDate`, `ExpertParameters` warnings,
  `ShutdownTerminal` and the report path rules.
- The `mql-bench` evaluation: 12 tasks, deterministic scorer, MQL4-ism detection with
  line numbers.

Not pinned, and cannot be:

- Anything that requires a live `terminal64.exe`. Those paths are exercised through a
  fake terminal that writes fixture files and exits — which proves the orchestration and
  the readers, not MetaTrader's behaviour.

## Security posture

Enforced today, in the order it bites:

1. `mt5_order_send` is not registered at all without `--allow-trading`, and
   `mt5_tester_run` not without `--allow-tester`. An agent cannot call a tool it
   cannot see.
2. With the flag, orders still execute only on a **demo** account —
   `_assert_demo_account()`, and no combination of flags lifts it.
3. A market order without a stop loss is refused, volume must be an exact
   multiple of the lot step, SL/TP are checked against the broker's stops level
   (from the closing price: Bid for a buy, Ask for a sell) and prices are snapped to the tick grid before anything is sent.
4. The account password comes from the environment only, and `--http` refuses a
   non-loopback bind without `--token`.
5. Every tool declares its MCP annotation hints — `readOnlyHint`,
   `destructiveHint`, `idempotentHint`, `openWorldHint` — so a client can route
   the call by risk. Live reads are open-world because they come from a broker's
   server; only the four local-file readers in `CLOSED_WORLD_TOOLS` declare
   themselves closed.

Deliberately *not* done. Each needs a maintainer decision rather than an
assumption, and `CONTRIBUTING.md` asks for an issue before non-trivial changes:

- **`requires_confirmation` on `mt5_order_send`.** `ToolExecutor` refuses a tool
  that declares it when no confirmation callback is plumbed, and the MCP path
  has none, so setting the flag today would make the tool uncallable rather than
  put a human in the loop. The honest version threads a callback through
  `build_server` — the bridge runs on the terminal's machine, which is where a
  human is, and its stdout is JSON-RPC so a prompt has to go to stderr — or
  queues the action into `ApprovalStore`.
  `tests/examples/test_mt5_mcp_server.py::TestToolTable` pins the current choice
  so flipping it is a deliberate act rather than a side effect.
- **`required_capabilities`.** Declaring a capability would let an
  `AgentPolicy` decide which agents may touch the bridge at all. No existing
  `Capability` value means "trade" (`tool:invoke` is about tools, not money),
  adding one to `src/openjarvis/security/capabilities.py` is a core change, and
  declaring an existing value could lock the bridge out of a default-deny
  deployment.
- **Deleting `mt5_order_send` from the example.** It is the only part of the
  bridge that demonstrates the gates above, and `--stub-trade-mode real`
  exercises them on any OS. Removing it would remove the demonstration rather
  than the risk: an agent with `shell_exec` can drive the terminal directly.

A reviewer with trading experience should still read `_assert_demo_account()`
and the order path first. This bridge runs commands on a machine that can reach
money, and layers 1-4 are what stand between an agent and a position.
