"""The mql documents are pinned to the code they describe.

Documentation drift is silent. A renamed flag, a reworded warning or a check
that was added to the analysis and never added to the page leaves the document
reading confidently and wrong, and nothing fails until a reader types the
command or greps for the message the page promised.

This branch produced three of those, which is why the file exists:

- the tutorial announced "seven signals" above a list of six, because two new
  warnings had been folded into bullets that already existed and the number had
  been bumped by guesswork;
- the README quoted ``the top pass is 9.6x the median of the next 19 passes``,
  but the analysis emits ``the top pass (14500) is 9.6x ...`` — a reader who
  greps for the quoted string finds nothing;
- three warnings (a report too small to judge, a rank metric no pass carries,
  and an in-sample winner that lands mid-table out of sample) were emitted by
  the code and documented nowhere.

So rather than policing a count that has to be updated by hand, the tests here
remove the need for one and pin the properties that actually matter:

- every long option the documents quote exists in one of this example's CLIs,
  or is named in :data:`FOREIGN_OPTIONS` as another program's;
- every ``mt5_*`` name in the documents is a tool the bridge can register, and
  every name the example config *enables* is registered by the default
  invocation — the config's own comment warns that an unregistered name is
  skipped silently, so enabling an opt-in tool without its flag would hide the
  bridge from the agent while looking correct;
- the tutorial's signal bullets correspond to messages the analysis really
  emits, in both directions;
- and the README's check table quotes **every** warning ``analyze_optimization``
  can produce, verbatim. That last one is the completeness check: a new warning
  with no row fails here, so the table cannot quietly become a subset again.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

import pytest
import tomlkit

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPANION = REPO_ROOT / "examples" / "mql_companion"
TUTORIAL = REPO_ROOT / "docs" / "tutorials" / "mql-companion.md"
README = COMPANION / "README.md"
SKILL_DIR = COMPANION / "skills" / "mql5-expert"
CONFIG = REPO_ROOT / "configs" / "openjarvis" / "examples" / "mql-assistant.toml"

#: Documents whose command lines belong to this example's own scripts. The
#: repository CHANGELOG is deliberately absent: it is the whole project's
#: history, so its flags belong to many tools (``jarvis self-update --check``,
#: not anything here) and its lines wrap mid-flag. Tool *names* are still
#: checked there, because ``mt5_*`` is unambiguous repo-wide.
MQL_DOCS: Tuple[Path, ...] = (
    TUTORIAL,
    README,
    COMPANION / "REVIEW-NOTES.md",
    COMPANION / "skills" / "mql5-expert" / "SKILL.md",
    COMPANION / "skills" / "mql5-expert" / "references" / "optimization.md",
    CONFIG,
)

DOCUMENTS: Tuple[Path, ...] = (REPO_ROOT / "CHANGELOG.md", *MQL_DOCS)

#: Scripts that expose a click command. ``metaeditor.py`` and ``compile_loop.py``'s
#: helpers are libraries; only the CLIs have options a document can quote wrong.
CLI_SCRIPTS = (
    "compile_loop.py",
    "install_skill.py",
    "mt5_mcp_server.py",
    "tester_report.py",
    "verify_on_terminal.py",
)

#: Options the documents quote that belong to another program. Each one is a
#: deliberate exception with its owner written down, so a new flag has to be
#: classified rather than ignored.
FOREIGN_OPTIONS = {
    "--backend": "jarvis eval run",
    "--extra": "uv sync",
    "--preset": "jarvis init",
    "--type": "jarvis scheduler create",
    "--value": "jarvis scheduler create",
}

#: Placeholder for every interpolated value when a message is flattened, so a
#: quoted example output can be matched against the template that produced it.
ANY = "@"


def _load(name: str) -> ModuleType:
    """Import an example script by path (the examples tree is not a package).

    Registered in ``sys.modules`` before ``exec_module`` because these modules
    use ``from __future__ import annotations`` together with ``@dataclass``.
    """
    spec = importlib.util.spec_from_file_location(
        f"mql_docs_{name}", COMPANION / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tester_report = _load("tester_report")
mt5_mcp_server = _load("mt5_mcp_server")
metaeditor = _load("metaeditor")
compile_loop = _load("compile_loop")
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


# --------------------------------------------------------------------------
# reading the code
# --------------------------------------------------------------------------


def _options(command: object) -> Set[str]:
    """Every long option a click command accepts, negations and subcommands too."""
    found: Set[str] = set()
    for param in getattr(command, "params", []):
        names = list(getattr(param, "opts", [])) + list(
            getattr(param, "secondary_opts", [])
        )
        found.update(name for name in names if name.startswith("--"))
    for sub in getattr(getattr(command, "commands", {}), "values", lambda: [])():
        found |= _options(sub)
    return found


def _option_inventory() -> Dict[str, Set[str]]:
    return {script: _options(_load(Path(script).stem).main) for script in CLI_SCRIPTS}


def _flatten(node: ast.AST) -> str:
    """Render a string expression as a template: literals kept, values as ``@``.

    Adjacent literals are how these messages are wrapped for the line length, so
    the concatenation is undone here; a conditional tail contributes its first
    branch, which is the text a reader would see quoted.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            ANY if isinstance(part, ast.FormattedValue) else _flatten(part)
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten(node.left) + _flatten(node.right)
    if isinstance(node, ast.IfExp):
        return _flatten(node.body)
    return ANY


def _messages(function: str) -> List[Tuple[str, str]]:
    """``(owner, template)`` for every ``warnings.append``/``notes.append``."""
    tree = ast.parse((COMPANION / "tester_report.py").read_text(encoding="utf-8"))
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function
    )
    found: List[Tuple[str, str]] = []
    for node in ast.walk(target):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "append":
            continue
        owner = getattr(node.func.value, "id", None) or getattr(
            node.func.value, "attr", None
        )
        if owner not in ("warnings", "notes"):
            continue
        for arg in node.args:
            template = " ".join(_flatten(arg).split())
            if template:
                found.append((str(owner), template))
    return found


def _canon(token: str) -> str:
    """A literal token, ignoring punctuation a quote may legitimately stop before."""
    return token.rstrip(":;,.").rstrip("—").rstrip(":;,.")


def _quote_matches_template(quote: str, template: str) -> bool:
    """True when a quoted example output is a prefix of a real message.

    Interpolated values match anything, so a document can quote one run's
    numbers; every other word has to be the code's own, which is what makes "the
    README quotes this message" a claim worth testing.
    """
    quoted = quote.split()
    message = template.split()
    if len(quoted) > len(message):
        return False
    for want, have in zip(quoted, message):
        if ANY in have:
            continue
        if _canon(want) == _canon(have):
            continue
        return False
    return True


def _registrable_tools(**flags: bool) -> Set[str]:
    tools: Iterable[object] = mt5_mcp_server.build_tools(None, **flags)
    return {tool.name for tool in tools}


# --------------------------------------------------------------------------
# reading the documents
# --------------------------------------------------------------------------


def _readme_check_table() -> List[Tuple[str, List[str]]]:
    """``(row label, quoted messages)`` from the README's check table."""
    text = README.read_text(encoding="utf-8")
    start = text.index("The checks, in the order they save you:")
    stop = text.index("\n### ", start)
    rows: List[Tuple[str, List[str]]] = []
    for line in text[start:stop].splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("Check", "") or set(cells[0]) <= {"-"}:
            continue
        rows.append((cells[0], re.findall(r"`([^`]+)`", cells[1])))
    return rows


def _tutorial_signal_bullets() -> List[str]:
    """Titles of the bullets in the tutorial's signal list, in order."""
    text = TUTORIAL.read_text(encoding="utf-8")
    start = text.index("each answering a question a sorted table cannot")
    stop = text.index("\n### ", start)
    return re.findall(r"^- \*\*(.+?)\.\*\*", text[start:stop], re.M)


#: The tutorial narrates each signal; this says which of the analysis's own
#: wordings that bullet is a promise about. A bullet with no phrase here would
#: be prose with nothing behind it, and a phrase that leaves the code would be a
#: promise the page still makes.
BULLETS = {
    "Too few trades on the winner": (
        "the best pass traded",
        "the best pass reports no trade count",
    ),
    "Spike instead of plateau": (
        "the top pass",
        "passes land within 10% of the best",
    ),
    "Inputs pinned at the edge of their range": ("sits at the",),
    "Criterion mismatch": ("check the EA against the criterion you",),
    "A ranking nobody could rank": ("nothing was ranked",),
    "Forward degradation": (
        "carry both a back and a forward",
        "median result falls from",
        "Spearman rho=",
        "ranked first in sample",
    ),
}


# --------------------------------------------------------------------------
# the documents describe an interface that exists
# --------------------------------------------------------------------------


@pytest.mark.parametrize("document", MQL_DOCS, ids=lambda path: path.name)
def test_documented_options_exist(document: Path) -> None:
    """A flag the documents quote is a flag one of these CLIs accepts.

    Checked line by line: a line that names a script is held to *that* script's
    options, and a line that does not (prose like "add ``--keep-ranges`` to keep
    the ranges") is held to all of them, because attributing it to a single CLI
    would be a guess. Either way an option that exists nowhere fails.
    """
    inventory = _option_inventory()
    union = set().union(*inventory.values()) | set(FOREIGN_OPTIONS)
    problems: List[str] = []
    for lineno, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
        mentioned = [script for script in CLI_SCRIPTS if script in line]
        allowed = set(FOREIGN_OPTIONS)
        allowed |= (
            set().union(*(inventory[script] for script in mentioned))
            if mentioned
            else union
        )
        for option in sorted(set(re.findall(r"(--[A-Za-z][\w-]*)", line))):
            if option not in allowed:
                where = (
                    f" (belongs to {FOREIGN_OPTIONS[option]})"
                    if option in FOREIGN_OPTIONS
                    else ""
                )
                problems.append(f"{document.name}:{lineno}: {option}{where}")
    assert not problems, (
        "documents quote options no CLI here accepts — rename the flag in the doc "
        "or classify it in FOREIGN_OPTIONS:\n  " + "\n  ".join(problems)
    )


def test_foreign_options_are_still_quoted() -> None:
    """The exception list does not grow by neglect.

    An entry whose flag left the documents is dead weight, and dead weight in an
    allowlist is how the next real drift gets waved through.
    """
    quoted = {
        option
        for document in MQL_DOCS
        for option in re.findall(
            r"(--[A-Za-z][\w-]*)", document.read_text(encoding="utf-8")
        )
    }
    stale = sorted(set(FOREIGN_OPTIONS) - quoted)
    assert not stale, f"no document quotes these any more; drop them: {stale}"


def test_tool_names_in_the_documents_are_registrable() -> None:
    """Every ``mt5_*`` name in the documents is a tool the bridge can register.

    Opt-in tools count as registrable here: the documents are allowed to name
    them, and it is the config test below that keeps them out of ``enabled``.
    """
    registrable = _registrable_tools() | _registrable_tools(
        allow_trading=True, allow_tester_run=True
    )
    unknown: Dict[str, List[str]] = {}
    for document in DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        for name in sorted(set(re.findall(r"\bmt5_[a-z_]+\b", text))):
            # the script's own filename, and prose wildcards like `mt5_tester_*`
            if name == "mt5_mcp_server" or name.endswith("_"):
                continue
            if name not in registrable:
                unknown.setdefault(name, []).append(document.name)
    assert not unknown, f"documents name tools the bridge never registers: {unknown}"


def test_config_enables_only_tools_the_default_bridge_registers() -> None:
    """An enabled name that is not registered makes the bridge invisible.

    ``jarvis ask`` filters MCP tools by the names in ``[tools] enabled``, and the
    config's own comment says a name that is not registered is skipped silently —
    so enabling ``mt5_order_send`` without starting the bridge with
    ``--allow-trading`` leaves the agent unable to see a tool while the config
    looks complete.
    """
    data = tomlkit.parse(CONFIG.read_text(encoding="utf-8"))
    enabled = [str(name) for name in data["tools"]["enabled"]]
    bridge = [name for name in enabled if name.startswith("mt5_")]
    missing = sorted(set(bridge) - _registrable_tools())
    assert bridge, "the preset should still enable the bridge's read-only tools"
    assert not missing, (
        f"{missing} are enabled but only registered behind a flag; either start the "
        "bridge with that flag in the documented command or comment the name out"
    )


@pytest.mark.parametrize(
    ("name", "flag"),
    [("mt5_order_send", "allow_trading"), ("mt5_tester_run", "allow_tester_run")],
)
def test_opt_in_tools_exist_only_behind_their_flag(name: str, flag: str) -> None:
    """The two dangerous tools stay unregistered until they are asked for."""
    assert name in CONFIG.read_text(encoding="utf-8"), (
        "the config should still tell the reader this tool exists and what enables it"
    )
    assert name not in _registrable_tools()
    assert name in _registrable_tools(**{flag: True})


# --------------------------------------------------------------------------
# the signals the documents promise are signals the code emits
# --------------------------------------------------------------------------


def test_tutorial_bullets_are_the_pinned_signals() -> None:
    """The tutorial's bullet list and this test's map agree, both ways.

    A new bullet has to arrive with the wording it describes, and a bullet whose
    signal was removed has to come off the page.
    """
    listed = _tutorial_signal_bullets()
    assert listed, "the tutorial's signal list is empty or was restructured"
    assert set(listed) == set(BULLETS), (
        "tutorial bullets and the pinned map disagree; "
        f"only in the page: {sorted(set(listed) - set(BULLETS))}, "
        f"only in the test: {sorted(set(BULLETS) - set(listed))}"
    )


@pytest.mark.parametrize("bullet", sorted(BULLETS))
def test_each_narrated_signal_is_a_message_the_analysis_emits(bullet: str) -> None:
    """Each bullet promises a wording the analysis still emits.

    Compared against the messages with their interpolation undone rather than
    against the source text: these strings are wrapped across adjacent literals
    to fit the line length, so the file does not contain them as a single run —
    ``"...agree (Spearman " "rho=..."`` fails a substring check on source.
    """
    joined = "\n".join(template for _, template in _messages("analyze_optimization"))
    for phrase in BULLETS[bullet]:
        assert phrase in joined, (
            f"the tutorial's {bullet!r} bullet describes {phrase!r}, which "
            "analyze_optimization no longer emits"
        )


def test_readme_table_quotes_are_verbatim() -> None:
    """Every message the README's check table quotes is the code's own wording.

    The table presents these as output, in backticks, with one run's numbers in
    them; the numbers may vary but the words may not.
    """
    templates = [template for _, template in _messages("analyze_optimization")]
    rows = _readme_check_table()
    quotes = [quote for _, row in rows for quote in row]
    unmatched = [
        f"{label}: `{quote}`"
        for label, row in rows
        for quote in row
        if not any(_quote_matches_template(quote, template) for template in templates)
    ]
    assert len(quotes) >= 10, (
        f"the check table lost its quoted examples ({len(quotes)} left)"
    )
    assert not unmatched, (
        "the README quotes messages the analysis does not emit (a quote may stop "
        "mid-sentence, but every other word has to be the code's):\n  "
        + "\n  ".join(unmatched)
    )


def test_readme_table_quotes_every_warning_the_analysis_can_emit() -> None:
    """Completeness: no warning is emitted that the check table does not list.

    This is the check that catches a new signal shipped without a row — three of
    them were live on this branch before the table was filled in. Notes are not
    covered: they describe what was read (grid size, filters applied, inputs the
    winners agreed on) rather than warning about a result.
    """
    warnings = [
        template
        for owner, template in _messages("analyze_optimization")
        if owner == "warnings"
    ]
    assert len(warnings) >= 11, "expected the analysis's warnings to be enumerated"
    quotes = [quote for _, row in _readme_check_table() for quote in row]
    undocumented = [
        template
        for template in warnings
        if not any(_quote_matches_template(quote, template) for quote in quotes)
    ]
    assert not undocumented, (
        "analyze_optimization warns about things the README's check table does not "
        "list:\n  " + "\n  ".join(undocumented)
    )


# --------------------------------------------------------------------------
# the commands the documents print are commands that work
# --------------------------------------------------------------------------


#: The ten fixed columns of an optimization table, then one column per optimized
#: input — the header MT5 writes, and the thing the reader sniffs to tell an
#: optimization table apart from a testing report.
OPT_HEADER = (
    "Pass",
    "Result",
    "Profit",
    "Expected Payoff",
    "Profit Factor",
    "Recovery Factor",
    "Sharpe Ratio",
    "Custom",
    "Equity DD %",
    "Trades",
    "InpFastEMA",
    "InpSlowEMA",
    "InpStopLoss",
    "InpUseFilter",
)


def _optimization_table(
    rows: Sequence[Sequence[str]], header: Sequence[str] = OPT_HEADER
) -> str:
    """An optimization report in the shape the terminal writes it."""

    def cells(values: Sequence[str]) -> str:
        return "  <Row>" + "".join(f"<Cell>{cell}</Cell>" for cell in values) + "</Row>"

    body = ['<?xml version="1.0" encoding="ANSI"?>', "<Table>", cells(header)]
    body += [cells(row) for row in rows]
    body.append("</Table>")
    return "\n".join(body) + "\n"


def _optimization_rows(count: int) -> List[Tuple[str, ...]]:
    """Rows for the ten fixed columns plus the four inputs ``OPT_HEADER`` names."""
    rows = []
    for number in range(1, count + 1):
        rows.append(
            (
                str(number),
                f"{10500.0 - number * 137.5:.2f}",
                f"{500.0 - number * 11.5:.2f}",
                f"{2.5 - number * 0.05:.2f}",
                f"{1.5 - number * 0.02:.2f}",
                f"{2.0 - number * 0.03:.2f}",
                f"{1.0 - number * 0.01:.2f}",
                "0",
                f"{10.0 + number * 0.4:.2f}",
                str(200 - number),
                str(5 + number % 26),
                "26",
                "500",
                "true",
            )
        )
    return rows


#: A testing report with the fields the gates below ask about: 243 trades,
#: profit factor 1.38, equity drawdown 8.01%, history quality 100%.
#:
#: The drawdown rows are asymmetric on purpose, because that is how MT5 writes
#: them: "Maximal" carries money and percent in one cell (``812.44 (8.01%)``),
#: "Relative" carries the percent alone. A first version of this fixture made
#: them symmetrical and the reader answered honestly —
#: ``equity_drawdown_relative_pct = 812.44 (FAILED <= 15.0)`` — which is the
#: two-figures-in-one-cell trap the parser exists to handle. Do not tidy it.
REPORT_HTML = """<html><head><title>MetaTrader 5 Strategy Tester Report</title></head>
<body><table width="100%" cellspacing="0" cellpadding="4" border="0">
<tr><td>Expert</td><td>MACD Sample.ex5</td>
    <td>Symbol</td><td>EURUSD (Euro vs US Dollar)</td></tr>
<tr><td>Period</td><td>1 Hour (H1)  2024.01.01 00:00 - 2024.06.30 23:59</td>
    <td>Model</td><td>Every tick</td></tr>
<tr><td>Initial deposit</td><td>10000.00</td><td>Currency</td><td>USD</td></tr>
<tr><td>Leverage</td><td>1:100</td><td>History quality, %</td><td>100</td></tr>
<tr><td>Total Net Profit</td><td>1 234.56</td><td>Total Trades</td><td>243</td></tr>
<tr><td>Gross Profit</td><td>4 500.00</td><td>Gross Loss</td><td>-3 265.44</td></tr>
<tr><td>Profit Factor</td><td>1.38</td><td>Sharpe Ratio</td><td>0.86</td></tr>
<tr><td>Recovery Factor</td><td>1.75</td><td>Expected Payoff</td><td>5.08</td></tr>
<tr><td>Balance Drawdown Maximal</td><td>705.00 (6.90%)</td>
    <td>Balance Drawdown Relative</td><td>6.90%</td></tr>
<tr><td>Equity Drawdown Maximal</td><td>812.44 (8.01%)</td>
    <td>Equity Drawdown Relative</td><td>8.01%</td></tr>
<tr><td>Profit Trades (% of total)</td><td>131 (53.91%)</td>
    <td>Loss Trades (% of total)</td><td>112 (46.09%)</td></tr>
</table></body></html>
"""

SET_TEMPLATE = (
    "; saved on 2026.09.30 12:00\n"
    "InpFastEMA=12||5||1||30||Y\n"
    "InpSlowEMA=26||20||5||60||Y\n"
    "InpStopLoss=500||200||50||1000||Y\n"
    "InpUseFilter=true||false||0||true||Y\n"
    "InpLots=0.10||0||0||0||N\n"
)


class TestDocumentedCommandsRun:
    """Every documented invocation that needs no Windows is executed here.

    Round 11 ran all 22 of them by hand from a clean checkout and recorded what
    each did; this keeps that result true, and pins the one thing each command's
    documentation promises — a gate that passes, a summary in the JSON, an ini
    printed instead of a terminal launched, a `.set` written, a dry run that
    writes nothing, and the two exit codes the documents publish (2 for a
    toolchain that is not there, nonzero for ``--latest`` with no data folder).
    """

    @pytest.fixture()
    def work(self, tmp_path: Path) -> Path:
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "MyEA.htm").write_text(REPORT_HTML, encoding="utf-8")
        (reports / "v2.htm").write_text(
            REPORT_HTML.replace("1 234.56", "900.10").replace("243", "180"),
            encoding="utf-8",
        )
        (tmp_path / "opt.xml").write_text(
            _optimization_table(_optimization_rows(24)), encoding="utf-8"
        )
        (tmp_path / "grid.set").write_text(SET_TEMPLATE, encoding="utf-8")
        return tmp_path

    def _run(self, argv: Sequence[str], **kwargs: object) -> Tuple[int, str]:
        # noqa: S603 - the documented command line, run from the repository root
        proc = subprocess.run(
            [sys.executable, *argv],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONPATH": "src"},
            **kwargs,  # type: ignore[arg-type]
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def test_report_with_ci_gates_passes(self, work: Path) -> None:
        code, out = self._run(
            [
                "examples/mql_companion/tester_report.py",
                str(work / "reports" / "MyEA.htm"),
                "--min-profit-factor",
                "1.3",
                "--min-trades",
                "50",
                "--max-equity-drawdown-pct",
                "15",
                "--min-history-quality-pct",
                "90",
            ]
        )
        assert code == 0, out
        assert '"passed": true' in out

    def test_prompt_flag_adds_the_compact_summary(self, work: Path) -> None:
        """``--prompt`` is documented as "a compact summary for a prompt"."""
        code, out = self._run(
            [
                "examples/mql_companion/tester_report.py",
                "--report",
                str(work / "reports" / "MyEA.htm"),
                "--prompt",
            ]
        )
        assert code == 0, out
        payload = json.loads(out)
        assert "summary" in payload["reports"][0], (
            "the summary the flag promises is missing"
        )
        assert "net_profit: 1234.56" in payload["reports"][0]["summary"]

    def test_two_reports_are_compared(self, work: Path) -> None:
        code, out = self._run(
            [
                "examples/mql_companion/tester_report.py",
                str(work / "reports" / "MyEA.htm"),
                str(work / "reports" / "v2.htm"),
            ]
        )
        assert code == 0, out
        assert '"reports": [' in out

    def test_forward_half_is_read_with_its_companion(self, work: Path) -> None:
        code, out = self._run(
            [
                "examples/mql_companion/tester_report.py",
                "--report",
                str(work / "reports" / "MyEA.htm"),
                "--forward-report",
                str(work / "reports" / "v2.htm"),
                "--prompt",
            ]
        )
        assert code == 0, out
        assert "forward" in out.lower()

    def test_print_ini_does_not_launch_a_terminal(self, work: Path) -> None:
        """``--run --print-ini`` prints the config and stops — nothing is started."""
        code, out = self._run(
            [
                "examples/mql_companion/tester_report.py",
                "--run",
                "--print-ini",
                "--expert",
                "Examples/MACD/MACD Sample",
                "--model",
                "4",
            ]
        )
        assert code == 0, out
        assert "[Tester]" in out and "Expert=" in out
        assert "terminal64" not in out.split("[Tester]")[-1]

    def test_latest_with_no_data_folder_points_at_search_dir(self) -> None:
        """The documented escape hatch is named when there is nothing to find."""
        code, out = self._run(
            [
                "examples/mql_companion/tester_report.py",
                "--latest",
                "--prompt",
            ]
        )
        assert code != 0, "a machine with no MT5 data folder cannot produce a report"
        assert "--search-dir" in out

    def test_optimization_ranking_and_grid(self, work: Path) -> None:
        code, out = self._run(
            [
                "examples/mql_companion/tester_report.py",
                str(work / "opt.xml"),
                "--rank-by",
                "recovery_factor",
                "--top",
                "20",
            ]
        )
        assert code == 0, out
        assert "recovery_factor" in out

    def test_set_from_pass_writes_the_file_it_names(self, work: Path) -> None:
        winner = work / "winner.set"
        code, out = self._run(
            [
                "examples/mql_companion/tester_report.py",
                str(work / "opt.xml"),
                "--set",
                str(work / "grid.set"),
                "--set-from-pass",
                "7",
                "--write-set",
                str(winner),
            ]
        )
        assert code == 0, out
        assert winner.is_file(), "the command reported success but wrote no .set"
        assert "InpFastEMA=" in winner.read_text(encoding="utf-8")

    def test_bridge_stub_answers_a_call(self) -> None:
        code, out = self._run(
            [
                "examples/mql_companion/mt5_mcp_server.py",
                "--stub",
                "--call",
                "mt5_calc",
                "--args",
                json.dumps(
                    {
                        "symbol": "EURUSD",
                        "side": "buy",
                        "volume": 0.5,
                        "price_close": 1.09,
                    }
                ),
            ]
        )
        assert code == 0, out
        assert "margin" in out

    def test_verifier_lists_its_checks_without_running_them(self) -> None:
        code, out = self._run(
            ["examples/mql_companion/verify_on_terminal.py", "--list"]
        )
        assert code == 0, out
        assert "10" in out, "the ten claims REVIEW-NOTES.md lists should all appear"
        # note 1's remaining question got a run of its own: id 11, plus the flag
        # that opts into the two-stage optimization it needs
        assert "11" in out and "--with-forward-opt" in out

    def test_review_notes_note_1_names_the_run_that_settles_it(self) -> None:
        """Note 1 promises an experiment; check 11 has to be what it points at.

        The note is the only place a reader learns what each outcome would mean,
        so it has to keep naming the command, the documented share it compares
        against, and the tie-breaker for a zero cell.
        """
        notes = _flat((COMPANION / "REVIEW-NOTES.md").read_text(encoding="utf-8"))
        note1 = notes[notes.index("## 1.") : notes.index("## 2.")]
        assert "--only 11" in note1 and "--with-forward-opt" in note1
        assert "10%" in note1, "the share the counts are compared against"
        for verdict in ("PASS", "FAIL", "UNKNOWN"):
            assert verdict in note1, f"note 1 has to say what {verdict} decides"
        assert "Forward Results tab" in note1, "the tie-breaker for a 0 cell"
        assert "treat 0 as missing" in note1

    def test_review_notes_names_the_custom_split_run(self) -> None:
        """Mode 4 is the one ForwardMode no documented share can judge.

        Note 1 carries the mapping and note 10 the date rules; check 12 is what
        observes both. The command, the share it asks for, the probe against the
        mode documented to ignore ForwardDate, and what stays documentation-derived
        all have to stay named, or the check drifts from the notes it settles.
        """
        notes = _flat((COMPANION / "REVIEW-NOTES.md").read_text(encoding="utf-8"))
        note1 = notes[notes.index("## 1.") : notes.index("## 2.")]
        assert "--only 12" in note1 and "--with-custom-split" in note1
        assert "CUSTOM_FORWARD_SHARE" in note1, "the share that keeps it apart"
        assert "ForwardMode=1" in note1, "the mode documented to ignore the date"
        assert "45 days" in note1, "how far the requested date sits from the 1/2 point"
        start = notes.index("## 10.")
        note10 = notes[start : notes.index(" ## ", start + 1)]
        assert "check 12" in note10, "the ForwardDate half of the same assumption"
        assert "out-of-range" in note10, "and what neither check probes"
        assert "probes FromDate" in note10, "check 1 does not probe ForwardDate"

    def test_every_surface_gives_the_three_step_set_fallback(self) -> None:
        """MetaQuotes lists ExpertParameters, then <EA>.set, then the defaults.

        Three surfaces once said an unresolvable name "proceeds with the EA's
        compiled defaults". The page documents a middle step - the terminal's
        own last-used inputs, ranges included - and does not say whether a bad
        name reaches it, so each surface has to carry the chain and none may
        assert the shorter outcome.
        """
        readme = _flat((COMPANION / "README.md").read_text(encoding="utf-8"))
        reference = _flat(
            (SKILL_DIR / "references" / "optimization.md").read_text(encoding="utf-8")
        )
        notes = _flat((COMPANION / "REVIEW-NOTES.md").read_text(encoding="utf-8"))
        assert "stale last-used inputs" in readme
        assert "run proceeds with the EA's compiled defaults" not in readme
        assert "grid, not defaults" in reference
        assert "A full path does not work" not in reference
        start = notes.index("## 8.")
        note8 = notes[start : notes.index(" ## ", start + 1)]
        assert "stale last-used inputs" in note8
        assert "Sub\\missing.set" in note8, "the experiment that settles it"
        start = notes.index("## 11.")
        note11 = notes[start : notes.index(" ## ", start + 1)]
        assert "forum/312820" in note11 and "thin" in note11

    def test_skill_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "examples/mql_companion/install_skill.py", "--dry-run"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONPATH": "src", "HOME": str(home)},
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert list(home.rglob("*")) == [], "a --dry-run that writes is not a dry run"

    def test_compile_only_without_a_toolchain_exits_two(self, tmp_path: Path) -> None:
        """The published exit-code table says 2 = toolchain problem."""
        source = tmp_path / "MyEA.mq5"
        source.write_text(
            (COMPANION / "skills/mql5-expert/templates/ea-template.mq5").read_text(),
            encoding="utf-8",
        )
        code, out = self._run(
            [
                "examples/mql_companion/compile_loop.py",
                "--source",
                str(source),
                "--compile-only",
            ]
        )
        assert code == 2, (
            f"expected the documented toolchain exit code, got {code}: {out}"
        )


CLI_BOOTSTRAP = "from openjarvis.cli import main; main()"


class TestDocumentedJarvisCommandsRun:
    """The ``jarvis`` half of the quick start, executed rather than trusted.

    Everything between "clone the repo" and "ask the agent something" is printed
    as ``jarvis ...`` in the README, the tutorial and the preset's own header,
    and none of it needs a model — so none of it had an excuse to stay
    unexecuted. Executing it found a real break: ``jarvis init --preset
    mql-assistant`` was rejected by a hardcoded ``click.Choice`` list while the
    preset file shipped and loaded cleanly. That is the first command the example
    tells you to run, and a test that only checked the file could not see it;
    ``tests/core/test_preset_configs.py`` now pins the general rule and these
    pin the example's own walk through it.
    """

    def _jarvis(self, args: Sequence[str], home: Path) -> Tuple[int, str]:
        """Run the console script the only way a source checkout can.

        ``jarvis`` is on PATH after an install, but the documented commands are
        what a fresh clone is told to type, so this bootstraps the entry point
        pyproject names for it (``openjarvis.cli:main``).
        """
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", CLI_BOOTSTRAP, *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            env={
                **os.environ,
                "PYTHONPATH": "src",
                "HOME": str(home),
                # OPENJARVIS_HOME outranks HOME (core/paths.py), and conftest
                # points it at one shared dir — left alone, these tests would
                # write the example's config and skill on top of every other
                # test's home.
                "OPENJARVIS_HOME": str(home),
            },
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def test_init_preset_installs_the_config_the_docs_promise(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        code, out = self._jarvis(["init", "--preset", "mql-assistant", "--force"], home)
        assert code == 0, out
        # init announces where it wrote; believe that only once the file is
        # really there, and really inside the home this test handed it.
        announced = re.search(r"installed to (\S+)", re.sub(r"\s+", " ", out))
        assert announced, f"init did not say where it wrote: {out}"
        config = Path(announced.group(1))
        assert config.is_file(), f"announced {config} but nothing is there"
        assert home in config.parents, f"init wrote outside its home: {config}"
        text = config.read_text(encoding="utf-8")
        # The preset exists to turn the bridge's reader tools on; a config that
        # did not name them would leave the agent unable to reach the terminal,
        # which is the entire point of the example.
        for tool in (
            "mt5_tester_report",
            "mt5_tester_compare",
            "mt5_tester_optimization",
            "mt5_tester_forward_check",
        ):
            assert f'"{tool}"' in text, f"{tool} is not enabled by the preset"
        assert "mql5-expert" in text
        # Installing a preset must not quietly arm the two tools that can move
        # money or launch the terminal: they ship commented, behind bridge flags.
        for opt_in in ("mt5_order_send", "mt5_tester_run"):
            enabled = [
                line
                for line in text.splitlines()
                if f'"{opt_in}"' in line and not line.lstrip().startswith("#")
            ]
            assert not enabled, f"{opt_in} became live in the installed config"

    def test_the_skill_installs_lists_and_runs_without_a_model(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "examples/mql_companion/install_skill.py"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "PYTHONPATH": "src",
                "HOME": str(home),
                "OPENJARVIS_HOME": str(home),
            },
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        installed = home / "skills" / "local" / "mql5-expert"
        assert installed.is_dir(), "the real install must place the skill"

        code, out = self._jarvis(["skill", "list"], home)
        assert code == 0, out
        assert "mql5-expert" in out, "README: `jarvis skill list  # -> mql5-expert`"

        # `skill run` is the documented next line and needs no engine: the two
        # steps are file_read then think, so what comes back is the rendered
        # review instruction, not a model answer.
        template = (
            COMPANION / "skills" / "mql5-expert" / "templates" / "ea-template.mq5"
        )
        code, out = self._jarvis(
            ["skill", "run", "mql5-expert", "-a", f"file_path={template}"], home
        )
        assert code == 0, out
        assert "Success" in out, out
        assert "MQL4-isms" in out, (
            "the rendered checklist should name the file it was pointed at"
        )


def test_tutorial_says_memory_index_needs_the_native_backend() -> None:
    """The one documented command that cannot run on a plain Python install.

    ``jarvis memory index ./mql5-reference/`` dies with
    ``MemoryBackendUnavailable`` when the native ``openjarvis_rust`` extension
    was never built — a traceback at the exact point where the tutorial promises
    the agent "stops guessing signatures". The note is pinned so the promise
    cannot outlive its prerequisite again.
    """
    text = TUTORIAL.read_text(encoding="utf-8")
    command_at = text.index("jarvis memory index ./mql5-reference/")
    after = text[command_at : command_at + 1200]
    assert "openjarvis_rust" in after, (
        "the tutorial hands out `memory index` without naming the native backend"
    )
    assert "MemoryBackendUnavailable" in after, (
        "say what the failure is called, so the traceback is recognizable"
    )


# ENUM_TRADE_TRANSACTION_TYPE, complete, from the MQL5 reference:
# mql5.com/en/docs/constants/tradingconstants/enum_trade_transaction_type
REAL_TRADE_TRANSACTIONS = frozenset(
    {
        "TRADE_TRANSACTION_ORDER_ADD",
        "TRADE_TRANSACTION_ORDER_UPDATE",
        "TRADE_TRANSACTION_ORDER_DELETE",
        "TRADE_TRANSACTION_DEAL_ADD",
        "TRADE_TRANSACTION_DEAL_UPDATE",
        "TRADE_TRANSACTION_DEAL_DELETE",
        "TRADE_TRANSACTION_HISTORY_ADD",
        "TRADE_TRANSACTION_HISTORY_UPDATE",
        "TRADE_TRANSACTION_HISTORY_DELETE",
        "TRADE_TRANSACTION_POSITION",
        "TRADE_TRANSACTION_REQUEST",
    }
)

#: ENUM_ORDER_TYPE_FILLING as the reference lists it
#: (mql5.com/en/docs/constants/tradingconstants/orderproperties,
#: #enum_order_type_filling). BOC is the one most write-ups omit; it exists and
#: is limit/stop-limit only.
REAL_ORDER_FILLING = frozenset(
    {
        "ORDER_FILLING_FOK",
        "ORDER_FILLING_IOC",
        "ORDER_FILLING_RETURN",
        "ORDER_FILLING_BOC",
    }
)

# Runtime errors, Account/Trade section plus the one the Sleep note cites, from
# mql5.com/en/docs/constants/errorswarnings/errorcodes
REAL_RUNTIME_ERRORS = frozenset(
    {
        "ERR_SLEEP_ERROR",  # 4020
        "ERR_ACCOUNT_WRONG_PROPERTY",  # 4701
        "ERR_TRADE_WRONG_PROPERTY",  # 4751
        "ERR_TRADE_DISABLED",  # 4752
        "ERR_TRADE_POSITION_NOT_FOUND",  # 4753
        "ERR_TRADE_ORDER_NOT_FOUND",  # 4754
        "ERR_TRADE_DEAL_NOT_FOUND",  # 4755
        "ERR_TRADE_SEND_FAILED",  # 4756
        "ERR_TRADE_CALC_FAILED",  # 4758
        "ERR_MARKET_NOT_SELECTED",  # 4302
        "ERR_FUNCTION_NOT_ALLOWED",  # 4014
        "ERR_NOTIFICATION_SEND_FAILED",  # 4515
        "ERR_NOTIFICATION_WRONG_PARAMETER",  # 4516
        "ERR_NOTIFICATION_WRONG_SETTINGS",  # 4517
        "ERR_NOTIFICATION_TOO_FREQUENT",  # 4518
        "ERR_INDICATOR_DATA_NOT_FOUND",  # 4806
    }
)

# MQL4-only terminal-state functions, mirroring the scorer's pattern below.
MQL4_ONLY_STATE_FUNCTIONS = (
    "IsTradeAllowed",
    "IsTradeContextBusy",
    "IsConnected",
    "IsTesting",
    "IsOptimization",
    "IsVisualMode",
    "IsDemo",
    "IsDllsAllowed",
)


def _flat(text: str) -> str:
    """Prose with the line wrapping and ``**emphasis**`` taken out.

    These pages wrap near 78 columns and mark code with backticks and emphasis,
    so a phrase that matters can straddle a break or sit inside a delimiter;
    matching the raw text fails on prose that plainly says the thing.
    """
    return " ".join(text.replace("**", "").replace("`", "").split())


def _call_sites(text: str, name: str) -> int:
    """How many times ``name(`` opens a call in ``text``."""
    return len(re.findall(rf"\b{re.escape(name)}\s*\(", text))


def _call_arguments(text: str, name: str, site: int = 0) -> List[str]:
    """The arguments of one ``name(...)`` call, split on commas at depth 0."""
    starts = [m.end() for m in re.finditer(rf"\b{re.escape(name)}\s*\(", text)]
    assert starts, f"no call to {name} in this text"
    index, depth, current, out = starts[site], 1, "", []
    while index < len(text) and depth:
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                out.append(current)
                break
        if char == "," and depth == 1:
            out.append(current)
            current = ""
        else:
            current += char
        index += 1
    return [argument.strip() for argument in out if argument.strip()]


class TestSkillKnowledgeMatchesTheMql5Reference:
    """The shipped knowledge is checked against the MQL5 reference, not the code.

    Every other test in this file compares the documents with the Python they
    describe, which cannot catch a document that disagrees with *MQL5*. Reading
    the skill's instruction and its four references against the API reference
    found four things a model would have copied straight out:

    * ``TRADE_TRANSACTION_ORDER_STATE`` — no such member. An order changing state
      is ``TRADE_TRANSACTION_ORDER_UPDATE``, whose documented description covers
      exactly that (``ORDER_STATE_STARTED`` to ``ORDER_STATE_PLACED``).
    * ``ERR_TRADE_POSITION_NOT_ALLOWED`` — no such constant; a blend of the real
      ``ERR_TRADE_DISABLED`` (4752) and ``ERR_TRADE_POSITION_NOT_FOUND`` (4753).
    * ``INIT_PARAMETERS_INCORRECT`` described as "the terminal retries this one —
      useful for inputs that depend on data not ready yet". Nothing retries it:
      the tester does not perform that pass and marks its row red, and too many
      such rows distort a genetic optimization. The advice pointed at the one
      workflow this example spends most of its words on.
    * ``Sleep()`` "ignored in the tester for MQL5 EAs" — that is the MQL4 rule.
      In MQL5 it suspends the program in the tester too, which is why
      ``ERR_SLEEP_ERROR`` ("out of test end date after calling Sleep()") exists.

    The first two compile to ``'X' - undeclared identifier``, the top row of the
    skill's own compile-error table: a file that tells the reader to verify
    signatures is still copied verbatim by a model with nothing indexed.
    """

    def _knowledge_files(self) -> List[Path]:
        return [
            SKILL_DIR / "SKILL.md",
            *sorted((SKILL_DIR / "references").glob("*.md")),
            *sorted((SKILL_DIR / "templates").glob("*.mq5")),
        ]

    def test_no_invented_mql5_constants(self) -> None:
        families = {
            "TRADE_TRANSACTION_": REAL_TRADE_TRANSACTIONS,
            "ERR_": REAL_RUNTIME_ERRORS,
            "ORDER_FILLING_": REAL_ORDER_FILLING,
        }
        invented: Dict[str, List[str]] = {}
        for path in self._knowledge_files():
            text = path.read_text(encoding="utf-8")
            for prefix, real in families.items():
                for token in set(re.findall(rf"\b{prefix}[A-Z0-9_]+\b", text)):
                    if token not in real:
                        invented.setdefault(token, []).append(path.name)
        assert not invented, (
            "the skill names MQL5 constants that do not exist — check each one "
            f"against the MQL5 reference before widening these sets: {invented}"
        )

    def test_the_two_corrected_explanations_stay_corrected(self) -> None:
        cheatsheet = (SKILL_DIR / "references" / "mql5-api-cheatsheet.md").read_text(
            encoding="utf-8"
        )
        init_note = cheatsheet[cheatsheet.index("`OnInit` return values") :]
        init_note = init_note[: init_note.index("## Indicators")]
        assert "nothing retries" in init_note.lower(), (
            "the note must say plainly that nothing retries "
            "INIT_PARAMETERS_INCORRECT — the text it replaced claimed the "
            "terminal retried it and recommended returning it to wait for data"
        )
        assert "retries this one" not in init_note
        assert "red" in init_note and "skip" in init_note, (
            "say what really happens: the pass is skipped and its row marked red"
        )
        transactions = cheatsheet[cheatsheet.index("## Event-driven confirmation") :]
        assert "TRADE_TRANSACTION_DEAL_ADD" in transactions, (
            "a position opened or closed by a deal does not raise "
            "TRADE_TRANSACTION_POSITION; without DEAL_ADD named here a reader "
            "waits on POSITION for a fill that never fires it"
        )

        instruction = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        sleep_note = instruction[instruction.index("Never use `Sleep()`") :]
        sleep_note = sleep_note[: sleep_note.index("\n- ", 5)]
        assert "ignored in the tester" not in sleep_note, (
            "that is the MQL4 rule; MQL5 suspends the EA in the tester as well"
        )
        assert "ERR_SLEEP_ERROR" in sleep_note

    def test_the_calc_functions_are_called_with_the_documented_arguments(
        self,
    ) -> None:
        """`OrderCalcProfit`'s fifth argument is a close price, not a stop loss.

        The snippet passed `sl` there — six arguments, so it compiles and the
        arity looks right — under a heading about profit math before sending,
        which reads as "the profit of the trade" and computes the loss if the
        stop is hit. Both signatures are documented
        (mql5.com/en/docs/trading/ordercalcmargin and .../ordercalcprofit): five
        arguments for margin, six for profit, the last of each the variable
        written to, so the returned bool is the only success signal.
        """
        cheatsheet = self._cheatsheet()
        start = cheatsheet.index("## Margin / profit math")
        # the next heading of the same level, whichever section that turns out to
        # be — naming one hardcodes an order the page does not promise
        section = _flat(cheatsheet[start : cheatsheet.index("\n## ", start + 1)])

        assert "price_close" in section, "the fifth parameter is never named"
        assert "not a stop loss" in section.lower(), (
            "passing sl there is legal and useful, but the page has to say what "
            "it then computes"
        )
        # the caveat lives in a code comment, whose `//` markers survive the
        # flattening — match the phrase up to the line break it straddles
        assert "no pending orders and no open" in section and "netting" in section, (
            "OrderCalcMargin is documented to ignore what the account already "
            "holds, which is the difference between this order's margin and the "
            "new total on a netting account"
        )
        for name, arity in (("OrderCalcMargin", 5), ("OrderCalcProfit", 6)):
            for site in range(_call_sites(cheatsheet, name)):
                arguments = _call_arguments(cheatsheet, name, site)
                assert len(arguments) == arity, (
                    f"{name} is documented with {arity} arguments; call {site} "
                    f"has {len(arguments)}: {arguments}"
                )

    def test_the_ea_template_checks_margin_with_the_documented_arity(self) -> None:
        """The template is the file a model is most likely to copy wholesale."""
        template = (SKILL_DIR / "templates" / "ea-template.mq5").read_text(
            encoding="utf-8"
        )
        assert _call_sites(template, "OrderCalcMargin"), (
            "the template no longer checks margin before sending"
        )
        for site in range(_call_sites(template, "OrderCalcMargin")):
            assert len(_call_arguments(template, "OrderCalcMargin", site)) == 5

    def test_the_filling_mode_rules_name_the_market_execution_exception(
        self,
    ) -> None:
        """Which filling modes are legal depends on the execution mode too.

        Both pages said "a mode the symbol accepts" and listed three of the four
        members, so a porter on a Market Execution broker could pick
        ORDER_FILLING_RETURN because SYMBOL_FILLING_MODE appeared to allow it —
        which the reference says is disabled in that mode regardless of the
        symbol's flags. The result is retcode 10030, INVALID_FILL: the code
        round 16 relabelled in the bridge.
        """
        cheatsheet = _flat(self._cheatsheet())
        porting = _flat(
            (SKILL_DIR / "references" / "mql4-to-mql5.md").read_text(encoding="utf-8")
        )
        for member in sorted(REAL_ORDER_FILLING):
            assert member in cheatsheet and member in porting, (
                f"{member} is a documented filling type and one page omits it"
            )
        assert "disabled regardless of the symbol's flags" in cheatsheet
        assert "refused under Market Execution whatever" in porting
        assert "10030" in cheatsheet and "10030" in porting, (
            "name the retcode this mistake produces"
        )
        assert "pending order" in cheatsheet.lower(), (
            "pending orders should carry RETURN whatever the execution mode"
        )
        assert "pending orders" in porting.lower()

    def test_the_filling_tie_break_is_the_documented_one(self) -> None:
        """When a symbol allows both FOK and IOC, the reference says FOK wins.

        "avoids retcode 10030" was true and incomplete: it does not say which
        mode arrives, and FOK means all-or-nothing, so a strategy written
        expecting partial fills silently stops having them.
        """
        cheatsheet = _flat(self._cheatsheet())
        assert "sets ORDER_FILLING_FOK" in cheatsheet
        assert "SetTypeFilling(ORDER_FILLING_IOC)" in cheatsheet

    def test_the_transaction_type_claim_keeps_its_citation(self) -> None:
        """The claim is right and third-party write-ups get it backwards.

        "A position changed by a deal does not raise TRADE_TRANSACTION_POSITION"
        is the reference's own wording; several published guides say the event
        fires for deal-driven changes too, so the source stays beside the claim.
        """
        cheatsheet = self._cheatsheet()
        start = cheatsheet.index("## Event-driven confirmation")
        section = _flat(cheatsheet[start : cheatsheet.index("\n## ", start + 1)])
        vendor_wording = "does not lead to the occurrence of TRADE_TRANSACTION_POSITION"
        assert vendor_wording in section
        assert "enum_trade_transaction_type" in section

    def test_the_ea_skeleton_marks_the_event_an_ea_never_receives(self) -> None:
        cheatsheet = self._cheatsheet()
        skeleton = cheatsheet[cheatsheet.index("## Program skeleton") :]
        skeleton = skeleton[: skeleton.index("## Indicators")]
        assert "OnCalculate" in skeleton, "the signature is worth keeping"
        assert "never receives" in _flat(skeleton), (
            "but an EA skeleton that lists OnCalculate without saying so invites "
            "a model to define it in an EA and wait for ticks that never come"
        )

    def test_the_sourced_claims_keep_their_sources(self) -> None:
        """Every claim this round corrected now rests on a page a reader can open."""
        cheatsheet = self._cheatsheet()
        for citation in (
            "ordercalcmargin",
            "ordercalcprofit",
            "ctradesettypefillingbysymbol",
            "enum_order_type_filling",
            "enum_trade_transaction_type",
        ):
            assert citation in cheatsheet, f"the claim resting on {citation} lost it"

    def _cheatsheet(self) -> str:
        return (SKILL_DIR / "references" / "mql5-api-cheatsheet.md").read_text(
            encoding="utf-8"
        )

    def test_the_instruction_does_not_recommend_mql4_only_functions(self) -> None:
        """The instruction used to tell the model to call ``IsTradeAllowed()``.

        That function is MQL4-only — it does not compile in MQL5 — and this
        branch's own scorer lists it as an MQL4-ism, so the skill built to hunt
        MQL4-isms was recommending one, and the benchmark would have scored the
        recommendation as contamination. Naming MQL4 functions is right in
        ``references/mql4-to-mql5.md``, whose left column is what they are being
        ported *from*; the instruction the model reads has to be pure MQL5.
        """
        from openjarvis.evals.scorers.mql_bench import MQL4_ISM_PATTERNS

        scored = " ".join(pattern for _, pattern in MQL4_ISM_PATTERNS)
        instruction = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        for name in MQL4_ONLY_STATE_FUNCTIONS:
            assert name in scored, (
                f"{name} is no longer scored as an MQL4-ism — this list and the "
                "scorer's pattern have drifted; update both together"
            )
            assert name not in instruction, (
                f"the instruction names {name}, which is MQL4-only and scored as "
                "an MQL4-ism; a model following it writes code that cannot compile"
            )


# --------------------------------------------------------------------------
# the platform's own values, on every surface a reader or a model picks from
# --------------------------------------------------------------------------

OPTIMIZATION_REFERENCE = SKILL_DIR / "references" / "optimization.md"
REVIEW_NOTES = COMPANION / "REVIEW-NOTES.md"
SKILL = SKILL_DIR / "SKILL.md"
VERIFY_SCRIPT = COMPANION / "verify_on_terminal.py"
TESTER_SCRIPT = COMPANION / "tester_report.py"


def _tester_cli_help(option: str) -> str:
    """The help a user actually reads for one ``tester_report.py`` option."""
    for param in tester_report.main.params:
        if option in getattr(param, "opts", ()):
            return str(param.help or "")
    raise AssertionError(f"tester_report.py has no {option} option")


def _tester_run_schema() -> Dict[str, Any]:
    """``mt5_tester_run``'s published input schema — the text a model reads."""
    tools = {
        tool.name: tool
        for tool in mt5_mcp_server.build_tools(None, allow_tester_run=True)
    }
    assert "mt5_tester_run" in tools, "the opt-in tester-run tool is missing"
    return tools["mt5_tester_run"].schema["properties"]


def _forward_mode_surfaces() -> List[Tuple[str, str]]:
    """Every place a ``ForwardMode`` integer gets chosen.

    MetaQuotes documents all five values ("0 — off, 1 — 1/2 of the testing
    period, 2 — 1/3 ..., 3 — 1/4 ..., 4 — custom interval specified using the
    ForwardDate parameter"), and the repo already depended on one of them:
    ``tester_ini_warnings`` warns that ``ForwardDate`` is read only with
    ``ForwardMode=4``. For fourteen rounds the reference page nevertheless told
    the reader the mapping "is not documented" — so it is now stated on every
    surface, and pinned here rather than trusted.
    """
    reference = OPTIMIZATION_REFERENCE.read_text(encoding="utf-8")
    row = next(
        line for line in reference.splitlines() if line.startswith("| `ForwardMode`")
    )
    schema = _tester_run_schema()
    return [
        ("references/optimization.md", row),
        ("tester_report.py --forward-mode", _tester_cli_help("--forward-mode")),
        ("build_tester_ini docstring", str(tester_report.build_tester_ini.__doc__)),
        ("mt5_tester_run.forward_mode", str(schema["forward_mode"]["description"])),
    ]


def _forward_date_surfaces() -> List[Tuple[str, str]]:
    """The surfaces that describe ``ForwardDate``, which mode 4 alone reads."""
    schema = _tester_run_schema()
    return [
        (
            "references/optimization.md",
            OPTIMIZATION_REFERENCE.read_text(encoding="utf-8"),
        ),
        ("tester_report.py --forward-date", _tester_cli_help("--forward-date")),
        ("build_tester_ini docstring", str(tester_report.build_tester_ini.__doc__)),
        ("mt5_tester_run.forward_date", str(schema["forward_date"]["description"])),
    ]


@pytest.mark.parametrize("surface,text", _forward_mode_surfaces())
def test_every_surface_states_the_forward_mode_integers(
    surface: str, text: str
) -> None:
    """1/2, 1/3, 1/4 and a custom mode 4 — wherever the value is picked."""
    for fraction in ("1/2", "1/3", "1/4"):
        assert fraction in text, f"{surface} does not state the {fraction} split"
    assert "custom" in text.lower(), f"{surface} never mentions the custom mode 4"


@pytest.mark.parametrize("surface,text", _forward_date_surfaces())
def test_every_surface_says_mode_4_alone_reads_forward_date(
    surface: str, text: str
) -> None:
    """The rule ``tester_ini_warnings`` enforces, stated where the date is set.

    MetaQuotes: "The parameter is valid only if ForwardMode=4." A user who sets
    a custom split date with mode 1 gets the half split silently ignored.
    """
    assert "4" in text, f"{surface} never names the mode that reads the date"
    assert "only" in text.lower(), f"{surface} does not say mode 4 is the only one"


@pytest.mark.parametrize(
    "path", [OPTIMIZATION_REFERENCE, REVIEW_NOTES, VERIFY_SCRIPT, TESTER_SCRIPT]
)
def test_no_surface_calls_the_forward_mode_mapping_undocumented(path: Path) -> None:
    """The hedge is pinned gone, in the exact phrases it used to wear."""
    text = path.read_text(encoding="utf-8")
    for hedge in (
        "mapping is not documented",
        "mapping is undocumented",
        "integer↔split mapping is not documented",
    ):
        assert hedge not in text, f"{path.name} still says: {hedge!r}"


@pytest.mark.parametrize("path", [SKILL, OPTIMIZATION_REFERENCE, README])
def test_the_forward_coverage_rule_reaches_the_reader(path: Path) -> None:
    """MT5 forward-runs only the best 10% (complete) or 25% (genetic) of passes.

    The two forward columns are therefore a slice the platform selected because
    those passes already won in sample, and ``spearman_back_vs_forward`` over
    that slice is weaker evidence than it looks. A reader who is not told this
    quotes the decay as if it described the run.
    """
    text = path.read_text(encoding="utf-8")
    assert "10%" in text and "25%" in text, f"{path.name} omits the 10%/25% rule"


def test_every_documented_criterion_value_has_a_row() -> None:
    """MetaQuotes lists ``OptimizationCriterion`` 0-7; 7 is the complex one."""
    text = OPTIMIZATION_REFERENCE.read_text(encoding="utf-8")
    start = text.index("## `OptimizationCriterion`")
    stop = text.index("\n## ", start + 1)
    values = {
        int(value) for value in re.findall(r"^\| (\d+) \|", text[start:stop], re.M)
    }
    assert values == set(range(8)), (
        "the criterion table lists "
        f"{sorted(values)}; the config-file documentation goes up to 7 (the "
        "maximum of the complex criterion)"
    )


def test_the_criterion_table_says_what_build_2530_changed() -> None:
    """The `Maximizes` column is the vendor's *old* wording, and says so.

    Build 2530 stopped multiplying the criterion by the balance, so on any
    current build the `Result` column is the metric itself. Quoting the product
    form without that note tells a reader to expect numbers a modern terminal
    does not write.
    """
    text = OPTIMIZATION_REFERENCE.read_text(encoding="utf-8")
    assert "2530" in text, "the criterion table does not mention build 2530"
    assert "ignore the balance" in text, (
        "the note has to say the criteria now ignore the balance, or a reader "
        "still expects balance x metric in the Result column"
    )


COMPILE_ERRORS = SKILL_DIR / "references" / "compile-errors.md"


def _skill_retcodes() -> List[Tuple[int, str]]:
    """``(code, CONSTANT)`` from the skill's trade-retcode table."""
    text = COMPILE_ERRORS.read_text(encoding="utf-8")
    start = text.index("## Trade retcodes")
    stop = text.index("\n## ", start + 1)
    return [
        (int(code), name)
        for code, name in re.findall(
            r"^\| (\d{5}) \| `([A-Z_]+)`", text[start:stop], re.M
        )
    ]


def test_the_skill_and_the_bridge_speak_one_retcode_vocabulary() -> None:
    """Two readers of one table: a model reads the page, a caller reads the label
    the bridge returns beside the number.

    They are pinned to each other because the bridge's labels were wrong for
    three codes while its numbers were right — 10027 called a client-side
    autotrading block a timeout, 10030 called an invalid filling mode invalid
    stops, 10031 called a lost connection a closed market — so anything that
    checked only one side of the pair would have passed.
    """
    rows = _skill_retcodes()
    assert rows, "the skill's retcode table did not parse"
    for code, name in rows:
        assert code in mt5_mcp_server.RETCODES, (
            f"the skill lists {code}, which the bridge would report as unknown"
        )
        assert mt5_mcp_server.RETCODES[code] == name.lower(), (
            f"the skill calls {code} {name}; the bridge calls it "
            f"{mt5_mcp_server.RETCODES[code]}"
        )


def test_the_skill_lists_every_code_that_means_success() -> None:
    """A page that names 10009 as the success path and stops there invites a
    resend of every pending order that reported 10008 `PLACED`."""
    rows = dict(_skill_retcodes())
    for code in sorted(mt5_mcp_server.RETCODE_SUCCESS):
        assert code in rows, f"{code} means success and the skill does not list it"


#: Runtime errors the skill's guard table cites, from the MQL5 reference's
#: errorcodes page (mql5.com/en/docs/constants/errorswarnings/errorcodes).
VENDOR_RUNTIME_ERRORS: Dict[str, Tuple[int, str]] = {
    "ERR_TRADE_DISABLED": (4752, "Trading by Expert Advisors prohibited"),
    "ERR_TRADE_POSITION_NOT_FOUND": (4753, "Position not found"),
    "ERR_TRADE_SEND_FAILED": (4756, "Trade request sending failed"),
    "ERR_MARKET_NOT_SELECTED": (4302, "Symbol is not selected in MarketWatch"),
    "ERR_FUNCTION_NOT_ALLOWED": (4014, "Function is not allowed for call"),
    "ERR_INDICATOR_DATA_NOT_FOUND": (4806, "Requested data not found"),
}

#: Compiler diagnostics the skill's table cites, as ``code -> reference
#: description`` (errorscompile / warningscompile). The tables give a number and
#: a description, not MetaEditor's literal message, so rows carry the number.
VENDOR_COMPILE_DIAGNOSTICS: Dict[int, str] = {
    154: "Semicolon ';' expected",
    163: "Function with this name is already defined and has another return type",
    164: "Function with this name is already defined and has a different set of "
    "parameters",
    165: "Function with this name is already defined and implemented",
    199: "Wrong number of parameters in the function",
    256: "Undeclared identifier",
    262: "Cannot convert to enumeration",
    106: "Error accessing a file in #include (probably the file does not exist)",
    130: "No such structure member",
    213: "Method of structure or class is not declared",
    43: "Possible loss of data at typecasting",
    62: "global variable",
    80: "Check operator precedence",
}


def _compile_table_rows() -> Dict[str, str]:
    """``first cell -> whole row`` of the compiler-error table."""
    text = COMPILE_ERRORS.read_text(encoding="utf-8")
    start = text.index("## Compiler errors")
    section = text[start : text.index("\n### ", start)]
    rows = {}
    for line in section.splitlines():
        if line.startswith("| `") and "Usual cause" not in line:
            rows[line.split("|")[1].strip()] = line
    return rows


class TestCompileTableCitesTheReference:
    """The page said ``cannot convert enum`` came from passing ``OP_BUY``, and
    listed ``expression not boolean`` and ``not a class member`` as MetaEditor
    messages. The reference's tables have none of the three wordings: ``OP_BUY``
    is simply *undeclared* in MQL5 (256), the real enumeration diagnostic is
    262, and the only table entry about mistaken conditions is the precedence
    warning 80. A fix loop that trusts a pattern the compiler never prints
    searches for the wrong thing, so every row now carries its reference number
    and the ones with no table entry carry none.
    """

    ROW_REFS = {
        "`'X' - undeclared identifier`": ("256",),
        "`';' - semicolon expected`": ("154",),
        "`'X' - function already defined`": ("163-165",),
        "`wrong parameters count for function 'X'`": ("199",),
        "`cannot convert enum`": ("262",),
        "`possible loss of data due to type conversion`": ("43",),
        "`'X' - file not found` / `cannot open include file`": ("106",),
    }

    def _row(self, key: str) -> str:
        rows = _compile_table_rows()
        for first, line in rows.items():
            if first.startswith(key):
                return line
        raise AssertionError(f"no compiler-table row starts with {key}")

    def test_each_row_names_its_reference_number(self) -> None:
        for key, refs in self.ROW_REFS.items():
            row = self._row(key)
            first = row.split("|")[1]
            for ref in refs:
                low = int(ref.split("-")[0])
                assert low in VENDOR_COMPILE_DIAGNOSTICS, f"{ref} is not pinned"
                assert f"ref. {ref}" in first, f"{key} lost ref. {ref}"

    def test_hides_row_cites_the_global_warning_and_its_neighbours(self) -> None:
        first = self._row("`declaration of 'X' hides global declaration`")
        first = first.split("|")[1]
        assert "ref. 62" in first
        assert "61" in first and "64" in first
        assert "global variable" in VENDOR_COMPILE_DIAGNOSTICS[62]

    def test_enum_row_does_not_blame_op_buy(self) -> None:
        row = " ".join(self._row("`cannot convert enum`").split())
        assert "Cannot convert to enumeration" in row
        assert "OP_BUY` itself is *undeclared*" in row
        assert "wrong type where an enumeration is expected" in row
        assert "Passing e.g." not in row
        undeclared = self._row("`'X' - undeclared identifier`")
        assert "OP_BUY" in undeclared

    def test_wordings_the_tables_do_not_list_are_gone(self) -> None:
        rows = " ".join(_compile_table_rows())
        assert "expression not boolean" not in rows
        assert "not a class member" not in rows
        text = COMPILE_ERRORS.read_text(encoding="utf-8")
        assert "expression not boolean" not in text
        assert "not a class member" not in text

    def test_precedence_warning_replaces_the_boolean_row(self) -> None:
        row = self._row("`check operator precedence")
        assert "ref. 80" in row
        assert "Check operator precedence" in VENDOR_COMPILE_DIAGNOSTICS[80]

    def test_method_row_cites_both_member_diagnostics(self) -> None:
        first = self._row("`'X' - undeclared identifier` on a method call")
        first = first.split("|")[1]
        assert "ref. 213" in first and "ref. 130" in first
        assert "not declared" in VENDOR_COMPILE_DIAGNOSTICS[213]
        assert "structure member" in VENDOR_COMPILE_DIAGNOSTICS[130]

    def test_the_header_explains_what_ref_means(self) -> None:
        text = " ".join(COMPILE_ERRORS.read_text(encoding="utf-8").split())
        assert "A row with no `ref.`" in text


class TestRuntimeErrorTable:
    """Each runtime-error row names the constant, then the code and the
    reference's description in parentheses; all are pinned to the reference's
    own table, the way the retcodes are."""

    @staticmethod
    def _rows() -> Dict[str, Tuple[int, str]]:
        text = COMPILE_ERRORS.read_text(encoding="utf-8")
        start = text.index("## Runtime errors worth guarding")
        return {
            name: (int(code), desc)
            for name, code, desc in re.findall(
                r'^\| `(ERR_[A-Z_]+)` \((\d{4}), "([^"]+)"\)',
                text[start:],
                re.M,
            )
        }

    def test_every_row_matches_the_reference(self) -> None:
        rows = self._rows()
        assert rows, "the runtime-error table did not parse"
        for name, got in rows.items():
            assert name in VENDOR_RUNTIME_ERRORS, f"{name} is not in the pinned table"
            assert got == VENDOR_RUNTIME_ERRORS[name], name

    def test_every_pinned_error_has_a_row(self) -> None:
        rows = self._rows()
        for name in VENDOR_RUNTIME_ERRORS:
            assert name in rows, f"{name} is pinned but the page lacks it"

    def test_the_constants_are_real_documented_names(self) -> None:
        for name in VENDOR_RUNTIME_ERRORS:
            assert name in REAL_RUNTIME_ERRORS, name


def test_the_filling_retcode_row_states_the_flag_values() -> None:
    """``SYMBOL_FILLING_MODE`` is a flag set (FOK 1, IOC 2, BOC 4); RETURN is not
    a flag, which is why reading the property cannot say whether it is allowed."""
    text = COMPILE_ERRORS.read_text(encoding="utf-8")
    row = " ".join(
        next(r for r in text.splitlines() if r.startswith("| 10030")).split()
    )
    for needle in ("`FOK` = 1", "`IOC` = 2", "`BOC` = 4", "`RETURN` has no flag"):
        assert needle in row, needle
    assert "SetTypeFillingBySymbol" in row


def _compile_produces_section() -> str:
    """The page's "What a compile produces" prose, flattened for matching.

    The page wraps at ~78 columns and emphasises with ``**markers**``, so a
    phrase can straddle a line break or sit inside one: raw substring checks
    would fail on text that plainly says the thing.
    """
    text = COMPILE_ERRORS.read_text(encoding="utf-8")
    start = text.index("### What a compile produces")
    section = text[start : text.index("## Trade retcodes", start)]
    return " ".join(section.replace("**", "").split())


def test_the_skill_says_which_sources_leave_a_binary_behind() -> None:
    """The page and ``ARTIFACT_EXT`` are two statements of one fact.

    A header mapped to an artifact of its own made ``find_artifact`` look for a
    ``.ex5`` no build writes, so a clean header compile carried the note
    reserved for the CLI's real silent failure. Pinning the pair keeps the page
    from promising an artifact the code does not look for, or the reverse.
    """
    section = _compile_produces_section()
    for ext in metaeditor.ARTIFACT_EXT:
        artifact = metaeditor.ARTIFACT_EXT[ext]
        assert f"`{ext}`" in section and f"`{artifact}`" in section, (
            f"the skill does not say {ext} compiles to {artifact}"
        )
    assert ".mqh" not in metaeditor.ARTIFACT_EXT
    assert "no artifact" in section, (
        "the skill must say a header produces no artifact of its own"
    )


def test_the_skill_points_at_the_header_recompile_rule() -> None:
    """Editing a ``.mqh`` changes nothing until the including program rebuilds.

    The compile-fix loop is exactly where that costs a round: the model edits a
    header, sees the errors go away, and the terminal keeps running the old
    binary.
    """
    assert "recompiled" in _compile_produces_section()


def test_the_two_metaeditor_hedges_carry_their_evidence() -> None:
    """Both claims are checkable, so both are cited rather than hedged.

    "The exit code varies by build" and "some builds fail silently" read as
    guesses; each has a public log or a vendor-fixed bug report behind it, and a
    reader who doubts the behaviour can go and look.
    """
    source = (COMPANION / "metaeditor.py").read_text(encoding="utf-8")
    section = _compile_produces_section()
    for thread in ("157533", "491543"):
        assert thread in source, f"metaeditor.py cites no evidence for {thread}"
        assert thread in section, f"the skill cites no evidence for {thread}"
    assert "build 5200" in source and "build 5200" in section


def test_the_published_exit_code_contract_covers_a_build_with_no_binary() -> None:
    """Three documents print the loop's exit codes; one of them drew the check.

    The tutorial's flowchart already routed "0 errors?" through "Check .ex5
    artifact" on the way to "Exit 0", and the CHANGELOG called a missing binary
    "rather than a false success" — while `compile_loop.py` printed `SUCCESS` and
    exited 0 over exactly that case, so both documents described a gate the code
    did not have. Every surface that publishes the contract now has to say what
    the artifact rule is and which flag relaxes it.
    """
    surfaces = {
        "README": README.read_text(encoding="utf-8"),
        "tutorial": TUTORIAL.read_text(encoding="utf-8"),
        "CHANGELOG": CHANGELOG.read_text(encoding="utf-8"),
    }
    for name, text in surfaces.items():
        assert "--allow-missing-artifact" in text, (
            f"{name} publishes the exit codes but not the flag that relaxes the "
            "artifact rule, so a reader cannot tell the default from the escape"
        )
        assert "491543" in text, (
            f"{name} states the silent-failure behaviour without the report that "
            "documents it"
        )
    assert "Exit 2: no binary" in surfaces["tutorial"], (
        "the flowchart still draws one way out of the artifact check"
    )


def test_the_flag_the_documents_promise_is_on_the_command() -> None:
    """The docs may only relax a gate the command actually exposes."""
    opts = {
        opt for param in compile_loop.main.params for opt in getattr(param, "opts", ())
    }
    assert "--allow-missing-artifact" in opts
    assert "--syntax-only" in opts and "--json-out" in opts


def _tutorial_calc_example() -> Dict[str, Any]:
    """The JSON block the tutorial prints under its ``mt5_calc`` command."""
    text = TUTORIAL.read_text(encoding="utf-8")
    start = text.index("--call mt5_calc")
    fence = text.index("```json", start)
    body = text[fence + len("```json") : text.index("```", fence + 7)]
    return json.loads(body)


class TestTutorialShowsWhatTheBridgeReturns:
    """The tutorial's ``mt5_calc`` payload predated the account-currency work:
    it had no ``synthetic``, ``account_currency``, ``currency_profit`` or
    ``note``, so a reader comparing it with the real output saw fields the page
    never explained, and the page still described the figures as unqualified
    money. Pinning the key set to what the stub returns keeps the example from
    ageing again; the *values* follow the clock and are not pinned.
    """

    def _stub_payload(self) -> Dict[str, Any]:
        stub = mt5_mcp_server.StubTerminal(now_fn=lambda: 1_790_000_000.0)
        return stub.calc("EURUSD", "buy", 0.5, None, 1.09)

    def test_the_example_has_exactly_the_fields_the_tool_returns(self) -> None:
        assert set(_tutorial_calc_example()) == set(self._stub_payload())

    def test_the_example_value_types_match(self) -> None:
        real = self._stub_payload()
        for key, value in _tutorial_calc_example().items():
            assert type(value) is type(real[key]), key

    def test_the_example_contract_fields_are_the_stubs_not_invented(self) -> None:
        """The page printed tick_value 10.0 beside tick_size 1e-05: a pip's worth
        beside a point-sized tick. Fields that do not depend on the clock must
        match what the stub returns for the same call."""
        real = self._stub_payload()
        shown = _tutorial_calc_example()
        for key in (
            "tick_size",
            "tick_value",
            "contract_size",
            "digits",
            "stops_level_points",
            "volume_step",
            "account_currency",
            "currency_profit",
        ):
            assert shown[key] == real[key], key
        assert shown["tick_value"] == shown["contract_size"] * shown["tick_size"]

    def test_the_page_talks_about_a_pip_not_a_point(self) -> None:
        text = _flat(TUTORIAL.read_text(encoding="utf-8"))
        assert 'risks "about $1 per pip"' in text
        assert "about $1 per point" not in text
        assert "a tick is contract_size x tick_size" in text

    def test_the_example_says_the_figures_are_in_the_account_currency(self) -> None:
        text = _flat(TUTORIAL.read_text(encoding="utf-8"))
        assert "are in the account currency, as order_calc_margin" in text
        assert "currency_profit says which currency" in text
        assert (
            "Margin required (also per lot) and profit at a close price, both" in text
        )

    def test_the_stop_is_sized_with_the_loss_side_tick_value(self) -> None:
        text = _flat(TUTORIAL.read_text(encoding="utf-8"))
        assert "tick_value_loss" in text and "a stop-loss is a losing tick" in text
        stub = mt5_mcp_server.StubTerminal(now_fn=lambda: 1_790_000_000.0)
        info = stub.symbol_info("EURUSD")
        assert "tick_value_loss" in info and "tick_value_profit" in info


def test_an_invented_price_is_not_blamed_on_retcode_10021() -> None:
    """10021 is ``TRADE_RETCODE_PRICE_OFF``: the reference calls it "no quotes to
    process the request", which says nothing about a price the caller chose.
    A far-off price gets a requote (10004) or an invalid-price retcode (10015)."""
    text = _flat(TUTORIAL.read_text(encoding="utf-8"))
    assert "10021" not in text
    assert "(10004, 10015) a server would answer with" in text
    source = " ".join(
        (REPO_ROOT / "examples/mql_companion/mt5_mcp_server.py")
        .read_text(encoding="utf-8")
        .split()
    )
    assert (
        'learns far more from "you invented this number" than from retcode 10021'
        not in source
    )
    assert "no quotes to process the request" in source


def test_the_readme_parenthesis_around_the_volatility_scale_is_closed() -> None:
    readme = (REPO_ROOT / "examples/mql_companion/README.md").read_text(
        encoding="utf-8"
    )
    assert "`sqrt(period)`" in readme


CHEATSHEET = SKILL_DIR / "references" / "mql5-api-cheatsheet.md"

#: What the reference says each clock returns in the Strategy Tester
#: (mql5.com/en/docs/dateandtime/<function>): ``function -> what it equals``.
VENDOR_TESTER_CLOCKS: Dict[str, str] = {
    "TimeTradeServer": "TimeCurrent()",
    "TimeLocal": "TimeCurrent()",
    "TimeGMT": "TimeTradeServer()",
}


def _clock_table() -> Dict[str, Tuple[str, str]]:
    """``function -> (live cell, tester cell)`` of the cheatsheet's clock table."""
    text = CHEATSHEET.read_text(encoding="utf-8")
    start = text.index("### Which clock a time function reads")
    section = text[start : text.index("\nNew-bar guard", start)]
    rows = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("| `Time") and len(cells) == 3:
            rows[cells[0].strip("`").rstrip("()")] = (cells[1], cells[2])
    return rows


class TestTimeClocks:
    """The cheatsheet taught ``bool is_london = (dt.hour >= 8 && ...)`` over
    ``TimeToStruct(TimeCurrent(), dt)``. ``TimeCurrent()`` is the *server's*
    clock, so the window was the broker's 08:00-17:00, not London's, and the
    name told a model it was a city session. The reference adds that in the
    tester ``TimeLocal()`` and ``TimeGMT()`` are both simply the simulated server
    time, so a GMT filter written with either one is right live and shifted by
    the broker's offset in a backtest. ``compile-errors.md`` warned off only
    ``TimeLocal()``.
    """

    def test_the_session_flag_is_not_named_after_a_city(self) -> None:
        text = CHEATSHEET.read_text(encoding="utf-8")
        assert "is_london" not in text
        assert "in_session" in text
        assert "// BROKER hours" in text

    def test_the_table_lists_every_clock(self) -> None:
        assert set(_clock_table()) == {
            "TimeCurrent",
            "TimeTradeServer",
            "TimeLocal",
            "TimeGMT",
        }

    @pytest.mark.parametrize("name,equals", sorted(VENDOR_TESTER_CLOCKS.items()))
    def test_each_tester_cell_matches_the_reference(
        self, name: str, equals: str
    ) -> None:
        _, tester = _clock_table()[name]
        assert tester.startswith(f"Always equal to `{equals}`"), (name, tester)

    def test_timecurrent_is_the_one_that_is_simulated_from_history(self) -> None:
        _, tester = _clock_table()["TimeCurrent"]
        assert "Simulated from the history" in tester

    def test_the_gmt_row_says_it_is_not_gmt_in_the_tester(self) -> None:
        _, tester = _clock_table()["TimeGMT"]
        assert "no GMT offset removed" in tester
        live, _ = _clock_table()["TimeGMT"]
        assert "PC's local time" in live

    def test_the_prose_names_the_consequence_and_the_safe_function(self) -> None:
        text = _flat(CHEATSHEET.read_text(encoding="utf-8"))
        assert "In the tester all four are the same server clock" in text
        assert "TimeCurrent() is the only one that means the same thing" in text
        assert "an input in server hours" in text

    def test_the_divergence_row_names_timegmt_as_well(self) -> None:
        lines = COMPILE_ERRORS.read_text(encoding="utf-8").splitlines()
        row = next(r for r in lines if r.startswith("| Tester vs live"))
        assert "TimeGMT()" in row and "TimeLocal()" in row


class TestCalendarInTheTester:
    """The divergence row said news-calendar lookups "cannot be reproduced" and
    stopped there. The reference book says more: every calendar call fails in
    the tester with FUNCTION_NOT_ALLOWED (4014), and what the EA does with that
    failure decides whether the backtest has no news filter or never trades.
    The row now names the error and the cheatsheet gives the documented
    workaround and the DST caveat for recorded times.
    """

    def _section(self) -> str:
        text = CHEATSHEET.read_text(encoding="utf-8")
        start = text.index("### News filters do not run in the tester")
        return _flat(text[start : text.index("\nNew-bar guard", start)])

    def test_the_divergence_row_points_at_4014(self) -> None:
        lines = COMPILE_ERRORS.read_text(encoding="utf-8").splitlines()
        row = next(r for r in lines if r.startswith("| Tester vs live"))
        assert "4014" in row and "cannot reproduce them" not in row

    def test_the_runtime_table_explains_both_misreadings(self) -> None:
        lines = COMPILE_ERRORS.read_text(encoding="utf-8").splitlines()
        row = " ".join(
            next(
                r for r in lines if r.startswith("| `ERR_FUNCTION_NOT_ALLOWED`")
            ).split()
        )
        assert 'reads the failure as "no news" backtests with no news filter' in row
        assert 'reads it as "news now" never trades' in row
        assert "online chart" in row

    def test_the_cheatsheet_gives_the_workaround_and_the_dst_caveat(self) -> None:
        text = self._section()
        assert "fails with ERR_FUNCTION_NOT_ALLOWED (4014)" in text
        assert "save the calendar records to files" in text
        assert (
            "trade-server time (TimeTradeServer(), with its time zone and DST)" in text
        )
        assert "turns the news filter off for the whole run" in text


class TestWhatElseTheTesterChanges:
    """The divergence row said "avoid real-tick assumptions" and SKILL.md said
    "dependence on tick history that the tester will not reproduce": nothing a
    reader could act on. MT5 Help's "Testing Features" page lists concrete
    differences, one of which flatters results: in the "Open prices only" and
    "1 minute OHLC" modes stops and pending orders fill at the order's own
    price, so a backtest in those modes shows no slippage at all.
    """

    def _section(self) -> str:
        text = CHEATSHEET.read_text(encoding="utf-8")
        start = text.index("### What else the Strategy Tester changes")
        return _flat(text[start : text.index("\nNew-bar guard", start)])

    def test_fills_in_the_cheap_modes_have_no_slippage(self) -> None:
        text = self._section()
        assert 'In the "Open prices only" and "1 minute OHLC" modes' in text
        assert "fill at the price written in the order, with no slippage" in text
        assert "only the accurate modes (every tick, real ticks)" in text

    def test_spread_is_read_from_history_and_floats(self) -> None:
        text = self._section()
        assert "read from the history" in text
        assert "last known spread" in text and "always floating" in text

    def test_objects_and_globals_are_emulated(self) -> None:
        text = self._section()
        assert "returns zero (visual mode is exempt)" in text
        assert "each testing agent has its own copy" in text

    def test_other_symbols_pause_the_test_and_get_their_own_ticks(self) -> None:
        text = self._section()
        assert "connected on first access" in text
        assert "its own tick sequence" in text

    def test_the_source_is_cited(self) -> None:
        text = self._section()
        assert "algotrading/testing_features" in text

    def test_the_clock_section_cites_the_deliberate_equality(self) -> None:
        text = _flat(CHEATSHEET.read_text(encoding="utf-8"))
        assert "the equality is deliberate" in text
        assert 'server time "always corresponds to the GMT time"' in text
        assert "nothing converts a broker's offset away" in text

    def test_skill_md_names_the_concrete_differences(self) -> None:
        text = _flat((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
        assert "the economic calendar (fails with 4014)" in text
        assert "chart objects (read as zero)" in text
        assert "dependence on tick history" not in text

    def test_the_divergence_row_points_at_the_new_section(self) -> None:
        lines = COMPILE_ERRORS.read_text(encoding="utf-8").splitlines()
        row = next(r for r in lines if r.startswith("| Tester vs live"))
        assert "What else the Strategy Tester changes" in row
        assert "real-tick assumptions" not in row


class TestTesterFilesAndRemoteAgents:
    """The cheatsheet said files land in ``MQL5\\Files`` "unless you pass
    FILE_COMMON". MT5 Help says that during a test every file operation happens
    in the *agent's* own ``MQL5\\Files``, isolated from the platform. That made
    the calendar workaround added in round 34 wrong as written: a file the live
    program writes to its own folder is not where the tester agent looks, so the
    recorded calendar would never be found. The workaround now says to use
    ``FILE_COMMON``, and the tester table and the optimization page carry the
    related agent facts (remote agents log almost nothing and run no DLLs).
    """

    def _cheat(self) -> str:
        return _flat(CHEATSHEET.read_text(encoding="utf-8"))

    def test_the_files_paragraph_separates_chart_from_tester(self) -> None:
        text = self._cheat()
        assert "In the tester that is a different folder" in text
        assert "the testing agent's own <agent folder>\\MQL5\\Files" in text
        assert "unless both sides use FILE_COMMON" in text

    def test_the_calendar_workaround_records_to_the_shared_folder(self) -> None:
        text = self._cheat()
        start = text.index("News filters do not run in the tester")
        section = text[start : text.index("What else the Strategy Tester changes")]
        assert "Record to FILE_COMMON" in section
        assert "the tester would find no calendar at all" in section

    def test_the_tester_table_has_the_file_and_remote_rows(self) -> None:
        text = self._cheat()
        assert "Lands in the testing agent's own MQL5\\Files" in text
        assert "only FILE_COMMON reaches the shared folder" in text
        assert "Not recorded on a remote agent" in text
        assert "DLL calls are forbidden there" in text
        assert "Allow import DLL" in text

    def test_the_conclusion_names_files_as_well_as_globals(self) -> None:
        assert "keeps state in global variables or files" in self._cheat()

    def test_the_optimization_page_warns_about_remote_agents(self) -> None:
        text = _flat(OPTIMIZATION_REFERENCE.read_text(encoding="utf-8"))
        assert "A remote agent records almost nothing in its log" in text
        assert "no DLL calls" in text
        assert "should use local agents" in text


class TestTesterModesLimits:
    """The tester table named the fill rule of the cheap modes but not what they
    do to the program: in "Open prices only" ``OnTick`` runs once per bar and
    the EA cannot read a timeframe below the test timeframe, so the template's
    ``InpTimeframe`` input can make a run fail on data that is fine live.
    "1 minute OHLC" calls ``OnTick`` four times a minute whatever the test
    timeframe. Both are on MT5 Help's "Real and Generated Ticks" page, which
    also says the two modes' stops "may trigger at a price different from the
    specified one" -- a caveat beside the "no slippage" rule from the other page.
    """

    def _section(self) -> str:
        text = CHEATSHEET.read_text(encoding="utf-8")
        start = text.index("### What else the Strategy Tester changes")
        return _flat(text[start : text.index("\nNew-bar guard", start)])

    def test_open_prices_only_runs_once_per_bar(self) -> None:
        text = self._section()
        assert "once per bar, at its open" in text
        assert "W1 and MN1 bars are generated once a day" in text

    def test_one_minute_ohlc_runs_four_times_a_minute(self) -> None:
        text = self._section()
        assert "four times a minute (open, high, low, close)" in text
        assert "even when the test runs on H1" in text

    def test_lower_timeframes_are_unreadable_in_open_prices_only(self) -> None:
        text = self._section()
        assert "nothing below the test timeframe" in text
        assert "a higher one must be a multiple of it" in text
        assert "test on M20: H1 yes, M30 no" in text
        assert "the first timeframe it accesses" in text
        assert "random-delay mode cannot be used" in text

    def test_the_trigger_caveat_sits_beside_the_fill_rule(self) -> None:
        text = self._section()
        assert "may trigger at a price different from the specified one" in text
        assert "tick_generation" in text

    def test_spread_differs_between_generated_and_real_ticks(self) -> None:
        text = self._section()
        assert "Generated ticks use the spread fixed in each minute bar" in text
        assert "real ticks let it change within the minute" in text

    def test_the_template_input_example_is_named(self) -> None:
        text = self._section()
        assert "an input such as a signal timeframe can make" in text


class TestAlertsWithoutTradingView:
    """A user without a TradingView plan asked about Pine alerts. MQL5 raises
    the same signal inside the terminal (``Alert``, ``SendNotification``,
    ``SendMail``); the MQL5 Reference says none of them runs in the tester, and
    gives ``SendNotification`` strict limits."""

    def _section(self) -> str:
        text = CHEATSHEET.read_text(encoding="utf-8")
        start = text.index("## Alerts and notifications")
        return _flat(text[start : text.index("\n## Logging and state", start)])

    def test_names_the_three_alert_functions(self) -> None:
        text = self._section()
        for name in ("Alert(...)", "SendNotification(text)", "SendMail(subject, text)"):
            assert name in text

    def test_notification_limits_match_the_reference(self) -> None:
        text = self._section()
        assert "255 characters" in text
        assert "at most 2 calls a second and 10 a minute" in text
        for code in ("4515", "4516", "4517", "4518"):
            assert code in text

    def test_permission_checks_are_named(self) -> None:
        text = self._section()
        assert "TERMINAL_NOTIFICATIONS_ENABLED" in text
        assert "TERMINAL_EMAIL_ENABLED" in text

    def test_webhook_needs_webrequest_and_a_paid_plan(self) -> None:
        text = self._section()
        assert "needs a paid TradingView plan" in text
        assert "WebRequest() , which needs the URL" in text.replace(
            "WebRequest()", "WebRequest() "
        )

    def test_tester_table_lists_the_outside_world_functions(self) -> None:
        text = CHEATSHEET.read_text(encoding="utf-8")
        start = text.index("### What else the Strategy Tester changes")
        flat = _flat(text[start : text.index("\nNew-bar guard", start)])
        for name in ("Alert()", "SendNotification()", "SendMail()", "WebRequest()"):
            assert name in flat
        assert "Not executed at all" in flat


#: Retcodes the template's pre-flight guards exist to avoid, with the vendor
#: constant (mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes).
GUARD_RETCODES = {
    10034: "LIMIT_VOLUME",  # SYMBOL_VOLUME_LIMIT
    10040: "LIMIT_POSITIONS",
    10042: "LONG_ONLY",  # SYMBOL_TRADE_MODE_LONGONLY
    10043: "SHORT_ONLY",
    10044: "CLOSE_ONLY",
    10046: "HEDGE_PROHIBITED",
}


class TestRetcodesForTheTemplateGuards:
    """Rounds 40 and 42 added guards for ``SYMBOL_VOLUME_LIMIT`` and
    ``SYMBOL_TRADE_MODE``; the retcode table did not list what the server
    answers when a guard is skipped, so a model reading a 10042 in a log found
    nothing. Each row's code and constant are the vendor's."""

    def _rows(self) -> Dict[int, str]:
        return dict(_skill_retcodes())

    def test_every_guard_retcode_is_listed_with_the_vendor_constant(self) -> None:
        rows = self._rows()
        for code, name in GUARD_RETCODES.items():
            assert rows.get(code) == name, (code, rows.get(code))

    def _row(self, code: int) -> str:
        text = COMPILE_ERRORS.read_text(encoding="utf-8")
        return next(
            line for line in text.splitlines() if line.startswith(f"| {code} |")
        )

    def test_one_way_rows_name_the_symbol_mode(self) -> None:
        assert "SYMBOL_TRADE_MODE_LONGONLY" in self._row(10042)
        assert "SYMBOL_TRADE_MODE_SHORTONLY" in self._row(10043)
        assert "SYMBOL_TRADE_MODE_CLOSEONLY" in self._row(10044)

    def test_a_long_only_symbol_refuses_the_sell_not_the_buy(self) -> None:
        # the direction is easy to flip: LONG_ONLY refuses a sell entry
        assert "A sell entry is refused" in self._row(10042)
        assert "A buy entry is refused" in self._row(10043)

    def test_volume_limit_row_points_at_the_symbol_property(self) -> None:
        row = self._row(10034)
        assert "SYMBOL_VOLUME_LIMIT" in row
        assert "per direction" in row
        assert "VolumeRoomFor" in row

    def test_positions_limit_row_keeps_the_netting_hedging_split(self) -> None:
        row = self._row(10040)
        assert "netting account only symbols that already have a position" in row
        assert "hedging account pending orders count too" in row

    def test_hedge_row_says_opposite_positions(self) -> None:
        row = self._row(10046)
        assert "forbids opposite positions" in row
        assert "pending sell" in row


class TestOnInitReturnCodes:
    """The cheatsheet listed three ``OnInit`` return codes and said nothing of
    what ``INIT_FAILED`` costs in an optimization or of the fourth code,
    ``INIT_AGENT_NOT_SUITABLE`` (MQL5 Reference, ``OnInit``)."""

    def _text(self) -> str:
        text = CHEATSHEET.read_text(encoding="utf-8")
        start = text.index("`OnInit` return values:")
        return _flat(text[start : text.index("## Indicators", start)])

    def test_init_failed_reloads_the_ea_for_the_next_pass(self) -> None:
        text = self._text()
        assert "unloaded from the agent's memory, then loaded again" in text
        assert "much more time than calling TesterStop()" in text

    def test_the_fourth_code_and_its_consequence(self) -> None:
        text = self._text()
        assert "INIT_AGENT_NOT_SUITABLE" in text
        assert "not enough RAM, no OpenCL support" in text
        assert "no more tasks until the end of that optimization" in text

    def test_the_example_guards_on_optimization_and_memory(self) -> None:
        # assert on the code block: the prose names the same identifiers
        text = CHEATSHEET.read_text(encoding="utf-8")
        start = text.index("`OnInit` return values:")
        section = text[start : text.index("## Indicators", start)]
        block = section.split("```mql5\n", 1)[1].split("```", 1)[0]
        assert block.splitlines()[0] == "if(MQLInfoInteger(MQL_OPTIMIZATION))"
        assert "TerminalInfoInteger(TERMINAL_MEMORY_TOTAL) < 2000" in block
        assert block.rstrip().endswith("return(INIT_AGENT_NOT_SUITABLE);")

    def test_position_select_snippet_uses_a_block(self) -> None:
        text = CHEATSHEET.read_text(encoding="utf-8")
        assert "if(PositionSelect(_Symbol))\n  {\n   double vol" in text


class TestRawOrderSendChecksTheRetcode:
    """The cheatsheet taught that a CTrade ``bool`` is not the server's verdict,
    then showed the raw ``OrderSend`` example with ``if(!OrderSend(req, res))``
    as the only test -- the same trap. The reference says to check the result's
    ``retcode`` first, because ``true`` for a market order means only that the
    order was placed for further execution."""

    def _section(self) -> str:
        text = CHEATSHEET.read_text(encoding="utf-8")
        start = text.index("## Trading with a raw request")
        return text[start : text.index("\n## Margin / profit math", start)]

    def test_snippet_tests_the_retcode_as_well_as_the_bool(self) -> None:
        block = self._section().split("```mql5\n", 1)[1].split("```", 1)[0]
        assert "if(!OrderSend(req, res) || !RetcodeIsSuccess(res.retcode))" in block
        assert "retcode=%u" in block  # the retcode is a uint

    def test_the_old_bool_only_test_is_gone(self) -> None:
        assert "if(!OrderSend(req, res))\n" not in self._section()

    def test_prose_quotes_the_reference_and_names_ordercheck(self) -> None:
        text = _flat(self._section())
        assert "successfully placed in the trading system for further execution" in text
        assert "check the result's retcode first" in text
        assert "run OrderCheck() on the request before sending it" in text
        assert "retcode_external" in text

    def test_the_helper_it_calls_is_defined_earlier_in_the_page(self) -> None:
        text = CHEATSHEET.read_text(encoding="utf-8")
        assert text.index("bool RetcodeIsSuccess(") < text.index(
            "## Trading with a raw request"
        )
