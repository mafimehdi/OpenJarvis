# Review notes — what only a real terminal can settle

Everything in `examples/mql_companion/` is covered by tests (509 in the suite at the
time of writing), but those tests run against **fixtures and a fake terminal**. A fake
terminal can prove the reader is honest about what a file does and does not contain; it
cannot prove anything about MetaTrader 5 itself.

This file lists what is still an assumption, where that assumption lives in code and
docs, and the shortest way to settle it on Windows (or Wine). Ordered by how much the
answer would change behaviour.

Nothing here is a known defect. It is a list of claims we could not check from
Linux — except item 6, which `verify_on_terminal.py` settled on its first run and
which is kept here because the fix still wants one real report from a non-English
terminal.

## One command instead of nine experiments

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
`--with-stability`, `--with-optimization`, `--with-bridge`), and nothing in the
script can place an order — the only bridge tools it may call are the read-only
names in `READ_ONLY_TOOLS`, and any other name raises. The report ends with a
`paste this back` block; send that and these notes can be turned into answers.

---

## 1. `ForwardMode` integer ↔ split mapping

**Assumption.** `0` turns the forward half off; any other value selects one of the
splits in the terminal's *Forward* dropdown; `ForwardDate` overrides with a custom
date. The **exact integer for each split is not documented** by MetaQuotes and is not
guessed anywhere in this repo.

**Where it lives.** Deliberately neutral wording:

- `skills/mql5-expert/references/optimization.md` — the ini-key table says the mapping
  is undocumented and tells the reader to read it back from a run.
- `tester_report.py` — `--forward-mode` passes the integer through untouched and only
  checks that it is a non-negative int.

**How to settle.** Run one optimization twice over a period whose midpoint you know
(e.g. `2024.01.01`–`2024.12.31`) with `ForwardMode=1` and `ForwardMode=2`. Compare the
optimization start/end dates the terminal logs in its Journal against the *Forward*
dropdown labels.

**If wrong.** Only the docs change — add the table. No code depends on the meaning of
the integer.

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
produces the terminal's unhelpful *"Optimization is not possible"* and silently falls
back to defaults.

---

## 9. Optimization report extension when `Report=` has no extension

**Assumption.** No extension means `.htm` for a single test and `.xml` for an
optimization.

**Risk: low.** The readers sniff the content (`parse_any_report`) rather than trusting
the extension, so a surprise here costs a doc line, not a wrong verdict.

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

`mt5_order_send` is gated on a **demo account check** with no flag that lifts it; the
guard is a function to edit deliberately, not a setting to flip. A reviewer with trading
experience should read that path first: this bridge runs commands on a machine that can
reach money, and the demo guard is the only thing standing between an agent and a live
position.
