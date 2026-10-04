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
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Dict, Iterable, List, Set, Tuple

import pytest
import tomlkit

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPANION = REPO_ROOT / "examples" / "mql_companion"
TUTORIAL = REPO_ROOT / "docs" / "tutorials" / "mql-companion.md"
README = COMPANION / "README.md"
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
