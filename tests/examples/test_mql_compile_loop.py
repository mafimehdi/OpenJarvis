"""The compile → fix → recompile loop, driven end to end.

`compile_loop.py` was the one large file in the MQL companion with no test
module of its own: it was reached only through the skill and docs suites, which
assert what its *documents* say. That is the shape of gap that let round 16's
wrong retcode labels survive fifteen rounds, so this file drives the real command
with a scripted compiler and a scripted model, and asserts the three things a CI
gate actually reads — the exit code, the verdict line, and the JSON report.

Nothing here needs MetaEditor or a model. `metaeditor.subprocess.run` is replaced
by a script that writes the log a given build would have written (and the
artifact, when the build owes one), and `openjarvis` is replaced by a module
whose `Jarvis.ask()` replies from a list. The compiler's own behaviour — log
discovery, UTF-16 decoding, summary parsing, artifact freshness — is the real
code, exercised by `test_mql_metaeditor.py`.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest
from click.testing import CliRunner

COMPANION = Path(__file__).resolve().parents[2] / "examples" / "mql_companion"

CLEAN_LOG = "Result: 0 errors, 0 warnings"
FAILING_LOG = (
    "C:\\MQL5\\Experts\\MyEA.mq5(7,4) : error: 'Ask' - undeclared identifier\n"
    "Result: 1 errors, 0 warnings"
)

#: Two stand-ins for a model's answer, as constants so the lines using them stay
#: inside the limit and read as "the model returned a fix".
A_FIX = "// fixed\nint OnInit(){return 0;}\n"
A_REWRITE = "// rewritten\nint OnInit(){return 0;}\n"


def _load(name: str) -> Any:
    """Import a companion module by path, registering it under its own name.

    `compile_loop` does `from metaeditor import ...`, so both have to be in
    `sys.modules` under those names for the pair to share one module object —
    which is what makes patching `metaeditor.subprocess.run` reach the loop.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, COMPANION / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


metaeditor = _load("metaeditor")
compile_loop = _load("compile_loop")


@pytest.fixture()
def editor(tmp_path: Path) -> Path:
    """A file that stands in for the MetaEditor binary."""
    fake = tmp_path / "metaeditor64.exe"
    fake.write_text("not really an executable")
    return fake


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    src = tmp_path / "MyEA.mq5"
    src.write_text("int OnInit() { return(INIT_SUCCEEDED); }\n", encoding="utf-8")
    return src


def _script_compiler(
    monkeypatch: pytest.MonkeyPatch,
    logs: Sequence[Optional[str]],
    *,
    writes_artifact: bool = False,
) -> List[List[str]]:
    """Replace the compiler with a script of log texts, one per call.

    ``None`` means "this build wrote no log at all" — the toolchain failure the
    loop must not hand to a model. The last entry repeats, so a script of one
    covers a loop of any length. Returns the command lines the loop built.
    """
    calls: List[List[str]] = []
    script = list(logs)

    def run(cmd: Sequence[Any], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append([str(part) for part in cmd])
        target = next(
            (str(p)[len("/compile:") :] for p in cmd if str(p).startswith("/compile:")),
            None,
        )
        text = script.pop(0) if len(script) > 1 else script[0]
        if target is not None and text is not None:
            log_path = metaeditor.default_log_path(Path(target))
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_bytes(text.encode("utf-16"))
            # Only a build that logged zero errors leaves a binary behind; a
            # failing round writing one would hide the case under test.
            if writes_artifact and "0 errors" in text:
                artifact = Path(target).with_suffix(
                    metaeditor.ARTIFACT_EXT[Path(target).suffix.lower()]
                )
                artifact.write_bytes(b"MZ")
        # A real MetaEditor can exit 1 on a build that logged zero errors
        # (mql5.com/en/forum/157533), so the script never returns 0: nothing
        # here may pass because the process code happened to agree.
        return subprocess.CompletedProcess(list(cmd), 1, "", "")

    monkeypatch.setattr(metaeditor.subprocess, "run", run)
    return calls


class _FakeJarvis:
    """The SDK surface the loop uses: a constructor, ``ask()`` and ``close()``."""

    replies: Sequence[Any] = ()
    prompts: List[str] = []
    init_kwargs: Dict[str, Any] = {}
    closed = False

    def __init__(self, **kwargs: Any) -> None:
        type(self).init_kwargs = kwargs

    def ask(self, prompt: str, **kwargs: Any) -> Any:
        type(self).prompts.append(prompt)
        reply = self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(prompt)
        return reply

    def close(self) -> None:
        type(self).closed = True


def _install_jarvis(
    monkeypatch: pytest.MonkeyPatch, replies: Sequence[Any]
) -> type[_FakeJarvis]:
    """Put a fake `openjarvis` in `sys.modules` and give it these replies.

    A reply may be a string (the model's answer), a callable (an agent that
    touches the file, as `apply_patch` would) or an Exception (a dead engine).
    """
    fake = type(
        "FakeJarvis",
        (_FakeJarvis,),
        {"replies": list(replies), "prompts": [], "init_kwargs": {}, "closed": False},
    )
    module = types.ModuleType("openjarvis")
    module.Jarvis = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openjarvis", module)
    return fake


def _fenced(body: str) -> str:
    return f"Here you go.\n```mql5\n{body}\n```\nHope that helps."


def _invoke(source: Path, editor: Path, *args: str) -> Any:
    runner = CliRunner()
    return runner.invoke(
        compile_loop.main,
        [
            "--source",
            str(source),
            "--metaeditor",
            str(editor),
            "--no-wine",
            *args,
        ],
        catch_exceptions=False,
    )


class TestTheVerdict:
    """Exit code, verdict line, and the JSON a CI gate reads."""

    def test_a_clean_build_with_its_binary_exits_zero(
        self, tmp_path: Path, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [CLEAN_LOG], writes_artifact=True)
        report = tmp_path / "report.json"

        result = _invoke(source, editor, "--compile-only", "--json-out", str(report))

        assert result.exit_code == 0, result.output
        assert "SUCCESS after 1 round(s)" in result.output
        assert f"artifact: {source.with_suffix('.ex5')}" in result.output
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["ok"] is True
        assert payload["artifact_missing"] is False
        assert payload["rounds"][0]["artifact"].endswith("MyEA.ex5")

    def test_zero_errors_with_no_binary_is_not_a_clean_compile(
        self, tmp_path: Path, source: Path, editor: Path, monkeypatch
    ) -> None:
        """The published contract says 0 means a clean compile.

        A zero-error log with no `.ex5` beside the source is MetaEditor's
        documented silent failure (mql5.com/en/forum/491543, fixed in build
        5200): the terminal has no binary to run, and exit 0 tells CI the EA
        was built. It exits 2 with the other toolchain problems instead,
        because another model round cannot fix it.
        """
        _script_compiler(monkeypatch, [CLEAN_LOG])
        report = tmp_path / "report.json"

        result = _invoke(source, editor, "--compile-only", "--json-out", str(report))

        assert result.exit_code == 2, result.output
        assert "NO ARTIFACT after 1 round(s)" in result.output
        assert ".ex5" in result.output and ".ex4" in result.output
        assert "toolchain problem, not a source problem" in result.output
        assert "SUCCESS" not in result.output
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["ok"] is False, "the gate must not read as a success"
        assert payload["artifact_missing"] is True
        # the compiler's own verdict is still recorded, per round
        assert payload["rounds"][0]["ok"] is True
        assert "no .ex5/.ex4 appeared" in payload["rounds"][0]["note"]

    def test_the_relaxation_flag_says_what_it_relaxes(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        """For a build whose binary lands somewhere this file does not look."""
        _script_compiler(monkeypatch, [CLEAN_LOG])

        result = _invoke(source, editor, "--compile-only", "--allow-missing-artifact")

        assert result.exit_code == 0, result.output
        assert "SUCCESS after 1 round(s)" in result.output

    def test_a_syntax_check_owes_no_artifact(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        calls = _script_compiler(monkeypatch, [CLEAN_LOG])

        result = _invoke(source, editor, "--compile-only", "--syntax-only")

        assert result.exit_code == 0, result.output
        assert "/s" in calls[0], "--syntax-only must reach the compiler"

    def test_a_header_owes_no_artifact(
        self, tmp_path: Path, editor: Path, monkeypatch
    ) -> None:
        """A `.mqh` compiles to nothing, so "no artifact" is its normal outcome.

        Round 16 removed `.mqh` from `ARTIFACT_EXT`; this is the loop honouring
        that instead of reporting a header syntax check as a broken build.
        """
        header = tmp_path / "MyLib.mqh"
        header.write_text("// constants\n", encoding="utf-8")
        _script_compiler(monkeypatch, [CLEAN_LOG])

        result = _invoke(header, editor, "--compile-only")

        assert result.exit_code == 0, result.output
        assert "SUCCESS after 1 round(s)" in result.output
        assert "NO ARTIFACT" not in result.output

    def test_a_source_that_still_fails_exits_one(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [FAILING_LOG])

        result = _invoke(source, editor, "--compile-only")

        assert result.exit_code == 1, result.output
        assert "FAILED after 1 round(s) — 1 error(s)" in result.output
        assert "'Ask' - undeclared identifier" in result.output
        assert "SUCCESS" not in result.output

    def test_the_process_exit_code_is_printed_as_advisory(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        """The scripted compiler exits 1 on a *successful* build, on purpose."""
        _script_compiler(monkeypatch, [CLEAN_LOG], writes_artifact=True)

        result = _invoke(source, editor, "--compile-only")

        assert "exit code: 1 (advisory only)" in result.output
        assert result.exit_code == 0, result.output


class TestToolchainFailures:
    """Problems a model round cannot fix must not be handed to a model."""

    def test_no_metaeditor_exits_two_with_install_hints(
        self, source: Path, monkeypatch
    ) -> None:
        for var in metaeditor.METAEDITOR_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(
            compile_loop, "find_metaeditor", lambda _: None
        )  # nothing on this machine has MetaEditor installed

        result = CliRunner().invoke(
            compile_loop.main, ["--source", str(source)], catch_exceptions=False
        )

        assert result.exit_code == 2, result.output
        assert "MetaEditor not found" in result.output
        assert "METAEDITOR_PATH" in result.output

    def test_an_unreadable_log_stops_before_spending_a_round(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [None])  # the build wrote no log at all
        _install_jarvis(monkeypatch, ["```mql5\nint OnInit(){return 0;}\n```"])

        result = _invoke(source, editor, "--max-rounds", "3")

        assert result.exit_code == 2, result.output
        assert "stopping: toolchain problem, not a source problem." in result.output
        assert "MetaEditor produced no log file" in result.output

    def test_a_dead_engine_is_reported_not_swallowed(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [FAILING_LOG])
        _install_jarvis(monkeypatch, [RuntimeError("ollama is not running")])

        result = _invoke(source, editor, "--max-rounds", "3")

        assert result.exit_code == 1, result.output
        assert "model failed: ollama is not running" in result.output

    def test_missing_openjarvis_says_how_to_install_or_opt_out(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [FAILING_LOG])
        monkeypatch.setitem(sys.modules, "openjarvis", None)  # forces ImportError

        result = _invoke(source, editor)

        assert result.exit_code == 1, result.output
        assert "openjarvis is not installed" in result.output
        assert "--compile-only" in result.output


class TestTheFixRounds:
    """The loop's own guards: it must stop when a round cannot help."""

    def test_a_model_that_echoes_the_file_back_costs_one_round_not_the_budget(
        self, tmp_path: Path, source: Path, editor: Path, monkeypatch
    ) -> None:
        """The answer differs from the previous *answer* — there is none yet."""
        _script_compiler(monkeypatch, [FAILING_LOG])
        _install_jarvis(monkeypatch, [_fenced(source.read_text(encoding="utf-8"))])
        report = tmp_path / "report.json"

        result = _invoke(source, editor, "--max-rounds", "5", "--json-out", str(report))

        assert result.exit_code == 1, result.output
        assert "stopping: the model returned the file unchanged." in result.output
        assert len(json.loads(report.read_text(encoding="utf-8"))["rounds"]) == 1

    def test_an_agent_that_reports_a_patch_it_never_wrote_is_stopped(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        """`agent-tools` mode has no returned body to compare, only the file."""
        _script_compiler(monkeypatch, [FAILING_LOG])
        _install_jarvis(monkeypatch, ["I replaced Ask with SymbolInfoDouble."])

        result = _invoke(source, editor, "--mode", "agent-tools", "--max-rounds", "5")

        assert result.exit_code == 1, result.output
        assert "stopping: the agent changed nothing on disk." in result.output

    def test_an_agent_that_really_patches_the_file_reaches_success(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        """The guard checks the file, not the claim — so a real edit passes it."""
        _script_compiler(monkeypatch, [FAILING_LOG, CLEAN_LOG], writes_artifact=True)

        def patch(prompt: str) -> str:
            source.write_text("// fixed\nint OnInit() { return(INIT_SUCCEEDED); }\n")
            return "Replaced the MQL4 predefined variable."

        _install_jarvis(monkeypatch, [patch])

        result = _invoke(source, editor, "--mode", "agent-tools", "--max-rounds", "3")

        assert result.exit_code == 0, result.output
        assert "round 1: FAILED" in result.output
        assert "round 2: OK" in result.output
        assert "SUCCESS after 2 round(s)" in result.output

    def test_a_fix_that_works_ends_the_loop(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [FAILING_LOG, CLEAN_LOG], writes_artifact=True)
        _install_jarvis(
            monkeypatch,
            [_fenced("// fixed\nint OnInit() { return(INIT_SUCCEEDED); }\n")],
        )

        result = _invoke(source, editor, "--max-rounds", "5")

        assert result.exit_code == 0, result.output
        assert "wrote 2 lines to MyEA.mq5" in result.output
        assert "SUCCESS after 2 round(s)" in result.output

    def test_the_round_budget_bounds_a_loop_that_never_converges(
        self, tmp_path: Path, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [FAILING_LOG])
        counter = {"n": 0}

        def a_different_attempt_each_time(prompt: str) -> str:
            counter["n"] += 1
            return _fenced(
                f"// attempt {counter['n']}\nint OnInit() {{ return(0); }}\n"
            )

        _install_jarvis(monkeypatch, [a_different_attempt_each_time])
        report = tmp_path / "report.json"

        result = _invoke(source, editor, "--max-rounds", "3", "--json-out", str(report))

        assert result.exit_code == 1, result.output
        assert counter["n"] == 3, "one model round per compile round, no more"
        assert "FAILED after 3 round(s)" in result.output
        assert len(json.loads(report.read_text(encoding="utf-8"))["rounds"]) == 3

    def test_each_round_snapshots_the_source_before_overwriting_it(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        original = source.read_text(encoding="utf-8")
        _script_compiler(monkeypatch, [FAILING_LOG, CLEAN_LOG], writes_artifact=True)
        _install_jarvis(monkeypatch, [_fenced(A_REWRITE)])

        result = _invoke(source, editor, "--max-rounds", "2")

        assert result.exit_code == 0, result.output
        backup = source.with_suffix(".mq5.round1.bak")
        assert backup.exists(), "the round's backup is the only way back"
        assert backup.read_text(encoding="utf-8") == original
        assert "// rewritten" in source.read_text(encoding="utf-8")

    def test_no_backup_is_written_when_asked_not_to(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [FAILING_LOG, CLEAN_LOG], writes_artifact=True)
        _install_jarvis(monkeypatch, [_fenced(A_REWRITE)])

        result = _invoke(source, editor, "--no-backup", "--max-rounds", "2")

        assert result.exit_code == 0, result.output
        assert not list(source.parent.glob("*.bak"))


class TestWhatTheModelIsTold:
    """The prompt is the loop's other output, and it is what the fix quality is."""

    def test_the_rewrite_prompt_carries_rules_diagnostics_and_source(
        self, tmp_path: Path, source: Path, editor: Path, monkeypatch
    ) -> None:
        header = tmp_path / "MyLib.mqh"
        header.write_text("// the included header\n", encoding="utf-8")
        _script_compiler(monkeypatch, [FAILING_LOG, CLEAN_LOG], writes_artifact=True)
        fake = _install_jarvis(monkeypatch, [_fenced(A_FIX)])

        result = _invoke(source, editor, "--max-rounds", "2", "--context", str(header))

        assert result.exit_code == 0, result.output
        # The rules are prose wrapped at ~76 columns, so a phrase can straddle a
        # line break: match on the flattened prompt, not the raw one.
        prompt = " ".join(fake.prompts[0].split())
        # the diagnostics, verbatim enough to act on
        assert "'Ask' - undeclared identifier" in prompt
        assert "compile result: FAILED" in prompt
        # the file it must return in full, and the supporting file asked for
        assert "int OnInit() { return(INIT_SUCCEEDED); }" in prompt
        assert "the included header" in prompt
        assert f"Supporting file {header.name}:" in prompt
        # the rules that keep a fix from being a deletion
        assert "COMPLETE corrected source" in prompt
        assert "Never widen risk to silence a compiler error" in prompt
        assert "SymbolInfoDouble(_Symbol, SYMBOL_ASK)" in prompt
        assert "```mql5 fence" in prompt

    def test_a_context_path_that_is_not_a_file_is_skipped(
        self, tmp_path: Path, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [FAILING_LOG, CLEAN_LOG], writes_artifact=True)
        fake = _install_jarvis(monkeypatch, [_fenced(A_FIX)])

        result = _invoke(
            source,
            editor,
            "--max-rounds",
            "2",
            "--context",
            str(tmp_path / "absent.mqh"),
        )

        assert result.exit_code == 0, result.output
        assert "Supporting file absent.mqh" not in fake.prompts[0]

    def test_the_agent_prompt_forbids_running_the_compiler(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        """Two compilers in one loop is how a round gets reported twice."""
        _script_compiler(monkeypatch, [FAILING_LOG])
        result = compile_loop.compile_source(source, metaeditor=editor, wine=False)
        assert result.ok is False, "the prompt is built from a real failing build"

        prompt = compile_loop._build_agent_prompt(source, result)

        assert "Do not run the compiler yourself" in prompt
        assert "the harness recompiles after you" in prompt
        assert "apply_patch" in prompt and "file_read" in prompt
        assert "'Ask' - undeclared identifier" in prompt
        assert str(source) in prompt

    def test_the_model_is_told_which_dialect_the_file_is(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [FAILING_LOG, CLEAN_LOG], writes_artifact=True)
        fake = _install_jarvis(monkeypatch, [_fenced(A_FIX)])

        _invoke(source, editor, "--max-rounds", "2")

        prompt = fake.prompts[0]
        assert "This is MQL5 unless the file extension says otherwise" in prompt


class TestTheReport:
    """`--json-out` is what a scheduler or CI reads instead of the console."""

    def test_the_report_records_every_round_and_the_gate(
        self, tmp_path: Path, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [FAILING_LOG, CLEAN_LOG], writes_artifact=True)
        _install_jarvis(monkeypatch, [_fenced(A_FIX)])
        report = tmp_path / "nested" / "report.json"

        result = _invoke(source, editor, "--max-rounds", "3", "--json-out", str(report))

        assert result.exit_code == 0, result.output
        assert report.exists(), "the report's directory is created for it"
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["source"] == str(source)
        assert payload["metaeditor"] == str(editor)
        assert payload["mode"] == "rewrite"
        assert payload["ok"] is True
        assert len(payload["rounds"]) == 2
        first = payload["rounds"][0]
        assert first["round"] == 1 and first["ok"] is False
        assert first["error_count"] == 1
        assert first["errors"] and "'Ask'" in first["errors"][0]
        assert first["exit_code"] == 1, "recorded, though advisory"
        assert first["artifact"] == ""
        assert "/compile:" in " ".join(first["command"])
        second = payload["rounds"][1]
        assert second["ok"] is True and second["artifact"].endswith(".ex5")

    def test_compile_only_reports_no_model(
        self, tmp_path: Path, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [CLEAN_LOG], writes_artifact=True)
        report = tmp_path / "report.json"

        _invoke(source, editor, "--compile-only", "--json-out", str(report))

        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["mode"] == "compile-only"
        assert payload["model"] == ""

    def test_the_loop_never_calls_a_model_in_compile_only_mode(
        self, source: Path, editor: Path, monkeypatch
    ) -> None:
        _script_compiler(monkeypatch, [FAILING_LOG])
        fake = _install_jarvis(monkeypatch, ["should never be asked"])

        result = _invoke(source, editor, "--compile-only", "--max-rounds", "4")

        assert result.exit_code == 1, result.output
        assert fake.prompts == [], "--compile-only must not spend a model call"
