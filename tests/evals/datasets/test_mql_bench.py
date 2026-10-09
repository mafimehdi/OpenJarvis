"""Tests for the mql-bench dataset (MQL5 Expert Advisor code generation).

The dataset is fully synthetic — no download, no network — so these tests run
in the default CI job alongside the other coding benchmarks.
"""

from __future__ import annotations

import re

import pytest

from openjarvis.evals.datasets.mql_bench import MQLBenchDataset

_ALL_TASK_IDS = {
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


@pytest.fixture()
def loaded() -> MQLBenchDataset:
    ds = MQLBenchDataset()
    ds.load()
    return ds


class TestDatasetShape:
    def test_identifiers(self) -> None:
        ds = MQLBenchDataset()
        assert ds.dataset_id == "mql-bench"
        assert ds.dataset_name == "MQL Bench"

    def test_load_populates_every_task(self, loaded: MQLBenchDataset) -> None:
        assert loaded.size() == len(_ALL_TASK_IDS)
        assert len(list(loaded.iter_records())) == len(_ALL_TASK_IDS)
        assert len(loaded) == loaded.size()

    def test_record_fields(self, loaded: MQLBenchDataset) -> None:
        for record in loaded.iter_records():
            assert record.record_id.startswith("mql-bench-")
            assert record.category == "coding"
            assert record.subject == "mql_bench"
            assert record.problem.strip()
            assert record.reference.strip()

    def test_task_ids_unique_and_known(self, loaded: MQLBenchDataset) -> None:
        ids = [r.metadata["task_id"] for r in loaded.iter_records()]
        assert len(ids) == len(set(ids))
        assert set(ids) == _ALL_TASK_IDS

    def test_metadata_carries_checks(self, loaded: MQLBenchDataset) -> None:
        for record in loaded.iter_records():
            meta = record.metadata
            assert meta["dialect"] == "mql5"
            assert isinstance(meta["required"], list) and meta["required"]
            assert isinstance(meta["optional"], list)
            assert meta["forbid_mql4"] is True
            assert isinstance(meta["extra_forbidden"], list)

    def test_prompt_states_mql5_rules(self, loaded: MQLBenchDataset) -> None:
        for record in loaded.iter_records():
            assert "MQL5 only" in record.problem
            assert "```mql5" in record.problem


class TestPortTask:
    def test_mql4_source_is_embedded(self, loaded: MQLBenchDataset) -> None:
        record = next(
            r
            for r in loaded.iter_records()
            if r.metadata["task_id"] == "port-ordersend"
        )
        assert "```mql4" in record.problem
        assert "OrderSend" in record.problem
        assert "OP_BUY" in record.problem

    def test_only_the_port_task_embeds_mql4(self, loaded: MQLBenchDataset) -> None:
        embedded = [
            r.metadata["task_id"]
            for r in loaded.iter_records()
            if "```mql4" in r.problem
        ]
        assert embedded == ["port-ordersend"]


class TestSampling:
    def test_max_samples_caps_records(self) -> None:
        ds = MQLBenchDataset()
        ds.load(max_samples=3)
        assert ds.size() == 3

    def test_seed_is_reproducible(self) -> None:
        first = MQLBenchDataset()
        first.load(seed=7)
        second = MQLBenchDataset()
        second.load(seed=7)
        assert [r.record_id for r in first.iter_records()] == [
            r.record_id for r in second.iter_records()
        ]

    def test_seed_permutes_but_preserves_membership(self) -> None:
        unseeded = MQLBenchDataset()
        unseeded.load()
        seeded = MQLBenchDataset()
        seeded.load(seed=3)
        assert {r.record_id for r in unseeded.iter_records()} == {
            r.record_id for r in seeded.iter_records()
        }

    @pytest.mark.parametrize("split", ["train", "test", "all"])
    def test_split_is_deterministic(self, split: str) -> None:
        first = MQLBenchDataset()
        first.load(split=split, seed=42)
        second = MQLBenchDataset()
        second.load(split=split, seed=42)
        assert [r.record_id for r in first.iter_records()] == [
            r.record_id for r in second.iter_records()
        ]

    def test_train_test_partition_the_corpus(self) -> None:
        train = MQLBenchDataset()
        train.load(split="train", seed=42)
        test = MQLBenchDataset()
        test.load(split="test", seed=42)
        everything = MQLBenchDataset()
        everything.load(split="all", seed=42)

        train_ids = {r.record_id for r in train.iter_records()}
        test_ids = {r.record_id for r in test.iter_records()}
        assert train_ids.isdisjoint(test_ids)
        assert train_ids | test_ids == {r.record_id for r in everything.iter_records()}
        assert everything.size() == len(_ALL_TASK_IDS)


class TestCliWiring:
    def test_benchmark_is_listed(self) -> None:
        from openjarvis.cli.eval_cmd import KNOWN_BENCHMARKS
        from openjarvis.evals.cli import BENCHMARKS

        assert "mql-bench" in BENCHMARKS
        assert BENCHMARKS["mql-bench"]["category"] == "coding"
        assert "mql-bench" in KNOWN_BENCHMARKS

    def test_dataset_factory(self) -> None:
        from openjarvis.evals.cli import _build_dataset

        assert isinstance(_build_dataset("mql-bench"), MQLBenchDataset)

    def test_scorer_factory(self) -> None:
        from openjarvis.evals.cli import _build_scorer
        from openjarvis.evals.scorers.mql_bench import MQLBenchScorer

        scorer = _build_scorer("mql-bench", None, "")
        assert isinstance(scorer, MQLBenchScorer)
        assert scorer.scorer_id == "mql_bench"


# --- the MQL5 vocabulary the shipped reference answers use --------------------
#
# Every name below was checked against the MQL5 reference manual
# (mql5.com/en/docs). The scorer cannot do this check: it looks for MQL4
# spellings and for the substrings a task requires, so an invented MQL5
# identifier inside a reference answer would score perfectly and still not
# compile — and the benchmark would be grading real answers against a fiction.
# Widen these sets only after checking the manual.
REF_CONSTANTS = frozenset(
    {
        "ACCOUNT_EQUITY",
        "ACCOUNT_TRADE_EXPERT",
        "DEAL_ENTRY",
        "DEAL_MAGIC",
        "DEAL_PROFIT",
        "DEAL_VOLUME",
        "ENUM_DEAL_ENTRY",
        "ENUM_POSITION_TYPE",
        "ENUM_SYMBOL_TRADE_MODE",
        "INIT_FAILED",
        "INIT_PARAMETERS_INCORRECT",
        "INIT_SUCCEEDED",
        "INVALID_HANDLE",
        "MODE_EMA",
        "MQL_TRADE_ALLOWED",
        "PERIOD_CURRENT",
        "POSITION_MAGIC",
        "POSITION_SL",
        "POSITION_SYMBOL",
        "POSITION_TP",
        "POSITION_TYPE",
        "POSITION_TYPE_BUY",
        "POSITION_VOLUME",
        "PRICE_CLOSE",
        "SYMBOL_ASK",
        "SYMBOL_BID",
        "SYMBOL_DIGITS",
        "SYMBOL_POINT",
        "SYMBOL_SPREAD",
        "SYMBOL_TRADE_MODE",
        "SYMBOL_TRADE_MODE_FULL",
        "SYMBOL_TRADE_STOPS_LEVEL",
        "SYMBOL_TRADE_TICK_SIZE",
        "SYMBOL_TRADE_TICK_VALUE",
        "SYMBOL_VOLUME_MAX",
        "SYMBOL_VOLUME_MIN",
        "SYMBOL_VOLUME_STEP",
        "TERMINAL_TRADE_ALLOWED",
        "TRADE_TRANSACTION_DEAL_ADD",
    }
)
REF_PREDEFINED = frozenset({"_Symbol"})
REF_GLOBAL_FUNCTIONS = frozenset(
    {
        "AccountInfoDouble",
        "AccountInfoInteger",
        "ArraySize",
        "BarsCalculated",
        "CopyBuffer",
        "EnumToString",
        "GetLastError",
        "HistoryDealGetDouble",
        "HistoryDealGetInteger",
        "HistoryDealSelect",
        "MQLInfoInteger",
        "MathFloor",
        "MathMax",
        "MathMin",
        "NormalizeDouble",
        "PositionGetDouble",
        "PositionGetInteger",
        "PositionGetString",
        "PositionGetTicket",
        "PositionSelectByTicket",
        "PositionsTotal",
        "Print",
        "PrintFormat",
        "SymbolInfoDouble",
        "SymbolInfoInteger",
        "TerminalInfoInteger",
        "TimeCurrent",
        "TimeToStruct",
        "iMA",
        "iTime",
    }
)
# Methods of CTrade. These exist only as members of that class, so calling one
# bare — `PositionModify(ticket, sl, tp)` — is an undeclared identifier, and the
# real globals that look like it (`PositionGetDouble`, `PositionSelectByTicket`)
# are what make the mistake easy to write and hard to notice.
REF_CTRADE_METHODS = frozenset(
    {
        "Buy",
        "PositionClosePartial",
        "PositionModify",
        "ResultRetcode",
        "ResultRetcodeDescription",
        "SetDeviationInPoints",
        "SetExpertMagicNumber",
        "SetTypeFillingBySymbol",
    }
)
MQL_KEYWORDS = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "return",
        "sizeof",
        "else",
        "case",
        "do",
        "break",
        "continue",
    }
)


def _code_only(text: str) -> str:
    """Strip string literals and line comments so prose cannot pose as code."""
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    return re.sub(r"//[^\n]*", "", text)


class TestReferencesAreRealMql5:
    """The reference answers checked against the language, not against the scorer.

    ``test_reference_is_correct`` (in ``tests/evals/scorers/``) proves every
    reference satisfies its own task, which is a closed loop: a reference can
    satisfy a task and still not compile, because nothing in that loop knows
    MQL5. Round 13 found the same class of error in the skill's cheatsheet — two
    constants that do not exist, in a file the model is told to copy. These tests
    supply the missing side for the twelve answers the benchmark grades against.
    """

    def _references(self, loaded: MQLBenchDataset) -> list[tuple[str, str]]:
        return [(r.record_id, _code_only(r.reference)) for r in loaded.iter_records()]

    def test_constants_and_predefined_variables_exist(
        self, loaded: MQLBenchDataset
    ) -> None:
        unknown: dict[str, list[str]] = {}
        for record_id, code in self._references(loaded):
            constants = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", code))
            predefined = set(re.findall(r"\b_[A-Za-z]\w*\b", code))
            for name in (constants - REF_CONSTANTS) | (predefined - REF_PREDEFINED):
                unknown.setdefault(name, []).append(record_id)
        assert not unknown, (
            "a reference answer names an MQL5 constant or predefined variable "
            "that does not exist; verify it against the reference manual before "
            f"widening the sets: {unknown}"
        )

    def test_every_call_is_a_real_global_or_a_method_on_an_object(
        self, loaded: MQLBenchDataset
    ) -> None:
        unknown: dict[str, list[str]] = {}
        for record_id, code in self._references(loaded):
            local = {
                name
                for name in re.findall(r"\b([A-Za-z_]\w*)\s*\([^;{)]*\)\s*\{", code)
                if name not in MQL_KEYWORDS
            }
            bare = {
                name
                for name in re.findall(r"(?<![.\w_])([A-Za-z_]\w*)\s*\(", code)
                if name not in MQL_KEYWORDS and name not in local
            }
            for name in bare - REF_GLOBAL_FUNCTIONS:
                unknown.setdefault(name, []).append(record_id)
            for name in set(re.findall(r"\.\s*([A-Za-z_]\w*)\s*\(", code)):
                if name not in REF_CTRADE_METHODS:
                    unknown.setdefault(f".{name}()", []).append(record_id)
        assert not unknown, (
            "a reference answer calls something that is not an MQL5 global "
            "function or a known CTrade method — a bare `PositionModify()` is the "
            f"usual shape of this; verify against the manual first: {unknown}"
        )
