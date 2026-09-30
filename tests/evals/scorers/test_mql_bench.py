"""Tests for the mql-bench scorer.

Two properties matter and are both covered here:

* **Self-consistency** — every reference answer shipped with the dataset must
  score as correct. A benchmark whose own references fail is measuring noise.
* **MQL4 detection** — the highest-signal check in this benchmark is that MQL5
  answers contain no MQL4-only idioms, and that idioms mentioned inside
  comments or strings are *not* counted.
"""

from __future__ import annotations

import pytest

from openjarvis.evals.core.types import EvalRecord
from openjarvis.evals.datasets.mql_bench import MQLBenchDataset
from openjarvis.evals.scorers.mql_bench import (
    MQLBenchScorer,
    extract_mql_source,
    find_mql4_isms,
    strip_comments_and_strings,
)


@pytest.fixture(scope="module")
def records() -> dict[str, EvalRecord]:
    ds = MQLBenchDataset()
    ds.load()
    return {r.metadata["task_id"]: r for r in ds.iter_records()}


@pytest.fixture()
def scorer() -> MQLBenchScorer:
    return MQLBenchScorer()


def _fenced(code: str, tag: str = "mql5") -> str:
    return f"Here you go:\n```{tag}\n{code}\n```\n"


class TestReferenceAnswersPass:
    @pytest.mark.parametrize(
        "task_id",
        sorted(
            {
                "risk-lot-size",
                "new-bar-guard",
                "trailing-stop",
                "safe-indicator-read",
                "port-ordersend",
                "count-own-positions",
                "daily-loss-limit",
                "atr-stop",
                "partial-close",
                "input-validation",
                "trade-transaction-log",
                "entry-filters",
            }
        ),
    )
    def test_reference_is_correct(
        self, scorer: MQLBenchScorer, records: dict[str, EvalRecord], task_id: str
    ) -> None:
        record = records[task_id]
        is_correct, meta = scorer.score(record, _fenced(record.reference))
        assert meta["missing_required"] == []
        assert meta["mql4_isms"] == []
        assert is_correct is True
        assert meta["score"] >= 0.85


class TestMql4Detection:
    def test_mql4_answer_fails_and_is_flagged(
        self, scorer: MQLBenchScorer, records: dict[str, EvalRecord]
    ) -> None:
        answer = _fenced(
            """
double CalcLotByRisk(double sl_points)
  {
   double risk_money = AccountBalance() * InpRiskPercent / 100.0;
   double tick_value = MarketInfo(Symbol(), MODE_TICKVALUE);
   return(NormalizeDouble(risk_money / (sl_points * tick_value), 2));
  }
""",
            tag="mql4",
        )
        is_correct, meta = scorer.score(records["risk-lot-size"], answer)
        assert is_correct is False
        assert meta["mql4_ism_count"] >= 3
        labels = " ".join(hit["label"] for hit in meta["mql4_isms"])
        assert "account function" in labels
        assert "MarketInfo" in labels
        assert meta["missing_required"]

    def test_bare_predefined_variables_are_flagged(self) -> None:
        code = strip_comments_and_strings(
            "double p = Ask; double s = Bid; double d = Point; int g = Digits;"
        )
        labels = " ".join(hit["label"] for hit in find_mql4_isms(code))
        assert "predefined Ask/Bid" in labels
        assert "predefined Point/Digits" in labels

    def test_mql5_equivalents_are_not_flagged(self) -> None:
        code = strip_comments_and_strings(
            """
double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
double point = _Point;
int digits = _Digits;
int bars = Bars(_Symbol, _Period);
double close = iClose(_Symbol, _Period, 1);
trade.OrderDelete(ticket);
bool ok = OrderSend(request, result);
"""
        )
        assert find_mql4_isms(code) == []

    def test_mql4_ordersend_arity_is_flagged(self) -> None:
        code = strip_comments_and_strings(
            'int t = OrderSend(Symbol(), OP_BUY, lots, price, 3, sl, tp, "ea", '
            "magic, 0, clrBlue);"
        )
        labels = " ".join(hit["label"] for hit in find_mql4_isms(code))
        assert "OrderSend argument list" in labels

    def test_mql5_ordersend_two_args_is_not_flagged(self) -> None:
        code = strip_comments_and_strings(
            """
MqlTradeRequest request = {};
MqlTradeResult  result = {};
if(!OrderSend(request, result))
   PrintFormat("retcode=%d", result.retcode);
"""
        )
        assert find_mql4_isms(code) == []

    def test_series_arrays_are_flagged(self) -> None:
        code = strip_comments_and_strings("double c = Close[1]; datetime t = Time[0];")
        hits = find_mql4_isms(code)
        assert any("series array" in hit["label"] for hit in hits)

    def test_idioms_in_comments_and_strings_are_ignored(self) -> None:
        code = strip_comments_and_strings(
            """
// MQL5 has no predefined Ask or Bid, no OrderSelect, no AccountBalance().
#property description "Unlike MQL4, Close[1] does not exist here"
Print("never call MarketInfo() or OrderClose() in MQL5");
double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
"""
        )
        assert find_mql4_isms(code) == []

    def test_safety_floor_at_three_hits(
        self, scorer: MQLBenchScorer, records: dict[str, EvalRecord]
    ) -> None:
        answer = _fenced(
            """
double Lots()
  {
   double equity = AccountEquity();
   double spread = MarketInfo(Symbol(), MODE_SPREAD);
   if(!IsTradeAllowed()) return(0.0);
   int ticket = OrderSelect(0, SELECT_BY_POS);
   OrderClose(ticket, OrderLots(), Bid, 3);
   return(AccountBalance() * 0.01);
  }
"""
        )
        _is_correct, meta = scorer.score(records["risk-lot-size"], answer)
        assert meta["safety_score"] == 0.0


class TestRequiredChecks:
    def test_regex_checks_are_supported(
        self, scorer: MQLBenchScorer, records: dict[str, EvalRecord]
    ) -> None:
        # `re:(iTime\(|CopyTime\()` must accept either spelling.
        via_copytime = _fenced(
            """
datetime g_last = 0;
bool IsNewBar()
  {
   datetime t[];
   if(CopyTime(_Symbol, PERIOD_CURRENT, 0, 1, t) != 1)
      return(false);
   if(t[0] == g_last)
      return(false);
   g_last = t[0];
   return(true);
  }
"""
        )
        is_correct, meta = scorer.score(records["new-bar-guard"], via_copytime)
        assert is_correct is True, meta["missing_required"]

    def test_missing_required_lowers_score(
        self, scorer: MQLBenchScorer, records: dict[str, EvalRecord]
    ) -> None:
        partial = _fenced(
            """
bool IsNewBar()
  {
   return(true);
  }
"""
        )
        is_correct, meta = scorer.score(records["new-bar-guard"], partial)
        assert is_correct is False
        assert 0.0 < meta["required_score"] < 1.0 or meta["required_matched"] == 0
        assert meta["missing_required"]

    def test_optional_weight_folds_away_when_absent(
        self, scorer: MQLBenchScorer
    ) -> None:
        record = EvalRecord(
            record_id="synthetic",
            problem="write it",
            reference="",
            category="coding",
            subject="mql_bench",
            metadata={
                "task_id": "synthetic",
                "required": ["SymbolInfoDouble"],
                "optional": [],
                "forbid_mql4": True,
            },
        )
        _is_correct, meta = scorer.score(
            record, _fenced("double a = SymbolInfoDouble(_Symbol, SYMBOL_ASK);")
        )
        assert meta["optional_score"] is None
        # 0.70 required + 0.20 safety, renormalized over 0.90 => 1.0
        assert meta["score"] == pytest.approx(1.0)


class TestExtraction:
    def test_prefers_mql5_fence_over_mql4_fence(self) -> None:
        answer = (
            "Before:\n```mql4\ndouble p = Ask;\n```\n"
            "After:\n```mql5\ndouble p = SymbolInfoDouble(_Symbol, SYMBOL_ASK);\n```"
        )
        source, fenced = extract_mql_source(answer)
        assert fenced is True
        assert "SYMBOL_ASK" in source
        assert "= Ask" not in source

    def test_prefers_last_mql5_shaped_generic_fence(self) -> None:
        answer = (
            "```\nint start() { double p = Ask; }\n```\n"
            "```\nint OnInit() { return(INIT_SUCCEEDED); }\n```"
        )
        source, _ = extract_mql_source(answer)
        assert "INIT_SUCCEEDED" in source

    def test_unfenced_answer_is_used_verbatim(self) -> None:
        source, fenced = extract_mql_source("int OnInit() { return(INIT_SUCCEEDED); }")
        assert fenced is False
        assert "OnInit" in source

    def test_empty_answer(self) -> None:
        assert extract_mql_source("") == ("", False)

    def test_no_source_fails_the_sample(
        self, scorer: MQLBenchScorer, records: dict[str, EvalRecord]
    ) -> None:
        is_correct, meta = scorer.score(
            records["risk-lot-size"], "I cannot help with trading code."
        )
        assert is_correct is False
        assert meta["fenced"] is False
        assert meta["required_matched"] == 0
        assert len(meta["missing_required"]) == meta["required_total"]

    def test_empty_answer_is_reported(
        self, scorer: MQLBenchScorer, records: dict[str, EvalRecord]
    ) -> None:
        is_correct, meta = scorer.score(records["risk-lot-size"], "")
        assert is_correct is False
        assert meta["source_chars"] == 0
        assert "note" in meta


class TestStripPreservesOffsets:
    def test_line_numbers_survive_stripping(self) -> None:
        code = (
            "// a comment mentioning Ask\n"
            "/* block\n   comment */\n"
            'Print("Ask and Bid");\n'
            "double price = Ask;\n"
        )
        stripped = strip_comments_and_strings(code)
        assert len(stripped) == len(code)
        hits = find_mql4_isms(stripped)
        assert len(hits) == 1
        # `double price = Ask;` is the fifth physical line: the block comment
        # spans two of them.
        assert hits[0]["line"] == 5

    def test_escaped_quotes_inside_strings(self) -> None:
        code = 'Print("a \\"quoted\\" Ask");\ndouble x = Bid;'
        stripped = strip_comments_and_strings(code)
        hits = find_mql4_isms(stripped)
        assert [h["match"] for h in hits] == ["Bid"]
