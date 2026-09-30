#!/usr/bin/env python3
"""Read a MetaTrader 5 Strategy Tester report and turn it into numbers.

The tester is the other half of the ground truth. MetaEditor says whether an
Expert Advisor *builds*; the tester says whether it *behaves*. Both produce
files an agent can read, and both have details that trip up a model that is
guessing:

* MT5's ``Gross Loss`` is **negative**. Net profit is
  ``gross_profit + gross_loss``, not the difference, and the profit factor is
  ``gross_profit / abs(gross_loss)``. Getting the sign wrong turns every
  derived metric into nonsense.
* There are **four** drawdown numbers per equity curve, not one: the money
  drawdown at its worst, the percentage at that same moment, the worst
  percentage seen, and the money drawdown at that worst percentage. The report
  labels them "Maximal" versus "Relative", which reads like a distinction
  without a difference until you know which is which — and it puts two of the
  four in one cell as ``"705.00 (6.90%)"``.
* Counts and percentages share a cell too: ``"131 (53.91%)"`` is 131 winning
  trades out of 243, and ``"7 (310.50)"`` is a 7-trade streak worth 310.50.
  One number is a count, the other is money, and the order flips between
  labels.
* Report HTML is localized, its layout changes between builds, and numbers use
  non-breaking-space thousands separators.

So this module does not depend on layout. It strips markup, walks the
resulting cell stream, and matches known metric labels to the value that
follows. English labels are the primary vocabulary; MQL5's
``ENUM_STATISTICS`` identifiers (``STAT_PROFIT_FACTOR``) and snake_case
spellings are accepted as aliases, which also covers XML-ish exports. Anything
not found is listed in ``missing`` rather than guessed, and the documented
identities are used to cross-check what *was* found — a report whose profit
factor disagrees with its own gross profit and loss gets a warning.

Typical use::

    # parse one report, print the metrics an agent needs
    python examples/mql_companion/tester_report.py report.htm --prompt

    # CI gate: exit 1 unless the EA clears these bars
    python examples/mql_companion/tester_report.py report.htm \\
        --min-profit-factor 1.3 --max-equity-drawdown-pct 15 --min-trades 50 \\
        --min-history-quality-pct 90

    # find the newest report under the terminal's data folders
    python examples/mql_companion/tester_report.py --latest

    # two or more reports are compared side by side, with deltas
    python examples/mql_companion/tester_report.py v1.htm v2.htm

    # run a backtest headlessly, then parse what it produced
    python examples/mql_companion/tester_report.py --run \\
        --expert "Examples/MACD/MACD Sample" --symbol EURUSD --period H1 \\
        --from-date 2024.01.01 --to-date 2024.06.30 --deposit 10000

An *optimization* writes a different file: an XML table with one row per pass.
This module reads that too, ranks the passes, and says what the ranking is
worth — the checks that matter are the ones a sorted table hides:

    # best 15 passes by result, with the overfitting checks
    python examples/mql_companion/tester_report.py --optimization-report opt.xml \\
        --top 15 --prompt

    # the .set that reproduces one pass, ready to drop into
    # MQL5/Profiles/Tester and re-test on its own
    python examples/mql_companion/tester_report.py --optimization-report opt.xml \\
        --set grid.set --set-from-pass 371 --write-set winner.set

    # what a .set actually asks the optimizer to do
    python examples/mql_companion/tester_report.py --set grid.set

Exit codes: 0 = parsed and every threshold passed, 1 = a threshold failed or
the report could not be parsed, 2 = toolchain problem (no terminal, no report).
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)
from xml.etree import ElementTree

import click

logger = logging.getLogger("mt5_tester_report")

# ---------------------------------------------------------------------------
# Metric vocabulary
# ---------------------------------------------------------------------------

# Canonical metric key -> every label that may introduce it in a report.
# English HTML labels first, then MQL5 ENUM_STATISTICS identifiers and their
# snake_case spellings. Aliases are matched after normalization, which keeps
# parentheses, so "Balance Drawdown Maximal" and "Balance Drawdown Maximal (%)"
# stay distinct — collapsing them is how two metrics silently trade places.
LABELS: Dict[str, Tuple[str, ...]] = {
    # -- what was tested ---------------------------------------------------
    "expert": ("Expert Advisor", "Expert", "EA"),
    "symbol": ("Symbol",),
    "period": ("Period", "Timeframe"),
    "from_date": ("From Date", "Start Date", "FromDate"),
    "to_date": ("To Date", "End Date", "ToDate"),
    "model": ("Model", "Testing Model", "Ticks Model"),
    "execution_mode": ("Execution Mode", "ExecutionMode", "Delays"),
    "inputs": ("Inputs", "Expert Parameters", "ExpertParameters"),
    "bars": ("Total Bars", "Bars", "Bars Processed"),
    "ticks": ("Ticks", "Total Ticks", "Ticks Processed"),
    "history_quality_pct": ("History Quality", "History quality"),
    "initial_deposit": ("Initial Deposit", "Deposit", "STAT_INITIAL_DEPOSIT"),
    "withdrawal": ("Withdrawal", "STAT_WITHDRAWAL"),
    "currency": ("Currency", "Deposit Currency"),
    "leverage": ("Leverage",),
    "duration": ("Time", "Duration", "Test duration"),
    # -- headline money ----------------------------------------------------
    "net_profit": ("Total Net Profit", "Net Profit", "STAT_PROFIT"),
    "gross_profit": ("Gross Profit", "STAT_GROSS_PROFIT"),
    "gross_loss": ("Gross Loss", "STAT_GROSS_LOSS"),
    "profit_factor": ("Profit Factor", "STAT_PROFIT_FACTOR"),
    "expected_payoff": ("Expected Payoff", "STAT_EXPECTED_PAYOFF"),
    "recovery_factor": ("Recovery Factor", "STAT_RECOVERY_FACTOR"),
    "sharpe_ratio": ("Sharpe Ratio", "STAT_SHARPE_RATIO"),
    "min_margin_level_pct": (
        "Minimum Margin Level",
        "Min Margin Level",
        "STAT_MIN_MARGINLEVEL",
    ),
    # -- drawdowns: four numbers per curve, and they are not the same -----
    "balance_drawdown": (
        "Balance Drawdown Maximal",
        "Balance DD Maximal",
        "STAT_BALANCE_DD",
    ),
    "balance_drawdown_pct": (
        "Balance Drawdown Maximal (%)",
        "STAT_BALANCEDD_PERCENT",
    ),
    "balance_drawdown_relative_pct": (
        "Balance Drawdown Relative",
        "Balance DD Relative",
        "STAT_BALANCE_DDREL_PERCENT",
    ),
    "balance_drawdown_relative": (
        "Balance Drawdown Relative (money)",
        "STAT_BALANCE_DD_RELATIVE",
    ),
    "equity_drawdown": (
        "Equity Drawdown Maximal",
        "Equity DD Maximal",
        "STAT_EQUITY_DD",
    ),
    "equity_drawdown_pct": ("Equity Drawdown Maximal (%)", "STAT_EQUITYDD_PERCENT"),
    "equity_drawdown_relative_pct": (
        "Equity Drawdown Relative",
        "Equity DD Relative",
        "STAT_EQUITY_DDREL_PERCENT",
    ),
    "equity_drawdown_relative": (
        "Equity Drawdown Relative (money)",
        "STAT_EQUITY_DD_RELATIVE",
    ),
    "balance_min": ("Balance Minimal", "Minimum Balance", "STAT_BALANCEMIN"),
    "equity_min": ("Equity Minimal", "Minimum Equity", "STAT_EQUITYMIN"),
    "balance_drawdown_absolute": (
        "Balance Drawdown Absolute",
        "Absolute Drawdown",
    ),
    "equity_drawdown_absolute": ("Equity Drawdown Absolute",),
    # -- shape of the equity curve: how much of the profit was one lucky run
    "z_score": ("Z-Score", "Z Score", "STAT_ZSCORE"),
    "ahpr": ("AHPR", "Average Holding Period Return"),
    "ghpr": ("GHPR", "Geometric Holding Period Return"),
    "lr_correlation": ("LR Correlation", "LRCorrelation"),
    "lr_standard_error": ("LR Standard Error", "LRStandardError"),
    "ontester_result": ("OnTester Result", "OnTester", "Custom Criterion"),
    "correlation_profit_mfe": ("Correlation Profits/MFE", "Correlation_Profits_MFE"),
    "correlation_profit_mae": ("Correlation Profits/MAE", "Correlation_Profits_MAE"),
    "correlation_mfe_mae": ("Correlation MFE/MAE", "Correlation_MFE_MAE"),
    "min_holding_time": ("Minimal Position Holding Time", "Min Holding Time"),
    "max_holding_time": ("Maximal Position Holding Time", "Max Holding Time"),
    "avg_holding_time": ("Average Position Holding Time", "Avg Holding Time"),
    # -- trade counts ------------------------------------------------------
    "total_trades": ("Total Trades", "Trades", "STAT_TRADES"),
    "total_deals": ("Total Deals", "Deals", "STAT_DEALS"),
    "profit_trades": (
        "Profit Trades",
        "Profit Trades (% of total)",
        "Profitable Trades",
        "STAT_PROFIT_TRADES",
    ),
    "loss_trades": (
        "Loss Trades",
        "Loss Trades (% of total)",
        "Losing Trades",
        "STAT_LOSS_TRADES",
    ),
    "short_trades": ("Short Trades", "Sell Trades", "STAT_SHORT_TRADES"),
    "long_trades": ("Long Trades", "Buy Trades", "STAT_LONG_TRADES"),
    "max_profit_trade": (
        "Largest Profit Trade",
        "Maximum Profit Trade",
        "STAT_MAX_PROFITTRADE",
    ),
    "max_loss_trade": (
        "Largest Loss Trade",
        "Maximum Loss Trade",
        "STAT_MAX_LOSSTRADE",
    ),
    "avg_profit_trade": ("Average Profit Trade",),
    "avg_loss_trade": ("Average Loss Trade",),
    # -- streaks: money and a trade count, in either order ----------------
    "conprofit_max": ("Maximal Consecutive Profit", "STAT_CONPROFITMAX"),
    "conprofit_max_trades": (
        "Maximal Consecutive Profit (trades)",
        "STAT_CONPROFITMAX_TRADES",
    ),
    "max_conwins": ("Maximum Consecutive Wins", "STAT_MAX_CONWINS"),
    "max_conprofit_trades": (
        "Maximum Consecutive Profit Trades",
        "STAT_MAX_CONPROFIT_TRADES",
    ),
    "conloss_max": ("Maximal Consecutive Loss", "STAT_CONLOSSMAX"),
    "conloss_max_trades": (
        "Maximal Consecutive Loss (trades)",
        "STAT_CONLOSSMAX_TRADES",
    ),
    "max_conlosses": ("Maximum Consecutive Losses", "STAT_MAX_CONLOSSES"),
    "max_conloss_trades": (
        "Maximum Consecutive Loss Trades",
        "STAT_MAX_CONLOSS_TRADES",
    ),
    "avg_conwins": ("Average Consecutive Wins", "STAT_PROFITTRADES_AVGCON"),
    "avg_conlosses": ("Average Consecutive Losses", "STAT_LOSSTRADES_AVGCON"),
}

# Keys holding text rather than numbers.
TEXT_KEYS = {
    "expert",
    "min_holding_time",
    "max_holding_time",
    "avg_holding_time",
    "symbol",
    "period",
    "period_text",
    "from_date",
    "to_date",
    "inputs",
    "currency",
    "leverage",
    "duration",
    "model",
    "execution_mode",
}

# Keys whose value is a percentage (stored as a plain number, unit implied).
PERCENT_KEYS = {
    "history_quality_pct",
    "balance_drawdown_pct",
    "balance_drawdown_relative_pct",
    "equity_drawdown_pct",
    "equity_drawdown_relative_pct",
    "profit_trades_pct",
    "loss_trades_pct",
    "short_trades_pct",
    "long_trades_pct",
    "min_margin_level_pct",
}

# Integer-valued counters.
INT_KEYS = {
    "bars",
    "ticks",
    "total_trades",
    "total_deals",
    "profit_trades",
    "loss_trades",
    "short_trades",
    "long_trades",
    "conprofit_max_trades",
    "conloss_max_trades",
    "max_conprofit_trades",
    "max_conloss_trades",
    "avg_conwins",
    "avg_conlosses",
    "model_id",
}

# Cells that carry two numbers. Maps the canonical key of the matched label to
# (key_for_the_value_outside_the_parentheses, key_for_the_aside, kind):
#   "ordered" -> the outside value belongs to the label and the aside to its
#                companion, whichever of money/percentage/count each is. So
#                "Equity Drawdown Maximal: 812.44 (8.01%)" fills the money key
#                then the percentage key, while "Equity Drawdown Relative:
#                8.01% (812.44)" fills the percentage key then the money one.
#   "auto"     -> one piece is a trade count and the other is money, and the
#                order is not fixed; decide by which piece is an integer, then
#                by the label's wording.
PAIR_SPLIT: Dict[str, Tuple[str, str, str]] = {
    "balance_drawdown": ("balance_drawdown", "balance_drawdown_pct", "ordered"),
    "balance_drawdown_relative_pct": (
        "balance_drawdown_relative_pct",
        "balance_drawdown_relative",
        "ordered",
    ),
    "equity_drawdown": ("equity_drawdown", "equity_drawdown_pct", "ordered"),
    "equity_drawdown_relative_pct": (
        "equity_drawdown_relative_pct",
        "equity_drawdown_relative",
        "ordered",
    ),
    "profit_trades": ("profit_trades", "profit_trades_pct", "ordered"),
    "loss_trades": ("loss_trades", "loss_trades_pct", "ordered"),
    "short_trades": ("short_trades", "short_trades_pct", "ordered"),
    "long_trades": ("long_trades", "long_trades_pct", "ordered"),
    "max_conwins": ("max_conwins", "max_conprofit_trades", "auto"),
    "max_conlosses": ("max_conlosses", "max_conloss_trades", "auto"),
    "conprofit_max": ("conprofit_max", "conprofit_max_trades", "auto"),
    "conloss_max": ("conloss_max", "conloss_max_trades", "auto"),
}

# The metrics worth putting in front of a model, in reading order.
HEADLINE_KEYS: Tuple[str, ...] = (
    "net_profit",
    "profit_factor",
    "recovery_factor",
    "sharpe_ratio",
    "expected_payoff",
    "equity_drawdown",
    "equity_drawdown_pct",
    "equity_drawdown_relative_pct",
    "balance_drawdown",
    "balance_drawdown_relative_pct",
    "total_trades",
    "total_deals",
    "profit_trades",
    "loss_trades",
    "profit_trades_pct",
    "max_profit_trade",
    "max_loss_trade",
    "avg_profit_trade",
    "avg_loss_trade",
    "max_conlosses",
    "max_conloss_trades",
    "avg_conwins",
    "avg_conlosses",
    "history_quality_pct",
    "initial_deposit",
    "min_margin_level_pct",
)

# MetaTrader 5 [Tester] ini enumerations, so generated configs stay readable.
TESTER_MODELS: Dict[int, str] = {
    0: "every tick",
    1: "1 minute OHLC",
    2: "open prices only",
    3: "math calculations",
    4: "every tick based on real ticks",
}
OPTIMIZATION_MODES: Dict[int, str] = {
    0: "disabled",
    1: "slow complete algorithm",
    2: "fast genetic based algorithm",
    3: "all symbols in Market Watch",
}
OPTIMIZATION_CRITERIA: Dict[int, str] = {
    0: "balance max",
    1: "profit factor max",
    2: "expected payoff max",
    3: "drawdown min",
    4: "recovery factor max",
    5: "sharpe ratio max",
    6: "custom max (OnTester)",
}
TIMEFRAMES: Tuple[str, ...] = (
    "MN1",
    "W1",
    "D1",
    "H12",
    "H8",
    "H6",
    "H4",
    "H3",
    "H2",
    "H1",
    "M30",
    "M20",
    "M15",
    "M12",
    "M10",
    "M6",
    "M5",
    "M4",
    "M3",
    "M2",
    "M1",
)

REPORT_SUFFIXES = (".htm", ".html", ".xml", ".txt")


# ---------------------------------------------------------------------------
# Value normalization
# ---------------------------------------------------------------------------

_NBSP = "\u00a0"
_THIN_SPACE = "\u2009"
_DATE_RE = re.compile(r"^\d{4}[./-]\d{1,2}[./-]\d{1,2}")
_LEVERAGE_RE = re.compile(r"^1\s*:\s*\d+$")
_PERCENT_IN_VALUE = re.compile(r"([-+]?\d[\d\s.,]*)\s*%")
_LETTER_DIGIT_RE = re.compile(r"(?:[A-Za-z]\d)|(?:\d[A-Za-z])")


def normalize_text(raw: Any) -> str:
    """Collapse the whitespace tricks MT5 reports use."""
    text = "" if raw is None else str(raw)
    text = text.replace(_NBSP, " ").replace(_THIN_SPACE, " ")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_number(raw: Any) -> Optional[float]:
    """Read a report value as a float, or ``None`` when it is not numeric.

    Handles the shapes MT5 emits: ``1 234.56`` (non-breaking-space thousands),
    ``-765.44``, ``1,234.56``, ``1234,56`` (European decimal comma),
    ``45.2%``, ``USD 1 234.56``, and accounting-style ``(765.44)``.
    """
    text = normalize_text(raw)
    if not text:
        return None
    if _DATE_RE.match(text) or _LEVERAGE_RE.match(text):
        # A date or a leverage ratio is not a number, even though it is full of
        # digits and dots.
        return None

    negative = False
    if text.startswith("(") and text.endswith(")") and "(" not in text[1:-1]:
        negative = True
        text = text[1:-1]

    percent = _PERCENT_IN_VALUE.search(text)
    if percent:
        text = percent.group(1)

    if _LETTER_DIGIT_RE.search(text):
        # "MACD Sample.ex5", "H1", "M15": an identifier, not a quantity. A
        # letter glued to a digit is never part of a number the tester means.
        return None
    text = re.sub(r"[A-Za-z]{2,}", " ", text).strip()
    text = text.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    if text.startswith("-"):
        negative = not negative
        text = text[1:].strip()
    text = text.replace(" ", "")
    if not text:
        return None

    has_comma = "," in text
    has_dot = "." in text
    if has_comma and has_dot:
        # The right-most separator is the decimal one.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        parts = text.split(",")
        if len(parts[-1]) == 3 and all(p.isdigit() for p in parts):
            text = "".join(parts)  # thousands separators
        else:
            text = text.replace(",", ".")  # decimal comma
    elif has_dot:
        parts = text.split(".")
        if len(parts) > 2 and all(p.isdigit() for p in parts):
            # 1.234.567 -> thousands; 2024.01.01 was caught as a date above.
            text = "".join(parts)

    try:
        value = float(text)
    except ValueError:
        return None
    if value != value:  # NaN
        return None
    return -value if negative else value


@dataclass
class SplitValue:
    """A cell decomposed into the numbers it actually carries."""

    text: str
    primary: Optional[float] = None
    primary_is_percent: bool = False
    secondary: Optional[float] = None
    secondary_is_percent: bool = False

    @property
    def has_pair(self) -> bool:
        return self.primary is not None and self.secondary is not None


def split_value(raw: Any) -> SplitValue:
    """Split ``"705.00 (6.90%)"`` into money 705.00 and percent 6.90.

    The parenthetical is the secondary number; whether it is a percentage or
    money depends on the ``%`` sign, which the report is reliable about.
    """
    text = normalize_text(raw)
    result = SplitValue(text=text)
    if not text:
        return result

    match = re.search(r"\(([^()]*)\)", text)
    outside = re.sub(r"\([^()]*\)", " ", text).strip() if match else text
    result.primary = normalize_number(outside)
    result.primary_is_percent = "%" in outside

    if match:
        inside = match.group(1)
        result.secondary = normalize_number(inside)
        result.secondary_is_percent = "%" in inside
    return result


def extract_period_code(text: str) -> Optional[str]:
    """Pull ``H1`` out of ``"1 Hour (H1)  2024.01.01 - ..."``, if present."""
    for code in TIMEFRAMES:
        if re.search(rf"(?<![A-Z0-9]){code}(?![A-Z0-9])", text):
            return code
    return None


def extract_date_range(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Pull the first two ``YYYY.MM.DD`` dates out of a period description."""
    found = re.findall(r"\d{4}[./]\d{2}[./]\d{2}", text)
    if len(found) >= 2:
        return found[0].replace("/", "."), found[1].replace("/", ".")
    if len(found) == 1:
        return found[0].replace("/", "."), None
    return None, None


# ---------------------------------------------------------------------------
# Report container
# ---------------------------------------------------------------------------


@dataclass
class TesterReport:
    """A parsed Strategy Tester report.

    ``metrics`` holds canonical keys with normalized values; ``raw`` keeps
    every label/value pair exactly as the file spelled it, so nothing is lost
    when a label is outside the vocabulary. ``missing`` lists the headline
    metrics that were not found — an agent should say so rather than invent
    them.
    """

    source: str = ""
    format: str = "unknown"
    metrics: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    derived: List[str] = field(default_factory=list)
    run: Dict[str, Any] = field(default_factory=dict)
    parsed_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    )

    @property
    def missing(self) -> List[str]:
        return [key for key in HEADLINE_KEYS if key not in self.metrics]

    def get(self, key: str, default: Any = None) -> Any:
        return self.metrics.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "format": self.format,
            "metrics": self.metrics,
            "missing": self.missing,
            "warnings": self.warnings,
            "derived": self.derived,
            "run": self.run,
            "raw_labels": len(self.raw),
        }


# ---------------------------------------------------------------------------
# Label index
# ---------------------------------------------------------------------------


def _normalize_label(label: str) -> str:
    text = normalize_text(label).lower()
    text = text.replace("_", " ").replace("-", " ")
    text = text.replace("%", " %")
    text = re.sub(r"[^a-z0-9 %]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_aside(label: str) -> str:
    """Drop a parenthetical qualifier: ``Short Trades (won %)`` -> ``short trades``.

    The parentheses have to go *before* normalization, which discards
    punctuation and would leave the qualifier's words behind.
    """
    text = re.sub(r"\([^)]*\)", " ", normalize_text(label))
    return re.sub(r"\s+", " ", _normalize_label(text)).strip(" %").strip()


def build_label_lookup() -> Dict[str, str]:
    """Reverse index: normalized label -> canonical key.

    Two passes. Explicit aliases are registered first and a collision between
    two of them raises: that would let one metric silently take another's
    value, which is worse than not parsing at all. The snake_case spelling of
    each key is added afterwards as a fallback for XML exports, and yields to
    any explicit alias already claiming that string ("Balance Drawdown
    Relative" is a real report label; ``balance_drawdown_relative`` is not).
    """
    lookup: Dict[str, str] = {}
    for key, aliases in LABELS.items():
        for alias in aliases:
            normalized = _normalize_label(alias)
            if not normalized:
                continue
            existing = lookup.get(normalized)
            if existing is not None and existing != key:
                raise ValueError(
                    f"label collision: {normalized!r} is claimed by both "
                    f"{existing!r} and {key!r}"
                )
            lookup[normalized] = key
    for key in LABELS:
        lookup.setdefault(_normalize_label(key), key)
    return lookup


def build_loose_lookup(strict: Dict[str, str]) -> Dict[str, str]:
    """Fallback index that ignores parenthetical qualifiers.

    Reports decorate labels in ways the vocabulary cannot enumerate — "Short
    Trades (won %)", "Profit Trades (% of total)", "Balance Drawdown Maximal
    ($)". Stripping the aside recovers most of them, but only where the result
    is unambiguous: "balance drawdown maximal" is claimed by both the money and
    the percentage metric, so the strict table decides that one and the loose
    table stays silent.
    """
    loose: Dict[str, str] = {}
    ambiguous = set()
    for key, aliases in LABELS.items():
        for alias in list(aliases) + [key]:
            stripped = _strip_aside(alias)
            if not stripped:
                continue
            existing = loose.get(stripped)
            if existing is None:
                loose[stripped] = key
            elif existing != key:
                ambiguous.add(stripped)
    for name in ambiguous:
        loose.pop(name, None)
    # Anything the strict table already resolves exactly needs no fallback.
    for name in list(loose):
        if name in strict and strict[name] != loose[name]:
            loose.pop(name)
    return loose


_LOOKUP = build_label_lookup()
_LOOKUP_LOOSE = build_loose_lookup(_LOOKUP)


def _match_label(cell: str) -> Optional[str]:
    """Return the canonical key for a cell that is a known metric label."""
    candidate = _normalize_label(cell)
    if not candidate or len(candidate) < 2:
        return None
    exact = _LOOKUP.get(candidate) or _LOOKUP.get(candidate.rstrip("%").strip())
    if exact is not None:
        return exact
    stripped = _strip_aside(cell)
    if not stripped or stripped == candidate:
        return None
    return _LOOKUP_LOOSE.get(stripped) or _LOOKUP_LOOSE.get(
        stripped.rstrip("%").strip()
    )


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_ROW_BREAK_RE = re.compile(r"</(tr|p|div|li|h\d|td|th)>|<br\s*/?>|\r?\n", re.IGNORECASE)


def html_cells(text: str) -> List[str]:
    """Flatten HTML into a stream of non-empty cell texts.

    Reports do not always separate a label from its value: a single-cell layout
    writes ``Total Net Profit: 1234.56``. Splitting on the colon when the left
    half is a known label keeps the "label then value" rule working for both.
    """
    # Row/cell boundaries become separators so a label never merges with the
    # value from the next cell.
    spaced = _ROW_BREAK_RE.sub(" \x00 ", text)
    stripped = _TAG_RE.sub(" ", spaced)
    cells: List[str] = []
    for chunk in stripped.split("\x00"):
        value = normalize_text(chunk)
        if not value:
            continue
        if ":" in value:
            label, _, remainder = value.partition(":")
            if _match_label(label) is not None and normalize_text(remainder):
                cells.append(normalize_text(label))
                cells.append(normalize_text(remainder))
                continue
        cells.append(value)
    return cells


def parse_cell_stream(cells: Sequence[str], report: TesterReport) -> None:
    """Fill ``report`` from a flat label/value stream.

    Rule: when a cell is a known label, the next cell that is *not* a known
    label is its value. Reports put two label/value pairs on one row, so a
    label may be followed directly by another label.
    """
    index = 0
    total = len(cells)
    while index < total:
        cell = cells[index]
        key = _match_label(cell)
        if key is None:
            index += 1
            continue
        value_cell: Optional[str] = None
        for probe in range(index + 1, min(index + 4, total)):
            if _match_label(cells[probe]) is not None:
                break
            value_cell = cells[probe]
            break
        if value_cell is None:
            index += 1
            continue
        report.raw.setdefault(normalize_text(cell), normalize_text(value_cell))
        _assign(report, key, value_cell, cell)
        index += 2


def _store(report: TesterReport, key: str, value: Any) -> None:
    """Coerce and store one metric."""
    if value is None:
        return
    if key in TEXT_KEYS:
        text = normalize_text(value)
        if text:
            report.metrics[key] = text
        return
    number = value if isinstance(value, (int, float)) else normalize_number(value)
    if number is None:
        text = normalize_text(value)
        if text:
            # Keep the text — `model` and `leverage` are words, not numbers.
            report.metrics[key] = text
        return
    if key in INT_KEYS:
        report.metrics[key] = int(round(number))
    elif key in PERCENT_KEYS:
        report.metrics[key] = round(number, 4)
    else:
        report.metrics[key] = round(number, 6)


def _assign(report: TesterReport, key: str, value_cell: str, label: str = "") -> None:
    """Store one metric, splitting cells that carry two numbers."""
    if key in TEXT_KEYS:
        # Splitting a text cell would "0.5" its way through ".ex5" filenames.
        _store(report, key, normalize_text(value_cell))
        return
    parts = split_value(value_cell)
    split = PAIR_SPLIT.get(key)

    if split is not None and parts.has_pair:
        primary_key, secondary_key, kind = split
        if kind != "auto":
            _store(report, primary_key, parts.primary)
            _store(report, secondary_key, parts.secondary)
        else:
            # All four MQL5 streak statistics are money; their companion
            # `*_TRADES` statistics are counts. The report writes both in one
            # cell, usually "count (money)" for wins/losses labels and
            # "money (count)" for profit/loss labels.
            first, second = parts.primary, parts.secondary
            count_is_first = _looks_like_count(first) and not _looks_like_count(second)
            count_is_second = _looks_like_count(second) and not _looks_like_count(first)
            if count_is_first:
                count, money = first, second
            elif count_is_second:
                count, money = second, first
            else:
                # Both pieces are plausible counts ("7 (310)"), so fall back to
                # how the label is worded.
                wording = _normalize_label(label or value_cell)
                if "win" in wording or "loss" in wording:
                    count, money = first, second
                else:
                    count, money = second, first
            _store(report, secondary_key, count)
            _store(report, primary_key, money)
        return

    if parts.has_pair:
        # An unrecognized pair: keep the outside number, ignore the aside.
        _store(report, key, parts.primary)
        return

    _store(report, key, parts.primary if parts.primary is not None else value_cell)


def _looks_like_count(value: Optional[float]) -> bool:
    return value is not None and abs(value) < 10_000 and float(value).is_integer()


def _read_header_line(cells: Sequence[str], report: TesterReport) -> None:
    """MT5 reports open with e.g. ``EURUSD (EURUSD,H1)  MACD Sample``."""
    pattern = re.compile(
        r"(?P<symbol>[A-Z0-9._-]{3,20})\s*\((?P<base>[A-Z0-9._-]{2,20})\s*,"
        r"\s*(?P<period>[A-Z0-9]{2,5})\)"
    )
    for cell in cells[:8]:
        match = pattern.search(cell)
        if match:
            if "symbol" not in report.metrics:
                report.metrics["symbol"] = match.group("symbol")
            if "period" not in report.metrics:
                report.metrics["period"] = match.group("period")
            remainder = cell[match.end() :].strip()
            if remainder and "expert" not in report.metrics:
                report.metrics["expert"] = remainder
            break


def _refine_text_metrics(report: TesterReport) -> None:
    """Normalize the wordy cells: timeframe codes, dates, model ids."""
    metrics = report.metrics
    period_text = str(metrics.get("period", ""))
    if period_text:
        code = extract_period_code(period_text)
        if code:
            if code != period_text:
                metrics["period_text"] = period_text
            metrics["period"] = code
        start, end = extract_date_range(period_text)
        if start:
            metrics.setdefault("from_date", start)
        if end:
            metrics.setdefault("to_date", end)

    model_text = normalize_text(metrics.get("model", ""))
    if model_text:
        lowered = re.sub(r"\([^)]*\)", "", model_text).strip().lower()
        if lowered.isdigit():
            metrics["model_id"] = int(lowered)
        else:
            for model_id, description in TESTER_MODELS.items():
                wanted = description.lower()
                if lowered == wanted or lowered.startswith(wanted):
                    metrics["model_id"] = model_id
                    break
    quality = metrics.get("history_quality_pct")
    if isinstance(quality, str):
        number = normalize_number(quality)
        if number is not None:
            metrics["history_quality_pct"] = round(number, 4)


def parse_html_report(text: str, source: str = "") -> TesterReport:
    report = TesterReport(source=source, format="html")
    cells = html_cells(text)
    parse_cell_stream(cells, report)
    _read_header_line(cells, report)
    _refine_text_metrics(report)
    derive_and_check(report)
    return report


def parse_xml_report(text: str, source: str = "") -> TesterReport:
    """Parse an XML export without pinning one schema.

    Report XML varies by build and export path, and optimization exports add
    per-pass attributes. Rather than hardcode a schema, every element text and
    attribute is offered to the label matcher, so ``<Profit_Factor>1.38``
    ``</Profit_Factor>``, ``<Stat name="STAT_PROFIT_FACTOR" value="1.38"/>``
    and ``<Result profit_factor="1.38"/>`` all land in ``profit_factor``.
    """
    report = TesterReport(source=source, format="xml")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        report.warnings.append(f"XML parse error: {exc}")
        derive_and_check(report)
        return report

    def walk(element: ElementTree.Element) -> None:
        text_value = normalize_text(element.text)
        key = _match_label(element.tag)
        if key is not None and text_value:
            report.raw.setdefault(element.tag, text_value)
            _assign(report, key, text_value, element.tag)
        for attr_name, attr_value in element.attrib.items():
            attr_key = _match_label(attr_name)
            if attr_key is not None:
                report.raw.setdefault(f"{element.tag}@{attr_name}", attr_value)
                _assign(report, attr_key, attr_value, attr_name)
            if attr_name.lower() in {"name", "key", "id", "title", "label"}:
                named_key = _match_label(attr_value)
                value_attr = (
                    element.attrib.get("value")
                    or element.attrib.get("val")
                    or element.attrib.get("data")
                    or text_value
                )
                if named_key is not None and value_attr:
                    report.raw.setdefault(attr_value, value_attr)
                    _assign(report, named_key, value_attr, attr_value)
        for child in element:
            walk(child)

    walk(root)
    _refine_text_metrics(report)
    derive_and_check(report)
    return report


def parse_text_report(text: str, source: str = "") -> TesterReport:
    """Fallback for plain-text dumps copied from the tester's Results pane.

    Accepts ``Label: value``, ``Label<TAB>value`` and ``Label  value`` lines.
    """
    report = TesterReport(source=source, format="text")
    cells: List[str] = []
    for line in text.splitlines():
        cleaned = normalize_text(line)
        if not cleaned:
            continue
        if ":" in cleaned:
            label, _, value = cleaned.partition(":")
            cells.extend([label.strip(), value.strip()])
        elif "\t" in cleaned:
            cells.extend(part.strip() for part in cleaned.split("\t"))
        else:
            cells.extend(part.strip() for part in re.split(r"\s{2,}", cleaned))
    parse_cell_stream([cell for cell in cells if cell], report)
    _refine_text_metrics(report)
    derive_and_check(report)
    return report


def decode_report_bytes(raw: bytes) -> str:
    """MT5 writes UTF-16LE (as it does for compile logs) or UTF-8/CP1251."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if b"\x00" in raw[:4096]:
        return raw.decode("utf-16-le", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for encoding in ("cp1251", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:  # pragma: no cover - latin-1 never fails
            continue
    return raw.decode("utf-8", errors="replace")


def parse_report(path: Path | str) -> TesterReport:
    """Parse a report file, sniffing the format when the extension lies."""
    report_path = Path(path).expanduser()
    if not report_path.is_file():
        raise FileNotFoundError(f"report not found: {report_path}")
    text = decode_report_bytes(report_path.read_bytes())
    suffix = report_path.suffix.lower()
    if suffix == ".xml" or text.lstrip()[:5] == "<?xml":
        return parse_xml_report(text, str(report_path))
    if suffix in (".htm", ".html") or "<html" in text[:4000].lower():
        return parse_html_report(text, str(report_path))
    return parse_text_report(text, str(report_path))


# ---------------------------------------------------------------------------
# Derived metrics and consistency checks
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, str):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def derive_and_check(report: TesterReport) -> TesterReport:
    """Fill gaps from documented identities and cross-check what was read.

    MQL5 documents these exactly:

    * ``net_profit = gross_profit + gross_loss`` — gross_loss is **negative**
    * ``profit_factor = gross_profit / abs(gross_loss)``
    * ``recovery_factor = net_profit / balance_drawdown``
    """
    m = report.metrics
    gross_profit = _as_float(m.get("gross_profit"))
    gross_loss = _as_float(m.get("gross_loss"))
    net = _as_float(m.get("net_profit"))

    if net is None and gross_profit is not None and gross_loss is not None:
        m["net_profit"] = round(gross_profit + gross_loss, 6)
        report.derived.append("net_profit = gross_profit + gross_loss")
        net = m["net_profit"]
    elif None not in (net, gross_profit, gross_loss):
        expected = round(gross_profit + gross_loss, 2)
        if abs(expected - net) > max(0.05, abs(net) * 0.005):
            report.warnings.append(
                f"net_profit {net} disagrees with gross_profit + gross_loss = "
                f"{expected}. MT5 reports gross_loss as a negative number, so "
                "one of the three was misread"
            )

    if gross_profit is not None and gross_loss is not None:
        if gross_loss > 0:
            report.warnings.append(
                f"gross_loss {gross_loss} is positive; MT5 reports it as a "
                "negative number, so this value was probably misread"
            )
        loss_magnitude = abs(gross_loss)
        expected_pf: Optional[float] = (
            None if loss_magnitude == 0 else round(gross_profit / loss_magnitude, 6)
        )
        pf = _as_float(m.get("profit_factor"))
        if pf is None and expected_pf is not None:
            m["profit_factor"] = expected_pf
            report.derived.append("profit_factor = gross_profit / abs(gross_loss)")
        elif (
            pf is not None
            and expected_pf is not None
            and abs(pf - expected_pf) > max(0.01, expected_pf * 0.01)
        ):
            report.warnings.append(
                f"profit_factor {pf} disagrees with gross_profit / "
                f"abs(gross_loss) = {expected_pf}"
            )

    balance_dd = _as_float(m.get("balance_drawdown"))
    if net is not None and balance_dd:
        expected_rf = round(net / balance_dd, 6)
        rf = _as_float(m.get("recovery_factor"))
        if rf is None:
            m["recovery_factor"] = expected_rf
            report.derived.append("recovery_factor = net_profit / balance_drawdown")
        elif abs(rf - expected_rf) > max(0.01, abs(expected_rf) * 0.02):
            report.warnings.append(
                f"recovery_factor {rf} disagrees with net_profit / "
                f"balance_drawdown = {expected_rf}"
            )

    wins = _as_float(m.get("profit_trades"))
    losses = _as_float(m.get("loss_trades"))
    if wins is not None and losses is not None and (wins + losses) > 0:
        if "profit_trades_pct" not in m:
            m["profit_trades_pct"] = round(100.0 * wins / (wins + losses), 4)
            report.derived.append("profit_trades_pct from win/loss counts")
        total = _as_float(m.get("total_trades"))
        if total is None:
            m["total_trades"] = int(wins + losses)
            report.derived.append("total_trades = profit_trades + loss_trades")
        elif abs(total - (wins + losses)) > 0.5:
            report.warnings.append(
                f"total_trades {int(total)} does not equal profit_trades "
                f"({int(wins)}) + loss_trades ({int(losses)}) = {int(wins + losses)}"
            )

    win_pct = _as_float(m.get("profit_trades_pct"))
    loss_pct = _as_float(m.get("loss_trades_pct"))
    if win_pct is not None and loss_pct is not None:
        if abs((win_pct + loss_pct) - 100.0) > 1.5:
            report.warnings.append(
                f"profit_trades_pct {win_pct} + loss_trades_pct {loss_pct} "
                "does not add up to 100% — one of them was misread"
            )

    quality = _as_float(m.get("history_quality_pct"))
    if quality is not None and quality < 90.0:
        report.warnings.append(
            f"history quality is {quality}% — gaps in the tick/bar history make "
            "these results less trustworthy than they look"
        )

    equity_dd = _as_float(m.get("equity_drawdown"))
    balance_dd_value = _as_float(m.get("balance_drawdown"))
    if (
        equity_dd is not None
        and balance_dd_value is not None
        and equity_dd + 1e-9 < balance_dd_value
    ):
        report.warnings.append(
            f"equity_drawdown {equity_dd} is smaller than balance_drawdown "
            f"{balance_dd_value}; equity drawdown normally exceeds balance "
            "drawdown because it includes floating losses"
        )
    return report


# ---------------------------------------------------------------------------
# Finding reports
# ---------------------------------------------------------------------------


def candidate_data_dirs() -> List[Path]:
    """Where a terminal keeps its tester output."""
    dirs: List[Path] = []

    for var in ("MT5_DATA_DIR", "TERMINAL_DATA_PATH"):
        value = os.environ.get(var)
        if value:
            dirs.append(Path(value).expanduser())

    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "MetaQuotes" / "Terminal")
    home = Path.home()
    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    if user:
        dirs.append(
            home / ".wine/drive_c/users" / user / "AppData/Roaming/MetaQuotes/Terminal"
        )
    for wine_root in sorted(home.glob(".wine*")):
        dirs.append(wine_root / "drive_c" / "MetaQuotes")

    terminal = find_terminal()
    if terminal is not None:
        dirs.append(terminal.parent)

    dirs.append(Path.cwd())

    unique: List[Path] = []
    seen = set()
    for candidate in dirs:
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover - defensive
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(candidate)
    return unique


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - race with a deletion
        return 0.0


def _looks_like_report(path: Path) -> bool:
    """Cheap content sniff so unrelated .xml/.html files are skipped."""
    name = path.name.lower()
    if any(token in name for token in ("report", "tester", "statement", "backtest")):
        return True
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    text = decode_report_bytes(head).lower()
    return any(
        token in text for token in ("strategy tester", "profit factor", "net profit")
    )


def find_reports(
    roots: Optional[Iterable[Path | str]] = None, limit: int = 20
) -> List[Path]:
    """Newest-first report files under the given roots (or the usual ones)."""
    search_roots = (
        [Path(root).expanduser() for root in roots] if roots else candidate_data_dirs()
    )
    found: Dict[Path, float] = {}
    for root in search_roots:
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix.lower() in REPORT_SUFFIXES:
                found[root.resolve()] = _mtime(root)
            continue
        for suffix in REPORT_SUFFIXES:
            for path in root.rglob(f"*{suffix}"):
                if path.is_file() and _looks_like_report(path):
                    found[path.resolve()] = _mtime(path)
    ordered = sorted(found.items(), key=lambda item: item[1], reverse=True)
    return [path for path, _ in ordered[:limit]]


# ---------------------------------------------------------------------------
# Thresholds and comparison
# ---------------------------------------------------------------------------


@dataclass
class ThresholdResult:
    """One CI gate against one report."""

    name: str
    rule: str
    actual: Optional[float]
    passed: bool
    message: str


def check_thresholds(
    report: TesterReport, rules: Dict[str, Optional[float]]
) -> List[ThresholdResult]:
    """Evaluate ``min_*`` / ``max_*`` rules against a parsed report.

    A metric that is missing from the report fails its gate: a CI check that
    passes because it could not find the number is worse than no check.
    """
    out: List[ThresholdResult] = []
    for rule, expected in rules.items():
        if expected is None:
            continue
        if rule.startswith("min_"):
            key, description = rule[4:], f">= {expected}"
            compare = "min"
        elif rule.startswith("max_"):
            key, description = rule[4:], f"<= {expected}"
            compare = "max"
        else:  # pragma: no cover - the CLI only passes min_/max_ keys
            continue
        actual = _as_float(report.metrics.get(key))
        if actual is None:
            out.append(
                ThresholdResult(
                    name=key,
                    rule=description,
                    actual=None,
                    passed=False,
                    message=(
                        f"{key} is not in the report — cannot verify {description}"
                    ),
                )
            )
            continue
        passed = actual >= expected if compare == "min" else actual <= expected
        out.append(
            ThresholdResult(
                name=key,
                rule=description,
                actual=actual,
                passed=passed,
                message=(
                    f"{key} = {actual} ({'ok' if passed else 'FAILED'} {description})"
                ),
            )
        )
    return out


def compare_reports(
    reports: Sequence[TesterReport], keys: Optional[Sequence[str]] = None
) -> Dict[str, Any]:
    """Side-by-side metrics with deltas against the first report."""
    columns = list(keys or HEADLINE_KEYS)
    sources = [
        report.source or f"report{index + 1}" for index, report in enumerate(reports)
    ]
    table: Dict[str, Any] = {"sources": sources, "rows": [], "verdict": {}}
    for key in columns:
        row: Dict[str, Any] = {
            "metric": key,
            "values": [report.metrics.get(key) for report in reports],
        }
        first = _as_float(reports[0].metrics.get(key))
        second = _as_float(reports[1].metrics.get(key)) if len(reports) > 1 else None
        if first is not None and second is not None:
            row["delta"] = round(second - first, 6)
        table["rows"].append(row)

    if len(reports) >= 2:
        verdict: Dict[str, Any] = {}
        for key, higher_is_better in (
            ("net_profit", True),
            ("profit_factor", True),
            ("recovery_factor", True),
            ("sharpe_ratio", True),
            ("equity_drawdown_relative_pct", False),
        ):
            scored = [
                (index, _as_float(report.metrics.get(key)))
                for index, report in enumerate(reports)
            ]
            scored = [(index, value) for index, value in scored if value is not None]
            if len(scored) < 2:
                continue
            best = (max if higher_is_better else min)(scored, key=lambda item: item[1])
            verdict[key] = {
                "winner": sources[best[0]],
                "value": best[1],
                "higher_is_better": higher_is_better,
            }
        table["verdict"] = verdict
    return table


def format_for_prompt(report: TesterReport, max_lines: int = 45) -> str:
    """A compact, model-facing summary — the part that goes into a prompt."""
    lines: List[str] = []
    header = []
    for key in ("expert", "symbol", "period", "from_date", "to_date", "model"):
        value = report.metrics.get(key)
        if value:
            header.append(f"{key}={value}")
    if header:
        lines.append("test: " + " ".join(header))
    for key in ("initial_deposit", "currency", "leverage", "history_quality_pct"):
        value = report.metrics.get(key)
        if value not in (None, ""):
            unit = "%" if key.endswith("_pct") else ""
            lines.append(f"{key}: {value}{unit}")
    for key in HEADLINE_KEYS:
        if key in ("initial_deposit", "history_quality_pct"):
            continue
        value = report.metrics.get(key)
        if value is None:
            continue
        unit = "%" if key.endswith("_pct") else ""
        lines.append(f"{key}: {value}{unit}")
    if report.run:
        lines.append(
            f"run: exit_code={report.run.get('exit_code')} "
            f"seconds={report.run.get('seconds')}"
        )
    if report.derived:
        lines.append("derived: " + "; ".join(report.derived))
    if report.warnings:
        lines.append("warnings:")
        for warning in report.warnings:
            lines.append(f"  - {warning}")
    if report.missing:
        lines.append(f"missing: {', '.join(report.missing)}")
    return "\n".join(lines[:max_lines])


# ---------------------------------------------------------------------------
# Forward checks
# ---------------------------------------------------------------------------

# A forward run splits the test period in two: MT5 optimizes (or tests) on the
# back half, then re-runs the winner on the forward half — data the search never
# saw. It writes two files, ``<name>.htm`` and ``<name>.forward.htm``, and the
# only honest use of the second one is to ask whether the first one survived.
# The trap is comparing them directly: the forward half is usually a fraction of
# the back half, so money and counts mean nothing until they are normalized per
# day, while ratios can be read as they are.
FORWARD_SUFFIXES: Tuple[str, ...] = (".forward.htm", ".forward.xml", ".forward.html")

# Ratios and per-trade figures: the same over a month as over a year, so these
# are compared as written.
PERIOD_INDEPENDENT_KEYS: Tuple[str, ...] = (
    "profit_factor",
    "recovery_factor",
    "sharpe_ratio",
    "expected_payoff",
    "profit_trades_pct",
    "equity_drawdown_pct",
    "equity_drawdown_relative_pct",
    "balance_drawdown_relative_pct",
    "avg_profit_trade",
    "avg_loss_trade",
    "max_conlosses",
    "max_conprofits",
)

# Money and counts: only comparable once divided by the length of each half.
PER_DAY_KEYS: Tuple[str, ...] = ("net_profit", "total_trades", "gross_profit")

LOWER_IS_BETTER_KEYS: FrozenSet[str] = frozenset(
    {
        "equity_drawdown_pct",
        "equity_drawdown_relative_pct",
        "balance_drawdown_relative_pct",
        "max_conlosses",
    }
)

# The ratios whose collapse is the finding, rather than a detail.
FORWARD_GATE_KEYS: Tuple[str, ...] = (
    "profit_factor",
    "recovery_factor",
    "sharpe_ratio",
)


def is_forward_report(path: Path | str) -> bool:
    """True for the ``.forward.`` half of a forward run."""
    return ".forward." in Path(path).name.lower()


def forward_companion(report_path: Path | str) -> List[Path]:
    """Candidate names for the *other* half of a forward run.

    Given ``Run.htm`` this returns the ``Run.forward.*`` names MT5 may have
    written; given a forward file it returns the back-half candidates. Waiting
    for exactly one spelling is how a caller times out on a run that succeeded —
    the terminal picks the extension.
    """
    path = Path(report_path)
    if is_forward_report(path):
        name = re.sub(r"\.forward(?=\.[^.]+$)", "", path.name, flags=re.IGNORECASE)
        base = path.parent / name
        found = [base]
        for suffix in (".htm", ".xml", ".html"):
            candidate = base.with_suffix(suffix)
            if candidate not in found:
                found.append(candidate)
        return found
    stem = path.with_suffix("")
    return [stem.with_suffix(suffix) for suffix in FORWARD_SUFFIXES]


def period_days(report: TesterReport) -> Optional[float]:
    """Length of a report's tested period in days, when it carries both dates."""
    start = str(report.metrics.get("from_date") or "")
    end = str(report.metrics.get("to_date") or "")
    if not start or not end:
        return None
    try:
        first = datetime.strptime(start[:10], "%Y.%m.%d")
        second = datetime.strptime(end[:10], "%Y.%m.%d")
    except ValueError:
        return None
    days = (second - first).days
    return float(days) if days > 0 else None


@dataclass
class ForwardCheck:
    """The back half against the forward half, with a verdict worth repeating.

    ``verdict`` is ``holds_up``, ``degrades`` or ``inconclusive``. The point of
    the third one is that a forward check on nine trades, or on two files with
    no metric in common, does not get to say anything — reporting "it held up"
    there would be worse than reporting nothing.
    """

    available: bool = False
    verdict: str = "inconclusive"
    back_source: str = ""
    forward_source: str = ""
    back_days: Optional[float] = None
    forward_days: Optional[float] = None
    rows: List[Dict[str, Any]] = field(default_factory=list)
    per_day: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    min_trades: int = 30
    max_degradation_pct: float = 50.0

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "available": self.available,
            "verdict": self.verdict,
            "back": self.back_source,
            "forward": self.forward_source,
            "back_days": self.back_days,
            "forward_days": self.forward_days,
            "metrics": self.rows,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "thresholds": {
                "min_trades": self.min_trades,
                "max_degradation_pct": self.max_degradation_pct,
            },
        }
        if self.per_day:
            payload["per_day"] = self.per_day
        return payload


def check_forward(
    back: TesterReport,
    forward: Optional[TesterReport] = None,
    *,
    min_trades: int = 30,
    max_degradation_pct: float = 50.0,
    keys: Optional[Sequence[str]] = None,
) -> ForwardCheck:
    """Compare the in-sample half of a run with its out-of-sample half.

    Three kinds of finding, in the order they matter: a *sign flip* (profitable
    in-sample, not out-of-sample) is a failure; *degradation* past
    ``max_degradation_pct`` on the gate ratios — or on profit per day, when both
    reports carry dates — is a failure; anything thinner than ``min_trades`` in
    either half makes the whole comparison ``inconclusive`` instead.

    Drawdown growth and low history quality are warnings, not verdicts: on their
    own they say the forward half was harder, not that the parameters broke.
    """
    check = ForwardCheck(
        back_source=back.source,
        min_trades=min_trades,
        max_degradation_pct=max_degradation_pct,
    )
    if forward is None:
        check.reasons.append(
            "no forward report: ForwardMode was off, or MT5 did not write the "
            "second file — nothing here says whether the parameters hold up"
        )
        return check

    check.available = True
    check.forward_source = forward.source
    check.back_days = period_days(back)
    check.forward_days = period_days(forward)
    degraded = False
    inconclusive = False

    # -- trade count first: every ratio below it is noise --------------------
    for label, report in (("back", back), ("forward", forward)):
        trades = _as_float(report.metrics.get("total_trades"))
        if trades is None or trades >= min_trades:
            continue
        inconclusive = True
        note = (
            f"the {label} half traded {int(trades)} time(s), under the "
            f"{min_trades} needed to read a ratio as anything but noise"
        )
        if label == "back":
            note += " — the in-sample side was already too thin to optimize on"
        check.reasons.append(note)

    if not any(
        _as_float(report.metrics.get("total_trades")) is not None
        for report in (back, forward)
    ):
        # Consistent with the optimization checks: a rule fires only on a value
        # the file contains, so an absent trade count warns rather than decides.
        check.warnings.append(
            "neither half states a trade count, so the thin-sample rule could "
            "not be applied — a ratio over an unknown number of trades is weaker "
            "evidence than it looks"
        )

    # -- the ratios, compared as written ------------------------------------
    for key in keys or PERIOD_INDEPENDENT_KEYS:
        left = _as_float(back.metrics.get(key))
        right = _as_float(forward.metrics.get(key))
        if left is None and right is None:
            continue
        row: Dict[str, Any] = {"metric": key, "back": left, "forward": right}
        if left is not None and right is not None:
            row["delta"] = round(right - left, 6)
            if left:
                worse = (
                    (right - left) if key in LOWER_IS_BETTER_KEYS else (left - right)
                )
                row["degradation_pct"] = round(worse / abs(left) * 100.0, 2)
        check.rows.append(row)

    # -- sign flips: the clearest failure there is ---------------------------
    flipped: set = set()
    for key in ("profit_factor", "expected_payoff", "net_profit"):
        left = _as_float(back.metrics.get(key))
        right = _as_float(forward.metrics.get(key))
        if left is None or right is None:
            continue
        floor = 1.0 if key == "profit_factor" else 0.0
        if left > floor >= right:
            degraded = True
            flipped.add(key)
            check.reasons.append(
                f"{key.replace('_', ' ')} fell from {left} to {right}: the back "
                "half cleared the bar and the forward half did not"
            )

    # -- degradation past the threshold --------------------------------------
    for row in check.rows:
        if row["metric"] not in FORWARD_GATE_KEYS or row["metric"] in flipped:
            continue
        pct = row.get("degradation_pct")
        if pct is not None and pct > max_degradation_pct:
            degraded = True
            check.reasons.append(
                f"{row['metric']} degraded {pct:g}% ({row['back']} -> "
                f"{row['forward']}), over the {max_degradation_pct:g}% this check "
                "allows"
            )

    # -- money and counts, normalized for the different period lengths -------
    if check.back_days and check.forward_days:
        per_day: Dict[str, Dict[str, Optional[float]]] = {}
        for key in PER_DAY_KEYS:
            left = _as_float(back.metrics.get(key))
            right = _as_float(forward.metrics.get(key))
            entry: Dict[str, Optional[float]] = {}
            if left is not None:
                entry["back"] = round(left / check.back_days, 4)
            if right is not None:
                entry["forward"] = round(right / check.forward_days, 4)
            # Fewer trades per day is activity, not failure, and a metric only
            # one half reports gets no degradation figure at all: turning a
            # missing number into "fell 100%" would be inventing a result.
            if (
                key != "total_trades"
                and key not in flipped
                and entry.get("back")
                and entry.get("forward") is not None
            ):
                entry["degradation_pct"] = round(
                    (entry["back"] - (entry["forward"] or 0.0))
                    / abs(entry["back"])
                    * 100.0,
                    2,
                )
                if entry["degradation_pct"] > max_degradation_pct:
                    degraded = True
                    check.reasons.append(
                        f"{key.replace('_', ' ')} per day fell "
                        f"{entry['degradation_pct']:g}% ({entry['back']} -> "
                        f"{entry['forward']}) once the different period lengths "
                        "are accounted for"
                    )
            if entry:
                per_day[key] = entry
        check.per_day = per_day
        if check.forward_days > check.back_days:
            check.warnings.append(
                f"the forward half ({check.forward_days:.0f} days) is longer than "
                f"the back half ({check.back_days:.0f} days); MT5 splits the other "
                "way round by default, so check ForwardMode and ForwardDate"
            )
    else:
        check.warnings.append(
            "the two reports do not both carry from/to dates, so money and trade "
            "counts are not normalized for period length — compare the ratios, "
            "not the profit"
        )

    # -- context that makes the forward half harder, not wrong ---------------
    back_dd = _as_float(back.metrics.get("equity_drawdown_relative_pct"))
    forward_dd = _as_float(forward.metrics.get("equity_drawdown_relative_pct"))
    if back_dd is None:
        back_dd = _as_float(back.metrics.get("equity_drawdown_pct"))
    if forward_dd is None:
        forward_dd = _as_float(forward.metrics.get("equity_drawdown_pct"))
    if back_dd and forward_dd and forward_dd > back_dd * 1.5 and forward_dd > 10:
        check.warnings.append(
            f"the forward drawdown ({forward_dd:g}%) is {forward_dd / back_dd:.1f}x "
            f"the back one ({back_dd:g}%)"
        )
    for label, report in (("back", back), ("forward", forward)):
        quality = _as_float(report.metrics.get("history_quality_pct"))
        if quality is not None and quality < 90:
            check.warnings.append(
                f"{label} half history quality is {quality:g}% — gaps in the tick "
                "or bar history make that curve look better than the data deserves"
            )
    if not check.rows:
        inconclusive = True
        check.reasons.append(
            "the two reports carry no metric in common — check that both are "
            "testing reports for the same EA"
        )

    if degraded:
        check.verdict = "degrades"
    elif inconclusive:
        check.verdict = "inconclusive"
    else:
        check.verdict = "holds_up"
        gated = ", ".join(FORWARD_GATE_KEYS)
        check.reasons.append(
            f"no sign flip and no degradation past {max_degradation_pct:g}% on "
            f"{gated}: the parameters did not break on data the search never saw. "
            "One split is evidence, not proof — another period, symbol or spread "
            "can still break them, and a drawdown that grew is listed as a "
            "warning rather than a verdict."
        )
    return check


def format_forward_for_prompt(check: ForwardCheck, max_lines: int = 30) -> str:
    """A compact, model-facing forward verdict."""
    lines: List[str] = [f"forward check: {check.verdict}"]
    if check.available:
        span = ""
        if check.back_days and check.forward_days:
            span = f" ({check.back_days:.0f}d back / {check.forward_days:.0f}d forward)"
        lines.append(
            f"back: {check.back_source} | forward: {check.forward_source}{span}"
        )
        for row in check.rows:
            pct = row.get("degradation_pct")
            tail = ""
            if pct is not None:
                tail = f" ({pct:g}% worse)" if pct > 0 else f" ({-pct:g}% better)"
            lines.append(f"{row['metric']}: {row['back']} -> {row['forward']}{tail}")
        for key, entry in check.per_day.items():
            if entry.get("back") is None or entry.get("forward") is None:
                continue
            pct = entry.get("degradation_pct")
            tail = ""
            if pct is not None:
                # A negative degradation is an improvement; saying "worse" there
                # would invert the finding.
                tail = f" ({pct:g}% worse)" if pct > 0 else f" ({-pct:g}% better)"
            lines.append(f"{key}/day: {entry['back']} -> {entry['forward']}{tail}")
    if check.reasons:
        lines.append("reasons:")
        lines.extend(f"  - {reason}" for reason in check.reasons)
    if check.warnings:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in check.warnings)
    return "\n".join(lines[:max_lines])


# ---------------------------------------------------------------------------
# Optimization reports
# ---------------------------------------------------------------------------

# An optimization run does not produce a testing report; it produces an XML
# *table*. One <Table>, a first <Row> that names the columns, then one <Row>
# per pass. MT5 spells those tags with capital letters (<Row>, <Cell>) where
# the testing report uses HTML (<tr>, <td>), and it saves the file in ANSI
# rather than UTF-16. The ten columns below are always present, in this order;
# every column after "Trades" is an optimized input, named by whoever wrote the
# EA. Forward-optimization files add "Back Result" and "Forward Result".
DEFAULT_OPT_COLUMNS: Tuple[str, ...] = (
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
)

# Header cell (normalized) -> canonical metric key. A header cell that does not
# map is an input name, which is how the parameter columns are recognized.
OPT_COLUMNS: Dict[str, str] = {
    "pass": "pass",
    "result": "result",
    "profit": "profit",
    "expected payoff": "expected_payoff",
    "profit factor": "profit_factor",
    "recovery factor": "recovery_factor",
    "sharpe ratio": "sharpe_ratio",
    "custom": "custom",
    "custom criterion": "custom",
    "ontester": "custom",
    "equity dd %": "equity_drawdown_pct",
    "equity dd": "equity_drawdown_pct",
    "drawdown %": "equity_drawdown_pct",
    "trades": "trades",
    "total trades": "trades",
    "deals": "deals",
    "back result": "back_result",
    "forward result": "forward_result",
}

# ``rank_by`` names, mapped to (metric key, descending?). "drawdown" sorts
# ascending because a small drawdown is the good one.
RANK_KEYS: Dict[str, Tuple[str, bool]] = {
    "result": ("result", True),
    "profit": ("profit", True),
    "payoff": ("expected_payoff", True),
    "expected_payoff": ("expected_payoff", True),
    "profit_factor": ("profit_factor", True),
    "recovery_factor": ("recovery_factor", True),
    "sharpe": ("sharpe_ratio", True),
    "sharpe_ratio": ("sharpe_ratio", True),
    "custom": ("custom", True),
    "drawdown": ("equity_drawdown_pct", False),
    "equity_drawdown_pct": ("equity_drawdown_pct", False),
    "trades": ("trades", True),
    "back_result": ("back_result", True),
    "forward_result": ("forward_result", True),
}

# The filters MT5 itself offers in the Optimization Results tab ("hide
# unsuccessful passes"): passes without trades, loss-making passes, drawdown
# above 50%, recovery factor below 1, Sharpe ratio below 0.5. They are the
# platform's own opinion of what a pass has to clear before it is worth
# reading, so they are the defaults here too.
PASS_FILTER_DEFAULTS: Dict[str, Optional[float]] = {
    "min_trades": 1.0,
    "min_profit": 0.0,
    "max_drawdown_pct": 50.0,
    "min_recovery_factor": 1.0,
    "min_sharpe_ratio": 0.5,
}


@dataclass
class OptimizationPass:
    """One row of an optimization report: a pass and the inputs it ran with.

    ``metrics`` holds the ten documented columns (plus ``back_result`` and
    ``forward_result`` for forward files), ``inputs`` holds the optimized
    parameter values keyed by the EA's own input names. A value MT5 did not
    write as a number — a bool, an enum, a string input — is kept as text.
    """

    number: Optional[int] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    inputs: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.metrics.get(key, default)

    def number_or_index(self, index: int) -> int:
        return self.number if self.number is not None else index

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"pass": self.number, "metrics": self.metrics}
        if self.inputs:
            payload["inputs"] = self.inputs
        if self.extra:
            payload["extra"] = self.extra
        return payload


@dataclass
class OptimizationResult:
    """A parsed optimization report.

    ``passes`` is every row the file contained, in file order (which for a
    genetic optimization is *not* best-first — rank them). ``parameter_names``
    are the optimized inputs, in column order.
    """

    source: str = ""
    format: str = "xml"
    columns: List[str] = field(default_factory=list)
    parameter_names: List[str] = field(default_factory=list)
    passes: List[OptimizationPass] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    forward: bool = False
    run: Dict[str, Any] = field(default_factory=dict)
    parsed_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    )

    def to_dict(self, *, include_passes: int = 0) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "source": self.source,
            "format": self.format,
            "passes": len(self.passes),
            "parameter_names": self.parameter_names,
            "columns": self.columns,
            "forward": self.forward,
            "warnings": self.warnings,
        }
        if include_passes:
            payload["rows"] = [item.to_dict() for item in self.passes[:include_passes]]
        if self.run:
            payload["run"] = self.run
        return payload


def _xml_unescape(text: str) -> str:
    """Undo the five entities XML allows, in the order that is safe."""
    for entity, character in (
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&apos;", "'"),
        ("&amp;", "&"),
    ):
        text = text.replace(entity, character)
    return text


def table_rows(text: str) -> Tuple[List[List[str]], str]:
    """Split an optimization report into rows of cell text.

    MT5 uses ``<Table>/<Row>/<Cell>`` for the optimization XML and HTML
    ``<table>/<tr>/<td>`` for the testing report. Both are accepted, because
    people paste one where the other belongs and because a spreadsheet export
    of the results tab is HTML.
    """
    row_re = re.compile(r"<Row\b[^>]*>(.*?)</Row\s*>", re.IGNORECASE | re.DOTALL)
    cell_re = re.compile(r"<Cell\b[^>]*>(.*?)</Cell\s*>", re.IGNORECASE | re.DOTALL)
    style = "xml"
    raw_rows = row_re.findall(text)
    if not raw_rows:
        row_re = re.compile(r"<tr\b[^>]*>(.*?)</tr\s*>", re.IGNORECASE | re.DOTALL)
        cell_re = re.compile(
            r"<t[dh]\b[^>]*>(.*?)</t[dh]\s*>", re.IGNORECASE | re.DOTALL
        )
        style = "html"
        raw_rows = row_re.findall(text)
    rows: List[List[str]] = []
    for raw_row in raw_rows:
        cells = [
            _xml_unescape(normalize_text(re.sub(r"<[^>]+>", " ", cell)))
            for cell in cell_re.findall(raw_row)
        ]
        rows.append(cells)
    return rows, style


def _opt_column_key(header_cell: str) -> Optional[str]:
    """Canonical metric key for a header cell, or None when it is an input."""
    text = normalize_text(header_cell)
    if not text:
        return None
    if text == "#":
        return "pass"
    return OPT_COLUMNS.get(_normalize_label(text))


def _opt_value(raw: str) -> Any:
    """A cell as a number when MT5 wrote a number, otherwise as text.

    Integers stay integers: an input MT5 reported as ``1000`` is an int input,
    and printing it back as ``1000.0`` would put a decimal point into the .set
    file that reproduces the pass.
    """
    text = normalize_text(raw)
    if not text:
        return None
    number = normalize_number(text)
    if number is None:
        return text
    if number.is_integer() and "." not in text and "," not in text:
        return int(number)
    return number


def parse_optimization_text(text: str, source: str = "") -> OptimizationResult:
    """Parse an optimization report table into passes.

    The header row is located by content, not by position: it is the first row
    that names a ``Pass`` column and at least one result column. A file with no
    recognizable header falls back to the documented column order and says so
    in ``warnings``, so a truncated export still yields the metrics rather than
    nothing.
    """
    result = OptimizationResult(source=source)
    rows, style = table_rows(text)
    result.format = style
    if not rows:
        result.warnings.append(
            "no <Row>/<Cell> table found; this does not look like an "
            "optimization report (a testing report has <table>/<tr>/<td> and "
            "one set of metrics, not one row per pass)"
        )
        return result

    header_index: Optional[int] = None
    for index, row in enumerate(rows):
        keys = {_opt_column_key(cell) for cell in row}
        if "pass" in keys and keys & {
            "result",
            "profit",
            "back_result",
            "forward_result",
        }:
            header_index = index
            break

    if header_index is None and style == "html":
        # An HTML table with no Pass/Result header is a *testing* report (or a
        # spreadsheet of something else). Guessing the documented column order
        # here would turn its labels into numbers, so say so instead.
        result.warnings.append(
            "no <Row>/<Cell> table and no Pass/Result header row; this does "
            "not look like an optimization report (a testing report has one "
            "set of metrics, not one row per pass)"
        )
        return result

    if header_index is None:
        header = list(DEFAULT_OPT_COLUMNS)
        data_rows = [row for row in rows if any(cell for cell in row)]
        result.warnings.append(
            "no header row found; assumed the documented column order "
            "(" + ", ".join(DEFAULT_OPT_COLUMNS) + ", then the inputs)"
        )
    else:
        header = [normalize_text(cell) for cell in rows[header_index]]
        data_rows = [
            row for row in rows[header_index + 1 :] if any(cell for cell in row)
        ]

    result.columns = header
    kinds: List[Optional[str]] = [_opt_column_key(cell) for cell in header]
    result.parameter_names = [
        header[index]
        for index, key in enumerate(kinds)
        if key is None and header[index]
    ]
    duplicates = {
        name
        for name in result.parameter_names
        if result.parameter_names.count(name) > 1
    }
    if duplicates:
        result.warnings.append(
            "the header names these input columns more than once: "
            + ", ".join(sorted(duplicates))
        )

    short_rows = 0
    wide_rows = 0
    for row in data_rows:
        item = OptimizationPass()
        if len(row) < len(header):
            short_rows += 1
        if len(row) > len(header):
            wide_rows += 1
        for index, cell in enumerate(row):
            if index >= len(header):
                item.extra[f"column_{index + 1}"] = normalize_text(cell)
                continue
            key = kinds[index]
            name = header[index]
            if key == "pass":
                number = normalize_number(cell)
                item.number = int(number) if number is not None else None
                continue
            if key:
                item.metrics[key] = _opt_value(cell)
                continue
            if name:
                item.inputs[name] = _opt_value(cell)
        if item.number is None and not item.metrics and not item.inputs:
            continue
        result.passes.append(item)

    if short_rows:
        result.warnings.append(
            f"{short_rows} row(s) had fewer cells than the "
            f"{len(header)} header columns; the trailing columns are missing "
            "for those passes rather than zero"
        )
    if wide_rows:
        result.warnings.append(
            f"{wide_rows} row(s) had more cells than the header; the extra "
            "values are kept under `extra`"
        )
    if not result.passes:
        result.warnings.append("the table had a header but no passes")

    metric_keys = {key for item in result.passes for key in item.metrics}
    result.forward = bool(metric_keys & {"back_result", "forward_result"})
    if result.forward and "forward_result" not in metric_keys:
        result.forward = False
    return result


def is_optimization_text(text: str) -> bool:
    """True when a file's contents look like an optimization table."""
    head = text[:20000].lower()
    return "<row" in head and "<cell" in head


def parse_optimization(path: Path | str) -> OptimizationResult:
    """Read an optimization report file (usually the ``.xml`` MT5 wrote)."""
    report_path = Path(path).expanduser()
    if not report_path.is_file():
        raise FileNotFoundError(f"optimization report not found: {report_path}")
    text = decode_report_bytes(report_path.read_bytes())
    return parse_optimization_text(text, str(report_path))


def parse_any_report(path: Path | str) -> Any:
    """Parse a file as an optimization table or a testing report, by content.

    Both are saved where ``Report=`` points, both can be ``.xml``, and asking
    an agent to know which is which before reading it is how it ends up
    reporting the first pass of an optimization as if it were a backtest.
    """
    report_path = Path(path).expanduser()
    if not report_path.is_file():
        raise FileNotFoundError(f"report not found: {report_path}")
    text = decode_report_bytes(report_path.read_bytes())
    if is_optimization_text(text):
        return parse_optimization_text(text, str(report_path))
    return parse_report(report_path)


def _median(values: Sequence[float]) -> Optional[float]:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _stats(values: Sequence[Any]) -> Dict[str, Optional[float]]:
    """count/best/median/worst over the numeric members of ``values``."""
    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    if not numbers:
        return {"count": 0, "best": None, "median": None, "worst": None}
    return {
        "count": len(numbers),
        "best": max(numbers),
        "median": _median(numbers),
        "worst": min(numbers),
    }


def _spearman(left: Sequence[float], right: Sequence[float]) -> Optional[float]:
    """Rank correlation of two equally long series, or None when too short."""
    if len(left) != len(right) or len(left) < 3:
        return None

    def ranks(values: Sequence[float]) -> List[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        index = 0
        while index < len(order):
            tie_end = index
            while (
                tie_end + 1 < len(order)
                and values[order[tie_end + 1]] == values[order[index]]
            ):
                tie_end += 1
            average = (index + tie_end) / 2.0 + 1.0
            for position in range(index, tie_end + 1):
                out[order[position]] = average
            index = tie_end + 1
        return out

    left_ranks = ranks(left)
    right_ranks = ranks(right)
    squared = sum((a - b) ** 2 for a, b in zip(left_ranks, right_ranks))
    count = len(left)
    denominator = count * (count * count - 1)
    if not denominator:
        return None
    return 1.0 - (6.0 * squared) / denominator


@dataclass
class PassFilter:
    """Which passes are worth reading at all.

    Defaults are MT5's own "hide unsuccessful passes" filters. A rule only
    fires on a value the report actually contains: a pass with no
    ``recovery_factor`` cell is not dropped for having a bad one.
    """

    min_trades: Optional[float] = 1.0
    min_profit: Optional[float] = 0.0
    max_drawdown_pct: Optional[float] = 50.0
    min_recovery_factor: Optional[float] = 1.0
    min_sharpe_ratio: Optional[float] = 0.5

    @classmethod
    def off(cls) -> "PassFilter":
        return cls(None, None, None, None, None)

    def reasons(self, item: OptimizationPass) -> List[str]:
        """Why this pass is hidden, as one phrase per rule it broke."""
        out: List[str] = []
        trades = item.metrics.get("trades")
        if self.min_trades is not None and isinstance(trades, (int, float)):
            if trades < self.min_trades:
                out.append(f"only {trades:g} trades")
        profit = item.metrics.get("profit")
        if self.min_profit is not None and isinstance(profit, (int, float)):
            if profit <= self.min_profit:
                out.append(f"profit {profit:g}")
        drawdown = item.metrics.get("equity_drawdown_pct")
        if self.max_drawdown_pct is not None and isinstance(drawdown, (int, float)):
            if drawdown > self.max_drawdown_pct:
                out.append(f"equity drawdown {drawdown:g}%")
        recovery = item.metrics.get("recovery_factor")
        if self.min_recovery_factor is not None and isinstance(recovery, (int, float)):
            if recovery < self.min_recovery_factor:
                out.append(f"recovery factor {recovery:g}")
        sharpe = item.metrics.get("sharpe_ratio")
        if self.min_sharpe_ratio is not None and isinstance(sharpe, (int, float)):
            if sharpe < self.min_sharpe_ratio:
                out.append(f"sharpe ratio {sharpe:g}")
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_trades": self.min_trades,
            "min_profit": self.min_profit,
            "max_drawdown_pct": self.max_drawdown_pct,
            "min_recovery_factor": self.min_recovery_factor,
            "min_sharpe_ratio": self.min_sharpe_ratio,
        }


def filter_passes(
    passes: Sequence[OptimizationPass],
    pass_filter: Optional[PassFilter] = None,
) -> Dict[str, Any]:
    """Split passes into the readable ones and the ones MT5 would hide."""
    active = PassFilter() if pass_filter is None else pass_filter
    kept: List[OptimizationPass] = []
    dropped: List[Dict[str, Any]] = []
    for index, item in enumerate(passes):
        reasons = active.reasons(item)
        if reasons:
            dropped.append({"pass": item.number_or_index(index), "reasons": reasons})
        else:
            kept.append(item)
    return {
        "rules": active.to_dict(),
        "kept": kept,
        "dropped": dropped,
        "kept_count": len(kept),
        "dropped_count": len(dropped),
    }


def _resolve_rank_key(rank_by: str) -> Tuple[str, bool]:
    """``rank_by`` as (metric key, bigger-is-better).

    Friendly names come from ``RANK_KEYS``; anything else is normalized into a
    metric key, so ``"Equity DD %"`` and ``"equity_drawdown_pct"`` both work
    and a column this module has never seen can still be ranked by its name.
    """
    key = normalize_text(rank_by) or "result"
    resolved = RANK_KEYS.get(_normalize_label(key))
    if resolved is None:
        return _normalize_label(key).replace(" ", "_"), True
    return resolved


def rank_passes(
    passes: Sequence[OptimizationPass],
    rank_by: str = "result",
    *,
    top: Optional[int] = None,
    descending: Optional[bool] = None,
) -> List[OptimizationPass]:
    """Sort passes by a metric, best first.

    ``rank_by`` accepts the friendly names in ``RANK_KEYS`` or any metric key
    the report actually has. Passes without that metric sort last rather than
    being treated as zero — a missing recovery factor is not a recovery factor
    of 0.
    """
    metric, best_first = _resolve_rank_key(rank_by)
    if descending is not None:
        best_first = descending

    def sort_key(pair: Tuple[int, OptimizationPass]) -> Tuple[int, float, int]:
        index, item = pair
        value = item.metrics.get(metric)
        if not isinstance(value, (int, float)):
            return (1, 0.0, index)  # missing sorts after every real number
        return (0, -float(value) if best_first else float(value), index)

    ordered = [item for _, item in sorted(enumerate(passes), key=sort_key)]
    if top is not None and top >= 0:
        return ordered[:top]
    return ordered


def analyze_optimization(
    result: OptimizationResult,
    *,
    rank_by: str = "result",
    top: int = 10,
    use_filter: bool = True,
    pass_filter: Optional[PassFilter] = None,
    set_file: Optional["SetFile"] = None,
    min_pass_trades: float = 30.0,
    luck_gap: float = 3.0,
) -> Dict[str, Any]:
    """Rank the passes and say what the ranking is worth.

    The point is not to find the biggest number — MT5 already sorts by that.
    The point is the part an agent gets wrong: a single pass that beat
    everything else is usually luck, an input pinned to the edge of its
    optimization range means the real optimum was never tested, and a ranking
    that does not survive forward testing is a ranking of noise. Each of those
    is checked here and reported as a sentence in ``warnings``.
    """
    passes = result.passes
    metric_key, best_first = _resolve_rank_key(rank_by)
    filtered = filter_passes(passes, pass_filter if use_filter else PassFilter.off())
    kept: List[OptimizationPass] = filtered["kept"]
    population = kept or passes
    ranked_all = rank_passes(population, rank_by)
    ranked = ranked_all[: max(0, top)]

    warnings: List[str] = list(result.warnings)
    notes: List[str] = []

    statistics: Dict[str, Dict[str, Optional[float]]] = {}
    for metric in (
        "result",
        "profit",
        "profit_factor",
        "recovery_factor",
        "sharpe_ratio",
        "expected_payoff",
        "equity_drawdown_pct",
        "trades",
    ):
        stats = _stats([item.metrics.get(metric) for item in population])
        if stats["count"]:
            statistics[metric] = stats

    best = ranked[0] if ranked else None

    if len(passes) < 20:
        warnings.append(
            f"only {len(passes)} pass(es) in this report: too few to say "
            "anything about robustness, and a genetic optimization this small "
            "has barely searched the space"
        )

    if best is not None:
        trades = best.metrics.get("trades")
        if isinstance(trades, (int, float)) and trades < min_pass_trades:
            warnings.append(
                f"the best pass traded {trades:g} time(s) — every ratio in that "
                f"row (profit factor, Sharpe, recovery) is noise below about "
                f"{min_pass_trades:g} trades"
            )

    # A spike where the winner is many times the median of its neighbours is
    # the classic signature of a lucky pass rather than a robust region. Only
    # meaningful when bigger is better: a tiny drawdown is not a "spike".
    if best_first and len(ranked_all) >= 5:
        window = ranked_all[: max(5, min(len(ranked_all), 20))]
        values = [
            float(item.metrics[metric_key])
            for item in window
            if isinstance(item.metrics.get(metric_key), (int, float))
        ]
        if len(values) >= 5:
            median_value = _median(values[1:]) or 0.0
            best_value = values[0]
            if median_value > 0 and best_value > luck_gap * median_value:
                warnings.append(
                    f"the top pass ({best_value:g}) is "
                    f"{best_value / median_value:.1f}x the median of the next "
                    f"{len(values) - 1} passes — a spike like that is usually a "
                    "lucky run, not an edge; prefer a plateau"
                )

    # How many passes come close to the best? A robust setting has neighbours
    # that also work; an overfit one is alone at the top.
    plateau: Dict[str, Any] = {
        "checked": False,
        "why": (
            "fewer than 20 passes to compare"
            if len(population) < 20
            else "the ranking metric is a minimum, so 'near the best' is not "
            "meaningful here"
        ),
    }
    if best_first and len(population) >= 20 and best is not None:
        best_value = best.metrics.get(metric_key)
        if isinstance(best_value, (int, float)) and best_value > 0:
            near = [
                item
                for item in population
                if isinstance(item.metrics.get(metric_key), (int, float))
                and abs(float(item.metrics[metric_key]) - float(best_value))
                <= 0.1 * abs(float(best_value))
            ]
            plateau = {
                "checked": True,
                "near_best": len(near),
                "share": round(len(near) / max(1, len(population)), 4),
                "within_pct": 10.0,
                "metric": metric_key,
                "population": len(population),
            }
            if len(near) <= max(1, 0.02 * len(population)):
                warnings.append(
                    f"only {len(near)} of {len(population)} passes land within "
                    f"10% of the best {metric_key} — the optimum is a spike, so "
                    "a small change in any input (or in the market) loses it"
                )

    # Inputs: which values the winners agree on, and whether the winners sit at
    # the edge of the range that was tested (which means the range was wrong).
    inputs_summary: Dict[str, Dict[str, Any]] = {}
    for name in result.parameter_names:
        top_values = [item.inputs.get(name) for item in ranked if name in item.inputs]
        numeric_top = [
            float(value) for value in top_values if isinstance(value, (int, float))
        ]
        all_values = [item.inputs.get(name) for item in passes if name in item.inputs]
        distinct_top = sorted({str(value) for value in top_values if value is not None})
        entry: Dict[str, Any] = {
            "distinct_in_top": len(distinct_top),
            "top_values": distinct_top[:8],
            "distinct_overall": len({str(value) for value in all_values}),
            "agreed_value": (distinct_top[0] if len(distinct_top) == 1 else None),
        }
        if len(distinct_top) == 1 and entry["distinct_overall"] > 1:
            notes.append(
                f"every top pass uses {name}={distinct_top[0]}; the other "
                f"values of {name} never made the top"
            )
        if set_file is not None and numeric_top:
            template = set_file.get(name)
            if template is not None:
                start, _, stop = template.range_numbers
                if start is not None and stop is not None and start != stop:
                    at_start = sum(
                        1 for value in numeric_top if abs(value - start) <= 1e-9
                    )
                    at_stop = sum(
                        1 for value in numeric_top if abs(value - stop) <= 1e-9
                    )
                    entry["range"] = [start, stop]
                    entry["at_range_start"] = at_start
                    entry["at_range_stop"] = at_stop
                    share = max(at_start, at_stop) / len(numeric_top)
                    if share >= 0.8:
                        edge = "start" if at_start >= at_stop else "stop"
                        value = start if edge == "start" else stop
                        warnings.append(
                            f"{name} sits at the {edge} of its tested range "
                            f"({value:g}) in {share:.0%} of the top passes — the "
                            "real optimum is probably outside the range; widen "
                            "it and re-run instead of trusting this pass"
                        )
                    if template.step_count is not None:
                        entry["steps"] = template.step_count
        inputs_summary[name] = entry

    # Forward optimization is the one test that actually measures overfitting:
    # the terminal re-runs the best in-sample passes on data they were not
    # optimized against.
    forward: Optional[Dict[str, Any]] = None
    if result.forward:
        paired = [
            item
            for item in passes
            if isinstance(item.metrics.get("back_result"), (int, float))
            and isinstance(item.metrics.get("forward_result"), (int, float))
        ]
        if paired:
            backs = [float(item.metrics["back_result"]) for item in paired]
            forwards = [float(item.metrics["forward_result"]) for item in paired]
            rho = _spearman(backs, forwards)
            median_back = _median(backs)
            median_forward = _median(forwards)
            forward = {
                "passes": len(paired),
                "median_back_result": median_back,
                "median_forward_result": median_forward,
                "spearman_back_vs_forward": (
                    round(rho, 4) if rho is not None else None
                ),
            }
            if median_back is not None and median_forward is not None:
                if median_back > 0 and median_forward < median_back:
                    loss = (median_back - median_forward) / abs(median_back)
                    forward["median_degradation_pct"] = round(loss * 100.0, 2)
                    if loss >= 0.5:
                        warnings.append(
                            f"out of sample the median result falls from "
                            f"{median_back:g} to {median_forward:g} "
                            f"({loss:.0%} worse) — the in-sample ranking does "
                            "not hold on data it was not fitted to"
                        )
            if rho is not None and len(paired) >= 10 and rho < 0.3:
                warnings.append(
                    f"back-test and forward ranks barely agree (Spearman "
                    f"rho={rho:.2f} over {len(paired)} passes): picking the "
                    "best in-sample pass is close to picking at random"
                )
            if best is not None and isinstance(
                best.metrics.get("forward_result"), (int, float)
            ):
                forward_rank = 1 + sum(
                    1
                    for item in paired
                    if float(item.metrics["forward_result"])
                    > float(best.metrics["forward_result"])
                )
                forward["best_pass_forward_rank"] = forward_rank
                if forward_rank > max(3, 0.1 * len(paired)):
                    warnings.append(
                        f"pass {best.number} ranked first in sample but "
                        f"{forward_rank} of {len(paired)} out of sample — do not "
                        "trade it on the strength of this report"
                    )

    # Optimizing one criterion and then judging by another is how a great
    # "Result" column hides a pass that a risk-adjusted metric would reject.
    criterion_note: Optional[Dict[str, Any]] = None
    if best is not None:
        alternatives: Dict[str, Any] = {}
        for label in ("recovery_factor", "sharpe_ratio", "profit_factor"):
            if label == metric_key:
                continue
            alternative = rank_passes(population, label, top=1)
            if not alternative:
                continue
            candidate = alternative[0]
            if candidate is not best and candidate.number != best.number:
                alternatives[label] = {
                    "pass": candidate.number,
                    "value": candidate.metrics.get(label),
                    "result": candidate.metrics.get("result"),
                }
        if alternatives:
            criterion_note = {
                "ranked_by": metric_key,
                "best_by_other_criteria": alternatives,
            }
            first_label = next(iter(alternatives))
            other = alternatives[first_label]
            notes.append(
                f"ranked by {metric_key} the winner is pass {best.number}; "
                f"ranked by {first_label} it is pass {other['pass']} "
                f"({other['value']}) — check the EA against the criterion you "
                "actually care about before re-testing"
            )

    grid = set_file.total_combinations() if set_file is not None else None
    if grid is not None and grid > 100000:
        notes.append(
            f"the .set grid has about {grid:,} combinations: fine for the "
            "genetic algorithm (Optimization=2), hopeless for the slow complete "
            "one (Optimization=1)"
        )

    if filtered["dropped_count"]:
        notes.append(
            f"{filtered['dropped_count']} of {len(passes)} passes were hidden "
            "by the filters MT5 itself offers (no trades, no profit, drawdown "
            "over 50%, recovery factor under 1, Sharpe under 0.5); pass "
            "--no-filter to see them"
        )

    return {
        "rank_by": metric_key,
        "passes": len(passes),
        "kept": filtered["kept_count"],
        "dropped": filtered["dropped_count"],
        "filter_rules": filtered["rules"],
        "dropped_examples": filtered["dropped"][:5],
        "top": [item.to_dict() for item in ranked],
        "best": best.to_dict() if best is not None else None,
        "statistics": statistics,
        "plateau": plateau,
        "inputs": inputs_summary,
        "forward": forward,
        "criterion": criterion_note,
        "grid_combinations": grid,
        "parameter_names": result.parameter_names,
        "warnings": warnings,
        "notes": notes,
    }


def format_optimization_for_prompt(
    result: OptimizationResult,
    analysis: Dict[str, Any],
    max_lines: int = 40,
) -> str:
    """A compact block an agent can read instead of the whole table."""
    lines: List[str] = []
    source = result.source or "optimization report"
    lines.append(f"optimization: {source}")
    lines.append(
        f"passes: {analysis['passes']} total, {analysis['kept']} after "
        f"filters, ranked by {analysis['rank_by']}"
    )
    if result.parameter_names:
        lines.append("inputs optimized: " + ", ".join(result.parameter_names))
    for position, item in enumerate(analysis["top"][:10], start=1):
        metrics = item["metrics"]
        parts = [f"pass {item['pass']}"]
        for key in (
            "result",
            "profit",
            "profit_factor",
            "recovery_factor",
            "sharpe_ratio",
            "equity_drawdown_pct",
            "trades",
        ):
            value = metrics.get(key)
            if isinstance(value, (int, float)):
                parts.append(f"{key}={value:g}")
        lines.append(f"{position:>2}. " + " ".join(parts))
        if item.get("inputs"):
            inputs = ", ".join(
                f"{name}={value}" for name, value in list(item["inputs"].items())[:8]
            )
            lines.append(f"      inputs: {inputs}")
    if analysis.get("forward"):
        forward = analysis["forward"]
        lines.append(
            "forward: median back "
            f"{forward.get('median_back_result')} vs forward "
            f"{forward.get('median_forward_result')}"
            + (
                f" (rho={forward['spearman_back_vs_forward']})"
                if forward.get("spearman_back_vs_forward") is not None
                else ""
            )
        )
    if analysis["warnings"]:
        lines.append("warnings:")
        for warning in analysis["warnings"]:
            lines.append(f"  - {warning}")
    if analysis["notes"]:
        lines.append("notes:")
        for note in analysis["notes"]:
            lines.append(f"  - {note}")
    return "\n".join(lines[:max_lines])


# ---------------------------------------------------------------------------
# .set input files
# ---------------------------------------------------------------------------

# A .set file is a line per EA input:
#
#     InpLots=0.1||0.01||0.01||1.0||Y
#     InpFastEMA=12||0||0||0||N
#     InpUseFilter=true||false||0||true||N
#
# that is value||start||step||stop||optimize. MT5 resolves the name given in
# the ini's ExpertParameters against MQL5\Profiles\Tester (where it also keeps
# the last inputs used for each EA), and without such a file it falls back to
# the EA's compiled defaults and *cannot optimize at all*.
SET_FIELD_SEPARATOR = "||"
# MT5 writes a bool input as true/false. MT4 wrote 1/0, which cannot be told
# apart from an integer input, so only true/false count as bools here: guessing
# would turn InpMagic=1 into a two-value optimization range.
SET_BOOLS = ("true", "false")


@dataclass
class SetInput:
    """One line of a ``.set`` file."""

    name: str
    value: str = ""
    start: str = ""
    step: str = ""
    stop: str = ""
    optimize: bool = False
    raw: str = ""

    @property
    def is_bool(self) -> bool:
        return self.value.strip().lower() in SET_BOOLS

    @property
    def bool_value(self) -> Optional[bool]:
        text = self.value.strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
        return None

    @property
    def value_number(self) -> Optional[float]:
        if self.is_bool:
            return None
        return normalize_number(self.value)

    @property
    def range_numbers(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        return (
            normalize_number(self.start),
            normalize_number(self.step),
            normalize_number(self.stop),
        )

    @property
    def step_count(self) -> Optional[int]:
        """How many values the optimizer will try for this input."""
        if not self.optimize:
            return 1
        if self.is_bool:
            return 2
        start, step, stop = self.range_numbers
        if start is None or stop is None:
            return None
        if step is None or step <= 0:
            return 1 if start == stop else None
        if stop < start:
            return None
        return int((stop - start) / step) + 1

    def is_edge_value(self, number: Optional[float]) -> Optional[bool]:
        """True when ``number`` is the first or last value of the range."""
        if number is None or not self.optimize:
            return None
        start, _, stop = self.range_numbers
        if start is None or stop is None or start == stop:
            return None
        return abs(number - start) <= 1e-9 or abs(number - stop) <= 1e-9

    def to_line(self) -> str:
        """Render as MT5's ``value||start||step||stop||flag`` form."""
        if self.is_bool:
            value = "true" if self.bool_value else "false"
            start = self.start.strip() or "false"
            step = self.step.strip() or "0"
            stop = self.stop.strip() or "true"
        else:
            value = self.value.strip()
            start = self.start.strip() or "0"
            step = self.step.strip() or "0"
            stop = self.stop.strip() or "0"
        flag = "Y" if self.optimize else "N"
        fields = (value, start, step, stop, flag)
        return self.name + "=" + SET_FIELD_SEPARATOR.join(fields)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "optimize": self.optimize,
        }
        if self.optimize:
            payload["start"] = self.start
            payload["step"] = self.step
            payload["stop"] = self.stop
            payload["steps"] = self.step_count
        return payload


@dataclass
class SetFile:
    """A parsed ``.set`` file, writable again without losing its shape."""

    path: str = ""
    inputs: List[SetInput] = field(default_factory=list)
    header: List[str] = field(default_factory=list)
    unknown: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def get(self, name: str) -> Optional[SetInput]:
        wanted = normalize_text(name)
        for item in self.inputs:
            if item.name == wanted:
                return item
        return None

    def names(self) -> List[str]:
        return [item.name for item in self.inputs]

    def optimized(self) -> List[SetInput]:
        return [item for item in self.inputs if item.optimize]

    def total_combinations(self) -> Optional[int]:
        """Size of the grid the optimizer has to walk."""
        optimized = self.optimized()
        if not optimized:
            return None
        total = 1
        for item in optimized:
            count = item.step_count
            if count is None:
                return None
            total *= max(1, count)
        return total

    def to_text(self) -> str:
        lines = list(self.header)
        lines.extend(item.to_line() for item in self.inputs)
        lines.extend(self.unknown)
        return "\n".join(lines).rstrip("\n") + "\n"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "inputs": [item.to_dict() for item in self.inputs],
            "optimized": [item.name for item in self.optimized()],
            "grid_combinations": self.total_combinations(),
            "unknown_lines": self.unknown,
            "warnings": self.warnings,
        }


def parse_set_text(text: str, path: str = "") -> SetFile:
    """Parse a ``.set`` file.

    MT5's own form is ``name=value||start||step||stop||Y``; a plain
    ``name=value`` line is a fixed input, and MT4-shaped
    ``name=value,flag,start,step,stop`` rows are read too because plenty of
    .set files in the wild were converted rather than regenerated. Lines that
    match none of those are kept verbatim in ``unknown`` — a .set is often
    round-tripped, and dropping a line nobody recognizes would change what the
    terminal loads.
    """
    out = SetFile(path=path)
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(";") or line.startswith("#"):
            if not out.inputs:
                out.header.append(raw_line.rstrip())
            continue
        if "=" in line:
            name, _, rest = line.partition("=")
            name = name.strip()
            rest = rest.strip()
            if not name:
                out.warnings.append(f"line {lineno}: no input name before '='")
                continue
            if SET_FIELD_SEPARATOR in rest:
                parts = [part.strip() for part in rest.split(SET_FIELD_SEPARATOR)]
                value = parts[0]
                start = parts[1] if len(parts) > 1 else ""
                step = parts[2] if len(parts) > 2 else ""
                stop = parts[3] if len(parts) > 3 else ""
                flag = parts[4] if len(parts) > 4 else ""
                optimize = flag.strip().upper().startswith("Y")
                if len(parts) > 5:
                    out.warnings.append(
                        f"line {lineno}: {len(parts) - 5} extra field(s) after "
                        "the optimize flag were ignored"
                    )
            elif rest.count(",") >= 4:
                parts = [part.strip() for part in rest.split(",")]
                value = parts[0]
                flag = parts[1]
                start, step, stop = parts[2], parts[3], parts[4]
                optimize = flag.upper() in ("Y", "1", "T", "TRUE")
                out.warnings.append(
                    f"line {lineno}: read as an MT4-shaped row "
                    "(value,flag,start,step,stop); it is written back in MT5 "
                    "form"
                )
            else:
                value, start, step, stop, optimize = rest, "", "", "", False
            existing = out.get(name)
            if existing is not None:
                existing.value = value
                existing.start = start
                existing.step = step
                existing.stop = stop
                existing.optimize = optimize
                existing.raw = raw_line.rstrip()
            else:
                out.inputs.append(
                    SetInput(
                        name=name,
                        value=value,
                        start=start,
                        step=step,
                        stop=stop,
                        optimize=optimize,
                        raw=raw_line.rstrip(),
                    )
                )
            continue
        fields = [part.strip() for part in line.split(",")]
        if len(fields) >= 5:
            # MT4 also writes the range on its own line: name,flag,start,step,stop
            name = fields[0]
            # Anything but an explicit yes stays fixed: guessing "optimize
            # this" is how a .set turns into a million-pass run nobody asked
            # for.
            optimize = fields[1].upper() in ("Y", "1", "T", "TRUE")
            existing = out.get(name)
            if existing is not None:
                existing.optimize = optimize
                existing.start = fields[2]
                existing.step = fields[3]
                existing.stop = fields[4]
                continue
        out.unknown.append(raw_line.rstrip())
        out.warnings.append(f"line {lineno}: not an input line, kept as-is")
    if not out.inputs:
        out.warnings.append("no inputs found in this .set file")
    return out


def parse_set_file(path: Path | str) -> SetFile:
    """Read a ``.set`` file from disk, decoding whatever MT5 wrote it as."""
    set_path = Path(path).expanduser()
    if not set_path.is_file():
        raise FileNotFoundError(f".set file not found: {set_path}")
    text = decode_report_bytes(set_path.read_bytes())
    return parse_set_text(text, str(set_path))


def write_set_file(set_file: SetFile, path: Optional[Path | str] = None) -> str:
    """Render a ``.set`` (and write it when ``path`` is given)."""
    text = set_file.to_text()
    if path is not None:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        # MT5 reads these as ANSI; ASCII-safe content is identical in both.
        target.write_text(text, encoding="utf-8", newline="\r\n")
    return text


def split_inputs_string(text: Any) -> List[Tuple[str, str]]:
    """Split a report's ``Inputs`` cell into name/value pairs.

    MT5 writes them comma-separated (``InpLots=0.1,InpFastEMA=12``); exports
    and pasted logs also turn up with semicolons or one pair per line, so all
    three separators are accepted.
    """
    if text is None:
        return []
    # Not normalize_text(): it collapses the newlines that separate pairs in a
    # pasted Inputs cell, which would glue every value to the next name.
    raw = str(text).replace(_NBSP, " ").replace(_THIN_SPACE, " ")
    raw = html_lib.unescape(raw)
    if not raw.strip():
        return []
    pieces = re.split(r"[,;\n\r]+", raw)
    pairs: List[Tuple[str, str]] = []
    for piece in pieces:
        item = normalize_text(piece)
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name = name.strip()
        if name:
            pairs.append((name, value.strip()))
    return pairs


def set_from_pairs(
    pairs: Sequence[Tuple[str, Any]],
    *,
    template: Optional[SetFile] = None,
    path: str = "",
) -> SetFile:
    """Build a ``.set`` from name/value pairs.

    With a ``template`` (usually the .set the optimization was run from) each
    input keeps its range and optimize flag and only the value changes — which
    is what makes the file usable for a follow-up optimization. Without one
    every input is written as fixed, which is what a single confirmation test
    wants.
    """
    out = SetFile(path=path)
    for name, value in pairs:
        key = normalize_text(name)
        if not key:
            continue
        text = value if isinstance(value, str) else _format_set_value(value)
        is_bool = text.strip().lower() in SET_BOOLS
        existing = template.get(key) if template is not None else None
        if existing is not None:
            out.inputs.append(
                SetInput(
                    name=key,
                    value=text,
                    start=existing.start,
                    step=existing.step,
                    stop=existing.stop,
                    optimize=existing.optimize,
                )
            )
            continue
        out.inputs.append(
            SetInput(
                name=key,
                value="true"
                if text.strip().lower() == "true"
                else "false"
                if is_bool
                else text,
                start="false" if is_bool else "0",
                step="0",
                stop="true" if is_bool else "0",
                optimize=False,
            )
        )
    return out


def _format_set_value(value: Any) -> str:
    """A value as MT5 wants to read it back from a .set file."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        # Not repr(): a tick-sized step like 1e-05 has to stay "0.00001".
        return f"{value:.10f}".rstrip("0").rstrip(".")
    return str(value)


def set_from_pass(
    item: OptimizationPass,
    *,
    template: Optional[SetFile] = None,
    path: str = "",
    keep_ranges: bool = False,
) -> SetFile:
    """A ``.set`` that reproduces one optimization pass.

    This is the step that turns a row of a table into something testable: MT5
    re-runs a single test from these values, and that test — not the pass — is
    the number to gate on.

    A pass row only lists the *optimized* inputs. The inputs the optimization
    held fixed are not in the report, and leaving them out would silently run
    the EA on its compiled defaults instead of what the pass actually used, so
    with a ``template`` (the .set the optimization was run from) they are
    carried over as well. Everything is written as fixed (``N``) unless
    ``keep_ranges`` is set, which keeps the template's ranges for a follow-up
    optimization around the winner.
    """
    pairs = dict(item.inputs)
    if template is None:
        set_file = set_from_pairs(list(pairs.items()), path=path)
        if not pairs:
            set_file.warnings.append(
                "this pass carries no input values, so the .set is empty; the "
                "report was probably saved without the parameter columns"
            )
        return set_file

    set_file = SetFile(path=path, header=list(template.header))
    seen: set[str] = set()
    for existing in template.inputs:
        if existing.name in pairs:
            seen.add(existing.name)
            value = _format_set_value(pairs[existing.name])
        else:
            value = existing.value
            set_file.warnings.append(
                f"{existing.name} is not in the optimization report; kept the "
                f"template value {value or '(empty)'}"
            )
        if keep_ranges:
            set_file.inputs.append(
                SetInput(
                    name=existing.name,
                    value=value,
                    start=existing.start,
                    step=existing.step,
                    stop=existing.stop,
                    optimize=existing.optimize,
                )
            )
        else:
            is_bool = value.strip().lower() in SET_BOOLS
            set_file.inputs.append(
                SetInput(
                    name=existing.name,
                    value=value,
                    start=("false" if is_bool else "0"),
                    step="0",
                    stop=("true" if is_bool else "0"),
                    optimize=False,
                )
            )
    for name, value in pairs.items():
        if name in seen:
            continue
        set_file.warnings.append(
            f"{name} came from the pass but is not in the template .set; added "
            "as a fixed input"
        )
    extra = set_from_pairs(
        [(name, value) for name, value in pairs.items() if name not in seen],
        path=path,
    )
    set_file.inputs.extend(extra.inputs)
    if not pairs:
        set_file.warnings.append(
            "this pass carries no input values, so only the template's own "
            "inputs were written"
        )
    return set_file


def set_from_report(
    report: TesterReport,
    *,
    template: Optional[SetFile] = None,
    path: str = "",
) -> SetFile:
    """A ``.set`` from the ``Inputs`` line of a testing report."""
    pairs = split_inputs_string(report.raw.get("Inputs", report.get("inputs", "")))
    set_file = set_from_pairs(pairs, template=template, path=path)
    if not pairs:
        set_file.warnings.append(
            "the report has no Inputs line, so there is nothing to reproduce"
        )
    return set_file


def tester_profiles_dir(data_dir: Path | str) -> Path:
    """``<data folder>\\MQL5\\Profiles\\Tester`` — where .set files live."""
    return Path(data_dir).expanduser() / "MQL5" / "Profiles" / "Tester"


def tester_ini_warnings(
    *,
    expert: str = "",
    expert_parameters: str = "",
    optimization: Optional[int] = None,
    model: Optional[int] = None,
    report: str = "",
    shutdown_terminal: bool = True,
    visual: bool = False,
) -> List[str]:
    """The ini mistakes that fail silently.

    Each of these leaves MT5 running something other than what was asked for,
    with no error on the command line: an optimization with no .set, a .set
    named by path when MT5 only looks in Profiles\\Tester, and a run whose
    report folder does not exist.
    """
    warnings: List[str] = []
    if optimization and optimization > 0 and not expert_parameters:
        warnings.append(
            f"Optimization={int(optimization)} without ExpertParameters: MT5 "
            f"falls back to MQL5\\Profiles\\Tester\\{Path(expert).name}.set, "
            "and if that file does not exist it uses the EA's compiled defaults "
            "and cannot optimize at all. Generate a .set first "
            "(--set-from-pass or write_set_file) and name it here."
        )
    if expert_parameters and re.search(r"[\\/]", expert_parameters):
        warnings.append(
            f"ExpertParameters='{expert_parameters}' contains a path separator: "
            "MT5 resolves that name inside MQL5\\Profiles\\Tester, so copy the "
            "file there and pass only its file name."
        )
    if expert_parameters and not expert_parameters.lower().endswith(".set"):
        warnings.append(
            f"ExpertParameters='{expert_parameters}' does not end in .set — "
            "that is the extension the terminal expects."
        )
    if report and re.search(r"[\\/]", report):
        warnings.append(
            f"Report='{report}' is relative to the platform installation "
            "directory, and MT5 does not create the folder: make sure it "
            "already exists or no report is written."
        )
    if model == 4:
        warnings.append(
            "Model=4 (every tick based on real ticks) replays recorded ticks: "
            "the first run on a symbol downloads them and takes far longer "
            "than the other models, and the terminal stays busy until it "
            "finishes."
        )
    if optimization and optimization > 0 and visual:
        warnings.append(
            "Visual=1 with optimization on slows the run down enormously and "
            "MT5 will not close the chart windows itself."
        )
    if not shutdown_terminal:
        warnings.append(
            "ShutdownTerminal=0 leaves terminal64.exe running after the report "
            "is written; a caller waiting on the process will wait forever, so "
            "wait on the report file instead."
        )
    return warnings


# ---------------------------------------------------------------------------
# Tester logs
# ---------------------------------------------------------------------------

_ERROR_LINE_RE = re.compile(r"\b(error|failed|failure|critical)\b", re.IGNORECASE)
_WARNING_LINE_RE = re.compile(r"\b(warning|warn)\b", re.IGNORECASE)


def scan_tester_log(path: Path | str, limit: int = 20) -> Dict[str, Any]:
    """Pull the EA's own complaints out of a tester or journal log."""
    log_path = Path(path).expanduser()
    if not log_path.is_file():
        raise FileNotFoundError(f"log not found: {log_path}")
    text = decode_report_bytes(log_path.read_bytes())
    errors: List[str] = []
    warnings: List[str] = []
    total = 0
    for line in text.splitlines():
        cleaned = normalize_text(line)
        if not cleaned:
            continue
        total += 1
        if _ERROR_LINE_RE.search(cleaned):
            errors.append(cleaned)
        elif _WARNING_LINE_RE.search(cleaned):
            warnings.append(cleaned)
    return {
        "source": str(log_path),
        "lines": total,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors[:limit],
        "warnings": warnings[:limit],
    }


def find_tester_logs(
    roots: Optional[Iterable[Path | str]] = None, limit: int = 5
) -> List[Path]:
    """Newest tester/journal logs under the data folders."""
    search_roots = (
        [Path(root).expanduser() for root in roots] if roots else candidate_data_dirs()
    )
    found: Dict[Path, float] = {}
    for root in search_roots:
        if not root.is_dir():
            continue
        for relative in ("Tester/logs", "MQL5/Logs", "logs"):
            folder = root / relative
            if not folder.is_dir():
                continue
            for path in folder.glob("*.log"):
                if path.is_file():
                    found[path.resolve()] = _mtime(path)
    ordered = sorted(found.items(), key=lambda item: item[1], reverse=True)
    return [path for path, _ in ordered[:limit]]


# ---------------------------------------------------------------------------
# Running the tester headlessly
# ---------------------------------------------------------------------------


def find_terminal(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate ``terminal64.exe``.

    The terminal lives next to ``metaeditor64.exe``, so the MetaEditor
    discovery ``metaeditor.py`` already does is the first place to look.
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.exists() else None

    for var in ("TERMINAL_PATH", "MT5_TERMINAL", "METATRADER_PATH"):
        value = os.environ.get(var)
        if value:
            candidate = Path(value).expanduser()
            if candidate.exists():
                return candidate

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from metaeditor import find_metaeditor

        editor = find_metaeditor()
        if editor is not None:
            for name in ("terminal64.exe", "terminal.exe"):
                sibling = editor.parent / name
                if sibling.exists():
                    return sibling
    except Exception as exc:  # pragma: no cover - sibling import is optional
        logger.debug("MetaEditor-based discovery failed: %s", exc)

    roots: List[Path] = []
    for base in (
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")),
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")),
        Path.home() / "Program Files",
    ):
        if base.exists():
            roots.append(base)
    for wine_root in sorted(Path.home().glob(".wine*")):
        roots.append(wine_root / "drive_c" / "Program Files")
        roots.append(wine_root / "drive_c" / "Program Files (x86)")

    for root in roots:
        try:
            for directory in sorted(root.iterdir()):
                if not directory.is_dir():
                    continue
                name = directory.name.lower()
                if not any(token in name for token in ("metatrader", "mt5", "mt 5")):
                    continue
                for candidate in directory.glob("terminal64.exe"):
                    return candidate
        except OSError:  # pragma: no cover - permission quirks
            continue

    from shutil import which

    on_path = which("terminal64.exe") or which("terminal64")
    return Path(on_path) if on_path else None


def build_tester_ini(
    *,
    expert: str,
    symbol: str = "",
    period: str = "",
    from_date: str = "",
    to_date: str = "",
    deposit: Optional[float] = None,
    currency: str = "",
    leverage: str = "",
    model: Optional[int] = None,
    execution_mode: Optional[int] = None,
    optimization: Optional[int] = None,
    optimization_criterion: Optional[int] = None,
    expert_parameters: str = "",
    report: str = "TesterReport",
    replace_report: bool = True,
    shutdown_terminal: bool = True,
    visual: bool = False,
    login: str = "",
    password: str = "",
    trade_server: str = "",
    use_local: Optional[int] = None,
    use_remote: Optional[int] = None,
    use_cloud: Optional[int] = None,
    forward_mode: Optional[int] = None,
    forward_date: str = "",
    profit_in_pips: Optional[int] = None,
) -> str:
    """Render a ``[Tester]`` config for ``terminal64.exe /config:<ini>``.

    Keys follow the documented MT5 start-up options: ``Model`` is 0 every tick,
    1 one-minute OHLC, 2 open prices only, 3 math calculations, 4 every tick
    based on real ticks; ``Optimization`` is 0 off, 1 slow complete, 2 fast
    genetic, 3 all Market Watch symbols. ``ShutdownTerminal=1`` is what makes
    the terminal exit when the run finishes — without it the process hangs
    around and a caller waiting on it waits forever.

    ``Expert`` is a path under ``MQL5/Experts`` (the ``.ex5`` extension is
    optional) and ``ExpertParameters`` is a ``.set`` file under
    ``MQL5/Profiles/Tester``.

    ``ForwardMode`` turns on forward testing — the optimizer re-runs its best
    passes on the part of the period it was not allowed to see, which is the
    only built-in check on overfitting. ``0`` disables it; the other values are
    the splits in the terminal's Forward drop-down, and ``ForwardDate`` sets a
    custom split date. The forward half is written to a second report whose
    name carries a ``.forward`` suffix.

    ``Report`` is relative to the platform *installation* directory and MT5
    does not create the folder it points into. An optimization writes ``.xml``
    (the pass table) where a single test writes ``.htm``; see
    ``tester_ini_warnings`` for the ways this config fails silently.
    """
    lines: List[str] = [
        "; Generated by examples/mql_companion/tester_report.py",
        f"; {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}",
        "[Tester]",
        f"Expert={expert}",
    ]
    for key, value in (
        ("ExpertParameters", expert_parameters),
        ("Symbol", symbol),
        ("Period", period),
        ("FromDate", from_date),
        ("ToDate", to_date),
        ("Currency", currency),
        ("Leverage", leverage),
        ("Report", report),
        ("ForwardDate", forward_date),
        ("Login", login),
        ("Password", password),
        ("Server", trade_server),
    ):
        if value:
            lines.append(f"{key}={value}")
    if deposit is not None:
        lines.append(f"Deposit={deposit:g}")
    for key, value in (
        ("Model", model),
        ("ExecutionMode", execution_mode),
        ("Optimization", optimization),
        ("OptimizationCriterion", optimization_criterion),
        ("ForwardMode", forward_mode),
        ("UseLocal", use_local),
        ("UseRemote", use_remote),
        ("UseCloud", use_cloud),
        ("ProfitInPips", profit_in_pips),
    ):
        if value is not None:
            lines.append(f"{key}={int(value)}")
    lines.append(f"Visual={1 if visual else 0}")
    lines.append(f"ReplaceReport={1 if replace_report else 0}")
    lines.append(f"ShutdownTerminal={1 if shutdown_terminal else 0}")
    return "\n".join(lines) + "\n"


def needs_wine(terminal: Path) -> bool:
    """True when the terminal is a Windows binary on a non-Windows host."""
    if sys.platform == "win32":
        return False
    return terminal.name.lower().endswith(".exe")


def tester_command(
    terminal: Path,
    ini_path: Path,
    *,
    portable: bool = False,
    wine: Optional[bool] = None,
) -> List[str]:
    """Build the launch command, adding ``wine`` off Windows when needed."""
    use_wine = needs_wine(terminal) if wine is None else wine
    command: List[str] = ["wine"] if use_wine else []
    command.append(str(terminal))
    if portable:
        command.append("/portable")
    command.append(f'/config:"{ini_path}"')
    return command


def report_candidates(report_path: Path) -> List[Path]:
    """Every name MT5 might give the report we asked for.

    ``Report=`` is written without an extension and the terminal appends one:
    ``.htm`` for a testing run, ``.xml`` for an optimization, and a second file
    with a ``.forward`` suffix for the forward half. Waiting for exactly one
    name is how a caller times out on a run that succeeded — an optimization
    asked for as ``TesterReport.htm`` still produces ``TesterReport.xml``.
    """
    stem = report_path.with_suffix("")
    ordered: List[Path] = [report_path]
    for suffix in (".xml", ".htm", ".html", ".forward.xml", ".forward.htm"):
        candidate = stem.with_suffix(suffix)
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _file_is_stable(path: Path, poll_interval: float) -> bool:
    """True when a file has stopped changing, so it is safe to parse."""
    wait = min(max(poll_interval, 0.01), 0.5)
    try:
        first = (path.stat().st_size, path.stat().st_mtime_ns)
    except OSError:
        return False
    time.sleep(wait)
    try:
        second = (path.stat().st_size, path.stat().st_mtime_ns)
    except OSError:  # pragma: no cover - removed underneath us
        return False
    return first == second


def _ini_value(ini_text: str, key: str) -> Optional[str]:
    """One ``Key=Value`` from a ``[Tester]`` ini, or None when it is not there."""
    match = re.search(
        rf"^\s*{re.escape(key)}\s*=\s*(.*)$", ini_text, re.MULTILINE | re.IGNORECASE
    )
    return match.group(1).strip() if match else None


def run_tester(
    *,
    terminal: Path,
    ini_text: str,
    report_path: Path,
    timeout: float = 1800.0,
    portable: bool = False,
    wine: Optional[bool] = None,
    poll_interval: float = 1.0,
    runner: Any = None,
    parser: Optional[Callable[[Path], Any]] = None,
    process_grace: float = 5.0,
    forward: Optional[bool] = None,
    forward_grace: float = 120.0,
) -> Dict[str, Any]:
    """Write the ini, launch the terminal, wait for the report, parse it.

    With ``ShutdownTerminal=1`` the terminal exits by itself when the run
    finishes, so the wait is on the report file's mtime advancing rather than
    on the process — the file is the ground truth either way. ``runner`` lets
    tests stand in for ``subprocess.Popen``.

    ``parser`` defaults to ``parse_report``; pass ``parse_optimization`` (or
    ``parse_any_report`` to let the file decide) for an optimization run, whose
    report is a table of passes rather than one set of metrics. Whatever comes
    back gets the run metadata attached under ``.run``.

    ``process_grace`` is how long to wait for the terminal to exit once the
    report is on disk and stable. The report is written *before* the process
    ends — ``ShutdownTerminal=1`` still takes a moment to close the terminal —
    so without this window a perfectly good run reports ``exit_code`` as
    ``None`` and the caller cannot tell a clean exit from a terminal that is
    still up.

    A forward run writes two files, and which one is "the report" matters: the
    back half is ``<name>.htm`` and the forward half is ``<name>.forward.htm``.
    ``forward`` says to collect both (``None``, the default, reads ``ForwardMode``
    out of ``ini_text`` and decides from that). The outcome then carries
    ``forward_path`` and ``forward_report`` next to ``report``, which is always
    the back half — waiting for "the newest file" would otherwise return the
    forward half as the result of the run. ``forward_grace`` bounds the extra
    wait; when nothing appears the outcome says so in ``forward_note`` rather
    than failing, because an optimization puts both halves in one table.
    """
    ini_path = report_path.with_suffix(".ini")
    ini_path.parent.mkdir(parents=True, exist_ok=True)
    ini_path.write_text(ini_text, encoding="utf-8")

    if forward is None:
        mode = _ini_value(ini_text, "ForwardMode")
        expect_forward = bool(mode) and mode != "0"
    else:
        expect_forward = bool(forward)

    candidates = report_candidates(report_path)
    started = datetime.now(tz=timezone.utc)
    before = {
        candidate: (_mtime(candidate) if candidate.exists() else 0.0)
        for candidate in candidates
    }
    command = tester_command(terminal, ini_path, portable=portable, wine=wine)
    logger.info("launching: %s", " ".join(command))

    def fresh(back_half_only: bool = False) -> Optional[Path]:
        """The candidate MT5 has just (re)written, newest first."""
        produced = [
            candidate
            for candidate in candidates
            if candidate.exists() and _mtime(candidate) > before[candidate]
        ]
        if back_half_only:
            produced = [item for item in produced if not is_forward_report(item)]
        if not produced:
            return None
        return max(produced, key=_mtime)

    launch = runner or subprocess.Popen
    process = launch(command)
    deadline = time.monotonic() + timeout
    written: Optional[Path] = None
    exit_code: Any = None
    while time.monotonic() < deadline:
        if hasattr(process, "poll"):
            exit_code = process.poll()
        candidate = fresh(back_half_only=expect_forward)
        if candidate is not None:
            # Writing a report is not atomic — an optimization with thousands of
            # passes takes seconds — and a half-written file parses as a report
            # with metrics missing, which reads as "the EA never produced them".
            # Wait for the size and mtime to stop moving.
            if _file_is_stable(candidate, poll_interval):
                written = candidate
                break
        elif exit_code is not None:
            # The terminal is gone and wrote nothing. One more beat for the
            # filesystem, then give up.
            time.sleep(min(poll_interval, 1.0))
            if fresh(back_half_only=expect_forward) is None:
                break
        time.sleep(poll_interval)

    if written is None:
        if hasattr(process, "kill"):
            try:
                process.kill()
            except OSError:  # pragma: no cover - already gone
                pass
        state = "still running" if exit_code is None else f"exit code {exit_code}"
        raise TimeoutError(
            f"no report at {report_path} (or its .htm/.xml/.forward variants) "
            f"within {timeout:.0f}s (terminal {state})"
        )

    poll = getattr(process, "poll", None)
    if callable(poll) and process_grace > 0:
        grace_deadline = time.monotonic() + process_grace
        while poll() is None and time.monotonic() < grace_deadline:
            time.sleep(min(poll_interval, 0.1))

    finished = datetime.now(tz=timezone.utc)
    exit_code = getattr(process, "returncode", exit_code)
    report = (parser or parse_report)(written)
    run_info = {
        "command": command,
        "ini_path": str(ini_path),
        "report_path": str(written),
        "requested_path": str(report_path),
        "exit_code": exit_code,
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "seconds": round((finished - started).total_seconds(), 2),
    }
    report.run = run_info
    if exit_code not in (0, None):
        report.warnings.append(
            f"the terminal exited with code {exit_code}; a nonzero exit usually "
            "means the EA or its inputs were rejected, so treat these numbers as "
            "a partial run"
        )

    outcome: Dict[str, Any] = dict(run_info, report=report)
    if not expect_forward:
        return outcome

    wanted = [item for item in candidates if is_forward_report(item)]
    forward_written: Optional[Path] = None
    forward_deadline = time.monotonic() + max(0.0, forward_grace)
    while time.monotonic() < forward_deadline:
        produced = [
            item for item in wanted if item.exists() and _mtime(item) > before[item]
        ]
        if produced:
            newest = max(produced, key=_mtime)
            if _file_is_stable(newest, poll_interval):
                forward_written = newest
                break
        elif poll is not None and callable(poll) and poll() is not None:
            # The terminal is gone: no forward file is coming.
            break
        time.sleep(poll_interval)

    if forward_written is None:
        outcome["forward_note"] = (
            f"ForwardMode was set but no .forward.* report appeared next to "
            f"{written.name} within {forward_grace:.0f}s. A single test writes "
            "one file, and an optimization puts both halves in one table as "
            "Back Result / Forward Result columns — check which run this was "
            "before concluding the forward half is missing."
        )
        return outcome

    forward_report = parse_any_report(forward_written)
    if hasattr(forward_report, "run"):
        forward_report.run = dict(run_info, report_path=str(forward_written))
    outcome["forward_path"] = str(forward_written)
    outcome["forward_report"] = forward_report
    return outcome


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _rules_from_options(**values: Optional[float]) -> Dict[str, Optional[float]]:
    return {key: value for key, value in values.items() if value is not None}


def _emit(payload: Any, json_out: Optional[Path], quiet: bool) -> None:
    text = json.dumps(payload, indent=2, default=str)
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(text + "\n", encoding="utf-8")
    if not quiet:
        click.echo(text)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument(
    "positional_reports",
    nargs=-1,
    required=False,
    metavar="[REPORTS]...",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--report",
    "report_paths",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Report file to parse (.htm/.html/.xml/.txt). Repeatable.",
)
@click.option(
    "--latest", is_flag=True, help="Parse the newest report in the data folders."
)
@click.option(
    "--search-dir",
    "search_dirs",
    multiple=True,
    type=click.Path(path_type=Path),
    help="Where to look with --latest. Repeatable.",
)
@click.option(
    "--log",
    "log_paths",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Tester/journal log to scan for EA errors.",
)
@click.option("--find-logs", is_flag=True, help="Scan the newest tester logs found.")
# --- optimization results ---------------------------------------------
@click.option(
    "--optimization-report",
    "optimization_paths",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optimization XML (the .xml an Optimization run writes). Repeatable.",
)
@click.option(
    "--top",
    type=int,
    default=10,
    show_default=True,
    help="How many passes to rank and show.",
)
@click.option(
    "--rank-by",
    default="result",
    show_default=True,
    help="result, profit, payoff, profit_factor, recovery_factor, sharpe, "
    "drawdown, trades, custom.",
)
@click.option(
    "--no-filter",
    is_flag=True,
    help="Also rank the passes MT5 hides: no trades, no profit, drawdown over "
    "50%, recovery factor under 1, Sharpe under 0.5.",
)
@click.option(
    "--no-analysis",
    is_flag=True,
    help="Rank the passes without the overfitting checks.",
)
# --- forward checks ----------------------------------------------------
@click.option(
    "--forward-report",
    "forward_report_paths",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="The forward half of a forward run (Report.forward.htm), one per "
    "back-half report. --run with --forward-mode collects it automatically.",
)
@click.option(
    "--min-forward-trades",
    default=30,
    show_default=True,
    type=int,
    help="Below this many trades in either half the forward check reports "
    "'inconclusive' instead of a verdict.",
)
@click.option(
    "--max-degradation-pct",
    default=50.0,
    show_default=True,
    type=float,
    help="How much profit factor, recovery factor or Sharpe may fall between "
    "the halves before the verdict is 'degrades'.",
)
# --- .set input files --------------------------------------------------
@click.option(
    "--set",
    "set_paths",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Read a .set file: values, optimization ranges, grid size. Its ranges "
    "are also used to spot inputs pinned at the edge of what was tested.",
)
@click.option(
    "--set-from-pass",
    "set_pass_number",
    type=int,
    default=None,
    help="Print a .set that reproduces this pass number.",
)
@click.option(
    "--write-set",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Where --set-from-pass writes the .set instead of printing it.",
)
@click.option(
    "--keep-ranges",
    is_flag=True,
    help="With --set-from-pass: keep the template's optimization ranges "
    "instead of fixing every input, for a follow-up run around the winner.",
)
@click.option(
    "--min-profit-factor",
    type=float,
    default=None,
    help="Gate: gross profit / gross loss must be at least this.",
)
@click.option(
    "--min-recovery-factor",
    type=float,
    default=None,
    help="Gate: net profit / balance drawdown must be at least this.",
)
@click.option(
    "--min-sharpe-ratio", type=float, default=None, help="Gate: Sharpe ratio floor."
)
@click.option(
    "--min-net-profit", type=float, default=None, help="Gate: net profit floor."
)
@click.option(
    "--min-trades",
    "min_total_trades",
    type=float,
    default=None,
    help="Gate: trade count floor — ratios on a handful of trades mean nothing.",
)
@click.option(
    "--min-history-quality-pct",
    type=float,
    default=None,
    help="Gate: history quality floor; below about 90% the data had gaps.",
)
@click.option(
    "--max-equity-drawdown-pct",
    "max_equity_drawdown_relative_pct",
    type=float,
    default=None,
    help="Cap on the worst equity drawdown percentage.",
)
@click.option(
    "--max-equity-drawdown",
    type=float,
    default=None,
    help="Cap on the worst equity drawdown in money.",
)
@click.option(
    "--max-balance-drawdown-pct",
    "max_balance_drawdown_relative_pct",
    type=float,
    default=None,
    help="Cap on the worst balance drawdown percentage.",
)
@click.option(
    "--json-out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the parsed result as JSON.",
)
@click.option("--prompt", is_flag=True, help="Include a compact summary for a prompt.")
@click.option("--quiet", is_flag=True, help="Do not print JSON to stdout.")
# --- running the tester -------------------------------------------------
@click.option("--run", is_flag=True, help="Run a backtest, then parse its report.")
@click.option("--terminal", default=None, help="Path to terminal64.exe.")
@click.option("--expert", default=None, help="EA to test (path under MQL5/Experts).")
@click.option(
    "--expert-parameters",
    default="",
    help=".set file in MQL5/Profiles/Tester.",
)
@click.option("--symbol", default="", help="Symbol to test.")
@click.option("--period", default="", help="Period, e.g. H1.")
@click.option("--from-date", default="", help="Start date, YYYY.MM.DD.")
@click.option("--to-date", default="", help="End date, YYYY.MM.DD.")
@click.option("--deposit", type=float, default=None, help="Initial deposit.")
@click.option("--currency", default="", help="Deposit currency.")
@click.option("--leverage", default="", help="Leverage, e.g. 1:100.")
@click.option(
    "--model",
    type=int,
    default=None,
    help="0 every tick, 1 M1 OHLC, 2 open prices, 3 math, 4 real ticks.",
)
@click.option(
    "--execution-mode",
    type=int,
    default=None,
    help="0 normal, -1 random delay, >0 delay in ms.",
)
@click.option(
    "--optimization",
    type=int,
    default=None,
    help="0 off, 1 slow complete, 2 fast genetic, 3 Market Watch symbols.",
)
@click.option(
    "--optimization-criterion",
    type=int,
    default=None,
    help="0 balance, 1 profit factor, 2 payoff, 3 drawdown, 4 recovery, "
    "5 Sharpe, 6 custom (OnTester).",
)
@click.option(
    "--out-report",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Where the tester should write its report.",
)
@click.option("--portable", is_flag=True, help="Pass /portable to the terminal.")
@click.option("--no-shutdown", is_flag=True, help="Leave the terminal open afterwards.")
@click.option(
    "--tester-timeout",
    type=float,
    default=1800.0,
    show_default=True,
    help="Seconds to wait for the report.",
)
@click.option(
    "--forward-mode",
    type=int,
    default=None,
    help="Forward (out-of-sample) testing: 0 off, otherwise the terminal's "
    "Forward split.",
)
@click.option(
    "--forward-date", default="", help="Custom forward split date, YYYY.MM.DD."
)
@click.option("--print-ini", is_flag=True, help="Print the generated ini and exit.")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging to stderr.")
def main(  # noqa: C901 - a CLI with one option per tester setting
    positional_reports: Tuple[Path, ...],
    report_paths: Tuple[Path, ...],
    latest: bool,
    search_dirs: Tuple[Path, ...],
    log_paths: Tuple[Path, ...],
    find_logs: bool,
    optimization_paths: Tuple[Path, ...],
    top: int,
    rank_by: str,
    no_filter: bool,
    no_analysis: bool,
    forward_report_paths: Tuple[Path, ...],
    min_forward_trades: int,
    max_degradation_pct: float,
    set_paths: Tuple[Path, ...],
    set_pass_number: Optional[int],
    write_set: Optional[Path],
    keep_ranges: bool,
    min_profit_factor: Optional[float],
    min_recovery_factor: Optional[float],
    min_sharpe_ratio: Optional[float],
    min_net_profit: Optional[float],
    min_total_trades: Optional[float],
    min_history_quality_pct: Optional[float],
    max_equity_drawdown_relative_pct: Optional[float],
    max_equity_drawdown: Optional[float],
    max_balance_drawdown_relative_pct: Optional[float],
    json_out: Optional[Path],
    prompt: bool,
    quiet: bool,
    run: bool,
    terminal: Optional[str],
    expert: Optional[str],
    expert_parameters: str,
    symbol: str,
    period: str,
    from_date: str,
    to_date: str,
    deposit: Optional[float],
    currency: str,
    leverage: str,
    model: Optional[int],
    execution_mode: Optional[int],
    optimization: Optional[int],
    optimization_criterion: Optional[int],
    out_report: Optional[Path],
    portable: bool,
    forward_mode: Optional[int],
    forward_date: str,
    no_shutdown: bool,
    tester_timeout: float,
    print_ini: bool,
    verbose: bool,
) -> None:
    """Parse MetaTrader 5 Strategy Tester reports.

    Pass one or more report files as arguments (or with --report); two or more
    are also compared side by side. Use --latest to take the newest report the
    terminal left behind, or --run to produce one first. An optimization
    report (the XML table of passes) is recognized by its contents and ranked
    instead, with the overfitting checks that a sorted table cannot show.

    A forward run writes a second report for the half the search never saw;
    --forward-report (or --run with --forward-mode) compares the two and says
    whether the parameters held up.
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    rules = _rules_from_options(
        min_profit_factor=min_profit_factor,
        min_recovery_factor=min_recovery_factor,
        min_sharpe_ratio=min_sharpe_ratio,
        min_net_profit=min_net_profit,
        min_total_trades=min_total_trades,
        min_history_quality_pct=min_history_quality_pct,
        max_equity_drawdown_relative_pct=max_equity_drawdown_relative_pct,
        max_equity_drawdown=max_equity_drawdown,
        max_balance_drawdown_relative_pct=max_balance_drawdown_relative_pct,
    )

    paths: List[Path] = (
        list(positional_reports) + list(report_paths) + list(optimization_paths)
    )
    reports: List[TesterReport] = []
    optimizations: List[OptimizationResult] = []
    forward_pairs: List[Tuple[TesterReport, TesterReport]] = []

    set_files: List[SetFile] = []
    for set_path in set_paths:
        try:
            set_files.append(parse_set_file(set_path))
        except (FileNotFoundError, OSError) as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
    # The first .set supplies the ranges the analysis needs to notice an input
    # pinned at the edge of what was optimized.
    set_template = set_files[0] if set_files else None

    if run:
        if not expert:
            click.echo("Error: --run needs --expert.", err=True)
            sys.exit(2)

        def render_ini(target: Path) -> str:
            """The [Tester] config for one report destination."""
            return build_tester_ini(
                expert=expert,
                symbol=symbol,
                period=period,
                from_date=from_date,
                to_date=to_date,
                deposit=deposit,
                currency=currency,
                leverage=leverage,
                model=model,
                execution_mode=execution_mode,
                optimization=optimization,
                optimization_criterion=optimization_criterion,
                expert_parameters=expert_parameters,
                forward_mode=forward_mode,
                forward_date=forward_date,
                report=str(target.with_suffix("")),
                shutdown_terminal=not no_shutdown,
            )

        if print_ini:
            # Inspecting the generated config needs no terminal installed.
            click.echo(render_ini(out_report or Path.cwd() / "TesterReport.xml"))
            return
        ini_warnings = tester_ini_warnings(
            expert=expert,
            expert_parameters=expert_parameters,
            optimization=optimization,
            model=model,
            report=str((out_report or Path.cwd() / "TesterReport.xml").with_suffix("")),
            shutdown_terminal=not no_shutdown,
        )
        for warning in ini_warnings:
            click.echo(f"  [ini] {warning}", err=True)
        terminal_path = find_terminal(terminal)
        if terminal_path is None:
            click.echo(
                "Error: terminal64.exe not found.\n"
                "  Windows:  install MetaTrader 5, or pass --terminal\n"
                "  Linux/macOS: pass --terminal ~/.wine/drive_c/.../terminal64.exe\n"
                "  Or set TERMINAL_PATH in the environment.",
                err=True,
            )
            sys.exit(2)
        target = out_report or Path.cwd() / "TesterReport.xml"
        ini_text = render_ini(target)
        click.echo(f"terminal: {terminal_path}", err=True)
        click.echo(f"report:   {target}", err=True)
        try:
            outcome = run_tester(
                terminal=terminal_path,
                ini_text=ini_text,
                report_path=target,
                timeout=tester_timeout,
                portable=portable,
                parser=parse_optimization if (optimization or 0) > 0 else None,
            )
        except (TimeoutError, FileNotFoundError, OSError) as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        # Keep the parsed report: re-reading the file would lose the run
        # metadata (command, exit code, duration) attached to it.
        produced = outcome["report"]
        if isinstance(produced, OptimizationResult):
            optimizations.append(produced)
        else:
            reports.append(produced)
        forward_produced = outcome.get("forward_report")
        if isinstance(produced, TesterReport) and isinstance(
            forward_produced, TesterReport
        ):
            forward_pairs.append((produced, forward_produced))
            click.echo(f"  [forward] {outcome.get('forward_path')}", err=True)
        elif forward_produced is not None:
            click.echo(
                "  [forward] the forward half is a table of optimization passes, "
                "not a testing report: its Back Result / Forward Result columns "
                "carry the comparison",
                err=True,
            )
        elif outcome.get("forward_note"):
            click.echo(f"  [forward] {outcome['forward_note']}", err=True)
        click.echo(
            f"tester finished in {outcome['seconds']}s "
            f"(exit code {outcome['exit_code']}, report {outcome['report_path']})",
            err=True,
        )

    if not reports and not paths and latest:
        found = find_reports(search_dirs or None, limit=1)
        if not found:
            click.echo(
                "Error: no report files found. Pass --report <file> or "
                "--search-dir <dir>.",
                err=True,
            )
            sys.exit(2)
        paths = found
        click.echo(f"newest report: {paths[0]}", err=True)

    if not reports and not optimizations and not paths and set_files:
        # --set on its own is a legitimate request: show what the .set holds.
        pass
    elif not reports and not optimizations and not paths and (set_pass_number is None):
        click.echo(
            "Error: nothing to parse. Pass REPORTS..., --report <file>, "
            "--optimization-report <file>, --latest, --set <file>, or "
            "--run --expert <EA>.",
            err=True,
        )
        sys.exit(2)

    for path in paths:
        try:
            parsed = parse_any_report(path)
        except (FileNotFoundError, OSError) as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        if isinstance(parsed, OptimizationResult):
            optimizations.append(parsed)
        else:
            reports.append(parsed)

    if forward_report_paths:
        forwards: List[Any] = []
        for forward_path in forward_report_paths:
            try:
                forwards.append(parse_any_report(forward_path))
            except (FileNotFoundError, OSError) as exc:
                click.echo(f"Error: {exc}", err=True)
                sys.exit(1)
        if any(isinstance(item, OptimizationResult) for item in forwards):
            click.echo(
                "Error: --forward-report was given an optimization table. A "
                "forward check compares two testing reports; the forward half of "
                "an optimization is inside the same table, as the Back Result and "
                "Forward Result columns.",
                err=True,
            )
            sys.exit(2)
        if len(forwards) != len(reports):
            click.echo(
                "Error: --forward-report takes one file per back-half report "
                f"({len(reports)} report(s) loaded, {len(forwards)} forward "
                "file(s) given).",
                err=True,
            )
            sys.exit(2)
        forward_pairs.extend(
            (back, item)
            for back, item in zip(reports, forwards)
            if isinstance(item, TesterReport)
        )

    logs: List[Dict[str, Any]] = []
    for log_path in log_paths:
        try:
            logs.append(scan_tester_log(log_path))
        except (FileNotFoundError, OSError) as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
    if find_logs:
        for log_path in find_tester_logs(limit=3):
            logs.append(scan_tester_log(log_path))

    threshold_failures = 0
    payload_single: Dict[str, Any] = {"reports": []}
    if len(reports) >= 2:
        payload_single["comparison"] = compare_reports(reports)
    for report in reports:
        entry: Dict[str, Any] = report.to_dict()
        if prompt:
            entry["summary"] = format_for_prompt(report)
        if rules:
            results = check_thresholds(report, rules)
            entry["thresholds"] = [
                {
                    "metric": result.name,
                    "rule": result.rule,
                    "actual": result.actual,
                    "passed": result.passed,
                    "message": result.message,
                }
                for result in results
            ]
            threshold_failures += sum(1 for result in results if not result.passed)
            if not quiet:
                for result in results:
                    mark = "ok  " if result.passed else "FAIL"
                    click.echo(f"  [{mark}] {result.message}", err=True)
        payload_single["reports"].append(entry)
    if forward_pairs:
        payload_single["forward_checks"] = []
        for back_report, forward_report in forward_pairs:
            check = check_forward(
                back_report,
                forward_report,
                min_trades=min_forward_trades,
                max_degradation_pct=max_degradation_pct,
            )
            forward_entry: Dict[str, Any] = check.to_dict()
            if prompt:
                forward_entry["summary"] = format_forward_for_prompt(check)
            if not quiet:
                click.echo(f"  [forward] verdict: {check.verdict}", err=True)
                for reason in check.reasons:
                    click.echo(f"  [forward] {reason}", err=True)
                for warning in check.warnings:
                    click.echo(f"  [warn] {warning}", err=True)
            payload_single["forward_checks"].append(forward_entry)
    if logs:
        payload_single["logs"] = logs
    if set_files:
        payload_single["sets"] = [item.to_dict() for item in set_files]
    if optimizations:
        payload_single["optimizations"] = []
        for item in optimizations:
            entry: Dict[str, Any] = item.to_dict()
            if no_analysis:
                entry["top"] = [
                    row.to_dict() for row in rank_passes(item.passes, rank_by, top=top)
                ]
            else:
                analysis = analyze_optimization(
                    item,
                    rank_by=rank_by,
                    top=top,
                    use_filter=not no_filter,
                    set_file=set_template,
                )
                entry["analysis"] = analysis
                if prompt:
                    entry["summary"] = format_optimization_for_prompt(item, analysis)
                if not quiet:
                    for warning in analysis["warnings"]:
                        click.echo(f"  [warn] {warning}", err=True)
            payload_single["optimizations"].append(entry)

    if set_pass_number is not None:
        if not optimizations:
            click.echo(
                "Error: --set-from-pass needs an optimization report "
                "(--optimization-report <file>, or --run --optimization N).",
                err=True,
            )
            sys.exit(2)
        wanted = None
        for item in optimizations:
            for row in item.passes:
                if row.number == set_pass_number:
                    wanted = row
                    break
            if wanted is not None:
                break
        if wanted is None:
            available = [
                str(row.number)
                for row in optimizations[0].passes[:10]
                if row.number is not None
            ]
            click.echo(
                f"Error: no pass {set_pass_number} in "
                f"{optimizations[0].source}. Pass numbers present (first 10): "
                + ", ".join(available),
                err=True,
            )
            sys.exit(2)
        generated = set_from_pass(
            wanted, template=set_template, keep_ranges=keep_ranges
        )
        if write_set:
            write_set_file(generated, write_set)
            click.echo(f"wrote {write_set}", err=True)
            payload_single["set_from_pass"] = {
                "pass": wanted.number,
                "path": str(write_set),
                "inputs": generated.to_dict()["inputs"],
            }
        else:
            # Piping a .set into MQL5/Profiles/Tester is the point, so the file
            # is the only thing on stdout.
            click.echo(generated.to_text(), nl=False)
            return

    _emit(payload_single, json_out, quiet)

    if threshold_failures:
        click.echo(f"{threshold_failures} threshold(s) not met.", err=True)
        sys.exit(1)


__all__ = [
    "DEFAULT_OPT_COLUMNS",
    "FORWARD_GATE_KEYS",
    "HEADLINE_KEYS",
    "LABELS",
    "OPTIMIZATION_CRITERIA",
    "OPTIMIZATION_MODES",
    "OPT_COLUMNS",
    "PAIR_SPLIT",
    "PASS_FILTER_DEFAULTS",
    "PER_DAY_KEYS",
    "PERIOD_INDEPENDENT_KEYS",
    "RANK_KEYS",
    "TESTER_MODELS",
    "ForwardCheck",
    "OptimizationPass",
    "OptimizationResult",
    "PassFilter",
    "SetFile",
    "SetInput",
    "TesterReport",
    "ThresholdResult",
    "analyze_optimization",
    "build_label_lookup",
    "build_tester_ini",
    "candidate_data_dirs",
    "check_forward",
    "check_thresholds",
    "compare_reports",
    "decode_report_bytes",
    "derive_and_check",
    "extract_date_range",
    "extract_period_code",
    "filter_passes",
    "find_reports",
    "find_terminal",
    "find_tester_logs",
    "format_for_prompt",
    "format_forward_for_prompt",
    "format_optimization_for_prompt",
    "forward_companion",
    "html_cells",
    "is_forward_report",
    "is_optimization_text",
    "needs_wine",
    "normalize_number",
    "normalize_text",
    "parse_any_report",
    "parse_html_report",
    "parse_optimization",
    "parse_optimization_text",
    "parse_report",
    "parse_set_file",
    "parse_set_text",
    "parse_text_report",
    "parse_xml_report",
    "period_days",
    "rank_passes",
    "report_candidates",
    "run_tester",
    "scan_tester_log",
    "set_from_pairs",
    "set_from_pass",
    "set_from_report",
    "split_inputs_string",
    "split_value",
    "table_rows",
    "tester_command",
    "tester_ini_warnings",
    "tester_profiles_dir",
    "write_set_file",
]


if __name__ == "__main__":
    main()
