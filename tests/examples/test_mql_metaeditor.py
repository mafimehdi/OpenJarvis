"""Tests for examples/mql_companion/metaeditor.py.

The MetaEditor toolchain only exists on Windows (or under Wine), so everything
here exercises the platform-independent parts: log decoding, log parsing,
command construction, artifact detection, and ``compile_source`` with a stubbed
``subprocess.run`` that writes a realistic UTF-16 log.

Loaded by path (the examples tree is not an importable package), following the
same pattern as ``tests/pearl/test_model_converter.py``.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "examples" / "mql_companion" / "metaeditor.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("mql_metaeditor", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registering before exec is required: the module uses
    # `from __future__ import annotations` plus @dataclass, and dataclasses
    # resolves annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


me = _load_module()

_EA = "C:\\MQL5\\Experts\\MyEA.mq5"
_UTF16_LOG = (
    f"{_EA}(37,12) : error C2065: 'lots' - undeclared identifier\r\n"
    f"{_EA}(41,4) : warning C4101: 'unused' - unreferenced variable\r\n"
    f"{_EA}(12,1) : information: see declaration\r\n"
    "Result: 1 errors, 1 warnings\r\n"
)


class TestDecodeCompileLog:
    def test_utf16_with_bom(self) -> None:
        raw = _UTF16_LOG.encode("utf-16")  # includes the BOM
        assert "undeclared identifier" in me.decode_compile_log(raw)

    def test_utf16le_without_bom(self) -> None:
        raw = _UTF16_LOG.encode("utf-16-le")
        assert "undeclared identifier" in me.decode_compile_log(raw)

    def test_plain_utf8(self) -> None:
        assert "Result" in me.decode_compile_log(
            "Result: 0 errors, 0 warnings".encode()
        )

    def test_empty(self) -> None:
        assert me.decode_compile_log(b"") == ""

    def test_undecodable_bytes_do_not_raise(self) -> None:
        assert isinstance(me.decode_compile_log(b"\xff\x00\x01 garbage"), str)

    def test_utf16be_without_bom_is_decoded(self) -> None:
        """NUL parity gives the byte order, and guessing wrong fails silently.

        A mis-read order does not raise: it decodes to plausible CJK glyphs, the
        summary regex matches nothing, and every count reads as zero.
        """
        raw = "Result: 0 errors, 0 warnings\n".encode("utf-16-be")
        text = me.decode_compile_log(raw)
        assert "0 errors" in text
        assert me.parse_compile_log(text)[2] == 0

    def test_utf8_bom_does_not_leak_into_the_first_diagnostic(self) -> None:
        raw = (
            "\ufeffMyEA.mq5(12,5) : error C2065: 'lot' - undeclared identifier\n"
            "0 errors, 0 warnings\n"
        ).encode("utf-8")
        diags, _other, errors, _w = me.parse_compile_log(me.decode_compile_log(raw))
        assert errors == 0
        assert len(diags) == 1
        assert diags[0].file == "MyEA.mq5"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert me.read_compile_log(tmp_path / "nope.log") == ""


class TestParseCompileLog:
    def test_positional_errors_and_summary(self) -> None:
        diags, other, errors, warnings = me.parse_compile_log(_UTF16_LOG)
        assert (errors, warnings) == (1, 1)
        assert len(diags) == 3
        by_severity = {d.severity for d in diags}
        assert by_severity == {"error", "warning", "information"}
        err = next(d for d in diags if d.severity == "error")
        assert err.line == 37
        assert err.column == 12
        assert err.is_error
        assert "undeclared identifier" in err.message
        assert err.file.endswith("MyEA.mq5")
        assert other == []

    def test_clean_compile(self) -> None:
        diags, other, errors, warnings = me.parse_compile_log(
            "Result: 0 errors, 0 warnings"
        )
        assert diags == []
        assert (errors, warnings) == (0, 0)
        assert other == []

    def test_parenthesised_summary_variant(self) -> None:
        _diags, _other, errors, warnings = me.parse_compile_log(
            "0 error(s), 2 warning(s)"
        )
        assert (errors, warnings) == (0, 2)

    def test_tabular_shape(self) -> None:
        diags, other, _e, _w = me.parse_compile_log(
            "'x' - undeclared identifier\tMyEA.mq5\t37\t12"
        )
        assert len(diags) == 1
        assert diags[0].line == 37 and diags[0].column == 12
        assert diags[0].severity == "error"
        assert other == []

    def test_fatal_error_counts_as_error(self) -> None:
        diags, _other, _e, _w = me.parse_compile_log(
            "MyEA.mq5(3,1) : fatal error C1001: internal compiler error"
        )
        assert diags[0].severity == "error"
        assert diags[0].is_error

    def test_unknown_lines_are_preserved(self) -> None:
        text = "Some build banner we do not understand\nResult: 0 errors, 0 warnings"
        _diags, other, _e, _w = me.parse_compile_log(text)
        assert other == ["Some build banner we do not understand"]

    def test_a_log_with_nothing_parseable_yields_no_evidence(self) -> None:
        """The shape ``compile_source`` refuses to call a success.

        No summary and no diagnostic: whatever MetaEditor meant by it, the
        parser holds no evidence either way.
        """
        diags, other, errors, warnings = me.parse_compile_log(
            "MetaEditor 5 build 4620\nsome banner we do not know\n"
        )
        assert diags == []
        assert (errors, warnings) == (None, None)
        assert len(other) == 2

    def test_blank_lines_ignored(self) -> None:
        _diags, other, _e, _w = me.parse_compile_log("\n\n   \n")
        assert other == []


class TestCommandBuilding:
    def test_default_command(self, tmp_path: Path) -> None:
        editor = tmp_path / "metaeditor64.exe"
        source = tmp_path / "MyEA.mq5"
        cmd = me.build_compile_command(editor, source)
        assert cmd[-3] == str(editor)
        assert f"/compile:{source}" in cmd
        assert "/log" in cmd
        assert "/s" not in cmd

    def test_explicit_log_include_and_syntax(self, tmp_path: Path) -> None:
        editor = tmp_path / "metaeditor64.exe"
        source = tmp_path / "MyEA.mq5"
        cmd = me.build_compile_command(
            editor,
            source,
            log_path=tmp_path / "build.log",
            include_dir=tmp_path / "MQL5",
            syntax_only=True,
            wine=False,
        )
        assert f"/log:{tmp_path / 'build.log'}" in cmd
        assert f"/inc:{tmp_path / 'MQL5'}" in cmd
        assert "/s" in cmd
        assert "wine" not in cmd

    def test_wine_prefix_is_forced(self, tmp_path: Path) -> None:
        cmd = me.build_compile_command(
            tmp_path / "metaeditor64.exe", tmp_path / "MyEA.mq5", wine=True
        )
        assert cmd[0] == "wine"

    def test_needs_wine_detects_windows_binary(self, tmp_path: Path) -> None:
        if os.name == "nt":
            pytest.skip("Wine detection is a non-Windows concern")
        assert me.needs_wine(tmp_path / "metaeditor64.exe") is True
        assert me.needs_wine(tmp_path / "metaeditor") is False

    def test_default_log_path_sits_next_to_source(self, tmp_path: Path) -> None:
        source = tmp_path / "Experts" / "MyEA.mq5"
        assert me.default_log_path(source) == tmp_path / "Experts" / "MyEA.mq5.log"


class TestArtifactDetection:
    def _write(self, path: Path, mtime: float) -> Path:
        path.write_text("x")
        os.utime(path, (mtime, mtime))
        return path

    def test_fresh_artifact_is_found(self, tmp_path: Path) -> None:
        src = self._write(tmp_path / "MyEA.mq5", 1_000.0)
        self._write(tmp_path / "MyEA.ex5", 2_000.0)
        assert me.find_artifact(src) == tmp_path / "MyEA.ex5"

    def test_stale_artifact_is_rejected(self, tmp_path: Path) -> None:
        src = self._write(tmp_path / "MyEA.mq5", 5_000.0)
        self._write(tmp_path / "MyEA.ex5", 1_000.0)
        assert me.find_artifact(src) is None

    def test_missing_artifact(self, tmp_path: Path) -> None:
        src = self._write(tmp_path / "MyEA.mq5", 1_000.0)
        assert me.find_artifact(src) is None

    def test_artifact_predating_the_run_is_rejected(self, tmp_path: Path) -> None:
        """``since`` closes the window the one-second margin leaves open.

        An artifact a fraction of a second older than the source still looks
        fresh on the strength of the margin alone — enough for a previous
        build's ``.ex5`` to be credited to a run that never wrote one.
        """
        source = tmp_path / "MyEA.mq5"
        source.write_text("// ea")
        artifact = tmp_path / "MyEA.ex5"
        artifact.write_text("binary")
        old = time.time() - 5.0
        os.utime(artifact, (old, old))
        os.utime(source, (old + 0.5, old + 0.5))

        assert me.find_artifact(source) == artifact
        assert me.find_artifact(source, since=time.time()) is None

    def test_mq4_maps_to_ex4(self, tmp_path: Path) -> None:
        src = self._write(tmp_path / "MyEA.mq4", 1_000.0)
        self._write(tmp_path / "MyEA.ex4", 2_000.0)
        assert me.find_artifact(src) == tmp_path / "MyEA.ex4"

    def test_unknown_extension(self, tmp_path: Path) -> None:
        src = self._write(tmp_path / "notes.txt", 1_000.0)
        assert me.find_artifact(src) is None


class TestHeaderCompiles:
    """A ``.mqh`` is compilable, and compiles to nothing.

    MetaEditor inlines a header into whatever includes it and writes only that
    program's ``.ex5``/``.ex4``. Mapping ``.mqh`` to an artifact of its own made
    ``find_artifact`` look for a file no build will ever write, and made a clean
    header compile carry the note reserved for the CLI's silent-failure bug
    (mql5.com/en/forum/491543, fixed in build 5200).
    """

    def _compile(self, tmp_path: Path, monkeypatch, name: str) -> "me.CompileResult":
        """Run a fake MetaEditor that logs a clean build for ``name``."""
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        source = tmp_path / name
        source.write_text("// syntax only")
        log_path = me.default_log_path(source)

        def run(cmd, **kwargs):
            # The compiler writes the log, not the test: compile_source clears a
            # stale one first and then waits for the summary to appear.
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_bytes("Result: 0 errors, 0 warnings".encode("utf-16"))
            return subprocess.CompletedProcess(cmd, 1, "", "")

        monkeypatch.setattr(me.subprocess, "run", run)
        return me.compile_source(source, metaeditor=editor, wine=False)

    def test_a_header_is_a_source_that_owes_no_artifact(self) -> None:
        assert ".mqh" in me.MQL_SOURCE_EXTS, "a header is compilable"
        assert ".mqh" not in me.ARTIFACT_EXT, "but it has no artifact of its own"
        assert me.expects_artifact(Path("Include/MyLib.mqh")) is False
        assert me.expects_artifact(Path("MyEA.mq5")) is True
        assert me.expects_artifact(Path("MyEA.MQ4")) is True, "case-insensitive"

    def test_nothing_is_looked_for_beside_a_header(self, tmp_path: Path) -> None:
        header = tmp_path / "MyLib.mqh"
        header.write_text("// constants")
        assert me.find_artifact(header) is None
        # A same-named .ex5 sitting there belongs to nothing: it is not what a
        # header compiles into, and claiming it would be worse than claiming none.
        (tmp_path / "MyLib.ex5").write_bytes(b"\x00")
        assert me.find_artifact(header) is None

    def test_a_clean_header_build_is_not_called_a_silent_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        result = self._compile(tmp_path, monkeypatch, "MyLib.mqh")
        assert result.ok is True, result.error_text
        assert result.artifact is None
        assert "silent" not in result.note and ".ex5" not in result.note

    def test_a_program_that_owed_an_ex5_still_gets_the_note(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The narrowing is not a blanket silence: the real bug still surfaces."""
        result = self._compile(tmp_path, monkeypatch, "MyEA.mq5")
        assert result.ok is True, result.error_text
        assert result.artifact is None
        assert "silent CLI failure" in result.note

    def test_the_exit_code_is_not_what_decides_a_build(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A published log shows metaeditor.exe exiting 1 on a run whose own
        summary read ``Result: 0 error(s), 0 warning(s)``
        (mql5.com/en/forum/157533), so the fake above exits 1 on purpose."""
        result = self._compile(tmp_path, monkeypatch, "MyEA.mq5")
        assert result.exit_code == 1
        assert result.ok is True, "the log decides, not the process"


class TestFindMetaEditor:
    def test_env_var_is_honoured(self, tmp_path: Path, monkeypatch) -> None:
        fake = tmp_path / "metaeditor64.exe"
        fake.write_text("not really a PE file")
        monkeypatch.setenv("METAEDITOR_PATH", str(fake))
        assert me.find_metaeditor() == fake

    def test_explicit_missing_path_returns_none(self, tmp_path: Path) -> None:
        assert me.find_metaeditor(str(tmp_path / "nope.exe")) is None

    def test_explicit_existing_path_is_returned(self, tmp_path: Path) -> None:
        fake = tmp_path / "editor.exe"
        fake.write_text("x")
        assert me.find_metaeditor(str(fake)) == fake

    def test_candidates_only_lists_existing_files(self) -> None:
        for path in me.candidate_metaeditor_paths():
            assert path.is_file()


class TestCompileSource:
    def _fake_run(self, monkeypatch, log_text: str, log_path: Path, returncode: int):
        calls = {}

        def run(cmd, **kwargs):
            calls["cmd"] = cmd
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_bytes(log_text.encode("utf-16"))
            return subprocess.CompletedProcess(cmd, returncode, "", "")

        monkeypatch.setattr(me.subprocess, "run", run)
        return calls

    def test_successful_compile(self, tmp_path: Path, monkeypatch) -> None:
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        source = tmp_path / "MyEA.mq5"
        source.write_text("int OnInit() { return(INIT_SUCCEEDED); }")
        log_path = me.default_log_path(source)
        calls = self._fake_run(
            monkeypatch, "Result: 0 errors, 0 warnings", log_path, returncode=0
        )

        result = me.compile_source(source, metaeditor=editor, wine=False)

        assert result.ok is True
        assert result.error_count == 0
        assert result.diagnostics == []
        assert result.exit_code == 0
        assert "/compile:" in " ".join(calls["cmd"])
        assert result.log_path == log_path

    def test_failed_compile_is_parsed(self, tmp_path: Path, monkeypatch) -> None:
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        source = tmp_path / "MyEA.mq5"
        source.write_text("int OnInit() { return(INIT_SUCCEEDED) }")
        self._fake_run(monkeypatch, _UTF16_LOG, me.default_log_path(source), 1)

        result = me.compile_source(source, metaeditor=editor, wine=False)

        assert result.ok is False
        assert result.error_count == 1
        assert len(result.errors) == 1
        assert result.errors[0].line == 37
        assert len(result.warnings) == 1
        prompt_text = result.format_for_prompt()
        assert "FAILED" in prompt_text
        assert "undeclared identifier" in prompt_text

    def test_artifact_is_reported_when_fresh(self, tmp_path: Path, monkeypatch) -> None:
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        source = tmp_path / "MyEA.mq5"
        source.write_text("// ea")
        artifact = tmp_path / "MyEA.ex5"

        def run(cmd, **kwargs):
            me.default_log_path(source).write_bytes(
                "Result: 0 errors, 0 warnings".encode("utf-16")
            )
            artifact.write_text("binary")  # created "by" the compiler
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(me.subprocess, "run", run)
        result = me.compile_source(source, metaeditor=editor, wine=False)
        assert result.ok is True
        assert result.artifact == artifact

    def test_clean_log_without_artifact_is_flagged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """MetaEditor's CLI can report 0 errors and still write no .ex5."""
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        source = tmp_path / "MyEA.mq5"
        source.write_text("// ea")
        self._fake_run(
            monkeypatch, "Result: 0 errors, 0 warnings", me.default_log_path(source), 0
        )

        result = me.compile_source(source, metaeditor=editor, wine=False)

        assert result.ok is True
        assert result.artifact is None
        assert "silent CLI failure" in result.note
        assert "note:" in result.format_for_prompt()

    def test_syntax_only_skips_artifact_check(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        source = tmp_path / "MyEA.mq5"
        source.write_text("// ea")
        calls = self._fake_run(
            monkeypatch, "Result: 0 errors, 0 warnings", me.default_log_path(source), 0
        )
        result = me.compile_source(
            source, metaeditor=editor, syntax_only=True, wine=False
        )
        assert result.artifact is None
        assert "/s" in calls["cmd"]

    def test_missing_log_is_a_toolchain_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        source = tmp_path / "MyEA.mq5"
        source.write_text("// ea")

        def run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, "", "wine: not found")

        monkeypatch.setattr(me.subprocess, "run", run)
        result = me.compile_source(
            source, metaeditor=editor, wine=False, log_settle_seconds=0.0
        )
        assert result.ok is False
        assert "no log file" in result.error_text
        assert "wine: not found" in result.error_text

    def test_timeout_is_reported(self, tmp_path: Path, monkeypatch) -> None:
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        source = tmp_path / "MyEA.mq5"
        source.write_text("// ea")

        def run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

        monkeypatch.setattr(me.subprocess, "run", run)
        result = me.compile_source(source, metaeditor=editor, timeout=1, wine=False)
        assert result.ok is False
        assert "timed out" in result.error_text

    def test_missing_source(self, tmp_path: Path) -> None:
        result = me.compile_source(tmp_path / "nope.mq5", metaeditor=None)
        assert result.ok is False
        assert "not found" in result.error_text

    def test_unsupported_extension(self, tmp_path: Path) -> None:
        source = tmp_path / "notes.txt"
        source.write_text("hello")
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        result = me.compile_source(source, metaeditor=editor)
        assert result.ok is False
        assert "unsupported extension" in result.error_text

    def test_summary_cannot_clear_parsed_errors(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A log that contradicts itself is not a clean build.

        The summary stays authoritative when it reports *more* errors than the
        parser found — an included file's diagnostics are counted without always
        being listed. The reverse is never believed.
        """
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        source = tmp_path / "MyEA.mq5"
        source.write_text("// ea")
        self._fake_run(
            monkeypatch,
            "MyEA.mq5(12,5) : error C2065: 'lot' - undeclared identifier\n"
            "MyEA.mq5(20,1) : error C1001: unexpected end of file\n"
            "0 errors, 0 warnings\n",
            me.default_log_path(source),
            0,
        )

        result = me.compile_source(source, metaeditor=editor, wine=False)

        assert result.ok is False
        assert result.error_count == 2
        assert "higher count" in result.note

    def test_unparseable_log_is_a_toolchain_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        source = tmp_path / "MyEA.mq5"
        source.write_text("// ea")
        self._fake_run(
            monkeypatch, "MetaEditor 5 build 4620\n", me.default_log_path(source), 0
        )

        result = me.compile_source(source, metaeditor=editor, wine=False)

        assert result.ok is False
        assert "could not be confirmed" in result.error_text
        assert "MetaEditor 5 build 4620" in result.error_text

    def test_syntax_only_run_needs_evidence_too(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """``/s`` has no artifact to cross-check, so the log is all the proof."""
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        source = tmp_path / "MyEA.mq5"
        source.write_text("// ea")
        self._fake_run(monkeypatch, "junk\n", me.default_log_path(source), 0)

        result = me.compile_source(
            source, metaeditor=editor, syntax_only=True, wine=False
        )

        assert result.ok is False
        assert result.note == ""

    def test_artifact_from_a_previous_run_is_not_credited(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A rebuild that wrote nothing must not inherit the last ``.ex5``."""
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("x")
        source = tmp_path / "MyEA.mq5"
        source.write_text("// ea")
        artifact = tmp_path / "MyEA.ex5"
        artifact.write_text("binary")
        old = time.time() - 30.0
        os.utime(artifact, (old, old))

        def run(cmd, **kwargs):
            me.default_log_path(source).write_bytes(
                "Result: 0 errors, 0 warnings".encode("utf-16")
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(me.subprocess, "run", run)

        result = me.compile_source(source, metaeditor=editor, wine=False)

        assert result.ok is True
        assert result.artifact is None
        assert "silent CLI failure" in result.note

    def test_no_editor_found(self, tmp_path: Path, monkeypatch) -> None:
        source = tmp_path / "MyEA.mq5"
        source.write_text("// ea")
        monkeypatch.setattr(me, "find_metaeditor", lambda explicit=None: None)
        result = me.compile_source(source)
        assert result.ok is False
        assert "MetaEditor not found" in result.error_text


class TestExtractMqlSource:
    def test_prefers_tagged_fence(self) -> None:
        text = "prose\n```mql5\nint OnInit() { return(INIT_SUCCEEDED); }\n```\nmore"
        assert "OnInit" in me.extract_mql_source(text)

    def test_unmarked_but_mql_shaped_fence(self) -> None:
        text = "```\n#property strict\nvoid OnTick() { }\n```"
        assert "OnTick" in me.extract_mql_source(text)

    def test_prose_only_returns_empty(self) -> None:
        assert me.extract_mql_source("I cannot help with that.") == ""

    def test_bare_mql_source_is_returned(self) -> None:
        text = "void OnTick() { SymbolInfoDouble(_Symbol, SYMBOL_ASK); }"
        assert me.extract_mql_source(text).startswith("void OnTick")
