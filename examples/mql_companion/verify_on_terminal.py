#!/usr/bin/env python3
"""Settle REVIEW-NOTES.md against a real MetaTrader 5 terminal.

The companion's tests run against fixtures and a fake terminal: they prove the
readers are honest about what a file does and does not contain, and they prove
nothing about MetaTrader itself. Ten claims in ``REVIEW-NOTES.md`` rest on
documentation and inference rather than observation. This script runs the
experiments those notes ask for, on a machine that has the terminal installed,
and prints what it observed in a form that can be pasted back into the pull
request.

It never sends an order. The only tool it will touch on the MCP bridge is one
of the read-only names in ``READ_ONLY_TOOLS``; asking it for anything else
raises rather than degrading. Nothing is written outside ``--out-dir``.

Every check that launches the terminal announces itself first, and nothing
launches without ``--yes`` — without it the script prints the plan, including
the ini each run would use, and exits.

Usage (from the repository root)::

    python examples/mql_companion/verify_on_terminal.py --list
    python examples/mql_companion/verify_on_terminal.py --yes
    python examples/mql_companion/verify_on_terminal.py --yes --json verify.json
    python examples/mql_companion/verify_on_terminal.py --only 1,2,6 --yes
    python examples/mql_companion/verify_on_terminal.py --yes --with-model4

Checks are numbered to match ``REVIEW-NOTES.md``. The ones that need a terminal
are the point of the script; the ones that do not (environment, decode order,
Wine discovery, ``ExpertParameters`` validation) run anywhere and are included
so one command produces one complete report.

Exit status is 0 when no check contradicted an assumption, 1 when one did, and
2 when the terminal is missing and the checks that need one could not run at
all — a broken environment is not a failed assumption, and the two deserve
different responses.
"""

from __future__ import annotations

import json
import platform
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import click

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import tester_report as tr  # noqa: E402

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"
SKIPPED = "skipped"
GLYPH = {PASS: "ok  ", FAIL: "FAIL", UNKNOWN: "?   ", SKIPPED: "--  "}

#: Read-only bridge tools this script is allowed to call. ``mt5_order_send`` is
#: deliberately absent and ``_guard_read_only`` refuses any name that looks like
#: it, so a future edit cannot turn a verification run into a trade.
READ_ONLY_TOOLS: Tuple[str, ...] = (
    "mt5_status",
    "mt5_account",
    "mt5_symbols",
    "mt5_symbol_info",
    "mt5_tick",
    "mt5_rates",
    "mt5_positions",
    "mt5_orders",
    "mt5_calc",
)

CHECKS: Tuple[Tuple[str, str, bool, str], ...] = (
    ("0", "environment: terminal, MetaEditor, out-dir", False, ""),
    ("1", "ForwardMode split vs the documented 1/2, 1/3, 1/4 (notes #1)", True, ""),
    ("2", "forward companion file name (notes #2)", True, ""),
    ("3", "Model=4 real-ticks run (notes #3)", True, "--with-model4"),
    ("4", "process_grace sufficiency (notes #4)", True, "--with-grace"),
    ("5", "_file_is_stable on this filesystem (notes #5)", True, "--with-stability"),
    ("6", "ANSI decode order, cp1251 vs cp1252 (notes #6)", False, ""),
    ("7", "Wine discovery (notes #7)", False, ""),
    ("8", "ExpertParameters subpath rejection (notes #8)", False, ""),
    ("9", "optimization report extension (notes #9)", True, "--with-optimization"),
    ("10", "MCP bridge, read-only smoke", True, "--with-bridge"),
)


def decode_samples() -> List[Tuple[str, bytes, str]]:
    """Bytes a real terminal might write, and what each should read back as.

    The interesting cases are the ambiguous ones. Byte 0x81 is undefined in
    cp1252 and so settles the code page by itself; byte 0x98 is undefined in
    both tables; ``№`` is cp1251's numero sign and cp1252's ``™``, and Russian
    reports use it, so a decoder that prefers Western readings must not touch
    it.
    """
    cyrillic = "Прибыль 1 850,25; Символ EURUSD"
    french = "Bénéfice 1 850,25; Symbole EURUSD"
    numero = "Custom: № 7"
    return [
        ("cp1251 Cyrillic words", cyrillic.encode("cp1251"), cyrillic),
        ("cp1252 accented Latin", french.encode("cp1252"), french),
        ("cp1251 lone numero sign", numero.encode("cp1251"), numero),
        ("byte 0x81, undefined in cp1252", b"Profit \x81 1850.25", "Profit Ѓ 1850.25"),
        ("byte 0x98, undefined in both", b"Profit \x98 1850.25", "Profit \x98 1850.25"),
        (
            "plain ASCII",
            b"Profit 1850.25; Symbol EURUSD",
            "Profit 1850.25; Symbol EURUSD",
        ),
    ]


@dataclass
class Result:
    """What one check observed. ``evidence`` is the part worth pasting back."""

    check_id: str
    title: str
    status: str = SKIPPED
    evidence: List[str] = field(default_factory=list)
    note: str = ""
    seconds: float = 0.0

    def add(self, line: str) -> None:
        self.evidence.append(line)

    def paste(self) -> str:
        """One compact line: id|status|evidence, joined with ``;``."""
        body = ";".join(
            item.replace(";", ",").replace("|", "/") for item in self.evidence
        )
        return f"{self.check_id}|{self.status}|{body[:1200]}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.check_id,
            "title": self.title,
            "status": self.status,
            "evidence": list(self.evidence),
            "note": self.note,
            "seconds": self.seconds,
        }


def _guard_read_only(name: str) -> str:
    """Refuse anything that could move money, before it is called."""
    if name not in READ_ONLY_TOOLS:
        raise ValueError(f"{name} is not a read-only tool; refusing to call it")
    lowered = name.lower()
    if "order_send" in lowered or "send" in lowered or "trade" in lowered:
        raise ValueError(f"{name} looks like it can place an order; refusing")
    return name


def _parse_day(raw: Optional[str]) -> Optional[date]:
    """``2022.01.01`` (with an optional time tail) -> a date, or None."""
    if not raw:
        return None
    text = str(raw).strip().split(" ")[0]
    for fmt in ("%Y.%m.%d", "%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _span(report: Any) -> Tuple[Optional[date], Optional[date]]:
    metrics = getattr(report, "metrics", {}) or {}
    return _parse_day(metrics.get("from_date")), _parse_day(metrics.get("to_date"))


#: A start date in the shape MT5 is documented *not* to parse: the help says
#: "FromDate — starting date of the testing range in format YYYY.MM.DD".
_DASH_DATE = "2022-01-01"
_DASH_DATE_PARSED = date(2022, 1, 1)


# MetaQuotes documents these integers in the terminal help's start-up options:
# "ForwardMode — forward testing mode (0 — off, 1 — 1/2 of the testing period,
# 2 — 1/3 of the testing period, 3 — 1/4 of the testing period, 4 — custom
# interval specified using the ForwardDate parameter)". So the mapping is not
# something this script has to discover — it is something this script can
# *check*, and a build whose halves disagree with the documentation is a finding
# worth pasting back rather than a gap to shrug at.
DOCUMENTED_FORWARD_SHARE: Dict[int, float] = {1: 0.5, 2: 1.0 / 3.0, 3: 0.25}
DOCUMENTED_FORWARD_LABEL: Dict[int, str] = {1: "1/2", 2: "1/3", 3: "1/4"}
# A split lands on a bar boundary, so the observed share sits within a day of the
# documented fraction. Two percentage points is far tighter than the gap between
# 1/2 and 1/3, so it cannot hide a real disagreement.
_SPLIT_TOLERANCE = 0.02


def _split_line(mode: int, back: Any, forward: Any) -> str:
    """Describe one ForwardMode as a back/forward date split, if the reports
    carry dates at all, and say whether it is the split MetaQuotes documents."""
    back_from, back_to = _span(back)
    fwd_from, fwd_to = _span(forward)
    if not (back_from and back_to and fwd_from and fwd_to):
        return f"mode={mode}: reports carry no dates, split not measurable"
    total = (fwd_to - back_from).days or 1
    back_days = (back_to - back_from).days
    back_pct = round(100 * back_days / total)
    line = (
        f"mode={mode}: back {back_from}..{back_to} ({back_days}d) | "
        f"forward {fwd_from}..{fwd_to} ({(fwd_to - fwd_from).days}d) | "
        f"split {back_pct}%/{100 - back_pct}%"
    )
    documented = DOCUMENTED_FORWARD_SHARE.get(mode)
    if documented is None:
        # 0 does not split the period, and 4 takes its date from ForwardDate, so
        # neither has a documented share to compare against.
        return line
    observed = (fwd_to - fwd_from).days / total
    label = DOCUMENTED_FORWARD_LABEL[mode]
    if abs(observed - documented) <= _SPLIT_TOLERANCE:
        return f"{line} | matches the documented {label}"
    return f"{line} | differs from the documented {label}"


class _SizeSampler(threading.Thread):
    """Watch report candidates grow while the terminal runs.

    ``_file_is_stable`` decides a write is finished when size and mtime stop
    moving for one poll interval. That is a guess on a slow or networked
    filesystem, and the only way to test the guess is to watch the file during
    a real run and see whether it kept growing after the reader returned.
    """

    def __init__(self, targets: Sequence[Path], interval: float = 0.2) -> None:
        super().__init__(daemon=True)
        self.targets = list(targets)
        self.interval = interval
        self.samples: List[Tuple[float, str, int]] = []
        # Not ``_stop``: that name belongs to threading.Thread, and shadowing
        # it makes the thread raise TypeError the moment it finishes.
        self._halt = threading.Event()

    def run(self) -> None:
        started = time.monotonic()
        while not self._halt.is_set():
            for target in self.targets:
                try:
                    size = target.stat().st_size
                except OSError:
                    continue
                self.samples.append(
                    (round(time.monotonic() - started, 3), target.name, size)
                )
            self._halt.wait(self.interval)

    def stop(self) -> None:
        self._halt.set()

    def summarize(self, returned_after: float) -> List[str]:
        lines: List[str] = []
        for target in self.targets:
            seen = [(t, size) for t, name, size in self.samples if name == target.name]
            if not seen:
                continue
            previous = 0
            changes: List[float] = []
            for sample_time, size in seen:
                if size != previous:
                    changes.append(sample_time)
                previous = size
            last_change = changes[-1] if changes else seen[0][0]
            grew = seen[-1][1] - seen[0][1]
            quiet = round(returned_after - last_change, 1)
            verdict = (
                "still growing after the reader returned"
                if last_change > returned_after
                else f"quiet for {quiet}s before the reader returned"
            )
            lines.append(
                f"{target.name}: {len(seen)} samples, {grew:+d} bytes, last change at "
                f"{last_change}s -> {verdict}"
            )
        return lines


class Verifier:
    """Runs the checks in order and caches terminal runs by shape.

    A single run with ``ForwardMode=1`` answers both the naming question and
    the first row of the mapping table, so runs are cached by ``(mode, model,
    optimization, grace)`` rather than repeated per check.
    """

    def __init__(
        self,
        *,
        terminal: Optional[Path],
        expert: str,
        symbol: str,
        period: str,
        from_date: str,
        to_date: str,
        out_dir: Path,
        modes: Sequence[int],
        set_file: str,
        timeout: float,
        allow_runs: bool,
    ) -> None:
        self.terminal = terminal
        self.expert = expert
        self.symbol = symbol
        self.period = period
        self.from_date = from_date
        self.to_date = to_date
        self.out_dir = out_dir
        self.modes = list(modes)
        self.set_file = set_file
        self.timeout = timeout
        self.allow_runs = allow_runs
        self._cache: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        self.launches = 0

    # -- one terminal run ---------------------------------------------------

    def _candidates(self, target: Path) -> List[Path]:
        return [
            target,
            target.with_suffix(".htm"),
            target.with_suffix(".xml"),
            target.with_suffix(".forward.htm"),
            target.with_suffix(".forward.xml"),
            Path(str(target) + ".forward.htm"),
            Path(str(target) + ".forward.xml"),
        ]

    def launch(
        self,
        *,
        forward_mode: int,
        tag: str,
        model: Optional[int] = 0,
        optimization: int = 0,
        process_grace: float = 5.0,
        sampler: Optional[_SizeSampler] = None,
        from_date_override: str = "",
    ) -> Dict[str, Any]:
        """One run of the terminal. Never raises: a failure is evidence."""
        key = (
            forward_mode,
            tag,
            model,
            optimization,
            process_grace,
            from_date_override,
        )
        if key in self._cache:
            return self._cache[key]
        target = self.out_dir / f"verify-{tag}-m{forward_mode}.xml"
        ini = tr.build_tester_ini(
            expert=self.expert,
            symbol=self.symbol,
            period=self.period,
            from_date=from_date_override or self.from_date,
            to_date=self.to_date,
            model=model,
            optimization=optimization,
            expert_parameters=self.set_file,
            forward_mode=forward_mode,
            report=str(target.with_suffix("")),
        )
        record: Dict[str, Any] = {
            "tag": tag,
            "ini": ini,
            "target": target,
            "outcome": None,
            "error": None,
            "new_files": [],
            "seconds": 0.0,
        }
        if not self.allow_runs:
            record["error"] = "dry run: --yes not given, no terminal launched"
            self._cache[key] = record
            return record
        if self.terminal is None:
            record["error"] = "no terminal64.exe found"
            self._cache[key] = record
            return record

        before = self._snapshot()
        started = time.monotonic()
        try:
            self.launches += 1
            record["outcome"] = tr.run_tester(
                terminal=self.terminal,
                ini_text=ini,
                report_path=target,
                timeout=self.timeout,
                parser=tr.parse_any_report,
                forward=forward_mode > 0,
                process_grace=process_grace,
            )
        except (TimeoutError, FileNotFoundError, OSError, ValueError) as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        record["seconds"] = round(time.monotonic() - started, 2)
        record["new_files"] = [
            str(p)
            for p in sorted(self._snapshot() - before)
            # The ini is ours, not the terminal's output.
            if p.suffix.lower() != ".ini"
        ]
        record["new_files"] += [
            f"(install dir) {p}" for p in self._install_dir_files(started)
        ]
        if sampler is not None:
            record["sampling"] = sampler.summarize(record["seconds"])
        self._cache[key] = record
        return record

    def _snapshot(self) -> set:
        try:
            return {p for p in self.out_dir.iterdir() if p.is_file()}
        except OSError:
            return set()

    def _install_dir_files(self, since: float) -> List[Path]:
        """MT5 resolves ``Report=`` against its *installation* directory, so a
        run can produce files somewhere other than the path we asked for. When
        that happens the reviewer needs to know where they landed."""
        if self.terminal is None:
            return []
        root = self.terminal.parent
        found: List[Path] = []
        try:
            for path in sorted(root.rglob("verify-*")):
                if not path.is_file() or path.stat().st_mtime < since - 1:
                    continue
                if path.suffix.lower() == ".ini":
                    continue
                try:
                    if self.out_dir.resolve() in path.resolve().parents:
                        continue  # already counted by the out-dir snapshot
                except OSError:
                    pass
                found.append(path)
        except OSError:
            return []
        return found[:20]

    # -- the checks ---------------------------------------------------------

    def check_0_environment(self) -> Result:
        result = Result("0", CHECKS[0][1])
        result.add(f"platform={platform.platform()}")
        result.add(f"python={platform.python_version()}")
        result.add(f"terminal={self.terminal or 'NOT FOUND'}")
        metaeditor = _find_metaeditor()
        result.add(f"metaeditor={metaeditor or 'NOT FOUND'}")
        result.add(f"expert={self.expert}")
        result.add(
            f"period={self.symbol} {self.period} {self.from_date}..{self.to_date}"
        )
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            probe = self.out_dir / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            result.add(f"out_dir={self.out_dir} (writable)")
        except OSError as exc:
            result.add(f"out_dir={self.out_dir} NOT WRITABLE: {exc}")
            result.status = FAIL
            return result
        result.add(f"MetaTrader5 package={'yes' if _have_mt5_package() else 'no'}")
        if self.terminal is None:
            result.status = FAIL
            result.note = (
                "No terminal: pass --terminal C:\\...\\terminal64.exe (or set "
                "TERMINAL_PATH). Checks 1-5, 9 and 10 need one."
            )
            return result
        result.status = PASS
        return result

    def check_1_forward_mode_mapping(self) -> Result:
        result = Result("1", CHECKS[1][1])
        started = time.monotonic()
        saw_forward = 0
        wrong_zero = False
        for mode in self.modes:
            record = self.launch(forward_mode=mode, tag="map")
            if record["error"]:
                result.add(f"mode={mode}: {record['error']}")
                continue
            outcome = record["outcome"] or {}
            report = outcome.get("report")
            forward = outcome.get("forward_report")
            new = [Path(p).name for p in record["new_files"] if not p.startswith("(")]
            result.add(f"mode={mode}: new files {new or 'none'}")
            if mode == 0 and forward is not None:
                wrong_zero = True
                result.add("mode=0 produced a forward half, which should not split")
            if forward is None:
                if mode != 0:
                    note = outcome.get("forward_note", "")
                    result.add(f"mode={mode}: no forward half. {note[:180]}")
                continue
            saw_forward += 1
            result.add(_split_line(mode, report, forward))

        # The date rules in tester_ini_warnings are read off MetaQuotes' config
        # documentation, not observed. This launch asks the terminal which
        # reading is true by requesting a start date in the shape the help says
        # it does not parse, then looks at the period the report actually covers.
        if self.allow_runs:
            record = self.launch(
                forward_mode=0, tag="bad-date", from_date_override=_DASH_DATE
            )
            if record["error"]:
                result.add(f"dash date: {record['error']}")
            else:
                outcome = record["outcome"] or {}
                saw_from, _saw_to = _span(outcome.get("report"))
                if saw_from == _DASH_DATE_PARSED:
                    verdict = (
                        "the terminal tested exactly that range, so it does read "
                        "dashes and the YYYY.MM.DD warning is too strict — "
                        "soften it and say so in the PR"
                    )
                elif saw_from is None:
                    verdict = "the report carries no from-date, so this says nothing"
                else:
                    verdict = (
                        f"the terminal tested from {saw_from} instead, which is the "
                        "silent fallback the warning describes"
                    )
                result.add(f"FromDate='{_DASH_DATE}' requested: {verdict}")
        result.seconds = round(time.monotonic() - started, 2)
        if wrong_zero:
            result.status = FAIL
            result.note = "ForwardMode=0 must not split the period."
        elif saw_forward:
            result.status = PASS
            disagreed = [
                line
                for line in result.evidence
                if "differs from the documented" in line
            ]
            result.note = "Paste the mode= lines back. " + (
                "This build splits the period differently from MetaQuotes' "
                "documented 1/2, 1/3 and 1/4. Keep the observed numbers: they are "
                "what this terminal actually does, and the docs should carry both "
                "readings with the build that produced each."
                if disagreed
                else "Every observed split matches the documented 1/2, 1/3 and "
                "1/4, so references/optimization.md and note 1 in REVIEW-NOTES.md "
                "are confirmed on this build."
            )
        else:
            result.status = UNKNOWN
            result.note = (
                "No forward half from a single test. Forward splitting may be an "
                "optimizer-only feature, in which case README.md and "
                "references/optimization.md are wrong about single tests and the "
                "pairing rule needs an optimization run to exercise it: re-run "
                "with --with-optimization --set-file <name>.set."
            )
        return result

    def check_2_forward_file_names(self) -> Result:
        result = Result("2", CHECKS[2][1])
        started = time.monotonic()
        record = self.launch(forward_mode=1, tag="map")
        if record["error"]:
            result.status = UNKNOWN
            result.add(record["error"])
            result.seconds = round(time.monotonic() - started, 2)
            return result
        outcome = record["outcome"] or {}
        report = outcome.get("report")
        back_path = Path(str(outcome.get("report_path", record["target"])))
        companions = tr.forward_companion(back_path) if report is not None else []
        # forward_companion() lists the names MT5 *may* have written; only the
        # ones on disk say anything about what it did write.
        present = [path for path in companions if path.exists()]
        result.add(f"back report: {back_path.name}")
        result.add(f"forward_companion() candidates: {[p.name for p in companions]}")
        result.add(f"present on disk: {[p.name for p in present] or 'none'}")
        landed = outcome.get("forward_path")
        landed_name = Path(str(landed)).name if landed else "none"
        result.add(f"run_tester picked as forward half: {landed_name}")
        dotted = [p for p in record["new_files"] if ".forward" in p]
        result.add(f"files containing '.forward': {dotted or 'none'}")
        if present and landed:
            check = tr.check_forward(report, outcome.get("forward_report"))
            result.add(f"verdict={check.verdict}")
            for reason in list(check.reasons)[:4]:
                result.add(f"  reason: {reason}")
            result.status = PASS
            result.note = "The name-based pairing rule matches what MT5 wrote."
        elif report is not None:
            result.status = FAIL
            result.note = (
                "MT5 wrote something, but not under the name forward_companion() "
                "looks for. Paste the file list back: the pairing rule in "
                "tester_report.py and the tables in README.md / "
                "references/optimization.md all state '<name>.forward.htm'."
            )
        else:
            result.status = UNKNOWN
        result.seconds = round(time.monotonic() - started, 2)
        return result

    def check_3_model4(self) -> Result:
        result = Result("3", CHECKS[3][1])
        started = time.monotonic()
        plain = self.launch(forward_mode=0, tag="model0")
        real = self.launch(forward_mode=0, tag="model4", model=4)
        for label, record in (("model 0", plain), ("model 4", real)):
            if record["error"]:
                result.add(f"{label}: {record['error']}")
                continue
            outcome = record["outcome"] or {}
            result.add(
                f"{label}: {record['seconds']}s, exit={outcome.get('exit_code')}, "
                f"report={Path(str(outcome.get('report_path', ''))).name or 'none'}"
            )
        result.seconds = round(time.monotonic() - started, 2)
        if real["error"]:
            result.status = UNKNOWN
            return result
        result.status = UNKNOWN
        result.note = (
            "Timings only. Whether real ticks freeze the terminal's UI thread is "
            "not observable from a script: watch the terminal during the model 4 "
            "run and say whether it stayed responsive. If the ratio model4/model0 "
            "is large, the caveat in README.md is worth keeping as it is."
        )
        return result

    def check_4_process_grace(self) -> Result:
        result = Result("4", CHECKS[4][1])
        started = time.monotonic()
        zero = self.launch(forward_mode=0, tag="grace0", process_grace=0.0)
        five = self.launch(forward_mode=0, tag="grace5", process_grace=5.0)
        codes: Dict[str, Any] = {}
        for label, record in (("grace=0", zero), ("grace=5", five)):
            if record["error"]:
                result.add(f"{label}: {record['error']}")
                continue
            outcome = record["outcome"] or {}
            codes[label] = outcome.get("exit_code")
            result.add(f"{label}: exit_code={outcome.get('exit_code')}")
        result.seconds = round(time.monotonic() - started, 2)
        if len(codes) < 2:
            result.status = UNKNOWN
            return result
        if codes["grace=0"] is None and codes["grace=5"] is not None:
            result.status = PASS
            result.note = (
                "The race is real on this machine and 5s covers it: without the "
                "grace the run returns exit_code=None."
            )
        elif codes["grace=5"] is None:
            result.status = FAIL
            result.note = (
                "5s was not enough here. Raise the process_grace default in "
                "tester_report.py (the parameter is already exposed to callers)."
            )
        else:
            result.status = UNKNOWN
            result.note = (
                "Both runs returned an exit code, so no race was observable on "
                "this machine. That does not prove 5s is enough on a slower one."
            )
        return result

    def check_5_file_stability(self) -> Result:
        result = Result("5", CHECKS[5][1])
        started = time.monotonic()
        target = self.out_dir / "verify-stability-m0.xml"
        sampler = _SizeSampler(self._candidates(target), interval=0.2)
        sampler.start()
        try:
            record = self.launch(forward_mode=0, tag="stability", sampler=sampler)
        finally:
            sampler.stop()
            sampler.join(timeout=2.0)
        if record["error"]:
            result.status = UNKNOWN
            result.add(record["error"])
            result.seconds = round(time.monotonic() - started, 2)
            return result
        for line in record.get("sampling", []):
            result.add(line)
        outcome = record["outcome"] or {}
        report = outcome.get("report")
        if report is not None:
            metrics = getattr(report, "metrics", {}) or {}
            missing = getattr(report, "missing", []) or []
            result.add(f"parsed metrics={len(metrics)}, missing={len(missing)}")
            result.add(f"warnings={list(getattr(report, 'warnings', []))[:3]}")
        result.seconds = round(time.monotonic() - started, 2)
        grew_after = any("still growing after" in line for line in result.evidence)
        if grew_after:
            result.status = FAIL
            result.note = (
                "The file kept changing after the reader returned, so "
                "_file_is_stable declared the write finished too early. Raise "
                "poll_interval or require two stable samples."
            )
        elif result.evidence:
            result.status = PASS
            result.note = "One run is a small sample; a network share is the real test."
        else:
            result.status = UNKNOWN
        return result

    def check_6_decode_order(self) -> Result:
        result = Result("6", CHECKS[6][1])
        mangled = 0
        for label, raw, expected in decode_samples():
            got = tr.decode_report_bytes(raw)
            if got == expected:
                result.add(f"{label}: reads back exactly")
            else:
                mangled += 1
                result.add(f"{label}: MANGLED -> {got!r}, wanted {expected!r}")
        try:
            produced = [
                p
                for p in sorted(self.out_dir.glob("*"))
                if p.suffix in (".htm", ".xml")
            ][:4]
        except OSError:
            produced = []
        for path in produced:
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            non_ascii = [b for b in raw if b > 127][:24]
            if non_ascii:
                result.add(
                    f"{path.name}: {len(non_ascii)} non-ASCII bytes -> "
                    f"{tr.decode_report_bytes(raw)[:120]!r}"
                )
        if mangled:
            result.status = FAIL
            result.note = (
                "A sample came back mangled. cp1251 leaves one byte value "
                "undefined where cp1252 leaves five, so it wins almost any "
                "first-match contest; "
                "decode_report_bytes only prefers cp1252 when the cp1251 reading "
                "has no Cyrillic words and every differing character is a "
                "Cyrillic-block char where cp1252 has a Western accent. Paste "
                "the mangled line back with the terminal's language."
            )
        else:
            result.status = PASS
            result.note = (
                "Every sample round-trips, including the ambiguous ones. Real "
                "confirmation still needs a report from a non-English terminal."
            )
        return result

    def check_7_wine(self) -> Result:
        result = Result("7", CHECKS[7][1])
        if sys.platform == "win32":
            result.status = SKIPPED
            result.note = "Windows: Wine discovery does not apply."
            return result
        try:
            import metaeditor as me
        except ImportError as exc:
            result.status = UNKNOWN
            result.add(f"cannot import metaeditor: {exc}")
            return result
        candidates = me.candidate_metaeditor_paths()
        result.add(f"{len(candidates)} candidate paths, first 6:")
        for path in candidates[:6]:
            result.add(f"  {path} {'EXISTS' if path.exists() else ''}")
        found = me.find_metaeditor()
        result.add(f"find_metaeditor() -> {found or 'NOT FOUND'}")
        if found is not None:
            result.add(f"needs_wine({found.name}) -> {me.needs_wine(found)}")
        result.status = PASS if found is not None else UNKNOWN
        result.note = (
            "If MetaEditor lives in a prefix this list does not cover, set "
            "METAEDITOR_PATH and say which prefix it was."
        )
        return result

    def check_8_expert_parameters(self) -> Result:
        result = Result("8", CHECKS[8][1])
        cases = (
            ("MyEA.set", 0),
            ("Tester\\MyEA.set", 1),
            ("MyEA.txt", 1),
            ("", 0),
        )
        for value, expected in cases:
            warnings = tr.tester_ini_warnings(
                expert="MyEA",
                expert_parameters=value,
                optimization=1 if value else 0,
                model=0,
                report="reports/MyEA",
            )
            relevant = [
                w for w in warnings if "ExpertParameters" in w or "not possible" in w
            ]
            shown = value or "(empty)"
            if expected and not relevant:
                result.status = FAIL
                result.add(f"{shown}: expected a warning, got none")
                continue
            if not expected and relevant:
                result.add(f"{shown}: warned unexpectedly -> {relevant[0][:120]}")
            else:
                result.add(f"{shown}: {relevant[0][:120] if relevant else 'accepted'}")
        result.status = result.status if result.status == FAIL else PASS
        result.note = (
            "This shows what our validator does. What MT5 itself accepts is the "
            "open question: if a subpath works on a real terminal, the rejection "
            "should become a note. Test it by putting a .set in "
            "MQL5/Profiles/Tester/ and running an optimization."
        )
        return result

    def check_9_optimization_extension(self) -> Result:
        result = Result("9", CHECKS[9][1])
        started = time.monotonic()
        if not self.set_file:
            result.status = SKIPPED
            result.note = (
                "Needs a .set file in MQL5/Profiles/Tester/ to optimize with: "
                "re-run with --set-file MyEA.set. Without one MT5 says "
                "'Optimization is not possible'."
            )
            return result
        record = self.launch(forward_mode=0, tag="opt", optimization=1)
        result.seconds = round(time.monotonic() - started, 2)
        if record["error"]:
            result.status = UNKNOWN
            result.add(record["error"])
            return result
        new = [Path(p).name for p in record["new_files"] if not p.startswith("(")]
        result.add(f"new files: {new or 'none'}")
        outcome = record["outcome"] or {}
        report = outcome.get("report")
        if report is None:
            result.status = FAIL
            result.add("no report parsed")
            return result
        passes = getattr(report, "passes", None)
        if passes:
            # The header lives on the table, not on a row: a pass carries
            # .number/.metrics/.inputs, and .columns names every column.
            columns = list(getattr(report, "columns", []))[:8]
            result.add(f"optimization table: {len(passes)} passes")
            result.add(f"columns: {columns}")
            result.add(
                f"parameter_names: {list(getattr(report, 'parameter_names', []))}"
            )
            result.status = PASS
            result.note = (
                "An optimization wrote .xml as documented, and parse_any_report "
                "sniffed it as a table."
            )
        else:
            metrics = getattr(report, "metrics", {}) or {}
            result.add(f"parsed as a testing report instead: {len(metrics)} metrics")
            result.status = FAIL
            result.note = (
                "The optimization run did not produce a pass table. Either the "
                ".set was rejected (check the terminal journal) or the extension "
                "assumption is wrong."
            )
        return result

    def check_10_bridge(self) -> Result:
        result = Result("10", CHECKS[10][1])
        try:
            import mt5_mcp_server as bridge
        except ImportError as exc:
            result.status = SKIPPED
            result.note = f"bridge not importable here: {exc}"
            return result
        if not _have_mt5_package():
            result.status = SKIPPED
            result.note = (
                "The MetaTrader5 package is Windows-only, so the live tools "
                "cannot be reached from this machine. Install it next to the "
                "terminal and re-run with --with-bridge."
            )
            return result
        server = bridge.build_server() if hasattr(bridge, "build_server") else None
        names = _tool_names(bridge, server)
        order_like = [n for n in names if "order_send" in n]
        result.add(f"{len(names)} tools registered; order-capable: {order_like}")
        result.add(f"read-only tools this script may call: {list(READ_ONLY_TOOLS)}")
        called = 0
        for name in READ_ONLY_TOOLS[:3]:
            _guard_read_only(name)
            if name not in names:
                continue
            try:
                outcome = _call_tool(bridge, server, name, {"symbol": self.symbol})
            except Exception as exc:  # noqa: BLE001 - evidence, not control flow
                result.add(f"{name}: raised {type(exc).__name__}: {exc}")
                continue
            called += 1
            result.add(f"{name}: {str(outcome)[:160]}")
        result.status = PASS if called else UNKNOWN
        result.note = (
            "Read-only calls against a live terminal. Nothing here can place an "
            "order; the guard raises on any name outside READ_ONLY_TOOLS."
        )
        return result

    # -- driving ------------------------------------------------------------

    def run(
        self, only: Sequence[str], skip: Sequence[str], opt_in: Dict[str, bool]
    ) -> List[Result]:
        methods = {
            "0": self.check_0_environment,
            "1": self.check_1_forward_mode_mapping,
            "2": self.check_2_forward_file_names,
            "3": self.check_3_model4,
            "4": self.check_4_process_grace,
            "5": self.check_5_file_stability,
            "6": self.check_6_decode_order,
            "7": self.check_7_wine,
            "8": self.check_8_expert_parameters,
            "9": self.check_9_optimization_extension,
            "10": self.check_10_bridge,
        }
        results: List[Result] = []
        for check_id, title, needs_terminal, flag in CHECKS:
            if only and check_id not in only:
                continue
            if check_id in skip:
                continue
            if flag and not opt_in.get(flag):
                results.append(
                    Result(check_id, title, SKIPPED, note=f"opt-in: pass {flag}")
                )
                continue
            if needs_terminal and self.terminal is None and check_id != "0":
                results.append(
                    Result(check_id, title, SKIPPED, note="no terminal found")
                )
                continue
            if needs_terminal and not self.allow_runs and check_id not in ("10",):
                results.append(
                    Result(
                        check_id,
                        title,
                        SKIPPED,
                        note="dry run: pass --yes to launch the terminal",
                    )
                )
                continue
            try:
                results.append(methods[check_id]())
            except Exception as exc:  # noqa: BLE001 - a check must not kill the run
                failed = Result(check_id, title, FAIL)
                failed.add(f"{type(exc).__name__}: {exc}")
                failed.note = "The check itself raised; that is worth reporting."
                results.append(failed)
        return results


def _find_metaeditor() -> Optional[Path]:
    try:
        import metaeditor as me
    except ImportError:
        return None
    return me.find_metaeditor()


def _have_mt5_package() -> bool:
    try:
        import MetaTrader5  # noqa: F401
    except ImportError:
        return False
    return True


def _tool_names(bridge: Any, server: Any) -> List[str]:
    if server is not None:
        registry = getattr(server, "tools", None) or getattr(server, "_tools", None)
        if isinstance(registry, dict):
            return sorted(registry)
        if isinstance(registry, (list, tuple)):
            return sorted(getattr(item, "name", str(item)) for item in registry)
    builder = getattr(bridge, "build_tools", None)
    if callable(builder):
        try:
            return sorted(getattr(item, "name", str(item)) for item in builder())
        except Exception:  # noqa: BLE001
            return []
    return []


def _call_tool(bridge: Any, server: Any, name: str, args: Dict[str, Any]) -> Any:
    caller = getattr(bridge, "call_tool", None)
    if callable(caller):
        return caller(name, args)
    if server is not None and hasattr(server, "call"):
        return server.call(name, args)
    raise LookupError(f"no way to invoke {name} on this bridge")


def render_report(results: Sequence[Result], verifier: Verifier) -> str:
    lines = [
        "MQL companion - terminal verification",
        f"when: {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}",
        f"host: {platform.platform()} / python {platform.python_version()}",
        f"terminal: {verifier.terminal or 'not found'}",
        f"out-dir: {verifier.out_dir}",
        "",
    ]
    for item in results:
        head = f"[{item.check_id}] {item.title} ... {GLYPH[item.status]}"
        if item.seconds:
            head += f" ({item.seconds:.1f}s)"
        lines.append(head)
        for entry in item.evidence:
            lines.append(f"    {entry}")
        if item.note:
            lines.append(f"    note: {item.note}")
        lines.append("")
    counts: Dict[str, int] = {}
    for item in results:
        counts[item.status] = counts.get(item.status, 0) + 1
    summary = ", ".join(
        f"{key}={counts[key]}"
        for key in (PASS, FAIL, UNKNOWN, SKIPPED)
        if key in counts
    )
    lines += [
        f"terminal launches: {verifier.launches}",
        f"summary: {summary}",
        "",
        "----- paste this back -----",
        f"host={platform.system()} {platform.release()}; "
        f"python={platform.python_version()}; "
        f"terminal={verifier.terminal or 'none'}; launches={verifier.launches}",
    ]
    lines += [item.paste() for item in results]
    lines.append("----- end -----")
    return "\n".join(lines)


def _id_list(raw: str) -> List[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--terminal", default=None, help="Path to terminal64.exe (else auto-discover)."
)
@click.option("--expert", default="Examples/MACD/MACD Sample", show_default=True)
@click.option("--symbol", default="EURUSD", show_default=True)
@click.option("--period", default="H1", show_default=True)
@click.option("--from-date", default="2022.01.01", show_default=True)
@click.option("--to-date", default="2023.03.31", show_default=True)
@click.option(
    "--out-dir",
    default="verify-reports",
    show_default=True,
    help="Where reports are written. Created if missing; MT5 will not create it.",
)
@click.option(
    "--modes",
    default="0,1,2,3",
    show_default=True,
    help="ForwardMode values to try for the mapping table.",
)
@click.option(
    "--set-file", default="", help="A .set under MQL5/Profiles/Tester for check 9."
)
@click.option(
    "--timeout", default=1800.0, show_default=True, help="Seconds per terminal run."
)
@click.option("--only", default="", help="Run just these check ids, e.g. 1,2,6.")
@click.option("--skip", default="", help="Skip these check ids.")
@click.option(
    "--with-model4", is_flag=True, help="Opt in to the real-ticks run (slow)."
)
@click.option(
    "--with-grace", is_flag=True, help="Opt in to the two extra process_grace runs."
)
@click.option("--with-stability", is_flag=True, help="Opt in to the sampled-write run.")
@click.option(
    "--with-optimization", is_flag=True, help="Opt in to an optimization run."
)
@click.option(
    "--with-bridge", is_flag=True, help="Opt in to read-only MCP bridge calls."
)
@click.option(
    "--yes", is_flag=True, help="Actually launch the terminal. Without it: plan only."
)
@click.option(
    "--json", "json_path", default=None, help="Also write the results as JSON."
)
@click.option("--list", "list_checks", is_flag=True, help="Print the checks and exit.")
def main(**options: Any) -> None:
    """Verify the REVIEW-NOTES.md assumptions on a machine with MT5 installed."""
    if options["list_checks"]:
        for check_id, title, needs_terminal, flag in CHECKS:
            extra = f" [{flag}]" if flag else ""
            where = "terminal" if needs_terminal else "local"
            click.echo(f"  {check_id:>2}  ({where}) {title}{extra}")
        return

    out_dir = Path(options["out_dir"]).expanduser().resolve()
    verifier = Verifier(
        terminal=tr.find_terminal(options["terminal"]),
        expert=options["expert"],
        symbol=options["symbol"],
        period=options["period"],
        from_date=options["from_date"],
        to_date=options["to_date"],
        out_dir=out_dir,
        modes=[int(m) for m in _id_list(options["modes"])],
        set_file=options["set_file"],
        timeout=float(options["timeout"]),
        allow_runs=bool(options["yes"]),
    )
    opt_in = {
        "--with-model4": options["with_model4"],
        "--with-grace": options["with_grace"],
        "--with-stability": options["with_stability"],
        "--with-optimization": options["with_optimization"],
        "--with-bridge": options["with_bridge"],
    }
    results = verifier.run(_id_list(options["only"]), _id_list(options["skip"]), opt_in)
    click.echo(render_report(results, verifier))
    if options["json_path"]:
        payload = {
            "host": platform.platform(),
            "python": platform.python_version(),
            "terminal": str(verifier.terminal or ""),
            "launches": verifier.launches,
            "results": [item.as_dict() for item in results],
        }
        Path(options["json_path"]).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        click.echo(f"wrote {options['json_path']}")

    selected = {item.check_id for item in results}
    needs_terminal = [check for check in CHECKS if check[2] and check[0] in selected]
    if needs_terminal and verifier.terminal is None:
        # Nothing that needs a terminal could run: an environment problem, not
        # a failed assumption.
        sys.exit(2)
    if any(item.status == FAIL for item in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
