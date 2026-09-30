"""Tests for the mql-bench dataset (MQL5 Expert Advisor code generation).

The dataset is fully synthetic — no download, no network — so these tests run
in the default CI job alongside the other coding benchmarks.
"""

from __future__ import annotations

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
