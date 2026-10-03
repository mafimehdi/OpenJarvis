#!/usr/bin/env python3
"""MetaEditor command-line compiler helpers for MQL4/MQL5 sources.

MetaQuotes ships no standalone MQL compiler: the only supported command-line
path is MetaEditor itself::

    metaeditor64.exe /compile:"C:\\...\\MyEA.mq5" /inc:"C:\\...\\MQL5" /log

On Linux/macOS the same binary works under Wine::

    wine ~/.wine/drive_c/.../metaeditor64.exe /compile:... /log:...

Facts this module is built around (see the MQL5 docs "Compiling from the
command line" and mql5.com forum threads):

* ``/log`` with no argument writes ``<source>.log`` next to the source file;
  ``/log:<path>`` writes to an explicit path.
* The log is **UTF-16LE** (with a BOM on most builds).
* Diagnostic lines look like ``<path>(<line>,<col>) : error: <message>``;
  some builds emit ``<message>\\t<file>\\t<line>\\t<col>`` instead.
* The run ends with a summary such as ``Result: 0 errors, 0 warnings`` or
  ``0 error(s), 0 warning(s)``.
* The process exit code is not a reliable success signal (it varies by build
  and by whether the GUI was already running), so success is decided from the
  parsed log plus the presence of a fresh ``.ex5`` / ``.ex4`` artifact.

The parser is deliberately tolerant: unknown non-empty lines are preserved in
:attr:`CompileResult.other_lines` so an LLM fix-up loop still sees the full
compiler output instead of only the lines we managed to structure.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Environment variables checked (in order) for an explicit MetaEditor path.
METAEDITOR_ENV_VARS: Tuple[str, ...] = ("METAEDITOR_PATH", "METAEDITOR", "MQL_EDITOR")

#: Source extensions understood by the MQL toolchain.
MQL_SOURCE_EXTS: Tuple[str, ...] = (".mq5", ".mq4", ".mqh", ".mq")

#: Compiled artifact extension per dialect.
ARTIFACT_EXT: Dict[str, str] = {".mq5": ".ex5", ".mq4": ".ex4", ".mqh": ".ex5"}

#: Default include directory name per dialect (relative to the data folder).
INCLUDE_DIR_NAME: Dict[str, str] = {".mq5": "MQL5", ".mq4": "MQL4"}

_DEFAULT_TIMEOUT = 180
_LOG_POLL_INTERVAL = 0.25

# `<path>(<line>,<col>) : error C2065: 'x' - undeclared identifier`
_POSITIONAL_RE = re.compile(
    r"""^\s*
        (?P<file>[^\t]+?)
        \((?P<line>\d+)\s*,\s*(?P<col>\d+)\)
        \s*:\s*
        (?P<sev>fatal\s+error|error|warning|information)
        \s*:?\s*
        (?P<msg>.*)$""",
    re.IGNORECASE | re.VERBOSE,
)

# `'x' - undeclared identifier\tMyEA.mq5\t37\t12` (GUI "Errors" tab shape)
_TABULAR_RE = re.compile(
    r"^(?P<msg>[^\t]+)\t(?P<file>[^\t]+)\t(?P<line>\d+)\t(?P<col>\d+)\s*$"
)

# `Result: 0 errors, 0 warnings` / `0 error(s), 0 warning(s)`
_SUMMARY_RE = re.compile(
    r"(?P<errors>\d+)\s*error(?:s|\(s\))?\s*,\s*(?P<warnings>\d+)\s*warning"
    r"(?:s|\(s\))?",
    re.IGNORECASE,
)

_SEVERITY_ERROR = ("fatal error", "error")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MqlDiagnostic:
    """One compiler diagnostic (error, warning, or informational note)."""

    severity: str  # "error" | "warning" | "information"
    message: str
    file: str = ""
    line: int = 0
    column: int = 0

    @property
    def is_error(self) -> bool:
        return self.severity in _SEVERITY_ERROR

    def format(self) -> str:
        """Render as ``file(line,col) severity: message`` (or without loc)."""
        loc = f"{self.file}({self.line},{self.column})" if self.file else "<unknown>"
        if self.line:
            return f"{loc} : {self.severity}: {self.message}"
        return f"{self.severity}: {self.message}"


@dataclass(slots=True)
class CompileResult:
    """Outcome of one MetaEditor compile invocation."""

    source: Path
    ok: bool = False
    exit_code: Optional[int] = None
    log_path: Optional[Path] = None
    raw_log: str = ""
    diagnostics: List[MqlDiagnostic] = field(default_factory=list)
    #: Non-empty log lines the parser could not structure. Kept so an LLM
    #: fix-up loop still sees the complete compiler output.
    other_lines: List[str] = field(default_factory=list)
    summary_errors: Optional[int] = None
    summary_warnings: Optional[int] = None
    artifact: Optional[Path] = None
    duration_seconds: float = 0.0
    command: List[str] = field(default_factory=list)
    error_text: str = ""
    #: Advisory note that does not by itself make the build a failure (e.g. a
    #: clean log with no artifact written).
    note: str = ""

    @property
    def errors(self) -> List[MqlDiagnostic]:
        return [d for d in self.diagnostics if d.is_error]

    @property
    def warnings(self) -> List[MqlDiagnostic]:
        return [d for d in self.diagnostics if d.severity == "warning"]

    @property
    def error_count(self) -> int:
        """The worse of the summary count and the diagnostics actually parsed.

        The summary is usually authoritative and can legitimately be the
        *higher* number: some builds count errors in an included file that never
        surface as diagnostics of their own. It may never be the lower one. A
        log carrying two ``error C2065`` lines under a ``0 errors`` summary
        contradicts itself, and believing the summary reports a clean build that
        did not happen.
        """
        parsed = len(self.errors)
        if self.summary_errors is None:
            return parsed
        return max(self.summary_errors, parsed)

    def format_for_prompt(self, *, max_items: int = 30) -> str:
        """Compact, LLM-friendly rendering of everything the compiler said."""
        lines: List[str] = []
        errors = self.errors
        warnings = self.warnings
        lines.append(
            f"compile result: {'OK' if self.ok else 'FAILED'} "
            f"({len(errors)} error(s), {len(warnings)} warning(s))"
        )
        for diag in errors[:max_items]:
            lines.append(f"  {diag.format()}")
        if len(errors) > max_items:
            lines.append(f"  ... {len(errors) - max_items} more error(s)")
        for diag in warnings[:max_items]:
            lines.append(f"  {diag.format()}")
        for extra in self.other_lines[:max_items]:
            lines.append(f"  | {extra}")
        if self.note:
            lines.append(f"  note: {self.note}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Locating MetaEditor
# ---------------------------------------------------------------------------


def _glob_candidates(base: Path, pattern: str) -> List[Path]:
    try:
        return sorted(p for p in base.glob(pattern) if p.is_file())
    except OSError:
        return []


def candidate_metaeditor_paths() -> List[Path]:
    """Return plausible MetaEditor locations, most specific first.

    Covers: explicit env vars, native Windows installs (including
    broker-branded folders and portable data folders under ``%APPDATA%``),
    and Wine prefixes on Linux/macOS.
    """
    out: List[Path] = []

    for var in METAEDITOR_ENV_VARS:
        raw = os.environ.get(var)
        if raw:
            out.append(Path(raw).expanduser())

    names = ("metaeditor64.exe", "metaeditor.exe")

    if os.name == "nt":
        roots = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
            Path(os.environ.get("LOCALAPPDATA", "") or "C:/nonexistent"),
        ]
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(Path(appdata) / "MetaQuotes" / "Terminal")
        for root in roots:
            for name in names:
                out.append(root / "MetaTrader 5" / name)
                out.append(root / "MetaTrader 4" / name)
            # Broker-branded installs: "RoboForex - MetaTrader 5", "IC Markets", ...
            for pattern in ("*MetaTrader*/*", "*MT5*/*", "*MT4*/*"):
                out.extend(_glob_candidates(root, pattern + "/metaeditor*.exe"))
            # Portable installs under the per-broker data folder.
            out.extend(_glob_candidates(root, "*/*/metaeditor64.exe"))
    else:
        home = Path.home()
        wine_roots = [home / ".wine" / "drive_c" / "Program Files"]
        wine_roots.append(home / ".wine" / "drive_c" / "Program Files (x86)")
        # Common symlinked prefixes, e.g. ~/.wine/MT5 -> drive_c/.../MT5
        for prefix in sorted(p for p in home.glob(".wine*") if p.is_dir()):
            wine_roots.append(prefix)
            wine_roots.append(prefix / "drive_c" / "Program Files")
        for root in wine_roots:
            for pattern in (
                "*MetaTrader*/metaeditor64.exe",
                "*MT5*/metaeditor64.exe",
                "*/metaeditor64.exe",
            ):
                out.extend(_glob_candidates(root, pattern))

    # Finally, anything already on PATH.
    for name in names:
        found = shutil.which(name)
        if found:
            out.append(Path(found))

    # De-duplicate while preserving order; keep only existing files.
    seen: set[str] = set()
    existing: List[Path] = []
    for path in out:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            existing.append(path)
    return existing


def find_metaeditor(explicit: Optional[str] = None) -> Optional[Path]:
    """Resolve the MetaEditor binary to use, or ``None`` when not found."""
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path
        return None
    candidates = candidate_metaeditor_paths()
    return candidates[0] if candidates else None


def needs_wine(metaeditor: Path) -> bool:
    """True when the binary is a Windows PE executable on a non-Windows host."""
    return os.name != "nt" and metaeditor.suffix.lower() == ".exe"


def guess_include_dir(metaeditor: Path, source: Path) -> Optional[Path]:
    """Guess the ``/inc`` directory (the terminal data folder's MQL5/MQL4).

    MetaEditor resolves includes relative to the *data folder* of the terminal
    it belongs to, which for portable installs sits next to the binary and for
    normal installs under ``%APPDATA%/MetaQuotes/Terminal/<hash>``.
    """
    dialect_dir = INCLUDE_DIR_NAME.get(source.suffix.lower(), "MQL5")

    # Portable install: <install dir>/MQL5
    sibling = metaeditor.parent / dialect_dir
    if sibling.is_dir():
        return sibling

    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            base = Path(appdata) / "MetaQuotes" / "Terminal"
            for child in sorted(base.glob("*")) if base.is_dir() else []:
                candidate = child / dialect_dir
                if candidate.is_dir():
                    return candidate
    else:
        home = Path.home()
        for prefix in sorted(p for p in home.glob(".wine*") if p.is_dir()):
            base = prefix / "drive_c" / "users"
            for child in (
                sorted(base.glob("*/*/MetaQuotes/Terminal/*"))
                if (base.is_dir())
                else []
            ):
                candidate = Path(child) / dialect_dir
                if candidate.is_dir():
                    return candidate
    return None


# ---------------------------------------------------------------------------
# Log decoding + parsing
# ---------------------------------------------------------------------------


def decode_compile_log(raw: bytes) -> str:
    """Decode a MetaEditor log, which is UTF-16LE on most builds.

    Tries, in order: a UTF-16 BOM, NUL parity when there is none, UTF-8 with or
    without a BOM, then cp1251/latin-1 as a last resort. A leading BOM never
    survives into the returned text — left in place it is glued to the file name
    of the first diagnostic.
    """
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace").lstrip("\ufeff")
    # NUL bytes in the first bytes mean UTF-16 without a BOM. This has to be
    # checked *before* UTF-8: ASCII-in-UTF-16LE decodes as "valid" UTF-8 with
    # interleaved NULs, which silently corrupts every parsed line. Their parity
    # gives the byte order — LE puts them on odd offsets, BE on even ones — and
    # guessing wrong turns the log into CJK glyphs rather than failing loudly,
    # so every count then reads as zero and the build looks unparseable.
    head = raw[:64]
    if b"\x00" in head:
        nul_even = sum(1 for i in range(0, len(head), 2) if head[i] == 0)
        nul_odd = sum(1 for i in range(1, len(head), 2) if head[i] == 0)
        byte_order = "utf-16-be" if nul_even > nul_odd else "utf-16-le"
        try:
            return raw.decode(byte_order, errors="replace").lstrip("\ufeff")
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    for encoding in ("cp1251", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _as_text(value: object) -> str:
    """Best-effort decode of a subprocess stream (bytes or str, or None)."""
    if not value:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def read_compile_log(path: Path) -> str:
    """Read and decode a compile log file; empty string when missing."""
    try:
        return decode_compile_log(path.read_bytes())
    except OSError:
        return ""


def _normalize_severity(raw: str) -> str:
    text = raw.strip().lower()
    if text.startswith("fatal"):
        return "error"
    if text.startswith("warn"):
        return "warning"
    if text.startswith("info"):
        return "information"
    return "error"


def parse_compile_log(
    text: str,
) -> Tuple[List[MqlDiagnostic], List[str], Optional[int], Optional[int]]:
    """Parse MetaEditor log text.

    Returns ``(diagnostics, other_lines, summary_errors, summary_warnings)``.
    """
    diagnostics: List[MqlDiagnostic] = []
    other_lines: List[str] = []
    summary_errors: Optional[int] = None
    summary_warnings: Optional[int] = None

    for raw_line in text.splitlines():
        line = raw_line.strip().strip("\x00")
        if not line:
            continue

        summary = _SUMMARY_RE.search(line)
        positional = _POSITIONAL_RE.match(line)
        tabular = _TABULAR_RE.match(line)

        if positional:
            diagnostics.append(
                MqlDiagnostic(
                    severity=_normalize_severity(positional.group("sev")),
                    message=positional.group("msg").strip(),
                    file=positional.group("file").strip().strip("'\""),
                    line=int(positional.group("line")),
                    column=int(positional.group("col")),
                )
            )
            if summary:
                summary_errors = int(summary.group("errors"))
                summary_warnings = int(summary.group("warnings"))
            continue

        if tabular:
            diagnostics.append(
                MqlDiagnostic(
                    severity="error",
                    message=tabular.group("msg").strip(),
                    file=tabular.group("file").strip().strip("'\""),
                    line=int(tabular.group("line")),
                    column=int(tabular.group("col")),
                )
            )
            continue

        if summary:
            summary_errors = int(summary.group("errors"))
            summary_warnings = int(summary.group("warnings"))
            continue

        other_lines.append(line)

    return diagnostics, other_lines, summary_errors, summary_warnings


def default_log_path(source: Path) -> Path:
    """The log path ``/log`` (no argument) produces: ``<source>.log``."""
    return source.with_suffix(source.suffix + ".log")


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def build_compile_command(
    metaeditor: Path,
    source: Path,
    *,
    log_path: Optional[Path] = None,
    include_dir: Optional[Path] = None,
    syntax_only: bool = False,
    wine: Optional[bool] = None,
) -> List[str]:
    """Build the MetaEditor command line (Wine-prefixed when needed)."""
    use_wine = needs_wine(metaeditor) if wine is None else wine
    cmd: List[str] = ["wine"] if use_wine else []
    cmd.append(str(metaeditor))
    cmd.append(f"/compile:{source}")
    if include_dir:
        cmd.append(f"/inc:{include_dir}")
    if log_path:
        cmd.append(f"/log:{log_path}")
    else:
        cmd.append("/log")
    if syntax_only:
        cmd.append("/s")
    return cmd


def _await_summary(log_path: Path, timeout: float) -> str:
    """Poll the log until a summary line appears (or ``timeout`` elapses).

    MetaEditor can return from ``/compile`` before the log is fully flushed,
    so a single read right after ``subprocess.run`` is not enough.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    text = ""
    while True:
        text = read_compile_log(log_path)
        if text and _SUMMARY_RE.search(text):
            return text
        if time.monotonic() >= deadline:
            return text
        time.sleep(_LOG_POLL_INTERVAL)


def find_artifact(source: Path, since: Optional[float] = None) -> Optional[Path]:
    """Return the compiled artifact when it exists and post-dates the build.

    On its own that means "newer than the source", with a margin for coarse
    filesystem timestamps. Pass the moment the compile was launched
    (``time.time()``) as ``since`` and it must also post-date *this run*: an
    ``.ex5`` left behind by the previous successful build is otherwise
    indistinguishable from a fresh one whenever the source was edited again
    moments before the run started.
    """
    ext = ARTIFACT_EXT.get(source.suffix.lower())
    if not ext:
        return None
    artifact = source.with_suffix(ext)
    if not artifact.exists():
        return None
    try:
        built = artifact.stat().st_mtime
        if built + 1 < source.stat().st_mtime:
            return None  # stale artifact from an earlier build
        # Two seconds of margin, not one: FAT and exFAT round timestamps to
        # two-second boundaries, so a genuinely fresh artifact can read early.
        if since is not None and built + 2 < since:
            return None  # artifact predates this compile run
    except OSError:
        return None
    return artifact


def compile_source(
    source: str | Path,
    *,
    metaeditor: Optional[str | Path] = None,
    log_path: Optional[str | Path] = None,
    include_dir: Optional[str | Path] = None,
    syntax_only: bool = False,
    wine: Optional[bool] = None,
    timeout: int = _DEFAULT_TIMEOUT,
    log_settle_seconds: float = 5.0,
) -> CompileResult:
    """Compile one MQL source file with MetaEditor and parse the log.

    Never raises for compiler errors — everything is reported through
    :class:`CompileResult`. Raises only for programmer errors (a missing
    source file) via the ``error_text`` field with ``ok=False``.
    """
    src = Path(source).expanduser()
    result = CompileResult(source=src)
    if not src.exists():
        result.error_text = f"source file not found: {src}"
        return result
    if src.suffix.lower() not in MQL_SOURCE_EXTS:
        result.error_text = (
            f"unsupported extension '{src.suffix}' (expected one of "
            f"{', '.join(MQL_SOURCE_EXTS)})"
        )
        return result

    editor = Path(metaeditor).expanduser() if metaeditor else find_metaeditor()
    if editor is None:
        result.error_text = (
            "MetaEditor not found. Pass --metaeditor, or set METAEDITOR_PATH. "
            "On Linux/macOS install MetaTrader under Wine and point at "
            "MetaEditor64.exe inside the prefix."
        )
        return result
    if not editor.is_file():
        result.error_text = f"MetaEditor not found at {editor}"
        return result

    log_file = Path(log_path).expanduser() if log_path else default_log_path(src)
    inc_dir = (
        Path(include_dir).expanduser()
        if include_dir
        else guess_include_dir(editor, src)
    )

    cmd = build_compile_command(
        editor,
        src,
        log_path=log_file,
        include_dir=inc_dir,
        syntax_only=syntax_only,
        wine=wine,
    )
    result.command = cmd
    result.log_path = log_file

    # Start from a clean slate so a stale log can never be mistaken for the
    # result of this run.
    try:
        log_file.unlink(missing_ok=True)
    except OSError:
        pass

    started = time.monotonic()
    started_wall = time.time()
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        result.exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        result.duration_seconds = time.monotonic() - started
        result.error_text = f"MetaEditor timed out after {timeout}s"
        return result
    except OSError as exc:
        result.duration_seconds = time.monotonic() - started
        result.error_text = f"could not launch MetaEditor: {exc}"
        return result

    text = _await_summary(log_file, log_settle_seconds)
    if not text:
        # Some builds only honour a bare `/log`; fall back to that location.
        fallback = default_log_path(src)
        if fallback != log_file:
            text = _await_summary(fallback, log_settle_seconds)
            if text:
                log_file = fallback
                result.log_path = fallback
    result.raw_log = text
    result.duration_seconds = time.monotonic() - started

    diagnostics, other_lines, sum_errors, sum_warnings = parse_compile_log(text)
    result.diagnostics = diagnostics
    result.other_lines = other_lines
    result.summary_errors = sum_errors
    result.summary_warnings = sum_warnings
    result.artifact = None if syntax_only else find_artifact(src, since=started_wall)

    if not text:
        # No log at all: MetaEditor may have failed to start (missing Wine,
        # GUI already running, path with spaces on some builds).
        stderr = _as_text(proc.stderr)
        result.error_text = (
            "MetaEditor produced no log file. Check the binary path, the Wine "
            "prefix, and that no MetaEditor GUI instance is holding the "
            f"compiler.{(' stderr: ' + stderr.strip()) if stderr.strip() else ''}"
        )
        result.ok = False
    elif result.summary_errors is None and not diagnostics:
        # A log that exists but holds neither a summary nor one parseable
        # diagnostic is not evidence of a clean build. MetaEditor may have died
        # before flushing, or written a shape this parser does not know, and
        # reporting OK here exits the fix-up loop on a build nobody confirmed.
        first = next((line for line in other_lines if line.strip()), "")
        result.error_text = (
            "the log has no 'N errors, M warnings' summary and no parseable "
            "diagnostic, so the build could not be confirmed"
            + (f" (first unparsed line: {first.strip()[:120]!r})" if first else "")
        )
        result.ok = False
    else:
        result.ok = result.error_count == 0

    if result.summary_errors is not None and len(result.errors) > result.summary_errors:
        result.note = (
            f"the summary reports {result.summary_errors} error(s) but "
            f"{len(result.errors)} diagnostic(s) were parsed; the higher count "
            "decides the build"
        )

    if result.ok and not syntax_only and result.artifact is None:
        # MetaEditor's CLI is known to fail silently on some large modular
        # projects: 0 errors in the log, no .ex5 written. Surface it instead of
        # reporting a clean build that produced nothing.
        result.note = (
            "compiler reported 0 errors but no .ex5/.ex4 appeared next to the "
            "source — possible silent CLI failure or stale artifact"
        )

    return result


def extract_mql_source(text: str) -> str:
    """Pull MQL source out of a model response.

    Prefers a fenced block tagged ``mql5``/``mql4``/``mq5``/``mq4``/``cpp``,
    then any fenced block that looks like MQL, then the whole answer.
    """
    fenced = re.findall(
        r"```(?P<tag>[A-Za-z0-9_+-]*)\s*\n(?P<body>.*?)```",
        text,
        re.DOTALL,
    )
    preferred = {"mql5", "mql4", "mq5", "mq4", "mql", "cpp", "c", ""}
    for tag, body in fenced:
        if tag.strip().lower() in preferred and _looks_like_mql(body):
            return body.strip() + "\n"
    for _tag, body in fenced:
        if _looks_like_mql(body):
            return body.strip() + "\n"
    if fenced:
        return fenced[0][1].strip() + "\n"
    if _looks_like_mql(text):
        return text.strip() + "\n"
    return ""


def _looks_like_mql(body: str) -> bool:
    markers = (
        "#property",
        "OnInit",
        "OnTick",
        "OnStart",
        "OnCalculate",
        "input ",
        "SymbolInfo",
        "CopyBuffer",
        "CTrade",
        "PositionSelect",
    )
    return any(marker in body for marker in markers)


__all__ = [
    "ARTIFACT_EXT",
    "MQL_SOURCE_EXTS",
    "CompileResult",
    "MqlDiagnostic",
    "build_compile_command",
    "candidate_metaeditor_paths",
    "compile_source",
    "decode_compile_log",
    "default_log_path",
    "extract_mql_source",
    "find_artifact",
    "find_metaeditor",
    "guess_include_dir",
    "needs_wine",
    "parse_compile_log",
    "read_compile_log",
]
