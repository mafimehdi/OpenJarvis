"""Tests for examples/mql_companion/verify_on_terminal.py.

The verifier exists to settle claims that a fake terminal cannot: what
MetaTrader actually writes, and when. So these tests cannot prove its answers
are right about MT5 — no test in this repository can. What they pin is the
machinery around those answers:

- a run that produces the expected files is reported as confirmed, and a run
  that produces something else *fails* rather than reporting nothing;
- the mapping table is derived from dates the reports actually carry, and says
  "not measurable" when they do not;
- the process_grace experiment distinguishes a real race from a machine that
  simply exits quickly;
- nothing launches a terminal without ``--yes``, and nothing calls a tool that
  is not on the read-only list.

The fake terminal here is deliberately closer to the real thing than the one in
``test_tester_report.py``: it reads ``ForwardMode`` out of the ini and writes a
forward half whose dates depend on it, so a wrong derivation shows up as a
wrong percentage rather than as a missing file.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional

import pytest
from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "examples" / "mql_companion" / "verify_on_terminal.py"


def _load_module() -> ModuleType:
    """Import the example script by path (the examples tree is not a package).

    Registered in ``sys.modules`` before ``exec_module`` because the module
    uses ``from __future__ import annotations`` with ``@dataclass``.
    """
    spec = importlib.util.spec_from_file_location("mql_verify", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vt = _load_module()


# A stand-in for terminal64.exe that behaves the way REVIEW-NOTES.md needs to
# check: the forward split depends on ForwardMode, the reports carry dates, and
# the exit can be delayed so the process_grace race is observable.
FAKE_TERMINAL = """#!/usr/bin/env bash
set -e
cfg=""
for a in "$@"; do
  case "$a" in /config:*) cfg="${a#/config:}"; cfg="${cfg%\\"}"; cfg="${cfg#\\"}";; esac
done
[ -f "$cfg" ] || { echo "no config: $cfg" >&2; exit 3; }
echo "$@" >> "$FAKE_CALL_LOG"
report=$(grep -m1 '^Report=' "$cfg" | cut -d= -f2-)
from=$(grep -m1 '^FromDate=' "$cfg" | cut -d= -f2-)
fmode=$(grep -m1 '^ForwardMode=' "$cfg" | cut -d= -f2-)
opt=$(grep -m1 '^Optimization=' "$cfg" | cut -d= -f2-)
[ "${FAKE_MODE:-ok}" = "ignoreopt" ] && opt=0
sleep "${FAKE_DELAY:-0}"
case "${FAKE_MODE:-ok}" in
  noreport) exit 0;;
esac
if [ "${opt:-0}" -gt 0 ]; then
  # An optimization writes one table, the way the terminal does.
  cat > "${report}.xml" <<OPT
<?xml version="1.0" encoding="ANSI"?>
<Table>
  <Row>
    <Cell>Pass</Cell><Cell>Result</Cell><Cell>Profit</Cell>
    <Cell>Expected Payoff</Cell><Cell>Profit Factor</Cell>
    <Cell>Recovery Factor</Cell><Cell>Sharpe Ratio</Cell><Cell>Custom</Cell>
    <Cell>Equity DD %</Cell><Cell>Trades</Cell><Cell>InpFastEMA</Cell>
  </Row>
  <Row>
    <Cell>4</Cell><Cell>11200</Cell><Cell>1200</Cell><Cell>6</Cell>
    <Cell>1.4</Cell><Cell>2.5</Cell><Cell>1.1</Cell><Cell>0</Cell>
    <Cell>8</Cell><Cell>200</Cell><Cell>12</Cell>
  </Row>
  <Row>
    <Cell>9</Cell><Cell>10800</Cell><Cell>800</Cell><Cell>4</Cell>
    <Cell>1.2</Cell><Cell>1.8</Cell><Cell>0.9</Cell><Cell>0</Cell>
    <Cell>11</Cell><Cell>200</Cell><Cell>20</Cell>
  </Row>
</Table>
OPT
  exit 0
fi
case "${FAKE_MODE:-ok}" in
  backonly) mid="" ;;
  *)
    case "${fmode:-0}" in
      1) mid="2022.09.30"; fwd_from="2022.10.01";;
      2) mid="2022.11.30"; fwd_from="2022.12.01";;
      3) mid="2023.01.31"; fwd_from="2023.02.01";;
      *) mid="";;
    esac;;
esac
cat > "${report}.htm" <<HTM
<html><body><table>
<tr><td>From Date</td><td>${from}</td></tr>
<tr><td>To Date</td><td>${mid:-2023.03.31}</td></tr>
<tr><td>Total Net Profit</td><td>1 850.25</td></tr>
<tr><td>Profit Factor</td><td>1.55</td></tr>
<tr><td>Total Trades</td><td>310</td></tr>
</table></body></html>
HTM
[ -z "$mid" ] && { sleep "${FAKE_EXIT_DELAY:-0}"; exit 0; }
cat > "${report}.forward.htm" <<FWD
<html><body><table>
<tr><td>From Date</td><td>${fwd_from}</td></tr>
<tr><td>To Date</td><td>2023.03.31</td></tr>
<tr><td>Total Net Profit</td><td>620.00</td></tr>
<tr><td>Profit Factor</td><td>1.31</td></tr>
<tr><td>Total Trades</td><td>96</td></tr>
</table></body></html>
FWD
sleep "${FAKE_EXIT_DELAY:-0}"
exit 0
"""


@pytest.fixture()
def fake_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An executable that behaves like the terminal for the checks we test."""
    script = tmp_path / "terminal64"
    script.write_text(FAKE_TERMINAL, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("FAKE_CALL_LOG", str(tmp_path / "calls.log"))
    monkeypatch.delenv("FAKE_MODE", raising=False)
    monkeypatch.delenv("FAKE_DELAY", raising=False)
    monkeypatch.delenv("FAKE_EXIT_DELAY", raising=False)
    return script


@pytest.fixture()
def verifier(fake_terminal: Path, tmp_path: Path) -> vt.Verifier:
    """A verifier pointed at the fake terminal, allowed to launch it."""
    return vt.Verifier(
        terminal=fake_terminal,
        expert="Examples/MACD/MACD Sample",
        symbol="EURUSD",
        period="H1",
        from_date="2022.01.01",
        to_date="2023.03.31",
        out_dir=tmp_path / "reports",
        modes=[0, 1],
        set_file="",
        timeout=60.0,
        allow_runs=True,
    )


def _run_cli(
    fake_terminal: Optional[Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: List[str],
) -> Any:
    """Invoke the CLI with terminal discovery pinned to the fake (or nothing)."""
    monkeypatch.setattr(
        vt.tr, "find_terminal", lambda explicit=None: fake_terminal or None
    )
    out_dir = tmp_path / "cli-reports"
    return CliRunner().invoke(
        vt.main, ["--out-dir", str(out_dir), "--timeout", "60", *args]
    )


class TestCli:
    def test_list_prints_every_check(self) -> None:
        result = CliRunner().invoke(vt.main, ["--list"])
        assert result.exit_code == 0
        ids = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
        assert ids == [check[0] for check in vt.CHECKS]
        assert "notes #1" in result.stdout

    def test_without_yes_nothing_launches(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _run_cli(fake_terminal, tmp_path, monkeypatch, ["--only", "1,2"])
        assert result.exit_code == 0
        assert "dry run" in result.stdout
        # The terminal was discovered but never invoked.
        assert not (tmp_path / "calls.log").exists()

    def test_no_terminal_is_reported_not_guessed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _run_cli(None, tmp_path, monkeypatch, ["--only", "0,1"])
        assert result.exit_code == 2
        assert "terminal=NOT FOUND" in result.stdout
        assert "no terminal found" in result.stdout


class TestForwardModeMapping:
    def test_it_derives_the_split_from_the_dates(self, verifier: vt.Verifier) -> None:
        result = verifier.check_1_forward_mode_mapping()
        assert result.status == vt.PASS
        joined = "\n".join(result.evidence)
        # 2022.01.01..2022.09.30 out of 2022.01.01..2023.03.31 is 60%/40%.
        assert "mode=1: back 2022-01-01..2022-09-30" in joined
        assert "split 60%/40%" in joined
        assert "mode=0: new files" in joined
        assert "Paste the mode= lines back" in result.note

    def test_mode_zero_must_not_split(self, verifier: vt.Verifier) -> None:
        result = verifier.check_1_forward_mode_mapping()
        mode0 = [
            line for line in result.evidence if line.startswith("mode=0: new files")
        ]
        assert mode0 and "forward" not in mode0[0]

    def test_a_forward_half_that_never_appears_is_unknown_not_pass(
        self, verifier: vt.Verifier, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "backonly")
        result = verifier.check_1_forward_mode_mapping()
        assert result.status == vt.UNKNOWN
        assert "no forward half" in "\n".join(result.evidence)
        # The note has to say what to try next, not just that it failed.
        assert "--with-optimization" in result.note

    def test_a_run_that_produces_nothing_says_so(
        self, verifier: vt.Verifier, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "noreport")
        result = verifier.check_1_forward_mode_mapping()
        assert result.status == vt.UNKNOWN
        assert "TimeoutError" in "\n".join(result.evidence)

    def test_reports_without_dates_are_called_unmeasurable(self) -> None:
        class Empty:
            metrics: dict = {}

        line = vt._split_line(1, Empty(), Empty())
        assert "no dates" in line


class TestForwardFileNames:
    def test_the_pairing_rule_matches_what_the_terminal_wrote(
        self, verifier: vt.Verifier
    ) -> None:
        result = verifier.check_2_forward_file_names()
        assert result.status == vt.PASS
        joined = "\n".join(result.evidence)
        assert "present on disk: ['verify-map-m1.forward.htm']" in joined
        assert "verdict=" in joined
        # The ini is ours, and a file already counted under --out-dir is not
        # listed again as an install-directory find.
        listing = [
            line for line in result.evidence if line.startswith("files containing")
        ]
        assert listing and ".ini" not in listing[0]
        assert listing[0].count("forward.htm") == 1
        assert result.note.startswith("The name-based pairing rule")

    def test_a_lone_back_half_fails_loudly(
        self, verifier: vt.Verifier, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "backonly")
        result = verifier.check_2_forward_file_names()
        assert result.status == vt.FAIL
        assert "present on disk: none" in "\n".join(result.evidence)
        assert "forward_companion()" in result.note

    def test_the_naming_check_reuses_the_mapping_run(
        self, verifier: vt.Verifier
    ) -> None:
        verifier.check_1_forward_mode_mapping()
        launches_after_mapping = verifier.launches
        verifier.check_2_forward_file_names()
        # Same shape as mode 1 of the mapping table: no second launch.
        assert verifier.launches == launches_after_mapping


class TestProcessGrace:
    def test_a_delayed_exit_proves_the_grace_is_needed(
        self, verifier: vt.Verifier, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_EXIT_DELAY", "2")
        result = verifier.check_4_process_grace()
        assert result.status == vt.PASS
        joined = "\n".join(result.evidence)
        assert "grace=0: exit_code=None" in joined
        assert "grace=5: exit_code=0" in joined

    def test_a_fast_exit_is_honest_about_proving_nothing(
        self, verifier: vt.Verifier
    ) -> None:
        result = verifier.check_4_process_grace()
        assert result.status == vt.UNKNOWN
        assert "no race was observable" in result.note


class TestFileStability:
    def test_it_samples_the_write_while_the_terminal_runs(
        self, verifier: vt.Verifier, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_DELAY", "0.6")
        result = verifier.check_5_file_stability()
        assert result.status == vt.PASS
        joined = "\n".join(result.evidence)
        assert "samples" in joined
        assert "still growing after" not in joined
        assert "parsed metrics=" in joined


class TestDecodeCheck:
    def test_the_ambiguous_samples_are_the_ones_listed(self) -> None:
        labels = [label for label, _, _ in vt.decode_samples()]
        assert any("numero" in label for label in labels)
        assert any("0x81" in label for label in labels)
        assert any("0x98" in label for label in labels)

    def test_every_sample_reads_back_exactly(self, verifier: vt.Verifier) -> None:
        result = verifier.check_6_decode_order()
        assert result.status == vt.PASS, result.evidence
        assert len(result.evidence) == len(vt.decode_samples())

    def test_a_mangled_sample_would_fail_the_check(
        self, verifier: vt.Verifier, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vt.tr, "decode_report_bytes", lambda raw: "nonsense")
        result = verifier.check_6_decode_order()
        assert result.status == vt.FAIL
        assert "MANGLED" in "\n".join(result.evidence)
        assert "first-match contest" in result.note


class TestLocalChecks:
    def test_the_environment_check_names_what_is_missing(self, tmp_path: Path) -> None:
        offline = vt.Verifier(
            terminal=None,
            expert="MyEA",
            symbol="EURUSD",
            period="H1",
            from_date="",
            to_date="",
            out_dir=tmp_path / "reports",
            modes=[1],
            set_file="",
            timeout=60.0,
            allow_runs=True,
        )
        result = offline.check_0_environment()
        assert result.status == vt.FAIL
        assert "terminal=NOT FOUND" in "\n".join(result.evidence)
        assert "writable" in "\n".join(result.evidence)

    def test_expert_parameters_rejections_are_shown(
        self, verifier: vt.Verifier
    ) -> None:
        result = verifier.check_8_expert_parameters()
        assert result.status == vt.PASS
        joined = "\n".join(result.evidence)
        assert "path separator" in joined
        assert "does not end in .set" in joined
        assert "MyEA.set: accepted" in joined

    def test_wine_discovery_does_not_apply_on_windows(
        self, verifier: vt.Verifier
    ) -> None:
        original = vt.sys.platform
        try:
            vt.sys.platform = "win32"  # type: ignore[misc]
            result = verifier.check_7_wine()
        finally:
            vt.sys.platform = original  # type: ignore[misc]
        assert result.status == vt.SKIPPED
        assert "Windows" in result.note

    def test_optimization_needs_a_set_file(self, verifier: vt.Verifier) -> None:
        result = verifier.check_9_optimization_extension()
        assert result.status == vt.SKIPPED
        assert "--set-file" in result.note
        assert verifier.launches == 0


class TestReadOnlyGuard:
    def test_the_order_tool_is_not_on_the_list(self) -> None:
        assert "mt5_order_send" not in vt.READ_ONLY_TOOLS
        assert all("order_send" not in name for name in vt.READ_ONLY_TOOLS)

    def test_a_read_only_name_is_allowed(self) -> None:
        assert vt._guard_read_only("mt5_symbol_info") == "mt5_symbol_info"

    def test_anything_else_raises(self) -> None:
        for name in ("mt5_order_send", "mt5_positions_close", "whatever"):
            with pytest.raises(ValueError):
                vt._guard_read_only(name)

    def test_the_bridge_check_skips_without_the_windows_package(
        self, verifier: vt.Verifier
    ) -> None:
        if vt._have_mt5_package():  # pragma: no cover - not on Linux
            pytest.skip("MetaTrader5 is installed; the skip path cannot run")
        result = verifier.check_10_bridge()
        assert result.status == vt.SKIPPED
        # check_10_bridge has two honest skip paths, and the note says which one
        # happened: the bridge is missing from this tree (a partial checkout), or
        # the Windows-only MetaTrader5 package is missing from this machine.
        # Asserting only the second let a tree without the bridge satisfy the
        # status check while testing nothing about the reason.
        if importlib.util.find_spec("mt5_mcp_server") is None:
            assert "bridge not importable" in result.note
        else:
            assert "Windows-only" in result.note


class TestOptInRuns:
    """The two checks that only run with a flag — and only on a real machine.

    They are exercised here against the fake terminal so that a Windows run
    fails on MetaTrader's behaviour rather than on a typo in the check.
    """

    def _verifier(self, fake_terminal: Path, tmp_path: Path, **kw: Any) -> vt.Verifier:
        args: Dict[str, Any] = {
            "terminal": fake_terminal,
            "expert": "MyEA",
            "symbol": "EURUSD",
            "period": "H1",
            "from_date": "2022.01.01",
            "to_date": "2023.03.31",
            "out_dir": tmp_path / "reports",
            "modes": [1],
            "set_file": "",
            "timeout": 60.0,
            "allow_runs": True,
        }
        args.update(kw)
        return vt.Verifier(**args)

    def test_model4_is_timed_and_does_not_claim_to_see_the_ui(
        self, fake_terminal: Path, tmp_path: Path
    ) -> None:
        verifier = self._verifier(fake_terminal, tmp_path)
        result = verifier.check_3_model4()
        assert result.status == vt.UNKNOWN
        joined = "\n".join(result.evidence)
        assert "model 0:" in joined
        assert "model 4:" in joined
        assert verifier.launches == 2
        # The honest part: a script cannot see a frozen UI thread.
        assert "not observable from a script" in result.note

    def test_a_malformed_start_date_is_put_to_the_terminal(
        self, fake_terminal: Path, tmp_path: Path
    ) -> None:
        """Check 1 also carries the one date rule that cannot be tested here.

        `tester_ini_warnings` claims MT5 parses only `YYYY.MM.DD`, and that
        comes from the documentation rather than from a terminal. The fake
        echoes `FromDate` straight into its report, so it cannot tell the two
        readings apart; on Windows this line is the evidence, and it says which
        way the warning should move.
        """
        verifier = self._verifier(fake_terminal, tmp_path)

        result = verifier.check_1_forward_mode_mapping()

        joined = "\n".join(result.evidence)
        assert "FromDate='2022-01-01' requested:" in joined
        assert verifier.launches == 2

    def test_an_optimization_run_writes_a_table(
        self, fake_terminal: Path, tmp_path: Path
    ) -> None:
        verifier = self._verifier(fake_terminal, tmp_path, set_file="MyEA.set")
        result = verifier.check_9_optimization_extension()
        assert result.status == vt.PASS, result.evidence
        joined = "\n".join(result.evidence)
        assert "optimization table: 2 passes" in joined
        assert "Pass" in joined and "Sharpe Ratio" in joined
        assert "InpFastEMA" in joined

    def test_an_optimization_that_produced_no_table_fails(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MODE", "ignoreopt")
        verifier = self._verifier(fake_terminal, tmp_path, set_file="MyEA.set")
        result = verifier.check_9_optimization_extension()
        assert result.status == vt.FAIL
        assert "parsed as a testing report" in "\n".join(result.evidence)


class TestReportAndOptIn:
    def test_opt_in_checks_are_skipped_without_their_flag(
        self, fake_terminal: Path, tmp_path: Path
    ) -> None:
        dry = vt.Verifier(
            terminal=fake_terminal,
            expert="MyEA",
            symbol="EURUSD",
            period="H1",
            from_date="",
            to_date="",
            out_dir=tmp_path / "reports",
            modes=[1],
            set_file="",
            timeout=60.0,
            allow_runs=False,
        )
        results = dry.run([], [], {})
        by_id = {item.check_id: item for item in results}
        for check_id in ("3", "4", "5", "9", "10"):
            assert by_id[check_id].status == vt.SKIPPED
            assert "opt-in" in by_id[check_id].note
        # Nothing that needs a terminal ran either: no --yes, no launches.
        assert dry.launches == 0
        assert by_id["1"].status == vt.SKIPPED

    def test_only_and_skip_select_checks(self, verifier: vt.Verifier) -> None:
        results = verifier.run(["6", "8"], ["8"], {})
        assert [item.check_id for item in results] == ["6"]

    def test_the_report_has_a_paste_back_block(self, verifier: vt.Verifier) -> None:
        results = verifier.run(["6", "8"], [], {})
        report = vt.render_report(results, verifier)
        assert "----- paste this back -----" in report
        assert "----- end -----" in report
        pasted = report.split("----- paste this back -----")[1]
        assert "6|pass|" in pasted
        assert "8|pass|" in pasted

    def test_a_raising_check_becomes_a_failure_not_a_crash(
        self, verifier: vt.Verifier, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode() -> vt.Result:
            raise RuntimeError("the terminal ate it")

        monkeypatch.setattr(verifier, "check_6_decode_order", explode)
        results = verifier.run(["6"], [], {})
        assert results[0].status == vt.FAIL
        assert "RuntimeError: the terminal ate it" in results[0].evidence

    def test_json_output_matches_the_printed_report(
        self, fake_terminal: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = tmp_path / "verify.json"
        result = _run_cli(
            fake_terminal,
            tmp_path,
            monkeypatch,
            ["--only", "0,6,7,8", "--yes", "--json", str(payload)],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(payload.read_text(encoding="utf-8"))
        assert [item["id"] for item in data["results"]] == ["0", "6", "7", "8"]
        assert data["terminal"] == str(fake_terminal)
        by_id = {item["id"]: item for item in data["results"]}
        assert by_id["6"]["status"] == vt.PASS


class TestHelpers:
    def test_parse_day_accepts_the_shapes_reports_use(self) -> None:
        assert str(vt._parse_day("2022.01.01")) == "2022-01-01"
        assert str(vt._parse_day("2022.01.01 00:00")) == "2022-01-01"
        assert str(vt._parse_day("2022-01-01")) == "2022-01-01"
        assert vt._parse_day(None) is None
        assert vt._parse_day("not a date") is None

    def test_the_paste_line_cannot_break_the_block_delimiters(self) -> None:
        item = vt.Result("1", "title", vt.PASS)
        item.add("a;b|c")
        pasted = item.paste()
        assert pasted.startswith("1|pass|")
        assert pasted.count("|") == 2
