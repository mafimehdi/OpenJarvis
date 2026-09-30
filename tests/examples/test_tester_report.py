"""Tests for examples/mql_companion/tester_report.py.

The Strategy Tester only runs on Windows (or under Wine), so these tests cover
everything that does not need a terminal: number normalization, the three
report formats, the identities MQL5 documents, report discovery, thresholds,
comparison, ``[Tester]`` ini generation, and the runner with a fake terminal
script that stands in for ``terminal64.exe``.

The parser is deliberately layout-independent, so several tests feed the same
numbers in different shapes — a two-column table, a single column of ``<br>``
lines, XML elements, XML attributes, tab-separated text — and assert the same
metrics come out. That is the property the whole tool rests on.

Optimization reports get the same treatment: the XML table of passes, the
ranking and the overfitting checks around it, and the ``.set`` input files that
tie a winning pass back to something the terminal can re-run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Tuple

import pytest
from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "examples" / "mql_companion" / "tester_report.py"


def _load_module() -> ModuleType:
    """Import the example script by path (the examples tree is not a package).

    Registering it in ``sys.modules`` before ``exec_module`` is required: the
    module uses ``from __future__ import annotations`` with ``@dataclass``, and
    dataclasses resolves annotations through sys.modules.
    """
    spec = importlib.util.spec_from_file_location("mql_tester_report", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tr = _load_module()


# ---------------------------------------------------------------------------
# Fixtures: one report's worth of numbers, in every shape the tester emits
# ---------------------------------------------------------------------------


def _row(*cells: str) -> str:
    """One report row — MT5 writes two label/value pairs per ``<tr>``."""
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>\n"


# English MT5 report: two pairs per row, non-breaking-space thousands
# separators, and the cells that carry two numbers at once.
_REPORT_ROWS: Tuple[Tuple[str, ...], ...] = (
    ("Expert Advisor", "MACD Sample.ex5", "Model", "every tick"),
    (
        "Symbol",
        "EURUSD (Euro vs US Dollar)",
        "Period",
        "1 Hour (H1)  2024.01.01 00:00 - 2024.06.30 23:59",
    ),
    ("Inputs", "InpLots=0.1;InpFastEMA=12", "History Quality", "100%"),
    ("Time", "0:00:12", "Total Bars", "2 611"),
    ("Ticks", "2 341 556", "Initial Deposit", "10 000.00 USD"),
    ("Leverage", "1:100", "Currency", "USD"),
    ("Total Trades", "243", "Total Deals", "486"),
    ("Total Net Profit", "1 234.56", "Gross Profit", "4 500.00"),
    ("Gross Loss", "-3 265.44", "Profit Factor", "1.38"),
    ("Expected Payoff", "5.08", "Recovery Factor", "1.75"),
    ("Sharpe Ratio", "0.86", "Minimum Margin Level", "1 540.22%"),
    (
        "Balance Drawdown Maximal",
        "705.00 (6.90%)",
        "Balance Drawdown Relative",
        "6.90%",
    ),
    (
        "Equity Drawdown Maximal",
        "812.44 (8.01%)",
        "Equity Drawdown Relative",
        "8.01%",
    ),
    (
        "Profit Trades (% of total)",
        "131 (53.91%)",
        "Loss Trades (% of total)",
        "112 (46.09%)",
    ),
    ("Short Trades (won %)", "120 (48.33%)", "Long Trades (won %)", "123 (59.35%)"),
    ("Largest profit trade", "145.20", "Largest loss trade", "-210.00"),
    ("Average profit trade", "34.35", "Average loss trade", "-29.16"),
    (
        "Maximum consecutive wins ($)",
        "7 (310.50)",
        "Maximum consecutive losses ($)",
        "5 (-412.00)",
    ),
    ("Average consecutive wins", "3", "Average consecutive losses", "2"),
)

HTML_REPORT = (
    "<html><head><title>MetaTrader 5 Strategy Tester Report</title></head>\n"
    '<body><div align="center">\n'
    '<table width="100%" cellspacing="0" cellpadding="4" border="0">\n'
    '<tr><td align="center" colspan="2"><b>'
    "EURUSD (EURUSD,H1)&nbsp;&nbsp;MACD Sample</b></td></tr>\n"
    + "".join(_row(*cells) for cells in _REPORT_ROWS)
    + "</table></div></body></html>\n"
)

# The same numbers, one label/value pair per line with <br> separators, to
# prove nothing depends on the table layout.
HTML_REPORT_BR = """<html><body>
Total Net Profit: 1234.56<br>Gross Profit: 4500.00<br>Gross Loss: -3265.44<br>
Profit Factor: 1.38<br>Sharpe Ratio: 0.86<br>Total Trades: 243<br>
Equity Drawdown Maximal: 812.44 (8.01%)<br>History Quality: 100%<br>
</body></html>
"""

XML_ELEMENTS = """<?xml version="1.0" encoding="UTF-8"?>
<Report>
  <Expert>MACD Sample</Expert><Symbol>EURUSD</Symbol><Period>H1</Period>
  <Model>every tick</Model><History_Quality>100%</History_Quality>
  <Total_Net_Profit>1 234.56</Total_Net_Profit>
  <Gross_Profit>4 500.00</Gross_Profit><Gross_Loss>-3 265.44</Gross_Loss>
  <Profit_Factor>1.38</Profit_Factor><Sharpe_Ratio>0.86</Sharpe_Ratio>
  <Total_Trades>243</Total_Trades><Total_Deals>486</Total_Deals>
  <Equity_Drawdown_Maximal>812.44 (8.01%)</Equity_Drawdown_Maximal>
  <Balance_Drawdown_Maximal>705.00 (6.90%)</Balance_Drawdown_Maximal>
</Report>
"""

XML_STAT_NAME_VALUE = """<?xml version="1.0"?>
<Results>
  <Stat name="STAT_PROFIT_FACTOR" value="1.42"/>
  <Stat name="STAT_PROFIT" value="900.50"/>
  <Stat name="STAT_BALANCE_DD" value="600"/>
  <Stat name="STAT_TRADES" value="120"/>
</Results>
"""

XML_ATTRIBUTES = """<?xml version="1.0"?>
<Optimization>
  <Result pass="1" profit_factor="1.55" net_profit="2100.25"
          equity_drawdown="300.50" total_trades="88"/>
</Optimization>
"""

TEXT_REPORT = """Total Net Profit\t1 234.56
Gross Profit: 4500.00
Gross Loss: -3265.44
Profit Factor  1.38
History Quality: 78%
Total Trades: 243
"""

# Every number in HTML_REPORT, so tests can assert equality instead of
# re-deriving expectations.
EXPECTED: Dict[str, Any] = {
    "expert": "MACD Sample.ex5",
    "symbol": "EURUSD (Euro vs US Dollar)",
    "period": "H1",
    "model": "every tick",
    "model_id": 0,
    "from_date": "2024.01.01",
    "to_date": "2024.06.30",
    "inputs": "InpLots=0.1;InpFastEMA=12",
    "history_quality_pct": 100.0,
    "bars": 2611,
    "ticks": 2341556,
    "initial_deposit": 10000.0,
    "leverage": "1:100",
    "currency": "USD",
    "total_trades": 243,
    "total_deals": 486,
    "net_profit": 1234.56,
    "gross_profit": 4500.0,
    "gross_loss": -3265.44,
    "profit_factor": 1.38,
    "expected_payoff": 5.08,
    "recovery_factor": 1.75,
    "sharpe_ratio": 0.86,
    "min_margin_level_pct": 1540.22,
    "balance_drawdown": 705.0,
    "balance_drawdown_pct": 6.9,
    "balance_drawdown_relative_pct": 6.9,
    "equity_drawdown": 812.44,
    "equity_drawdown_pct": 8.01,
    "equity_drawdown_relative_pct": 8.01,
    "profit_trades": 131,
    "profit_trades_pct": 53.91,
    "loss_trades": 112,
    "loss_trades_pct": 46.09,
    "short_trades": 120,
    "short_trades_pct": 48.33,
    "long_trades": 123,
    "long_trades_pct": 59.35,
    "max_profit_trade": 145.2,
    "max_loss_trade": -210.0,
    "avg_profit_trade": 34.35,
    "avg_loss_trade": -29.16,
    "max_conwins": 310.5,
    "max_conprofit_trades": 7,
    "max_conlosses": -412.0,
    "max_conloss_trades": 5,
    "avg_conwins": 3,
    "avg_conlosses": 2,
}


@pytest.fixture()
def report(tmp_path: Path) -> Path:
    path = tmp_path / "TesterReport.htm"
    path.write_text(HTML_REPORT, encoding="utf-8")
    return path


def _metrics(text: str, parser: str = "html") -> Dict[str, Any]:
    parsed = getattr(tr, f"parse_{parser}_report")(text)
    return parsed.metrics


# ---------------------------------------------------------------------------
# Number normalization — where a report reader quietly goes wrong
# ---------------------------------------------------------------------------


class TestNumberNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1 234.56", 1234.56),
            ("1\u00a0234.56", 1234.56),  # non-breaking space thousands
            ("1\u2009234.56", 1234.56),  # thin space
            ("-3 265.44", -3265.44),
            ("(765.44)", -765.44),  # accounting style
            ("1,234.56", 1234.56),
            ("1234,56", 1234.56),  # European decimal comma
            ("1.234.567", 1234567.0),
            ("45.2%", 45.2),
            ("USD 1 234.56", 1234.56),
            ("1 234.56 EUR", 1234.56),
            ("\u221212.5", -12.5),  # unicode minus
            ("0", 0.0),
            ("0.00", 0.0),
        ],
    )
    def test_reads_the_shapes_mt5_emits(self, raw: str, expected: float) -> None:
        assert tr.normalize_number(raw) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "raw",
        ["2024.01.01", "1:100", "MACD Sample.ex5", "H1", "M15", "every tick", "", None],
    )
    def test_refuses_things_that_are_not_numbers(self, raw: Any) -> None:
        """A date, a leverage ratio or a filename is not a quantity.

        ``MACD Sample.ex5`` used to parse as 0.5 once the letters were
        stripped — the kind of silent nonsense this module exists to prevent.
        """
        assert tr.normalize_number(raw) is None

    def test_text_keeps_its_text(self) -> None:
        assert tr.normalize_text("  1\u00a0234  ") == "1 234"
        assert tr.normalize_text("a &amp; b") == "a & b"

    def test_split_value_separates_money_from_percentage(self) -> None:
        parts = tr.split_value("812.44 (8.01%)")
        assert parts.primary == pytest.approx(812.44)
        assert parts.secondary == pytest.approx(8.01)
        assert parts.secondary_is_percent is True

    def test_split_value_handles_the_reversed_order(self) -> None:
        parts = tr.split_value("8.01% (812.44)")
        assert parts.primary == pytest.approx(8.01)
        assert parts.primary_is_percent is True
        assert parts.secondary == pytest.approx(812.44)

    def test_split_value_reports_when_there_is_no_pair(self) -> None:
        assert tr.split_value("1.38").has_pair is False


# ---------------------------------------------------------------------------
# HTML: the format the tester actually saves
# ---------------------------------------------------------------------------


class TestHtmlReport:
    def test_every_expected_metric_is_read(self) -> None:
        metrics = _metrics(HTML_REPORT)
        for key, expected in EXPECTED.items():
            assert metrics.get(key) == expected, key

    def test_nothing_headline_is_missing(self) -> None:
        parsed = tr.parse_html_report(HTML_REPORT)
        assert parsed.missing == []

    def test_a_consistent_report_produces_no_warnings(self) -> None:
        parsed = tr.parse_html_report(HTML_REPORT)
        assert parsed.warnings == []
        assert parsed.derived == [], "nothing needed deriving"

    def test_gross_loss_keeps_its_sign(self) -> None:
        """MT5 reports gross loss as a negative number; so must we."""
        assert _metrics(HTML_REPORT)["gross_loss"] < 0

    def test_the_four_drawdown_numbers_stay_distinct(self) -> None:
        """Maximal is money + its percentage; Relative is the worst percentage."""
        metrics = _metrics(HTML_REPORT)
        assert metrics["balance_drawdown"] == 705.0
        assert metrics["balance_drawdown_pct"] == 6.9
        assert metrics["balance_drawdown_relative_pct"] == 6.9
        assert metrics["equity_drawdown"] == 812.44
        assert metrics["equity_drawdown_pct"] == 8.01
        assert metrics["equity_drawdown_relative_pct"] == 8.01

    def test_count_and_percentage_come_out_of_one_cell(self) -> None:
        metrics = _metrics(HTML_REPORT)
        assert (metrics["profit_trades"], metrics["profit_trades_pct"]) == (131, 53.91)
        assert (metrics["loss_trades"], metrics["loss_trades_pct"]) == (112, 46.09)

    def test_a_streak_yields_both_the_money_and_the_count(self) -> None:
        metrics = _metrics(HTML_REPORT)
        assert metrics["max_conwins"] == 310.5, "the money of the winning streak"
        assert metrics["max_conprofit_trades"] == 7, "how many trades it took"
        assert metrics["max_conlosses"] == -412.0
        assert metrics["max_conloss_trades"] == 5

    def test_a_streak_whose_money_is_also_a_whole_number(self) -> None:
        """Both pieces look like a count, so the label's wording decides."""
        wins = _metrics(
            "<tr><td>Maximum consecutive wins ($)</td><td>7 (500)</td></tr>"
        )
        assert wins["max_conprofit_trades"] == 7
        assert wins["max_conwins"] == 500.0

        profit = _metrics(
            "<tr><td>Maximal consecutive profit</td><td>500 (7)</td></tr>"
        )
        assert profit["conprofit_max"] == 500.0
        assert profit["conprofit_max_trades"] == 7

    def test_a_decorated_label_still_matches(self) -> None:
        """Reports qualify labels in ways the vocabulary cannot enumerate."""
        metrics = _metrics(
            "<tr><td>Short Trades (won % of total)</td><td>120 (48.33%)</td></tr>"
        )
        assert metrics["short_trades"] == 120
        assert metrics["short_trades_pct"] == 48.33

    def test_the_qualified_drawdown_labels_do_not_trade_places(self) -> None:
        """Both strip to "balance drawdown maximal"; only one may win."""
        money = _metrics(
            "<tr><td>Balance Drawdown Maximal</td><td>705.00 (6.90%)</td></tr>"
        )
        assert money["balance_drawdown"] == 705.0
        percent = _metrics(
            "<tr><td>Balance Drawdown Maximal (%)</td><td>6.90%</td></tr>"
        )
        assert percent["balance_drawdown_pct"] == 6.9

    def test_the_period_cell_is_reduced_to_a_timeframe_code(self) -> None:
        metrics = _metrics(HTML_REPORT)
        assert metrics["period"] == "H1"
        assert metrics["period_text"].startswith("1 Hour (H1)")
        assert metrics["from_date"] == "2024.01.01"
        assert metrics["to_date"] == "2024.06.30"

    def test_the_model_is_mapped_to_its_ini_value(self) -> None:
        assert _metrics(HTML_REPORT)["model_id"] == 0, "every tick"
        assert (
            _metrics("<tr><td>Model</td><td>1 minute OHLC</td></tr>")["model_id"] == 1
        )

    def test_the_header_line_is_a_fallback_not_an_override(self) -> None:
        """`EURUSD (EURUSD,H1)  MACD Sample` fills gaps the labels did not."""
        header_only = "<table><tr><td>EURUSD (EURUSD,H1)  MACD Sample</td></tr></table>"
        metrics = _metrics(header_only)
        assert metrics["symbol"] == "EURUSD"
        assert metrics["period"] == "H1"
        assert metrics["expert"] == "MACD Sample"

        # ...but an explicit label wins over the header.
        both = _metrics(HTML_REPORT)
        assert both["expert"] == "MACD Sample.ex5"

    def test_the_layout_does_not_matter(self) -> None:
        """Same numbers in a <br>-separated single column read the same."""
        br = _metrics(HTML_REPORT_BR)
        table = _metrics(HTML_REPORT)
        for key in (
            "net_profit",
            "gross_profit",
            "gross_loss",
            "profit_factor",
            "sharpe_ratio",
            "total_trades",
            "equity_drawdown",
            "equity_drawdown_pct",
            "history_quality_pct",
        ):
            assert br[key] == table[key], key

    def test_unrecognized_labels_are_ignored_not_guessed(self) -> None:
        parsed = tr.parse_html_report(
            "<table><tr><td>Total Net Profit</td><td>10.00</td></tr>"
            "<tr><td>Some Future Metric</td><td>99</td></tr></table>"
        )
        assert parsed.metrics["net_profit"] == 10.0
        assert "99" not in json.dumps(parsed.metrics)

    def test_a_localized_report_degrades_to_missing_not_nonsense(self) -> None:
        parsed = tr.parse_html_report(
            "<table><tr><td>Общая чистая прибыль</td><td>1 234.56</td></tr></table>"
        )
        assert parsed.metrics == {}
        assert "net_profit" in parsed.missing


# ---------------------------------------------------------------------------
# XML and plain text
# ---------------------------------------------------------------------------


class TestXmlReport:
    def test_elements(self) -> None:
        metrics = _metrics(XML_ELEMENTS, "xml")
        assert metrics["net_profit"] == 1234.56
        assert metrics["profit_factor"] == 1.38
        assert metrics["equity_drawdown"] == 812.44
        assert metrics["equity_drawdown_pct"] == 8.01
        assert metrics["expert"] == "MACD Sample"

    def test_stat_name_value_pairs(self) -> None:
        """``<Stat name="STAT_PROFIT_FACTOR" value="1.42"/>`` is a real shape."""
        metrics = _metrics(XML_STAT_NAME_VALUE, "xml")
        assert metrics["profit_factor"] == 1.42
        assert metrics["net_profit"] == 900.5
        assert metrics["balance_drawdown"] == 600.0
        assert metrics["total_trades"] == 120

    def test_attributes(self) -> None:
        """Optimization exports put everything in attributes."""
        metrics = _metrics(XML_ATTRIBUTES, "xml")
        assert metrics["profit_factor"] == 1.55
        assert metrics["net_profit"] == 2100.25
        assert metrics["total_trades"] == 88

    def test_malformed_xml_warns_instead_of_raising(self) -> None:
        parsed = tr.parse_xml_report("<Report><unclosed>", "broken.xml")
        assert parsed.metrics == {}
        assert any("XML parse error" in w for w in parsed.warnings)

    def test_derivation_runs_on_xml_too(self) -> None:
        parsed = tr.parse_xml_report(XML_STAT_NAME_VALUE, "stats.xml")
        assert parsed.metrics["recovery_factor"] == pytest.approx(900.5 / 600.0)


class TestTextReport:
    def test_tabs_colons_and_double_spaces(self) -> None:
        metrics = _metrics(TEXT_REPORT, "text")
        assert metrics["net_profit"] == 1234.56
        assert metrics["gross_profit"] == 4500.0
        assert metrics["gross_loss"] == -3265.44
        assert metrics["total_trades"] == 243
        assert metrics["history_quality_pct"] == 78.0


class TestFileParsing:
    def test_dispatches_on_extension(self, report: Path) -> None:
        assert tr.parse_report(report).format == "html"

    def test_sniffs_xml_even_with_the_wrong_extension(self, tmp_path: Path) -> None:
        misnamed = tmp_path / "results.htm"
        misnamed.write_text(XML_ELEMENTS, encoding="utf-8")
        assert tr.parse_report(misnamed).format == "xml"

    def test_utf16le_files_are_decoded(self, tmp_path: Path) -> None:
        """MT5 writes UTF-16 for its logs and sometimes its reports."""
        path = tmp_path / "u16.htm"
        path.write_bytes(
            "<html><table><tr><td>Total Net Profit</td><td>777.50</td>"
            "</tr></table></html>".encode("utf-16-le")
        )
        parsed = tr.parse_report(path)
        assert parsed.metrics["net_profit"] == 777.5

    def test_utf16_with_bom(self, tmp_path: Path) -> None:
        path = tmp_path / "bom.htm"
        markup = (
            "<html><table><tr><td>Profit Factor</td><td>2.10</td></tr></table></html>"
        )
        path.write_bytes(markup.encode("utf-16"))
        assert tr.parse_report(path).metrics["profit_factor"] == 2.1

    def test_a_missing_file_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            tr.parse_report(tmp_path / "nope.htm")


# ---------------------------------------------------------------------------
# The identities MQL5 documents, and the misreads they catch
# ---------------------------------------------------------------------------


class TestDerivation:
    def test_net_profit_is_gross_profit_plus_a_negative_gross_loss(self) -> None:
        parsed = tr.parse_html_report(
            "<tr><td>Gross Profit</td><td>4500.00</td></tr>"
            "<tr><td>Gross Loss</td><td>-3265.44</td></tr>"
        )
        assert parsed.metrics["net_profit"] == pytest.approx(1234.56)
        assert any("net_profit" in note for note in parsed.derived)

    def test_profit_factor_divides_by_the_magnitude_of_the_loss(self) -> None:
        parsed = tr.parse_html_report(
            "<tr><td>Gross Profit</td><td>4500.00</td></tr>"
            "<tr><td>Gross Loss</td><td>-3265.44</td></tr>"
        )
        assert parsed.metrics["profit_factor"] == pytest.approx(4500 / 3265.44)

    def test_recovery_factor_uses_the_balance_drawdown(self) -> None:
        parsed = tr.parse_html_report(
            "<tr><td>Total Net Profit</td><td>900.00</td></tr>"
            "<tr><td>Balance Drawdown Maximal</td><td>600.00 (6%)</td></tr>"
        )
        assert parsed.metrics["recovery_factor"] == pytest.approx(1.5)

    def test_win_rate_is_derived_from_the_counts(self) -> None:
        parsed = tr.parse_html_report(
            "<tr><td>Profit Trades</td><td>131</td></tr>"
            "<tr><td>Loss Trades</td><td>112</td></tr>"
        )
        assert parsed.metrics["profit_trades_pct"] == pytest.approx(53.9098, abs=1e-3)
        assert parsed.metrics["total_trades"] == 243

    def test_a_zero_gross_loss_does_not_divide_by_zero(self) -> None:
        parsed = tr.parse_html_report(
            "<tr><td>Gross Profit</td><td>100.00</td></tr>"
            "<tr><td>Gross Loss</td><td>0.00</td></tr>"
        )
        assert "profit_factor" not in parsed.metrics
        assert parsed.metrics["net_profit"] == 100.0


class TestLabelVocabulary:
    def test_a_collision_is_refused_rather_than_resolved_silently(self) -> None:
        """Two aliases claiming one label would swap two metrics' values."""
        original = tr.LABELS["net_profit"]
        tr.LABELS["net_profit"] = tuple(original) + ("Profit Factor",)
        try:
            with pytest.raises(ValueError, match="collision"):
                tr.build_label_lookup()
        finally:
            tr.LABELS["net_profit"] = original
        assert tr.build_label_lookup() == tr._LOOKUP

    def test_stat_identifiers_are_accepted_as_labels(self) -> None:
        metrics = _metrics(
            "<tr><td>STAT_PROFIT_FACTOR</td><td>1.9</td>"
            "<td>STAT_BALANCE_DD</td><td>400</td></tr>"
        )
        assert metrics["profit_factor"] == 1.9
        assert metrics["balance_drawdown"] == 400.0

    def test_snake_case_keys_are_accepted_as_labels(self) -> None:
        metrics = _metrics("<tr><td>sharpe_ratio</td><td>1.25</td></tr>")
        assert metrics["sharpe_ratio"] == 1.25

    def test_every_headline_key_has_at_least_one_label(self) -> None:
        """A headline metric nobody can match would always show up missing."""
        for key in tr.HEADLINE_KEYS:
            if key.endswith("_pct") and key not in tr.LABELS:
                continue  # derived from its companion cell
            assert key in tr.LABELS or key in {
                secondary for _, secondary, _ in tr.PAIR_SPLIT.values()
            }, key


class TestConsistencyChecks:
    def _warnings(self, body: str) -> List[str]:
        return tr.parse_html_report(f"<table>{body}</table>").warnings

    def test_a_positive_gross_loss_is_flagged(self) -> None:
        warnings = self._warnings(
            "<tr><td>Gross Profit</td><td>4500.00</td>"
            "<td>Gross Loss</td><td>3265.44</td></tr>"
        )
        assert any("gross_loss" in w and "negative" in w for w in warnings)

    def test_a_profit_factor_that_disagrees_is_flagged(self) -> None:
        warnings = self._warnings(
            "<tr><td>Gross Profit</td><td>4500.00</td>"
            "<td>Gross Loss</td><td>-3265.44</td>"
            "<td>Profit Factor</td><td>9.99</td></tr>"
        )
        assert any("profit_factor" in w for w in warnings)

    def test_a_net_profit_that_disagrees_is_flagged(self) -> None:
        warnings = self._warnings(
            "<tr><td>Total Net Profit</td><td>50.00</td>"
            "<td>Gross Profit</td><td>4500.00</td>"
            "<td>Gross Loss</td><td>-3265.44</td></tr>"
        )
        assert any("net_profit" in w for w in warnings)

    def test_trade_counts_that_do_not_add_up_are_flagged(self) -> None:
        warnings = self._warnings(
            "<tr><td>Total Trades</td><td>999</td>"
            "<td>Profit Trades (% of total)</td><td>131 (53.91%)</td>"
            "<td>Loss Trades (% of total)</td><td>112 (46.09%)</td></tr>"
        )
        assert any("total_trades" in w for w in warnings)

    def test_win_and_loss_percentages_must_sum_to_a_hundred(self) -> None:
        warnings = self._warnings(
            "<tr><td>Profit Trades (% of total)</td><td>131 (80%)</td>"
            "<td>Loss Trades (% of total)</td><td>112 (40%)</td></tr>"
        )
        assert any("100%" in w for w in warnings)

    def test_equity_drawdown_below_balance_drawdown_is_flagged(self) -> None:
        warnings = self._warnings(
            "<tr><td>Balance Drawdown Maximal</td><td>700.00 (7%)</td>"
            "<td>Equity Drawdown Maximal</td><td>100.00 (1%)</td></tr>"
        )
        assert any("equity_drawdown" in w for w in warnings)

    def test_low_history_quality_is_flagged(self) -> None:
        warnings = self._warnings("<tr><td>History Quality</td><td>62%</td></tr>")
        assert any("history quality" in w for w in warnings)

    def test_a_clean_report_is_silent(self) -> None:
        assert (
            self._warnings(
                "<tr><td>Total Net Profit</td><td>1234.56</td>"
                "<td>Gross Profit</td><td>4500.00</td>"
                "<td>Gross Loss</td><td>-3265.44</td>"
                "<td>Profit Factor</td><td>1.38</td>"
                "<td>History Quality</td><td>100%</td></tr>"
            )
            == []
        )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_finds_reports_newest_first(self, tmp_path: Path) -> None:
        older = tmp_path / "old.htm"
        older.write_text(HTML_REPORT, encoding="utf-8")
        os.utime(older, (1_000_000, 1_000_000))
        newer = tmp_path / "Tester" / "new.htm"
        newer.parent.mkdir()
        newer.write_text(HTML_REPORT, encoding="utf-8")
        os.utime(newer, (2_000_000, 2_000_000))

        found = tr.find_reports([tmp_path])
        assert [path.name for path in found] == ["new.htm", "old.htm"]

    def test_ignores_files_that_are_not_reports(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("shopping list", encoding="utf-8")
        (tmp_path / "index.html").write_text(
            "<html><body>nothing to do with trading</body></html>", encoding="utf-8"
        )
        (tmp_path / "statement.htm").write_text(HTML_REPORT, encoding="utf-8")
        assert [p.name for p in tr.find_reports([tmp_path])] == ["statement.htm"]

    def test_a_name_is_enough_to_be_considered(self, tmp_path: Path) -> None:
        """`report1.htm` with no recognizable content is still a candidate."""
        (tmp_path / "report1.htm").write_text("<html></html>", encoding="utf-8")
        assert [p.name for p in tr.find_reports([tmp_path])] == ["report1.htm"]

    def test_honours_a_limit(self, tmp_path: Path) -> None:
        for index in range(5):
            (tmp_path / f"report{index}.htm").write_text(HTML_REPORT, encoding="utf-8")
            time.sleep(0.01)
        assert len(tr.find_reports([tmp_path], limit=2)) == 2

    def test_a_file_can_be_passed_directly(self, report: Path) -> None:
        assert tr.find_reports([report]) == [report.resolve()]

    def test_a_missing_root_is_not_an_error(self, tmp_path: Path) -> None:
        assert tr.find_reports([tmp_path / "does-not-exist"]) == []


# ---------------------------------------------------------------------------
# Thresholds — the CI gate
# ---------------------------------------------------------------------------


class TestThresholds:
    def test_passing_rules(self, report: Path) -> None:
        parsed = tr.parse_report(report)
        results = tr.check_thresholds(
            parsed, {"min_profit_factor": 1.3, "max_equity_drawdown_relative_pct": 15}
        )
        assert all(result.passed for result in results)
        assert len(results) == 2

    def test_a_failing_rule(self, report: Path) -> None:
        parsed = tr.parse_report(report)
        (result,) = tr.check_thresholds(parsed, {"min_sharpe_ratio": 1.0})
        assert result.passed is False
        assert result.actual == 0.86
        assert "FAILED" in result.message

    def test_a_missing_metric_fails_its_gate(self) -> None:
        """A gate that passes because the number was not found is no gate."""
        parsed = tr.TesterReport(metrics={"profit_factor": 1.5})
        (result,) = tr.check_thresholds(parsed, {"min_total_trades": 50})
        assert result.passed is False
        assert result.actual is None
        assert "not in the report" in result.message

    def test_none_rules_are_skipped(self, report: Path) -> None:
        parsed = tr.parse_report(report)
        assert tr.check_thresholds(parsed, {"min_profit_factor": None}) == []

    def test_drawdown_caps_use_the_relative_percentage(self, report: Path) -> None:
        parsed = tr.parse_report(report)
        (result,) = tr.check_thresholds(
            parsed, {"max_equity_drawdown_relative_pct": 5.0}
        )
        assert result.passed is False, "8.01% exceeds a 5% cap"


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


class TestCompare:
    def _second(self, tmp_path: Path) -> Path:
        path = tmp_path / "v2.htm"
        path.write_text(
            HTML_REPORT.replace("1 234.56", "2 500.00")
            .replace(">1.38<", ">1.62<")
            .replace("8.01%", "5.00%"),
            encoding="utf-8",
        )
        return path

    def test_deltas_are_second_minus_first(self, report: Path, tmp_path: Path) -> None:
        first = tr.parse_report(report)
        second = tr.parse_report(self._second(tmp_path))
        table = tr.compare_reports([first, second])
        rows = {row["metric"]: row for row in table["rows"]}
        assert rows["net_profit"]["delta"] == pytest.approx(1265.44)
        assert rows["profit_factor"]["delta"] == pytest.approx(0.24)

    def test_the_verdict_names_a_winner_per_criterion(
        self, report: Path, tmp_path: Path
    ) -> None:
        table = tr.compare_reports(
            [tr.parse_report(report), tr.parse_report(self._second(tmp_path))]
        )
        verdict = table["verdict"]
        assert verdict["net_profit"]["winner"].endswith("v2.htm")
        assert verdict["profit_factor"]["higher_is_better"] is True
        # A smaller drawdown wins, so the second report takes this one too.
        assert verdict["equity_drawdown_relative_pct"]["winner"].endswith("v2.htm")
        assert verdict["equity_drawdown_relative_pct"]["higher_is_better"] is False

    def test_a_metric_missing_from_one_file_has_no_delta(
        self, report: Path, tmp_path: Path
    ) -> None:
        sparse = tmp_path / "sparse.htm"
        sparse.write_text(
            "<table><tr><td>Total Net Profit</td><td>10.00</td></tr></table>",
            encoding="utf-8",
        )
        table = tr.compare_reports([tr.parse_report(report), tr.parse_report(sparse)])
        rows = {row["metric"]: row for row in table["rows"]}
        assert "delta" not in rows["profit_factor"], "absent is not zero"
        assert rows["net_profit"]["values"][1] == 10.0

    def test_a_custom_key_list_is_honoured(self, report: Path, tmp_path: Path) -> None:
        table = tr.compare_reports(
            [tr.parse_report(report), tr.parse_report(self._second(tmp_path))],
            keys=["profit_factor"],
        )
        assert [row["metric"] for row in table["rows"]] == ["profit_factor"]

    def test_sources_are_listed_in_order(self, report: Path, tmp_path: Path) -> None:
        table = tr.compare_reports(
            [tr.parse_report(report), tr.parse_report(self._second(tmp_path))]
        )
        assert table["sources"] == [str(report), str(tmp_path / "v2.htm")]


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------


class TestPromptFormat:
    def test_the_headline_numbers_appear(self, report: Path) -> None:
        summary = tr.format_for_prompt(tr.parse_report(report))
        for fragment in (
            "net_profit: 1234.56",
            "profit_factor: 1.38",
            "equity_drawdown: 812.44",
            "equity_drawdown_relative_pct: 8.01%",
            "history_quality_pct: 100.0%",
            "total_trades: 243",
        ):
            assert fragment in summary, fragment

    def test_warnings_and_missing_are_surfaced(self) -> None:
        parsed = tr.parse_html_report(
            "<table><tr><td>History Quality</td><td>40%</td></tr></table>"
        )
        summary = tr.format_for_prompt(parsed)
        assert "warnings:" in summary
        assert "history quality is 40.0%" in summary
        assert "missing:" in summary

    def test_it_stays_short(self, report: Path) -> None:
        assert len(tr.format_for_prompt(tr.parse_report(report)).splitlines()) <= 45

    def test_to_dict_is_json_serializable(self, report: Path) -> None:
        payload = tr.parse_report(report).to_dict()
        assert json.loads(json.dumps(payload, default=str))["format"] == "html"


# ---------------------------------------------------------------------------
# Tester logs — the EA's own complaints
# ---------------------------------------------------------------------------


class TestLogScan:
    def test_errors_and_warnings_are_counted(self, tmp_path: Path) -> None:
        log = tmp_path / "20240601.log"
        log.write_text(
            "2024.06.01 10:00:01   started\n"
            "2024.06.01 10:00:02   error: failed to open position\n"
            "2024.06.01 10:00:03   warning: spread too wide\n"
            "2024.06.01 10:00:04   tick processed\n",
            encoding="utf-8",
        )
        scanned = tr.scan_tester_log(log)
        assert scanned["lines"] == 4
        assert scanned["error_count"] == 1
        assert scanned["warning_count"] == 1
        assert "failed to open position" in scanned["errors"][0]

    def test_utf16_logs_are_read(self, tmp_path: Path) -> None:
        log = tmp_path / "u16.log"
        log.write_bytes("error: not enough money\n".encode("utf-16-le"))
        assert tr.scan_tester_log(log)["error_count"] == 1

    def test_logs_are_found_under_the_usual_folders(self, tmp_path: Path) -> None:
        folder = tmp_path / "Tester" / "logs"
        folder.mkdir(parents=True)
        (folder / "20240601.log").write_text("error: boom", encoding="utf-8")
        (tmp_path / "unrelated.log").write_text("error: ignored", encoding="utf-8")
        found = tr.find_tester_logs([tmp_path])
        assert [path.name for path in found] == ["20240601.log"]

    def test_a_missing_log_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            tr.scan_tester_log(tmp_path / "nope.log")


# ---------------------------------------------------------------------------
# [Tester] ini generation
# ---------------------------------------------------------------------------


class TestIniGeneration:
    def test_the_minimum_usable_config(self) -> None:
        ini = tr.build_tester_ini(expert="MyEA")
        assert "[Tester]" in ini
        assert "Expert=MyEA" in ini
        assert "ShutdownTerminal=1" in ini, "otherwise the terminal never exits"
        assert "Report=TesterReport" in ini

    def test_every_setting_lands_in_the_file(self) -> None:
        ini = tr.build_tester_ini(
            expert="Examples/MACD/MACD Sample",
            symbol="EURUSD",
            period="H1",
            from_date="2024.01.01",
            to_date="2024.06.30",
            deposit=10000,
            currency="USD",
            leverage="1:100",
            model=4,
            execution_mode=-1,
            optimization=2,
            optimization_criterion=5,
            expert_parameters="macd.set",
            report="/out/Report",
            visual=True,
            login="12345",
            password="secret",
            trade_server="Broker-Demo",
            use_local=1,
        )
        for expected in (
            "Expert=Examples/MACD/MACD Sample",
            "ExpertParameters=macd.set",
            "Symbol=EURUSD",
            "Period=H1",
            "FromDate=2024.01.01",
            "ToDate=2024.06.30",
            "Deposit=10000",
            "Currency=USD",
            "Leverage=1:100",
            "Model=4",
            "ExecutionMode=-1",
            "Optimization=2",
            "OptimizationCriterion=5",
            "Report=/out/Report",
            "Login=12345",
            "Server=Broker-Demo",
            "UseLocal=1",
            "Visual=1",
            "ReplaceReport=1",
        ):
            assert expected in ini, expected

    def test_empty_settings_are_omitted(self) -> None:
        ini = tr.build_tester_ini(expert="MyEA")
        assert "Symbol=" not in ini
        assert "Model=" not in ini
        assert "Login=" not in ini

    def test_zero_is_a_setting_not_an_absence(self) -> None:
        """Model=0 (every tick) and Optimization=0 (off) are real choices."""
        ini = tr.build_tester_ini(expert="MyEA", model=0, optimization=0)
        assert "Model=0" in ini
        assert "Optimization=0" in ini

    def test_shutdown_can_be_left_off_on_purpose(self) -> None:
        ini = tr.build_tester_ini(expert="MyEA", shutdown_terminal=False)
        assert "ShutdownTerminal=0" in ini

    def test_the_documented_enumerations_are_recorded(self) -> None:
        assert tr.TESTER_MODELS[4] == "every tick based on real ticks"
        assert tr.OPTIMIZATION_MODES[2].startswith("fast genetic")
        assert tr.OPTIMIZATION_CRITERIA[6].startswith("custom")


# ---------------------------------------------------------------------------
# Terminal discovery and command building
# ---------------------------------------------------------------------------


class TestTerminalDiscovery:
    def test_an_explicit_path_must_exist(self, tmp_path: Path) -> None:
        assert tr.find_terminal(str(tmp_path / "nope.exe")) is None
        real = tmp_path / "terminal64.exe"
        real.write_text("", encoding="utf-8")
        assert tr.find_terminal(str(real)) == real

    def test_the_environment_variable_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = tmp_path / "terminal64.exe"
        real.write_text("", encoding="utf-8")
        monkeypatch.setenv("TERMINAL_PATH", str(real))
        assert tr.find_terminal() == real

    def test_a_windows_binary_needs_wine_elsewhere(self) -> None:
        if sys.platform == "win32":
            assert tr.needs_wine(Path("terminal64.exe")) is False
        else:
            assert tr.needs_wine(Path("terminal64.exe")) is True
            assert tr.needs_wine(Path("terminal64")) is False

    def test_the_command_quotes_the_config_path(self) -> None:
        command = tr.tester_command(
            Path("/opt/mt5/terminal64"), Path("/tmp/my config.ini"), wine=False
        )
        assert command[0] == "/opt/mt5/terminal64"
        assert command[-1] == '/config:"/tmp/my config.ini"'

    def test_portable_is_passed_through(self) -> None:
        command = tr.tester_command(
            Path("/opt/mt5/terminal64"), Path("/tmp/x.ini"), portable=True, wine=False
        )
        assert "/portable" in command

    def test_wine_wraps_a_windows_binary_off_windows(self) -> None:
        command = tr.tester_command(Path("C:/MT5/terminal64.exe"), Path("/tmp/x.ini"))
        if sys.platform == "win32":
            assert command[0] == "C:/MT5/terminal64.exe"
        else:
            assert command[0] == "wine"


# ---------------------------------------------------------------------------
# The runner, against a fake terminal
# ---------------------------------------------------------------------------

FAKE_TERMINAL = """#!/usr/bin/env bash
# Stands in for terminal64.exe: reads the [Tester] ini and writes a report.
set -e
cfg=""
for a in "$@"; do
  case "$a" in /config:*) cfg="${a#/config:}"; cfg="${cfg%\\"}"; cfg="${cfg#\\"}";; esac
done
echo "$@" >> "$FAKE_CALL_LOG"
[ -f "$cfg" ] || { echo "no config: $cfg" >&2; exit 3; }
report=$(grep -m1 '^Report=' "$cfg" | cut -d= -f2-)
expert=$(grep -m1 '^Expert=' "$cfg" | cut -d= -f2-)
symbol=$(grep -m1 '^Symbol=' "$cfg" | cut -d= -f2-)
sleep "${FAKE_DELAY:-0}"
case "${FAKE_MODE:-ok}" in
  ok|failafter)
    cat > "${report}.xml" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<Report>
  <Expert>$expert</Expert><Symbol>$symbol</Symbol><Period>H1</Period>
  <Model>every tick</Model><History_Quality>100%</History_Quality>
  <Total_Net_Profit>1 850.25</Total_Net_Profit>
  <Gross_Profit>5 200.00</Gross_Profit><Gross_Loss>-3 349.75</Gross_Loss>
  <Profit_Factor>1.55</Profit_Factor><Sharpe_Ratio>1.12</Sharpe_Ratio>
  <Total_Trades>310</Total_Trades><Total_Deals>620</Total_Deals>
  <Equity_Drawdown_Maximal>940.10 (9.30%)</Equity_Drawdown_Maximal>
  <Balance_Drawdown_Maximal>820.00 (8.10%)</Balance_Drawdown_Maximal>
</Report>
XML
    [ "${FAKE_MODE:-ok}" = "failafter" ] && exit 7
    exit 0;;
  htm)
    # A single test writes .htm even when the ini asked for a bare name, and
    # an optimization writes .xml: the runner has to accept either.
    cat > "${report}.htm" <<HTM
<html><body><table>
<tr><td>Total Net Profit</td><td>777.00</td></tr>
<tr><td>Profit Factor</td><td>1.25</td></tr>
</table></body></html>
HTM
    exit 0;;
  optimization)
    # One row per pass, the way an Optimization run writes its .xml. Whitespace
    # between the cells is fine: the parser reads tags, not layout.
    cat > "${report}.xml" <<OPT
<?xml version="1.0" encoding="ANSI"?>
<Table>
  <Row>
    <Cell>Pass</Cell><Cell>Result</Cell><Cell>Profit</Cell>
    <Cell>Expected Payoff</Cell><Cell>Profit Factor</Cell>
    <Cell>Recovery Factor</Cell><Cell>Sharpe Ratio</Cell>
    <Cell>Custom</Cell><Cell>Equity DD %</Cell><Cell>Trades</Cell>
    <Cell>InpFastEMA</Cell><Cell>InpStopLoss</Cell>
  </Row>
  <Row>
    <Cell>4</Cell><Cell>11200</Cell><Cell>1200</Cell><Cell>6</Cell>
    <Cell>1.4</Cell><Cell>2.5</Cell><Cell>1.1</Cell><Cell>0</Cell>
    <Cell>8</Cell><Cell>200</Cell><Cell>12</Cell><Cell>500</Cell>
  </Row>
  <Row>
    <Cell>9</Cell><Cell>10800</Cell><Cell>800</Cell><Cell>4</Cell>
    <Cell>1.2</Cell><Cell>1.8</Cell><Cell>0.9</Cell><Cell>0</Cell>
    <Cell>11</Cell><Cell>200</Cell><Cell>20</Cell><Cell>750</Cell>
  </Row>
</Table>
OPT
    exit 0;;
  forward)
    # A forward run writes the back half and a second file with a .forward
    # suffix. Which one the runner returns as "the report" is the whole point.
    cat > "${report}.htm" <<HTM
<html><body><table>
<tr><td>Total Net Profit</td><td>1 850.25</td></tr>
<tr><td>Profit Factor</td><td>1.55</td></tr>
<tr><td>Total Trades</td><td>310</td></tr>
</table></body></html>
HTM
    cat > "${report}.forward.htm" <<FWD
<html><body><table>
<tr><td>Total Net Profit</td><td>-220.00</td></tr>
<tr><td>Profit Factor</td><td>0.91</td></tr>
<tr><td>Total Trades</td><td>88</td></tr>
</table></body></html>
FWD
    exit 0;;
  noreport) exit 0;;
  fail) exit 7;;
  failafter) exit 7;;
esac
"""


@pytest.fixture()
def fake_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An executable that behaves like the terminal for the paths we test."""
    script = tmp_path / "terminal64"
    script.write_text(FAKE_TERMINAL, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("FAKE_CALL_LOG", str(tmp_path / "calls.log"))
    monkeypatch.delenv("FAKE_MODE", raising=False)
    monkeypatch.delenv("FAKE_DELAY", raising=False)
    return script


class TestRunner:
    def test_it_writes_the_ini_launches_and_parses_the_result(
        self, fake_terminal: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "out" / "Run.xml"
        ini = tr.build_tester_ini(
            expert="MyEA",
            symbol="EURUSD",
            period="H1",
            report=str(target.with_suffix("")),
        )
        outcome = tr.run_tester(
            terminal=fake_terminal,
            ini_text=ini,
            report_path=target,
            timeout=30,
            wine=False,
            poll_interval=0.05,
        )
        assert outcome["exit_code"] == 0
        assert Path(outcome["ini_path"]).is_file(), "the ini is written for the run"
        report = outcome["report"]
        assert report.metrics["net_profit"] == 1850.25
        assert report.metrics["profit_factor"] == 1.55
        assert report.metrics["equity_drawdown"] == 940.1
        assert report.metrics["equity_drawdown_pct"] == 9.3
        assert report.run["exit_code"] == 0
        assert report.run["seconds"] >= 0

    def test_the_terminal_receives_the_config_path(
        self, fake_terminal: Path, tmp_path: Path
    ) -> None:
        calls = tmp_path / "calls.log"
        target = tmp_path / "Run.xml"
        tr.run_tester(
            terminal=fake_terminal,
            ini_text=tr.build_tester_ini(
                expert="MyEA", report=str(target.with_suffix(""))
            ),
            report_path=target,
            timeout=30,
            wine=False,
            poll_interval=0.05,
        )
        assert f'/config:"{target.with_suffix(".ini")}"' in calls.read_text()

    def test_a_nonzero_exit_is_reported_as_a_warning(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The report may still be written; the exit code says it was partial."""
        monkeypatch.setenv("FAKE_MODE", "failafter")
        target = tmp_path / "Partial.xml"
        outcome = tr.run_tester(
            terminal=fake_terminal,
            ini_text=tr.build_tester_ini(
                expert="MyEA", report=str(target.with_suffix(""))
            ),
            report_path=target,
            timeout=10,
            wine=False,
            poll_interval=0.05,
        )
        assert outcome["exit_code"] == 7
        assert outcome["report"].metrics["net_profit"] == 1850.25
        assert any(
            "exit" in warning and "partial" in warning
            for warning in outcome["report"].warnings
        )

    def test_a_terminal_that_writes_nothing_times_out(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "fail")
        target = tmp_path / "Fail.xml"
        with pytest.raises(TimeoutError):
            tr.run_tester(
                terminal=fake_terminal,
                ini_text=tr.build_tester_ini(
                    expert="MyEA", report=str(target.with_suffix(""))
                ),
                report_path=target,
                timeout=2,
                wine=False,
                poll_interval=0.05,
            )

    def test_a_missing_report_is_a_timeout_not_a_guess(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "noreport")
        target = tmp_path / "Nothing.xml"
        with pytest.raises(TimeoutError) as excinfo:
            tr.run_tester(
                terminal=fake_terminal,
                ini_text=tr.build_tester_ini(
                    expert="MyEA", report=str(target.with_suffix(""))
                ),
                report_path=target,
                timeout=2,
                wine=False,
                poll_interval=0.05,
            )
        assert "no report" in str(excinfo.value)

    def test_a_stale_report_is_not_accepted(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mtime must advance, or we would parse the previous run."""
        monkeypatch.setenv("FAKE_MODE", "noreport")
        target = tmp_path / "Stale.xml"
        target.write_text("<html><body>stale</body></html>", encoding="utf-8")
        with pytest.raises(TimeoutError):
            tr.run_tester(
                terminal=fake_terminal,
                ini_text=tr.build_tester_ini(
                    expert="MyEA", report=str(target.with_suffix(""))
                ),
                report_path=target,
                timeout=2,
                wine=False,
                poll_interval=0.05,
            )

    def test_a_slow_terminal_is_still_waited_for(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_DELAY", "0.4")
        target = tmp_path / "Slow.xml"
        outcome = tr.run_tester(
            terminal=fake_terminal,
            ini_text=tr.build_tester_ini(
                expert="MyEA", report=str(target.with_suffix(""))
            ),
            report_path=target,
            timeout=30,
            wine=False,
            poll_interval=0.05,
        )
        assert outcome["report"].metrics["net_profit"] == 1850.25


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run(args: List[str]) -> Any:
    result = CliRunner().invoke(tr.main, args, catch_exceptions=False)
    return result


class TestCli:
    def test_no_arguments_explains_what_to_do(self) -> None:
        result = _run([])
        assert result.exit_code == 2
        assert "--report" in result.output

    def test_parsing_a_report_prints_json(self, report: Path) -> None:
        result = _run(["--report", str(report)])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["reports"][0]["metrics"]["profit_factor"] == 1.38

    def test_prompt_adds_a_summary(self, report: Path) -> None:
        result = _run(["--report", str(report), "--prompt"])
        payload = json.loads(result.output)
        assert "net_profit: 1234.56" in payload["reports"][0]["summary"]

    def test_json_out_writes_the_file(self, report: Path, tmp_path: Path) -> None:
        target = tmp_path / "metrics.json"
        result = _run(["--report", str(report), "--json-out", str(target), "--quiet"])
        assert result.exit_code == 0
        assert json.loads(target.read_text())["reports"][0]["format"] == "html"

    def test_passing_thresholds_exit_zero(self, report: Path) -> None:
        result = _run(
            [
                "--report",
                str(report),
                "--min-profit-factor",
                "1.3",
                "--max-equity-drawdown-pct",
                "15",
                "--min-trades",
                "50",
                "--quiet",
            ]
        )
        assert result.exit_code == 0

    def test_a_failed_threshold_exits_one(self, report: Path) -> None:
        result = _run(
            ["--report", str(report), "--min-profit-factor", "2.0", "--quiet"]
        )
        assert result.exit_code == 1

    def test_a_missing_metric_fails_the_gate_from_the_cli(self, report: Path) -> None:
        result = _run(["--report", str(report), "--min-sharpe-ratio", "5", "--quiet"])
        assert result.exit_code == 1

    def test_compare_prints_deltas(self, report: Path, tmp_path: Path) -> None:
        second = tmp_path / "v2.htm"
        second.write_text(HTML_REPORT.replace("1 234.56", "2 500.00"), encoding="utf-8")
        result = _run(
            [
                str(report),
                str(second),
                "--json-out",
                str(tmp_path / "c.json"),
                "--quiet",
            ]
        )
        assert result.exit_code == 0
        payload = json.loads((tmp_path / "c.json").read_text())
        rows = {row["metric"]: row for row in payload["comparison"]["rows"]}
        assert rows["net_profit"]["delta"] == pytest.approx(1265.44)

    def test_latest_finds_the_newest_report(self, tmp_path: Path) -> None:
        (tmp_path / "report.htm").write_text(HTML_REPORT, encoding="utf-8")
        out = tmp_path / "latest.json"
        result = _run(
            [
                "--latest",
                "--search-dir",
                str(tmp_path),
                "--json-out",
                str(out),
                "--quiet",
            ]
        )
        assert result.exit_code == 0
        payload = json.loads(out.read_text())
        assert payload["reports"][0]["source"].endswith("report.htm")

    def test_latest_with_nothing_to_find_exits_two(self, tmp_path: Path) -> None:
        assert _run(["--latest", "--search-dir", str(tmp_path)]).exit_code == 2

    def test_a_missing_report_file_exits_one(self, tmp_path: Path) -> None:
        result = _run(["--report", str(tmp_path / "nope.htm")])
        assert result.exit_code != 0

    def test_log_scanning_is_included(self, report: Path, tmp_path: Path) -> None:
        log = tmp_path / "tester.log"
        log.write_text("error: something broke\nok line\n", encoding="utf-8")
        result = _run(["--report", str(report), "--log", str(log)])
        payload = json.loads(result.output)
        assert payload["logs"][0]["error_count"] == 1

    def test_print_ini_does_not_launch_anything(self, tmp_path: Path) -> None:
        result = _run(
            [
                "--run",
                "--print-ini",
                "--expert",
                "MyEA",
                "--symbol",
                "EURUSD",
                "--period",
                "H1",
                "--model",
                "4",
                "--out-report",
                str(tmp_path / "R.xml"),
            ]
        )
        assert result.exit_code == 0
        assert "Expert=MyEA" in result.output
        assert "Model=4" in result.output

    def test_run_without_an_expert_exits_two(self) -> None:
        assert _run(["--run"]).exit_code == 2

    def test_run_without_a_terminal_exits_two(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tr, "find_terminal", lambda explicit=None: None)
        result = _run(["--run", "--expert", "MyEA"])
        assert result.exit_code == 2

    def test_help_documents_the_gate_options(self) -> None:
        result = _run(["--help"])
        assert result.exit_code == 0
        for option in (
            "--min-profit-factor",
            "--max-equity-drawdown-pct",
            "--min-history-quality-pct",
            "--latest",
            "--report",
            "--run",
            "REPORTS",
        ):
            assert option in result.output


# ---------------------------------------------------------------------------
# Optimization reports: the XML table of passes
# ---------------------------------------------------------------------------

OPT_HEADER: Tuple[str, ...] = (
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

FORWARD_HEADER: Tuple[str, ...] = (
    "Pass",
    "Back Result",
    "Forward Result",
    "Profit",
    "Expected Payoff",
    "Profit Factor",
    "Recovery Factor",
    "Sharpe Ratio",
    "Custom",
    "Equity DD %",
    "Trades",
    "InpFastEMA",
)

SET_TEXT = (
    "\n".join(
        (
            "; saved on 2026.09.30 12:00",
            "InpFastEMA=12||5||1||30||Y",
            "InpSlowEMA=26||20||5||60||Y",
            "InpStopLoss=500||200||50||1000||Y",
            "InpUseFilter=true||false||0||true||Y",
            "InpLots=0.10||0||0||0||N",
            "InpMagic=100010||0||0||0||N",
        )
    )
    + "\n"
)


def _cells_row(cells: Any) -> str:
    """One ``<Row>`` of ``<Cell>`` elements, as MT5 writes it."""
    return "  <Row>" + "".join(f"<Cell>{cell}</Cell>" for cell in cells) + "</Row>"


def optimization_xml(rows: Any, header: Any = OPT_HEADER) -> str:
    """An optimization report in the shape the terminal writes."""
    out: List[str] = ['<?xml version="1.0" encoding="ANSI"?>', "<Table>"]
    if header:
        out.append(_cells_row(header))
    for row in rows:
        out.append(_cells_row(row))
    out.append("</Table>")
    return "\n".join(out) + "\n"


def _pass_cells(
    number: int,
    *,
    result: float = 10500.0,
    profit: float = 500.0,
    payoff: float = 2.5,
    profit_factor: float = 1.5,
    recovery_factor: float = 2.0,
    sharpe: float = 1.0,
    custom: str = "0",
    drawdown_pct: float = 10.0,
    trades: int = 200,
    fast: Any = 12,
    slow: Any = 26,
    stop_loss: Any = 500,
    use_filter: Any = "true",
) -> Tuple[str, ...]:
    """One pass row. The defaults clear every filter MT5 offers."""
    return (
        str(number),
        str(result),
        str(profit),
        str(payoff),
        str(profit_factor),
        str(recovery_factor),
        str(sharpe),
        custom,
        str(drawdown_pct),
        str(trades),
        str(fast),
        str(slow),
        str(stop_loss),
        str(use_filter),
    )


def _sample_rows() -> List[Tuple[str, ...]]:
    """A report with a lucky winner, a field of decent passes, and junk.

    Pass 37 is the trap every optimization produces: by far the best result, on
    12 trades, with every input at the edge of the range that was tested.
    """
    rows: List[Tuple[str, ...]] = [
        _pass_cells(
            37,
            result=108000.0,
            profit=98000.0,
            payoff=8166.67,
            profit_factor=14.86,
            recovery_factor=30.17,
            sharpe=1.63,
            drawdown_pct=32.48,
            trades=12,
            fast=5,
            slow=60,
            stop_loss=1000,
        )
    ]
    for index in range(1, 31):
        rows.append(
            _pass_cells(
                index,
                result=11000.0 + index * 10,
                profit=1000.0 + index * 10,
                sharpe=3.5 if index == 5 else 1.0,
                fast=(index % 26) + 5,
                slow=20 + (index % 9) * 5,
                stop_loss=1000,
                use_filter="true" if index % 2 else "false",
            )
        )
    # The five ways a pass fails MT5's own "hide unsuccessful passes" filters.
    rows.append(_pass_cells(90, result=9000.0, profit=-1000.0, trades=0))
    rows.append(
        _pass_cells(91, result=9100.0, profit=-900.0, recovery_factor=-0.5, sharpe=-1.2)
    )
    rows.append(_pass_cells(92, result=9200.0, profit=100.0, drawdown_pct=80.0))
    rows.append(_pass_cells(93, result=9300.0, profit=100.0, recovery_factor=0.4))
    rows.append(_pass_cells(94, result=9400.0, profit=100.0, sharpe=0.1))
    return rows


@pytest.fixture()
def optimization_file(tmp_path: Path) -> Path:
    """The sample optimization report, written as ANSI like the terminal does."""
    target = tmp_path / "ReportOptimizer-555849.xml"
    target.write_bytes(optimization_xml(_sample_rows()).encode("cp1251"))
    return target


@pytest.fixture()
def grid_set(tmp_path: Path) -> Path:
    target = tmp_path / "grid.set"
    target.write_bytes(SET_TEXT.encode("cp1251"))
    return target


def _payload(result: Any) -> Dict[str, Any]:
    """The JSON on stdout, ignoring the warnings the CLI writes to stderr."""
    return json.loads(result.stdout)


class TestOptimizationParsing:
    def test_it_reads_the_documented_columns(self, optimization_file: Path) -> None:
        result = tr.parse_optimization(optimization_file)
        assert result.format == "xml"
        assert len(result.passes) == 36
        assert result.parameter_names == [
            "InpFastEMA",
            "InpSlowEMA",
            "InpStopLoss",
            "InpUseFilter",
        ]
        assert result.columns[:3] == ["Pass", "Result", "Profit"]
        assert result.forward is False

    def test_metrics_are_numbers_and_inputs_keep_their_type(self) -> None:
        result = tr.parse_optimization_text(optimization_xml(_sample_rows()))
        winner = next(row for row in result.passes if row.number == 37)
        assert winner.metrics["result"] == 108000.0
        assert winner.metrics["profit_factor"] == 14.86
        assert winner.metrics["equity_drawdown_pct"] == 32.48
        assert winner.metrics["custom"] == 0
        assert winner.inputs["InpStopLoss"] == 1000
        assert isinstance(winner.inputs["InpStopLoss"], int)
        assert winner.inputs["InpUseFilter"] == "true"

    def test_a_missing_header_falls_back_to_the_documented_order(self) -> None:
        result = tr.parse_optimization_text(
            optimization_xml(_sample_rows()[:2], header=None)
        )
        assert any("no header row" in warning for warning in result.warnings)
        assert result.passes[0].metrics["result"] == 108000.0
        assert result.passes[0].metrics["trades"] == 12
        # Only the ten documented columns are known, so the four input cells
        # are kept positionally rather than guessed at.
        assert result.parameter_names == []
        assert result.passes[0].extra["column_11"] == "5"

    def test_a_short_row_leaves_the_missing_columns_absent(self) -> None:
        result = tr.parse_optimization_text(optimization_xml([_pass_cells(1)[:11]]))
        assert result.passes[0].metrics["trades"] == 200
        assert "InpSlowEMA" not in result.passes[0].inputs
        assert any("fewer cells" in warning for warning in result.warnings)

    def test_a_wide_row_keeps_the_extra_cells(self) -> None:
        rows = [_pass_cells(1) + ("extra-value",)]
        result = tr.parse_optimization_text(optimization_xml(rows))
        assert result.passes[0].extra["column_15"] == "extra-value"
        assert any("more cells" in warning for warning in result.warnings)

    def test_html_tables_are_read_too(self) -> None:
        rows: List[Any] = [OPT_HEADER, _pass_cells(1), _pass_cells(2)]
        html_text = (
            "<table>"
            + "".join(
                "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            + "</table>"
        )
        result = tr.parse_optimization_text(html_text)
        assert result.format == "html"
        assert len(result.passes) == 2
        assert result.passes[1].inputs["InpFastEMA"] == 12

    def test_forward_files_are_recognized(self) -> None:
        rows = [
            (
                "4",
                "12000",
                "3000",
                "900",
                "4.5",
                "1.4",
                "2.5",
                "1.1",
                "0",
                "9",
                "200",
                "12",
            ),
            (
                "9",
                "11800",
                "-400",
                "-1400",
                "-7",
                "0.9",
                "1.1",
                "0.4",
                "0",
                "22",
                "200",
                "20",
            ),
        ]
        result = tr.parse_optimization_text(
            optimization_xml(rows, header=FORWARD_HEADER)
        )
        assert result.forward is True
        assert result.passes[0].metrics["back_result"] == 12000
        assert result.passes[1].metrics["forward_result"] == -400
        assert result.passes[1].metrics["profit"] == -1400
        assert result.passes[1].inputs["InpFastEMA"] == 20
        assert result.parameter_names == ["InpFastEMA"]

    def test_a_testing_report_is_not_an_optimization_report(self) -> None:
        result = tr.parse_optimization_text(HTML_REPORT)
        assert result.passes == []
        assert any(
            "does not look like an optimization report" in warning
            for warning in result.warnings
        )

    def test_an_empty_table_says_so(self) -> None:
        result = tr.parse_optimization_text(optimization_xml([]))
        assert any("no passes" in warning for warning in result.warnings)

    def test_a_file_with_no_table_at_all_says_so(self) -> None:
        result = tr.parse_optimization_text("not a report")
        assert result.passes == []
        assert any("no <Row>/<Cell> table" in warning for warning in result.warnings)

    def test_entities_are_unescaped(self) -> None:
        result = tr.parse_optimization_text(
            optimization_xml([_pass_cells(1, use_filter="A &amp; B")])
        )
        assert result.passes[0].inputs["InpUseFilter"] == "A & B"

    def test_ansi_bytes_are_decoded(self, tmp_path: Path) -> None:
        target = tmp_path / "ansi.xml"
        target.write_bytes(
            optimization_xml([_pass_cells(1, custom="\u2116 7")]).encode("cp1251")
        )
        assert tr.parse_optimization(target).passes[0].metrics["custom"] == "\u2116 7"

    def test_utf16_bytes_are_decoded(self, tmp_path: Path) -> None:
        target = tmp_path / "wide.xml"
        target.write_bytes(optimization_xml(_sample_rows()[:1]).encode("utf-16"))
        assert len(tr.parse_optimization(target).passes) == 1

    def test_parse_any_report_routes_by_content(
        self, optimization_file: Path, report: Path
    ) -> None:
        assert isinstance(tr.parse_any_report(optimization_file), tr.OptimizationResult)
        assert isinstance(tr.parse_any_report(report), tr.TesterReport)

    def test_is_optimization_text_sniffs_the_tags(self) -> None:
        assert tr.is_optimization_text(optimization_xml(_sample_rows()[:1]))
        assert not tr.is_optimization_text(HTML_REPORT)

    def test_a_missing_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            tr.parse_optimization(tmp_path / "nope.xml")

    def test_to_dict_counts_instead_of_dumping_every_pass(
        self, optimization_file: Path
    ) -> None:
        result = tr.parse_optimization(optimization_file)
        assert result.to_dict()["passes"] == 36
        assert "rows" not in result.to_dict()
        assert len(result.to_dict(include_passes=2)["rows"]) == 2


class TestPassFiltering:
    def test_the_defaults_are_the_filters_mt5_offers(self) -> None:
        assert tr.PASS_FILTER_DEFAULTS == {
            "min_trades": 1.0,
            "min_profit": 0.0,
            "max_drawdown_pct": 50.0,
            "min_recovery_factor": 1.0,
            "min_sharpe_ratio": 0.5,
        }

    def test_junk_passes_are_dropped_with_a_reason_each(self) -> None:
        result = tr.parse_optimization_text(optimization_xml(_sample_rows()))
        filtered = tr.filter_passes(result.passes)
        assert filtered["kept_count"] == 31
        assert filtered["dropped_count"] == 5
        reasons = {item["pass"]: item["reasons"] for item in filtered["dropped"]}
        assert "only 0 trades" in reasons[90]
        assert any("profit -900" in reason for reason in reasons[91])
        assert any("drawdown 80%" in reason for reason in reasons[92])
        assert any("recovery factor 0.4" in reason for reason in reasons[93])
        assert any("sharpe ratio 0.1" in reason for reason in reasons[94])

    def test_a_rule_needs_the_value_to_fire(self) -> None:
        rows: List[Any] = [
            _pass_cells(1)[:10],
            ("7", "10000", "100", "1", "1.2", "1.5", "0.8", "0", "9"),
        ]
        result = tr.parse_optimization_text(optimization_xml(rows))
        kept = tr.filter_passes(result.passes)["kept"]
        # The second row reports no trade count, so that rule cannot judge it:
        # a missing metric is not a failed one.
        assert [item.number for item in kept] == [1, 7]

    def test_turning_the_filters_off_keeps_everything(self) -> None:
        result = tr.parse_optimization_text(optimization_xml(_sample_rows()))
        filtered = tr.filter_passes(result.passes, tr.PassFilter.off())
        assert filtered["kept_count"] == 36
        assert filtered["dropped_count"] == 0

    def test_custom_limits_are_honoured(self) -> None:
        result = tr.parse_optimization_text(optimization_xml(_sample_rows()))
        filtered = tr.filter_passes(
            result.passes, tr.PassFilter(min_trades=100.0, min_sharpe_ratio=None)
        )
        assert all(float(item.metrics["trades"]) >= 100.0 for item in filtered["kept"])


class TestRanking:
    def test_result_ranks_best_first(self) -> None:
        result = tr.parse_optimization_text(optimization_xml(_sample_rows()))
        ranked = tr.rank_passes(result.passes, "result")
        assert ranked[0].number == 37
        assert ranked[1].number == 30

    def test_drawdown_ranks_smallest_first(self) -> None:
        rows = [
            _pass_cells(1, drawdown_pct=40.0),
            _pass_cells(2, drawdown_pct=5.0),
            _pass_cells(3, drawdown_pct=20.0),
        ]
        result = tr.parse_optimization_text(optimization_xml(rows))
        ranked = tr.rank_passes(result.passes, "drawdown")
        assert [item.number for item in ranked] == [2, 3, 1]

    def test_friendly_names_and_metric_keys_both_work(self) -> None:
        result = tr.parse_optimization_text(optimization_xml(_sample_rows()))
        assert tr.rank_passes(result.passes, "sharpe")[0].number == 5
        assert tr.rank_passes(result.passes, "sharpe_ratio")[0].number == 5

    def test_a_label_with_spaces_is_normalized_into_a_key(self) -> None:
        result = tr.parse_optimization_text(optimization_xml(_sample_rows()))
        ranked = tr.rank_passes(result.passes, "Expected Payoff")
        assert ranked[0].metrics["expected_payoff"] == 8166.67

    def test_a_forward_column_can_be_ranked(self) -> None:
        rows = [
            (
                "4",
                "12000",
                "3000",
                "900",
                "4.5",
                "1.4",
                "2.5",
                "1.1",
                "0",
                "9",
                "200",
                "12",
            ),
            (
                "9",
                "11800",
                "9000",
                "1400",
                "7",
                "0.9",
                "1.1",
                "0.4",
                "0",
                "22",
                "200",
                "20",
            ),
        ]
        result = tr.parse_optimization_text(
            optimization_xml(rows, header=FORWARD_HEADER)
        )
        assert tr.rank_passes(result.passes, "forward_result")[0].number == 9

    def test_a_missing_metric_sorts_last(self) -> None:
        rows = [
            ("1", "10500", "500", "2.5", "1.5", "2", "", "0", "10", "200", "12"),
            ("2", "10400", "400", "2", "1.4", "1.9", "0.9", "0", "11", "190", "14"),
        ]
        result = tr.parse_optimization_text(optimization_xml(rows))
        ranked = tr.rank_passes(result.passes, "sharpe")
        assert [item.number for item in ranked] == [2, 1]

    def test_top_limits_the_list(self) -> None:
        result = tr.parse_optimization_text(optimization_xml(_sample_rows()))
        assert len(tr.rank_passes(result.passes, "result", top=3)) == 3

    def test_ties_keep_file_order(self) -> None:
        rows = [_pass_cells(number) for number in (5, 3, 9)]
        result = tr.parse_optimization_text(optimization_xml(rows))
        ranked = tr.rank_passes(result.passes, "result")
        assert [item.number for item in ranked] == [5, 3, 9]

    def test_descending_can_be_forced(self) -> None:
        result = tr.parse_optimization_text(optimization_xml(_sample_rows()))
        ranked = tr.rank_passes(result.passes, "result", descending=False)
        assert ranked[0].number == 90


class TestOptimizationAnalysis:
    def test_it_ranks_and_counts(self, optimization_file: Path) -> None:
        analysis = tr.analyze_optimization(tr.parse_optimization(optimization_file))
        assert analysis["passes"] == 36
        assert analysis["kept"] == 31
        assert analysis["dropped"] == 5
        assert analysis["rank_by"] == "result"
        assert len(analysis["top"]) == 10
        assert analysis["best"]["pass"] == 37

    def test_a_thin_best_pass_is_called_out(self, optimization_file: Path) -> None:
        analysis = tr.analyze_optimization(tr.parse_optimization(optimization_file))
        assert any("traded 12 time" in warning for warning in analysis["warnings"])

    def test_a_lucky_spike_is_called_out(self, optimization_file: Path) -> None:
        analysis = tr.analyze_optimization(tr.parse_optimization(optimization_file))
        assert any("x the median of the next" in w for w in analysis["warnings"])
        assert any("lucky run" in warning for warning in analysis["warnings"])

    def test_a_lonely_peak_is_called_out(self, optimization_file: Path) -> None:
        analysis = tr.analyze_optimization(tr.parse_optimization(optimization_file))
        assert analysis["plateau"]["checked"] is True
        assert analysis["plateau"]["near_best"] == 1
        assert any("within 10%" in warning for warning in analysis["warnings"])

    def test_a_plateau_does_not_warn(self) -> None:
        rows = [_pass_cells(number, result=11000.0 + number) for number in range(1, 31)]
        analysis = tr.analyze_optimization(
            tr.parse_optimization_text(optimization_xml(rows))
        )
        assert analysis["plateau"]["near_best"] == 30
        assert not any("spike" in warning for warning in analysis["warnings"])
        assert not any("within 10%" in warning for warning in analysis["warnings"])

    def test_a_small_field_is_not_checked_for_a_plateau(self) -> None:
        analysis = tr.analyze_optimization(
            tr.parse_optimization_text(optimization_xml(_sample_rows()[:3]))
        )
        assert analysis["plateau"]["checked"] is False
        assert "why" in analysis["plateau"]

    def test_edge_pinned_inputs_need_the_ranges(
        self, optimization_file: Path, grid_set: Path
    ) -> None:
        result = tr.parse_optimization(optimization_file)
        without = tr.analyze_optimization(result, top=3)
        assert not any("tested range" in warning for warning in without["warnings"])
        analysis = tr.analyze_optimization(
            result, top=3, set_file=tr.parse_set_file(grid_set)
        )
        assert any(
            "InpStopLoss sits at the stop of its tested range (1000)" in warning
            for warning in analysis["warnings"]
        )
        assert analysis["inputs"]["InpStopLoss"]["at_range_stop"] == 3
        assert analysis["inputs"]["InpStopLoss"]["range"] == [200.0, 1000.0]
        assert analysis["inputs"]["InpStopLoss"]["steps"] == 17

    def test_an_input_at_the_start_of_its_range_is_also_flagged(self) -> None:
        rows = [
            _pass_cells(number, fast=5, result=12000.0 - number)
            for number in range(1, 6)
        ]
        analysis = tr.analyze_optimization(
            tr.parse_optimization_text(optimization_xml(rows)),
            top=3,
            set_file=tr.parse_set_text(SET_TEXT),
        )
        assert any(
            "InpFastEMA sits at the start of its tested range (5)" in warning
            for warning in analysis["warnings"]
        )

    def test_the_winners_agreeing_on_an_input_is_a_note(
        self, optimization_file: Path
    ) -> None:
        analysis = tr.analyze_optimization(tr.parse_optimization(optimization_file))
        assert any(
            "every top pass uses InpStopLoss=1000" in note for note in analysis["notes"]
        )

    def test_another_criterion_preferring_another_pass_is_a_note(
        self, optimization_file: Path
    ) -> None:
        analysis = tr.analyze_optimization(tr.parse_optimization(optimization_file))
        assert analysis["criterion"]["ranked_by"] == "result"
        assert analysis["criterion"]["best_by_other_criteria"]["sharpe_ratio"] == {
            "pass": 5,
            "value": 3.5,
            "result": 11050.0,
        }
        assert any("ranked by sharpe_ratio" in note for note in analysis["notes"])

    def test_a_small_report_says_it_is_too_small(self) -> None:
        analysis = tr.analyze_optimization(
            tr.parse_optimization_text(optimization_xml(_sample_rows()[:3]))
        )
        assert any("only 3 pass" in warning for warning in analysis["warnings"])

    def test_filters_can_be_turned_off(self, optimization_file: Path) -> None:
        analysis = tr.analyze_optimization(
            tr.parse_optimization(optimization_file), use_filter=False
        )
        assert analysis["kept"] == 36
        assert analysis["dropped"] == 0
        assert not any("hidden by the filters" in note for note in analysis["notes"])

    def test_the_grid_size_comes_from_the_set(
        self, optimization_file: Path, grid_set: Path
    ) -> None:
        analysis = tr.analyze_optimization(
            tr.parse_optimization(optimization_file),
            set_file=tr.parse_set_file(grid_set),
        )
        assert analysis["grid_combinations"] == 26 * 9 * 17 * 2

    def test_an_absurd_grid_is_a_note(self) -> None:
        huge = tr.SetFile(
            inputs=[
                tr.SetInput("A", "1", "0", "1", "1000", True),
                tr.SetInput("B", "1", "0", "1", "1000", True),
                tr.SetInput("C", "1", "0", "1", "1000", True),
            ]
        )
        rows = [_pass_cells(number) for number in range(1, 4)]
        analysis = tr.analyze_optimization(
            tr.parse_optimization_text(optimization_xml(rows)), set_file=huge
        )
        assert analysis["grid_combinations"] == 1001**3
        assert any("genetic algorithm" in note for note in analysis["notes"])

    def test_statistics_cover_the_survivors(self, optimization_file: Path) -> None:
        analysis = tr.analyze_optimization(tr.parse_optimization(optimization_file))
        assert analysis["statistics"]["profit"]["best"] == 98000.0
        assert analysis["statistics"]["trades"]["count"] == 31

    def test_the_summary_is_readable(self, optimization_file: Path) -> None:
        result = tr.parse_optimization(optimization_file)
        analysis = tr.analyze_optimization(result, top=2)
        summary = tr.format_optimization_for_prompt(result, analysis)
        assert "passes: 36 total, 31 after filters" in summary
        assert "1. pass 37" in summary
        assert "InpStopLoss=1000" in summary
        assert "warnings:" in summary


class TestForwardAnalysis:
    @staticmethod
    def _forward_rows(*, agreeable: bool) -> List[Tuple[str, ...]]:
        """Twelve passes whose forward results agree with, or invert, the back ones."""
        rows: List[Tuple[str, ...]] = []
        for index in range(12):
            back = 20000.0 - index * 1000.0
            forward = back * 0.8 if agreeable else 2000.0 + index * 900.0
            rows.append(
                (
                    str(index + 1),
                    str(back),
                    str(round(forward, 2)),
                    str(round(forward - 10000.0, 2)),
                    "5",
                    "1.4",
                    "2.5",
                    "1.1",
                    "0",
                    "9",
                    "200",
                    str(10 + index),
                )
            )
        return rows

    def test_agreeing_ranks_do_not_warn(self) -> None:
        result = tr.parse_optimization_text(
            optimization_xml(self._forward_rows(agreeable=True), FORWARD_HEADER)
        )
        analysis = tr.analyze_optimization(result, rank_by="back_result")
        assert result.forward is True
        assert analysis["forward"]["passes"] == 12
        assert analysis["forward"]["spearman_back_vs_forward"] == 1.0
        assert analysis["forward"]["best_pass_forward_rank"] == 1
        assert not any("rho" in warning for warning in analysis["warnings"])

    def test_scrambled_ranks_are_called_out(self) -> None:
        result = tr.parse_optimization_text(
            optimization_xml(self._forward_rows(agreeable=False), FORWARD_HEADER)
        )
        analysis = tr.analyze_optimization(
            result, rank_by="back_result", use_filter=False
        )
        forward = analysis["forward"]
        assert forward["spearman_back_vs_forward"] == -1.0
        assert forward["best_pass_forward_rank"] == 12
        assert forward["median_degradation_pct"] > 50.0
        assert any("barely agree" in warning for warning in analysis["warnings"])
        assert any(
            "ranked first in sample but 12 of 12" in warning
            for warning in analysis["warnings"]
        )
        assert any("out of sample" in warning for warning in analysis["warnings"])

    def test_spearman_handles_short_series_and_ties(self) -> None:
        assert tr._spearman([1.0, 2.0], [1.0, 2.0]) is None
        assert tr._spearman([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0
        assert tr._spearman([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == -1.0
        assert tr._spearman([1.0, 1.0, 2.0], [1.0, 1.0, 2.0]) == 1.0


# ---------------------------------------------------------------------------
# .set input files
# ---------------------------------------------------------------------------


class TestSetFiles:
    def test_it_reads_the_mt5_form(self) -> None:
        parsed = tr.parse_set_text(SET_TEXT, "grid.set")
        assert parsed.path == "grid.set"
        assert parsed.names() == [
            "InpFastEMA",
            "InpSlowEMA",
            "InpStopLoss",
            "InpUseFilter",
            "InpLots",
            "InpMagic",
        ]
        fast = parsed.get("InpFastEMA")
        assert fast is not None
        assert (fast.value, fast.start, fast.step, fast.stop) == ("12", "5", "1", "30")
        assert fast.optimize is True
        assert parsed.get("InpLots").optimize is False
        assert parsed.header == ["; saved on 2026.09.30 12:00"]

    def test_step_counts_and_grid_size(self) -> None:
        parsed = tr.parse_set_text(SET_TEXT)
        assert parsed.get("InpFastEMA").step_count == 26
        assert parsed.get("InpSlowEMA").step_count == 9
        assert parsed.get("InpStopLoss").step_count == 17
        assert parsed.get("InpUseFilter").step_count == 2
        assert parsed.get("InpLots").step_count == 1
        assert parsed.total_combinations() == 26 * 9 * 17 * 2

    def test_a_broken_range_has_no_grid_size(self) -> None:
        parsed = tr.parse_set_text("InpX=5||10||1||5||Y\n")
        assert parsed.get("InpX").step_count is None
        assert parsed.total_combinations() is None

    def test_a_zero_step_with_a_single_value_is_one_pass(self) -> None:
        parsed = tr.parse_set_text("InpX=5||5||0||5||Y\n")
        assert parsed.get("InpX").step_count == 1

    def test_plain_name_value_lines_are_fixed_inputs(self) -> None:
        parsed = tr.parse_set_text("InpLots=0.1\nInpUseFilter=true\n")
        assert parsed.get("InpLots").value == "0.1"
        assert parsed.get("InpLots").optimize is False
        assert parsed.get("InpUseFilter").is_bool is True
        assert parsed.get("InpUseFilter").bool_value is True
        assert parsed.total_combinations() is None

    def test_mt4_shaped_rows_are_read_and_flagged(self) -> None:
        parsed = tr.parse_set_text("InpX=5,Y,1,2,9\n")
        item = parsed.get("InpX")
        assert (item.value, item.start, item.step, item.stop) == ("5", "1", "2", "9")
        assert item.optimize is True
        assert any("MT4-shaped" in warning for warning in parsed.warnings)

    def test_an_mt4_range_on_its_own_line_merges(self) -> None:
        parsed = tr.parse_set_text("InpX=5\nInpX,F,1,2,9\n")
        item = parsed.get("InpX")
        assert item.value == "5"
        assert (item.start, item.step, item.stop) == ("1", "2", "9")
        assert item.optimize is False
        assert parsed.unknown == []

    def test_unrecognized_lines_are_kept(self) -> None:
        parsed = tr.parse_set_text("InpX=5\nsomething odd here\n")
        assert parsed.unknown == ["something odd here"]
        assert any("not an input line" in warning for warning in parsed.warnings)
        assert "something odd here" in parsed.to_text()

    def test_a_nameless_line_is_a_warning(self) -> None:
        parsed = tr.parse_set_text("=5||0||0||0||N\n")
        assert parsed.inputs == []
        assert any("no input name" in warning for warning in parsed.warnings)

    def test_extra_fields_are_reported(self) -> None:
        parsed = tr.parse_set_text("InpX=5||1||1||9||Y||surprise\n")
        assert any("extra field" in warning for warning in parsed.warnings)
        assert parsed.get("InpX").optimize is True

    def test_an_empty_file_says_so(self) -> None:
        parsed = tr.parse_set_text("; nothing here\n")
        assert any("no inputs found" in warning for warning in parsed.warnings)

    def test_round_trip_is_stable(self) -> None:
        once = tr.parse_set_text(SET_TEXT)
        twice = tr.parse_set_text(once.to_text())
        assert twice.names() == once.names()
        assert twice.to_text() == once.to_text()

    def test_a_fixed_input_is_written_in_mt5_form(self) -> None:
        parsed = tr.parse_set_text("InpLots=0.1\nInpFlag=true\n")
        assert parsed.to_text() == (
            "InpLots=0.1||0||0||0||N\nInpFlag=true||false||0||true||N\n"
        )

    def test_writing_creates_the_folder(self, tmp_path: Path) -> None:
        target = tmp_path / "profiles" / "winner.set"
        text = tr.write_set_file(tr.parse_set_text(SET_TEXT), target)
        assert target.is_file()
        assert "InpFastEMA=12||5||1||30||Y" in text
        assert "\r\n" in target.read_bytes().decode("utf-8")

    def test_reading_from_disk_decodes_ansi(self, grid_set: Path) -> None:
        assert tr.parse_set_file(grid_set).get("InpSlowEMA").value == "26"

    def test_a_missing_set_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            tr.parse_set_file(tmp_path / "nope.set")

    def test_edge_values_are_recognized(self) -> None:
        item = tr.parse_set_text("InpX=5||5||1||30||Y\n").get("InpX")
        assert item.is_edge_value(5.0) is True
        assert item.is_edge_value(30.0) is True
        assert item.is_edge_value(17.0) is False
        assert item.range_numbers == (5.0, 1.0, 30.0)
        assert tr.parse_set_text("InpX=5\n").get("InpX").is_edge_value(5.0) is None

    def test_to_dict_hides_ranges_for_fixed_inputs(self) -> None:
        payload = tr.parse_set_text(SET_TEXT).to_dict()
        fixed = next(item for item in payload["inputs"] if item["name"] == "InpLots")
        assert "start" not in fixed
        assert payload["optimized"] == [
            "InpFastEMA",
            "InpSlowEMA",
            "InpStopLoss",
            "InpUseFilter",
        ]

    def test_inputs_strings_split_on_any_separator(self) -> None:
        expected = [("InpLots", "0.1"), ("InpFastEMA", "12")]
        assert tr.split_inputs_string("InpLots=0.1,InpFastEMA=12") == expected
        assert tr.split_inputs_string("InpLots=0.1;InpFastEMA=12") == expected
        assert tr.split_inputs_string("InpLots=0.1\nInpFastEMA=12") == expected
        assert tr.split_inputs_string("") == []

    def test_a_set_can_be_built_from_a_testing_report(self, report: Path) -> None:
        parsed = tr.set_from_report(tr.parse_report(report))
        assert parsed.names() == ["InpLots", "InpFastEMA"]
        assert parsed.get("InpLots").value == "0.1"
        assert all(item.optimize is False for item in parsed.inputs)

    def test_a_report_without_inputs_says_so(self) -> None:
        parsed = tr.set_from_report(tr.TesterReport())
        assert parsed.inputs == []
        assert any("no Inputs line" in warning for warning in parsed.warnings)

    def test_values_are_formatted_the_way_mt5_reads_them(self) -> None:
        assert tr._format_set_value(5.0) == "5"
        assert tr._format_set_value(0.1) == "0.1"
        assert tr._format_set_value(0.00001) == "0.00001"
        assert tr._format_set_value(True) == "true"
        assert tr._format_set_value(12) == "12"

    def test_the_profiles_folder_is_where_mt5_looks(self) -> None:
        assert tr.tester_profiles_dir("/data/MT5") == Path(
            "/data/MT5/MQL5/Profiles/Tester"
        )


class TestSetFromPass:
    def test_without_a_template_only_the_optimized_inputs_appear(self) -> None:
        result = tr.parse_optimization_text(optimization_xml(_sample_rows()))
        winner = next(row for row in result.passes if row.number == 37)
        generated = tr.set_from_pass(winner)
        assert generated.names() == [
            "InpFastEMA",
            "InpSlowEMA",
            "InpStopLoss",
            "InpUseFilter",
        ]
        assert "InpFastEMA=5||0||0||0||N" in generated.to_text()
        assert "InpUseFilter=true||false||0||true||N" in generated.to_text()
        assert all(item.optimize is False for item in generated.inputs)

    def test_a_template_carries_the_inputs_the_pass_does_not_list(self) -> None:
        result = tr.parse_optimization_text(optimization_xml(_sample_rows()))
        winner = next(row for row in result.passes if row.number == 37)
        template = tr.parse_set_text(SET_TEXT)
        generated = tr.set_from_pass(winner, template=template)
        assert generated.names() == template.names()
        assert generated.get("InpFastEMA").value == "5"
        assert generated.get("InpStopLoss").value == "1000"
        # Not optimized, so it keeps the value the run used instead of letting
        # the terminal fall back to the EA's compiled default.
        assert generated.get("InpLots").value == "0.10"
        assert generated.get("InpLots").optimize is False

    def test_keep_ranges_leaves_the_grid_intact(self) -> None:
        result = tr.parse_optimization_text(optimization_xml(_sample_rows()))
        winner = next(row for row in result.passes if row.number == 37)
        generated = tr.set_from_pass(
            winner, template=tr.parse_set_text(SET_TEXT), keep_ranges=True
        )
        assert "InpFastEMA=5||5||1||30||Y" in generated.to_text()
        assert generated.total_combinations() == 26 * 9 * 17 * 2

    def test_an_input_the_template_does_not_know_is_added(self) -> None:
        header = OPT_HEADER[:10] + ("InpNewThing",)
        result = tr.parse_optimization_text(
            optimization_xml([_pass_cells(1)[:11]], header=header)
        )
        generated = tr.set_from_pass(
            result.passes[0], template=tr.parse_set_text(SET_TEXT)
        )
        assert "InpNewThing" in generated.names()
        assert any("not in the template" in warning for warning in generated.warnings)

    def test_a_template_input_missing_from_the_pass_is_reported(self) -> None:
        header = OPT_HEADER[:10] + ("InpFastEMA",)
        result = tr.parse_optimization_text(
            optimization_xml([_pass_cells(1)[:11]], header=header)
        )
        generated = tr.set_from_pass(
            result.passes[0], template=tr.parse_set_text(SET_TEXT)
        )
        assert any(
            "InpSlowEMA is not in the optimization report" in warning
            for warning in generated.warnings
        )

    def test_a_pass_without_inputs_says_so(self) -> None:
        result = tr.parse_optimization_text(
            optimization_xml([_pass_cells(1)[:10]], header=OPT_HEADER[:10])
        )
        assert result.parameter_names == []
        generated = tr.set_from_pass(result.passes[0])
        assert generated.inputs == []
        assert any(
            "carries no input values" in warning for warning in generated.warnings
        )


# ---------------------------------------------------------------------------
# The ini warnings, and the report names MT5 actually writes
# ---------------------------------------------------------------------------


class TestIniWarnings:
    def test_a_sane_config_has_no_warnings(self) -> None:
        assert (
            tr.tester_ini_warnings(
                expert="MyEA",
                expert_parameters="MyEA.set",
                optimization=2,
                model=1,
                report="TesterReport",
            )
            == []
        )

    def test_optimization_without_a_set_file_cannot_run(self) -> None:
        warnings = tr.tester_ini_warnings(expert="Sub\\MyEA", optimization=2)
        assert any("cannot optimize at all" in warning for warning in warnings)
        assert any("MyEA.set" in warning for warning in warnings)

    def test_a_set_file_named_by_path_is_not_found(self) -> None:
        warnings = tr.tester_ini_warnings(
            expert="MyEA", expert_parameters="C:\\sets\\grid.set"
        )
        assert any("Profiles\\Tester" in warning for warning in warnings)

    def test_a_set_file_with_the_wrong_extension_is_flagged(self) -> None:
        warnings = tr.tester_ini_warnings(expert_parameters="grid.txt")
        assert any("does not end in .set" in warning for warning in warnings)

    def test_a_report_folder_mt5_will_not_create_is_flagged(self) -> None:
        warnings = tr.tester_ini_warnings(report="reports\\EURUSD")
        assert any("does not create the folder" in warning for warning in warnings)

    def test_real_ticks_are_slow_and_that_is_worth_saying(self) -> None:
        assert any(
            "real ticks" in warning for warning in tr.tester_ini_warnings(model=4)
        )

    def test_visual_optimization_is_flagged(self) -> None:
        warnings = tr.tester_ini_warnings(optimization=2, visual=True)
        assert any("Visual=1" in warning for warning in warnings)

    def test_leaving_the_terminal_open_is_flagged(self) -> None:
        warnings = tr.tester_ini_warnings(shutdown_terminal=False)
        assert any("wait on the report file" in warning for warning in warnings)

    def test_the_new_keys_reach_the_ini(self) -> None:
        text = tr.build_tester_ini(
            expert="MyEA",
            optimization=2,
            forward_mode=1,
            forward_date="2024.04.01",
            use_remote=1,
            use_cloud=0,
            profit_in_pips=0,
        )
        assert "ForwardMode=1" in text
        assert "ForwardDate=2024.04.01" in text
        assert "UseRemote=1" in text
        assert "UseCloud=0" in text
        assert "ProfitInPips=0" in text


class TestReportCandidates:
    def test_the_requested_name_comes_first(self) -> None:
        candidates = tr.report_candidates(Path("/tmp/out/Run.xml"))
        assert candidates[0] == Path("/tmp/out/Run.xml")
        assert Path("/tmp/out/Run.htm") in candidates
        assert Path("/tmp/out/Run.forward.xml") in candidates
        assert len(candidates) == len(set(candidates))

    def test_a_bare_stem_is_expanded_to_every_suffix(self) -> None:
        names = [path.name for path in tr.report_candidates(Path("/tmp/out/Run"))]
        assert names[0] == "Run"
        assert "Run.xml" in names
        assert "Run.forward.htm" in names


class TestRunnerReportExtensions:
    def test_it_finds_the_htm_a_single_test_writes(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "htm")
        target = tmp_path / "Run.xml"
        ini = tr.build_tester_ini(
            expert="MyEA", symbol="EURUSD", report=str(target.with_suffix(""))
        )
        outcome = tr.run_tester(
            terminal=fake_terminal,
            ini_text=ini,
            report_path=target,
            timeout=30,
            poll_interval=0.01,
        )
        assert outcome["report_path"].endswith("Run.htm")
        assert outcome["requested_path"].endswith("Run.xml")
        assert outcome["report"].metrics["profit_factor"] == 1.25

    def test_it_parses_an_optimization_run_as_a_table_of_passes(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "optimization")
        target = tmp_path / "Run.htm"  # asked for .htm; an optimization writes .xml
        ini = tr.build_tester_ini(
            expert="MyEA",
            symbol="EURUSD",
            optimization=2,
            report=str(target.with_suffix("")),
        )
        outcome = tr.run_tester(
            terminal=fake_terminal,
            ini_text=ini,
            report_path=target,
            timeout=30,
            poll_interval=0.01,
            parser=tr.parse_optimization,
        )
        result = outcome["report"]
        assert isinstance(result, tr.OptimizationResult)
        assert len(result.passes) == 2
        assert result.run["exit_code"] == 0
        assert result.parameter_names == ["InpFastEMA", "InpStopLoss"]

    def test_no_report_at_all_still_times_out(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "noreport")
        with pytest.raises(TimeoutError) as error:
            tr.run_tester(
                terminal=fake_terminal,
                ini_text=tr.build_tester_ini(expert="MyEA"),
                report_path=tmp_path / "Run.xml",
                timeout=1,
                poll_interval=0.01,
            )
        assert ".forward variants" in str(error.value)


# ---------------------------------------------------------------------------
# CLI: optimization results and .set files
# ---------------------------------------------------------------------------


class TestOptimizationCli:
    def test_an_optimization_file_is_ranked(self, optimization_file: Path) -> None:
        result = _run(["--optimization-report", str(optimization_file), "--top", "3"])
        assert result.exit_code == 0
        entry = _payload(result)["optimizations"][0]
        assert entry["passes"] == 36
        assert len(entry["analysis"]["top"]) == 3
        assert entry["analysis"]["top"][0]["pass"] == 37

    def test_a_positional_file_is_recognized_by_content(
        self, optimization_file: Path
    ) -> None:
        payload = _payload(_run([str(optimization_file)]))
        assert payload["optimizations"][0]["analysis"]["passes"] == 36
        assert payload["reports"] == []

    def test_rank_by_changes_the_winner(self, optimization_file: Path) -> None:
        result = _run(
            ["--optimization-report", str(optimization_file), "--rank-by", "sharpe"]
        )
        assert _payload(result)["optimizations"][0]["analysis"]["top"][0]["pass"] == 5

    def test_no_filter_keeps_the_junk(self, optimization_file: Path) -> None:
        result = _run(["--optimization-report", str(optimization_file), "--no-filter"])
        assert _payload(result)["optimizations"][0]["analysis"]["kept"] == 36

    def test_no_analysis_just_ranks(self, optimization_file: Path) -> None:
        result = _run(
            ["--optimization-report", str(optimization_file), "--no-analysis"]
        )
        entry = _payload(result)["optimizations"][0]
        assert "analysis" not in entry
        assert len(entry["top"]) == 10

    def test_prompt_adds_a_summary(self, optimization_file: Path) -> None:
        result = _run(["--optimization-report", str(optimization_file), "--prompt"])
        assert "passes: 36 total" in _payload(result)["optimizations"][0]["summary"]

    def test_a_set_file_can_be_read_on_its_own(self, grid_set: Path) -> None:
        result = _run(["--set", str(grid_set)])
        assert result.exit_code == 0
        payload = _payload(result)
        assert payload["sets"][0]["grid_combinations"] == 26 * 9 * 17 * 2
        assert payload["reports"] == []
        assert payload.get("optimizations", []) == []

    def test_set_from_pass_prints_only_the_set(
        self, optimization_file: Path, grid_set: Path
    ) -> None:
        result = _run(
            [
                "--optimization-report",
                str(optimization_file),
                "--set",
                str(grid_set),
                "--set-from-pass",
                "37",
            ]
        )
        assert result.exit_code == 0
        assert result.stdout.startswith("; saved on")
        assert "InpFastEMA=5||0||0||0||N" in result.stdout
        assert "InpMagic=100010" in result.stdout
        assert "{" not in result.stdout  # no JSON mixed into the .set

    def test_keep_ranges_writes_an_optimizable_set(
        self, optimization_file: Path, grid_set: Path
    ) -> None:
        result = _run(
            [
                "--optimization-report",
                str(optimization_file),
                "--set",
                str(grid_set),
                "--set-from-pass",
                "37",
                "--keep-ranges",
            ]
        )
        assert "InpFastEMA=5||5||1||30||Y" in result.stdout

    def test_write_set_saves_the_file_and_reports_it(
        self, optimization_file: Path, grid_set: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "winner.set"
        result = _run(
            [
                "--optimization-report",
                str(optimization_file),
                "--set",
                str(grid_set),
                "--set-from-pass",
                "37",
                "--write-set",
                str(target),
            ]
        )
        assert result.exit_code == 0
        assert target.is_file()
        assert "InpStopLoss=1000" in target.read_text()
        entry = _payload(result)["set_from_pass"]
        assert entry["pass"] == 37
        assert entry["path"] == str(target)
        assert entry["inputs"][0]["name"] == "InpFastEMA"

    def test_an_unknown_pass_number_exits_two(self, optimization_file: Path) -> None:
        result = _run(
            ["--optimization-report", str(optimization_file), "--set-from-pass", "999"]
        )
        assert result.exit_code == 2
        assert "no pass 999" in result.output

    def test_set_from_pass_needs_an_optimization_report(self) -> None:
        result = _run(["--set-from-pass", "3"])
        assert result.exit_code == 2
        assert "needs an optimization report" in result.output

    def test_the_ini_warnings_reach_the_runner(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "optimization")
        monkeypatch.setattr(tr, "find_terminal", lambda explicit=None: fake_terminal)
        result = _run(
            [
                "--run",
                "--expert",
                "MyEA",
                "--symbol",
                "EURUSD",
                "--optimization",
                "2",
                "--out-report",
                str(tmp_path / "Run.xml"),
            ]
        )
        assert result.exit_code == 0
        assert "cannot optimize at all" in result.output
        assert _payload(result)["optimizations"][0]["passes"] == 2

    def test_help_documents_the_new_options(self) -> None:
        result = _run(["--help"])
        for option in (
            "--optimization-report",
            "--rank-by",
            "--top",
            "--no-filter",
            "--no-analysis",
            "--set",
            "--set-from-pass",
            "--write-set",
            "--keep-ranges",
            "--forward-mode",
            "--forward-date",
        ):
            assert option in result.output


# ---------------------------------------------------------------------------
# Runner: waiting for the terminal to exit after the report lands
# ---------------------------------------------------------------------------


class _LateProcess:
    """Stands in for Popen: exits ``delay`` seconds after it was created."""

    def __init__(self, delay: float, code: int = 0) -> None:
        self._deadline = time.monotonic() + delay
        self._code = code
        self.returncode: Any = None
        self.killed = False

    def poll(self) -> Any:
        if self.returncode is None and time.monotonic() >= self._deadline:
            self.returncode = self._code
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _late_runner(report: Path, delay: float, code: int = 0) -> Any:
    """Write the report when launched, then hand back a process that lingers."""

    def launch(command: Any) -> _LateProcess:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(HTML_REPORT, encoding="utf-8")
        return _LateProcess(delay, code)

    return launch


class TestRunnerProcessGrace:
    """The terminal writes the report before it exits, so wait a little."""

    def test_it_waits_for_the_terminal_to_exit_so_the_code_is_not_none(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "Run.xml"
        outcome = tr.run_tester(
            terminal=tmp_path / "terminal64",
            ini_text=tr.build_tester_ini(expert="MyEA", report=str(target)),
            report_path=target,
            timeout=10,
            poll_interval=0.01,
            runner=_late_runner(target, delay=0.3),
        )
        assert outcome["exit_code"] == 0
        assert outcome["report"].run["exit_code"] == 0

    def test_a_nonzero_code_arriving_late_still_warns(self, tmp_path: Path) -> None:
        target = tmp_path / "Run.xml"
        outcome = tr.run_tester(
            terminal=tmp_path / "terminal64",
            ini_text=tr.build_tester_ini(expert="MyEA", report=str(target)),
            report_path=target,
            timeout=10,
            poll_interval=0.01,
            runner=_late_runner(target, delay=0.3, code=3),
        )
        assert outcome["exit_code"] == 3
        assert any("exited with code 3" in w for w in outcome["report"].warnings)

    def test_process_grace_zero_keeps_the_old_behaviour(self, tmp_path: Path) -> None:
        """No grace window: the code is None, and that is not treated as a failure."""
        target = tmp_path / "Run.xml"
        outcome = tr.run_tester(
            terminal=tmp_path / "terminal64",
            ini_text=tr.build_tester_ini(expert="MyEA", report=str(target)),
            report_path=target,
            timeout=10,
            poll_interval=0.01,
            runner=_late_runner(target, delay=0.3),
            process_grace=0,
        )
        assert outcome["exit_code"] is None
        # HTML_REPORT carries an undefined XML entity, so a warning is expected —
        # what must not appear is one blaming the exit code.
        assert not any("exit" in warning for warning in outcome["report"].warnings)

    def test_a_terminal_that_never_exits_is_not_waited_on_forever(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "Run.xml"
        started = time.monotonic()
        outcome = tr.run_tester(
            terminal=tmp_path / "terminal64",
            ini_text=tr.build_tester_ini(expert="MyEA", report=str(target)),
            report_path=target,
            timeout=10,
            poll_interval=0.01,
            runner=_late_runner(target, delay=3600),
            process_grace=0.2,
        )
        assert outcome["exit_code"] is None
        assert time.monotonic() - started < 5


# ---------------------------------------------------------------------------
# Forward checks: back half against the half the search never saw
# ---------------------------------------------------------------------------


FORWARD_BACK_HTML = """<html><body><table>
<tr><td>Period</td><td>2022.01.01 - 2022.12.31</td></tr>
<tr><td>History Quality</td><td>100%</td></tr>
<tr><td>Total Net Profit</td><td>12 000.00</td></tr>
<tr><td>Gross Profit</td><td>27 000.00</td></tr>
<tr><td>Profit Factor</td><td>1.80</td></tr>
<tr><td>Recovery Factor</td><td>2.50</td></tr>
<tr><td>Sharpe Ratio</td><td>1.40</td></tr>
<tr><td>Expected Payoff</td><td>4.20</td></tr>
<tr><td>Total Trades</td><td>480</td></tr>
<tr><td>Equity Drawdown Maximal</td><td>4 800.00 (12.00%)</td></tr>
</table></body></html>"""

# The same edge, weaker: nothing flips, nothing degrades past 50%.
FORWARD_MILD_HTML = """<html><body><table>
<tr><td>Period</td><td>2023.01.01 - 2023.03.31</td></tr>
<tr><td>History Quality</td><td>100%</td></tr>
<tr><td>Total Net Profit</td><td>2 500.00</td></tr>
<tr><td>Gross Profit</td><td>9 400.00</td></tr>
<tr><td>Profit Factor</td><td>1.36</td></tr>
<tr><td>Recovery Factor</td><td>1.90</td></tr>
<tr><td>Sharpe Ratio</td><td>1.00</td></tr>
<tr><td>Expected Payoff</td><td>3.10</td></tr>
<tr><td>Total Trades</td><td>110</td></tr>
<tr><td>Equity Drawdown Maximal</td><td>1 300.00 (21.00%)</td></tr>
</table></body></html>"""

# The edge is gone: profit factor under 1 and a loss.
FORWARD_FLIP_HTML = """<html><body><table>
<tr><td>Period</td><td>2023.01.01 - 2023.03.31</td></tr>
<tr><td>History Quality</td><td>88%</td></tr>
<tr><td>Total Net Profit</td><td>-400.00</td></tr>
<tr><td>Profit Factor</td><td>0.82</td></tr>
<tr><td>Recovery Factor</td><td>0.20</td></tr>
<tr><td>Sharpe Ratio</td><td>-0.20</td></tr>
<tr><td>Expected Payoff</td><td>-1.10</td></tr>
<tr><td>Total Trades</td><td>140</td></tr>
<tr><td>Equity Drawdown Maximal</td><td>2 000.00 (18.00%)</td></tr>
</table></body></html>"""


@pytest.fixture()
def forward_files(tmp_path: Path) -> Tuple[Path, Path, Path]:
    """Back half, a forward half that holds, and one that does not."""
    back = tmp_path / "Run.htm"
    mild = tmp_path / "Run.mild.forward.htm"
    flip = tmp_path / "Run.flip.forward.htm"
    back.write_text(FORWARD_BACK_HTML, encoding="utf-8")
    mild.write_text(FORWARD_MILD_HTML, encoding="utf-8")
    flip.write_text(FORWARD_FLIP_HTML, encoding="utf-8")
    return back, mild, flip


def _report(source: str, **metrics: Any) -> Any:
    return tr.TesterReport(source=source, metrics=dict(metrics))


class TestForwardCompanion:
    def test_it_names_the_forward_half_of_a_testing_report(self) -> None:
        names = [path.name for path in tr.forward_companion(Path("Run.htm"))]
        assert names == ["Run.forward.htm", "Run.forward.xml", "Run.forward.html"]

    def test_it_names_the_back_half_of_a_forward_report(self) -> None:
        names = [path.name for path in tr.forward_companion(Path("Run.forward.htm"))]
        assert names[0] == "Run.htm"
        assert "Run.xml" in names

    def test_it_keeps_the_directory(self, tmp_path: Path) -> None:
        found = tr.forward_companion(tmp_path / "out" / "Run.xml")
        assert all(path.parent == tmp_path / "out" for path in found)

    def test_a_forward_file_is_recognized_by_name(self) -> None:
        assert tr.is_forward_report("reports/Run.forward.xml")
        assert not tr.is_forward_report("reports/Run.xml")
        assert not tr.is_forward_report("reports/Run.htm")


class TestPeriodDays:
    def test_it_reads_the_span_from_both_dates(self) -> None:
        report = _report("r", from_date="2022.01.01", to_date="2022.12.31")
        assert tr.period_days(report) == 364.0

    def test_it_returns_none_when_a_date_is_missing(self) -> None:
        assert tr.period_days(_report("r", from_date="2022.01.01")) is None

    def test_it_returns_none_when_the_dates_are_not_dates(self) -> None:
        report = _report("r", from_date="last year", to_date="yesterday")
        assert tr.period_days(report) is None

    def test_a_single_day_is_none_rather_than_zero(self) -> None:
        """Zero days would divide by zero in the per-day figures."""
        report = _report("r", from_date="2022.01.01", to_date="2022.01.01")
        assert tr.period_days(report) is None


class TestCheckForward:
    def test_no_forward_report_is_inconclusive_and_says_why(self) -> None:
        check = tr.check_forward(_report("back", profit_factor=1.8))
        assert check.available is False
        assert check.verdict == "inconclusive"
        assert "no forward report" in check.reasons[0]

    def test_a_surviving_edge_holds_up(self) -> None:
        back = _report(
            "back",
            from_date="2022.01.01",
            to_date="2022.12.31",
            net_profit=12000.0,
            profit_factor=1.8,
            sharpe_ratio=1.4,
            total_trades=480,
        )
        forward = _report(
            "forward",
            from_date="2023.01.01",
            to_date="2023.03.31",
            net_profit=2500.0,
            profit_factor=1.36,
            sharpe_ratio=1.0,
            total_trades=110,
        )
        check = tr.check_forward(back, forward)
        assert check.verdict == "holds_up"
        assert check.available is True
        assert check.back_days == 364.0
        assert check.forward_days == 89.0
        assert "evidence, not proof" in check.reasons[-1]

    def test_a_profit_factor_that_crosses_one_degrades(self) -> None:
        check = tr.check_forward(
            _report("back", profit_factor=1.8, total_trades=480),
            _report("forward", profit_factor=0.82, total_trades=140),
        )
        assert check.verdict == "degrades"
        assert any("profit factor fell from 1.8 to 0.82" in r for r in check.reasons)

    def test_a_net_profit_that_crosses_zero_degrades(self) -> None:
        check = tr.check_forward(
            _report("back", net_profit=5000.0, profit_factor=1.5, total_trades=200),
            _report("forward", net_profit=-300.0, profit_factor=1.2, total_trades=90),
        )
        assert check.verdict == "degrades"
        assert any("net profit fell" in reason for reason in check.reasons)

    def test_a_flip_is_not_also_reported_as_degradation(self) -> None:
        """One failure, one reason: the flip already says the ratio collapsed."""
        check = tr.check_forward(
            _report(
                "back",
                from_date="2022.01.01",
                to_date="2022.12.31",
                net_profit=12000.0,
                profit_factor=1.8,
                total_trades=480,
            ),
            _report(
                "forward",
                from_date="2023.01.01",
                to_date="2023.03.31",
                net_profit=-400.0,
                profit_factor=0.82,
                total_trades=140,
            ),
        )
        profit_factor_reasons = [
            reason for reason in check.reasons if "profit_factor" in reason
        ]
        assert not profit_factor_reasons, "the flip covers it"
        assert len([r for r in check.reasons if "profit factor fell" in r]) == 1
        net_profit_reasons = [r for r in check.reasons if "net profit" in r]
        assert len(net_profit_reasons) == 1

    def test_degradation_past_the_threshold_degrades(self) -> None:
        """60% off the profit factor, still above 1 — no flip, just decay."""
        check = tr.check_forward(
            _report("back", profit_factor=3.0, sharpe_ratio=2.0, total_trades=400),
            _report("forward", profit_factor=1.2, sharpe_ratio=1.9, total_trades=200),
        )
        assert check.verdict == "degrades"
        assert any("profit_factor degraded 60" in reason for reason in check.reasons)

    def test_degradation_under_the_threshold_holds_up(self) -> None:
        check = tr.check_forward(
            _report("back", profit_factor=2.0, sharpe_ratio=2.0, total_trades=400),
            _report("forward", profit_factor=1.2, sharpe_ratio=1.9, total_trades=200),
        )
        assert check.verdict == "holds_up"
        assert check.rows[0]["degradation_pct"] == 40.0

    def test_the_degradation_threshold_is_the_callers(self) -> None:
        back = _report("back", profit_factor=2.0, total_trades=400)
        forward = _report("forward", profit_factor=1.2, total_trades=200)
        assert tr.check_forward(back, forward, max_degradation_pct=90).verdict == (
            "holds_up"
        )
        assert tr.check_forward(back, forward, max_degradation_pct=10).verdict == (
            "degrades"
        )

    def test_a_thin_forward_half_is_inconclusive(self) -> None:
        check = tr.check_forward(
            _report("back", profit_factor=1.8, total_trades=480),
            _report("forward", profit_factor=2.4, total_trades=9),
        )
        assert check.verdict == "inconclusive"
        assert "forward half traded 9 time(s)" in check.reasons[0]

    def test_a_thin_back_half_is_inconclusive_too(self) -> None:
        check = tr.check_forward(
            _report("back", profit_factor=1.8, total_trades=12),
            _report("forward", profit_factor=1.7, total_trades=140),
        )
        assert check.verdict == "inconclusive"
        assert any("too thin to optimize on" in reason for reason in check.reasons)

    def test_min_trades_is_the_callers(self) -> None:
        back = _report("back", profit_factor=1.8, total_trades=20)
        forward = _report("forward", profit_factor=1.7, total_trades=18)
        assert tr.check_forward(back, forward, min_trades=10).verdict == "holds_up"
        assert tr.check_forward(back, forward, min_trades=30).verdict == (
            "inconclusive"
        )

    def test_a_degrades_verdict_wins_over_a_thin_sample(self) -> None:
        """A sign flip is a finding even on few trades; thinness is not an alibi."""
        check = tr.check_forward(
            _report("back", profit_factor=1.8, total_trades=480),
            _report("forward", profit_factor=0.7, total_trades=12),
        )
        assert check.verdict == "degrades"

    def test_money_is_normalized_per_day(self) -> None:
        """A quarter earning 2 500 is not worse than a year earning 12 000."""
        check = tr.check_forward(
            _report(
                "back",
                from_date="2022.01.01",
                to_date="2022.12.31",
                net_profit=12000.0,
                total_trades=480,
                profit_factor=1.8,
            ),
            _report(
                "forward",
                from_date="2023.01.01",
                to_date="2023.03.31",
                net_profit=2500.0,
                total_trades=110,
                profit_factor=1.36,
            ),
        )
        assert check.per_day["net_profit"]["back"] == 32.967
        assert check.per_day["net_profit"]["forward"] == 28.0899
        assert check.per_day["net_profit"]["degradation_pct"] == 14.79
        # Trade counts are activity, not a verdict: no degradation figure.
        assert "degradation_pct" not in check.per_day["total_trades"]

    def test_per_day_degradation_can_be_the_finding(self) -> None:
        check = tr.check_forward(
            _report(
                "back",
                from_date="2022.01.01",
                to_date="2022.12.31",
                net_profit=12000.0,
                profit_factor=1.8,
                total_trades=480,
            ),
            _report(
                "forward",
                from_date="2023.01.01",
                to_date="2023.03.31",
                net_profit=400.0,
                profit_factor=1.36,
                total_trades=110,
            ),
        )
        assert check.verdict == "degrades"
        assert any("net profit per day fell" in reason for reason in check.reasons)

    def test_without_dates_it_warns_instead_of_comparing_raw_profit(self) -> None:
        check = tr.check_forward(
            _report("back", net_profit=12000.0, profit_factor=1.8, total_trades=480),
            _report("forward", net_profit=2500.0, profit_factor=1.36, total_trades=110),
        )
        assert check.per_day == {}
        assert any("not normalized" in warning for warning in check.warnings)
        assert check.verdict == "holds_up"

    def test_a_forward_half_longer_than_the_back_warns(self) -> None:
        check = tr.check_forward(
            _report(
                "back",
                from_date="2023.01.01",
                to_date="2023.02.01",
                profit_factor=1.8,
                total_trades=480,
            ),
            _report(
                "forward",
                from_date="2022.01.01",
                to_date="2022.12.31",
                profit_factor=1.7,
                total_trades=1100,
            ),
        )
        assert any("longer than the back half" in w for w in check.warnings)

    def test_low_history_quality_in_either_half_warns(self) -> None:
        check = tr.check_forward(
            _report(
                "back", history_quality_pct=100.0, profit_factor=1.8, total_trades=480
            ),
            _report(
                "forward", history_quality_pct=71.0, profit_factor=1.7, total_trades=110
            ),
        )
        assert any("forward half history quality is 71%" in w for w in check.warnings)

    def test_a_growing_drawdown_warns_without_changing_the_verdict(self) -> None:
        check = tr.check_forward(
            _report(
                "back",
                equity_drawdown_relative_pct=12.0,
                profit_factor=1.8,
                total_trades=480,
            ),
            _report(
                "forward",
                equity_drawdown_relative_pct=21.0,
                profit_factor=1.7,
                total_trades=110,
            ),
        )
        assert any("1.8x the back one" in warning for warning in check.warnings)
        assert check.verdict == "holds_up"

    def test_an_improving_ratio_reads_as_a_negative_degradation(self) -> None:
        check = tr.check_forward(
            _report("back", profit_factor=1.4, total_trades=480),
            _report("forward", profit_factor=1.9, total_trades=200),
        )
        row = next(r for r in check.rows if r["metric"] == "profit_factor")
        assert row["degradation_pct"] < 0
        assert check.verdict == "holds_up"

    def test_a_growing_drawdown_counts_as_degradation_on_its_own_row(self) -> None:
        """Drawdown is lower-is-better, so growth is the positive percentage."""
        check = tr.check_forward(
            _report(
                "back", equity_drawdown_pct=10.0, profit_factor=1.8, total_trades=480
            ),
            _report(
                "forward", equity_drawdown_pct=18.0, profit_factor=1.7, total_trades=110
            ),
        )
        row = next(r for r in check.rows if r["metric"] == "equity_drawdown_pct")
        assert row["degradation_pct"] == 80.0

    def test_reports_with_nothing_in_common_are_inconclusive(self) -> None:
        check = tr.check_forward(
            _report("back", initial_deposit=10000.0),
            _report("forward", currency="USD"),
        )
        assert check.verdict == "inconclusive"
        assert any("no metric in common" in reason for reason in check.reasons)

    def test_the_compared_keys_can_be_restricted(self) -> None:
        check = tr.check_forward(
            _report("back", profit_factor=1.8, sharpe_ratio=1.4, total_trades=480),
            _report("forward", profit_factor=0.5, sharpe_ratio=1.3, total_trades=110),
            keys=("sharpe_ratio",),
        )
        assert [row["metric"] for row in check.rows] == ["sharpe_ratio"]

    def test_to_dict_carries_the_verdict_rows_and_thresholds(self) -> None:
        check = tr.check_forward(
            _report("back", profit_factor=1.8, total_trades=480),
            _report("forward", profit_factor=1.7, total_trades=110),
            min_trades=25,
            max_degradation_pct=40.0,
        )
        payload = check.to_dict()
        assert payload["verdict"] == "holds_up"
        assert payload["thresholds"] == {
            "min_trades": 25,
            "max_degradation_pct": 40.0,
        }
        assert payload["metrics"][0]["metric"] == "profit_factor"
        assert payload["available"] is True


class TestFormatForwardForPrompt:
    def test_it_leads_with_the_verdict(self) -> None:
        check = tr.check_forward(
            _report("back", profit_factor=1.8, total_trades=480),
            _report("forward", profit_factor=1.7, total_trades=110),
        )
        assert tr.format_forward_for_prompt(check).splitlines()[0] == (
            "forward check: holds_up"
        )

    def test_it_says_better_when_a_metric_improved(self) -> None:
        check = tr.check_forward(
            _report(
                "back",
                from_date="2022.01.01",
                to_date="2022.12.31",
                profit_factor=1.4,
                net_profit=1000.0,
                total_trades=480,
            ),
            _report(
                "forward",
                from_date="2023.01.01",
                to_date="2023.03.31",
                profit_factor=1.9,
                net_profit=900.0,
                total_trades=110,
            ),
        )
        summary = tr.format_forward_for_prompt(check)
        assert "better)" in summary
        assert "worse)" not in summary

    def test_an_unavailable_check_prints_only_its_reason(self) -> None:
        summary = tr.format_forward_for_prompt(
            tr.check_forward(_report("back", profit_factor=1.8))
        )
        assert summary.startswith("forward check: inconclusive")
        assert "no forward report" in summary
        assert "reasons:" in summary

    def test_it_lists_reasons_and_warnings(self) -> None:
        check = tr.check_forward(
            _report(
                "back", profit_factor=1.8, total_trades=480, history_quality_pct=100.0
            ),
            _report(
                "forward",
                profit_factor=0.82,
                total_trades=140,
                history_quality_pct=60.0,
            ),
        )
        summary = tr.format_forward_for_prompt(check)
        assert "reasons:" in summary
        assert "warnings:" in summary
        assert "history quality is 60%" in summary

    def test_max_lines_is_respected(self) -> None:
        check = tr.check_forward(
            _report("back", profit_factor=1.8, total_trades=480),
            _report("forward", profit_factor=1.7, total_trades=110),
        )
        assert len(tr.format_forward_for_prompt(check, max_lines=3).splitlines()) == 3


class TestRunnerForward:
    def test_it_returns_the_back_half_and_collects_the_forward_one(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "forward")
        target = tmp_path / "Run.xml"
        ini = tr.build_tester_ini(
            expert="MyEA",
            symbol="EURUSD",
            forward_mode=1,
            report=str(target.with_suffix("")),
        )
        outcome = tr.run_tester(
            terminal=fake_terminal,
            ini_text=ini,
            report_path=target,
            timeout=30,
            wine=False,
            poll_interval=0.01,
        )
        assert Path(outcome["report_path"]).name == "Run.htm"
        assert Path(outcome["forward_path"]).name == "Run.forward.htm"
        assert outcome["report"].metrics["profit_factor"] == 1.55
        assert outcome["forward_report"].metrics["profit_factor"] == 0.91

    def test_forward_mode_in_the_ini_is_enough(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No forward= argument: the runner reads ForwardMode out of the ini."""
        monkeypatch.setenv("FAKE_MODE", "forward")
        target = tmp_path / "Run.xml"
        ini = tr.build_tester_ini(
            expert="MyEA", forward_mode=2, report=str(target.with_suffix(""))
        )
        outcome = tr.run_tester(
            terminal=fake_terminal,
            ini_text=ini,
            report_path=target,
            timeout=30,
            wine=False,
            poll_interval=0.01,
        )
        assert "forward_report" in outcome

    def test_the_forward_report_carries_the_run_metadata(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "forward")
        target = tmp_path / "Run.xml"
        outcome = tr.run_tester(
            terminal=fake_terminal,
            ini_text=tr.build_tester_ini(
                expert="MyEA",
                forward_mode=1,
                report=str(target.with_suffix("")),
            ),
            report_path=target,
            timeout=30,
            wine=False,
            poll_interval=0.01,
        )
        forward = outcome["forward_report"]
        assert forward.run["exit_code"] == 0
        assert forward.run["report_path"] == outcome["forward_path"]

    def test_a_forward_run_that_writes_one_file_explains_itself(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ForwardMode set, one file written: a note, not a timeout."""
        monkeypatch.setenv("FAKE_MODE", "htm")
        target = tmp_path / "Run.xml"
        outcome = tr.run_tester(
            terminal=fake_terminal,
            ini_text=tr.build_tester_ini(
                expert="MyEA",
                forward_mode=1,
                report=str(target.with_suffix("")),
            ),
            report_path=target,
            timeout=30,
            wine=False,
            poll_interval=0.01,
            forward_grace=0.3,
        )
        assert "forward_report" not in outcome
        assert "Back Result" in outcome["forward_note"]

    def test_forward_off_ignores_the_second_file(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "forward")
        target = tmp_path / "Run.xml"
        outcome = tr.run_tester(
            terminal=fake_terminal,
            ini_text=tr.build_tester_ini(
                expert="MyEA",
                report=str(target.with_suffix("")),
            ),
            report_path=target,
            timeout=30,
            wine=False,
            poll_interval=0.01,
            forward=False,
        )
        assert "forward_report" not in outcome
        assert "forward_note" not in outcome

    def test_the_ini_reader_finds_a_key(self) -> None:
        ini = tr.build_tester_ini(expert="MyEA", forward_mode=1)
        assert tr._ini_value(ini, "ForwardMode") == "1"
        assert tr._ini_value(ini, "Expert") == "MyEA"
        assert tr._ini_value(ini, "NotThere") is None


class TestForwardCli:
    def test_a_forward_report_adds_a_check_block(self, forward_files: Any) -> None:
        back, mild, _flip = forward_files
        result = _run(["--report", str(back), "--forward-report", str(mild)])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        checks = payload["forward_checks"]
        assert len(checks) == 1
        assert checks[0]["verdict"] == "holds_up"
        assert checks[0]["per_day"]["net_profit"]["degradation_pct"] == 14.79

    def test_a_flipped_forward_half_is_reported_as_degrades(
        self, forward_files: Any
    ) -> None:
        back, _mild, flip = forward_files
        result = _run(["--report", str(back), "--forward-report", str(flip)])
        payload = json.loads(result.stdout)
        assert payload["forward_checks"][0]["verdict"] == "degrades"
        assert "[forward] verdict: degrades" in result.output

    def test_the_prompt_summary_is_included(self, forward_files: Any) -> None:
        back, mild, _flip = forward_files
        result = _run(
            ["--report", str(back), "--forward-report", str(mild), "--prompt"]
        )
        summary = json.loads(result.stdout)["forward_checks"][0]["summary"]
        assert summary.startswith("forward check: holds_up")

    def test_min_forward_trades_reaches_the_check(self, forward_files: Any) -> None:
        back, mild, _flip = forward_files
        result = _run(
            [
                "--report",
                str(back),
                "--forward-report",
                str(mild),
                "--min-forward-trades",
                "500",
            ]
        )
        check = json.loads(result.stdout)["forward_checks"][0]
        assert check["verdict"] == "inconclusive"
        assert check["thresholds"]["min_trades"] == 500

    def test_max_degradation_pct_reaches_the_check(self, forward_files: Any) -> None:
        """24% off the profit factor: fine by default, a failure at 10%."""
        back, mild, _flip = forward_files
        default = json.loads(
            _run(["--report", str(back), "--forward-report", str(mild)]).stdout
        )["forward_checks"][0]
        assert default["verdict"] == "holds_up"
        strict = json.loads(
            _run(
                [
                    "--report",
                    str(back),
                    "--forward-report",
                    str(mild),
                    "--max-degradation-pct",
                    "10",
                ]
            ).stdout
        )["forward_checks"][0]
        assert strict["verdict"] == "degrades"
        assert strict["thresholds"]["max_degradation_pct"] == 10.0

    def test_mismatched_counts_are_an_error(self, forward_files: Any) -> None:
        back, mild, flip = forward_files
        result = _run(
            [
                "--report",
                str(back),
                "--forward-report",
                str(mild),
                "--forward-report",
                str(flip),
            ]
        )
        assert result.exit_code == 2
        assert "one file per back-half report" in result.output

    def test_an_optimization_table_as_the_forward_half_is_refused(
        self, forward_files: Any, tmp_path: Path
    ) -> None:
        back, _mild, _flip = forward_files
        table = tmp_path / "opt.xml"
        table.write_text(
            '<?xml version="1.0"?>\n<Table><Row>'
            + "".join(f"<Cell>{cell}</Cell>" for cell in OPT_HEADER)
            + "</Row></Table>\n",
            encoding="utf-8",
        )
        result = _run(["--report", str(back), "--forward-report", str(table)])
        assert result.exit_code == 2
        assert "Back Result" in result.output

    def test_help_documents_the_forward_options(self) -> None:
        result = _run(["--help"])
        assert result.exit_code == 0
        for option in (
            "--forward-report",
            "--min-forward-trades",
            "--max-degradation-pct",
        ):
            assert option in result.output

    def test_a_forward_run_from_the_cli_checks_both_halves(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "forward")
        monkeypatch.setattr(tr, "find_terminal", lambda explicit=None: fake_terminal)
        result = _run(
            [
                "--run",
                "--expert",
                "MyEA",
                "--symbol",
                "EURUSD",
                "--forward-mode",
                "1",
                "--out-report",
                str(tmp_path / "Run.xml"),
            ]
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["reports"][0]["metrics"]["profit_factor"] == 1.55
        assert payload["forward_checks"][0]["verdict"] == "degrades"
        assert "[forward] verdict: degrades" in result.output
