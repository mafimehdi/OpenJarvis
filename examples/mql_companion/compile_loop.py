#!/usr/bin/env python3
"""Compile-in-the-loop fixer for MQL4/MQL5 Expert Advisors.

The MQL compiler is the only ground truth that matters when writing an Expert
Advisor: a local model will happily invent indicator handles, confuse MQL4
``OrderSend`` with the MQL5 trade request, or drop a ``const``. This script
closes the loop — compile with MetaEditor, feed the exact diagnostics back to
an OpenJarvis agent, let it fix the source, recompile — until the file
builds clean or the round budget runs out.

Usage::

    # compile only (no model call) — useful in CI or under `jarvis scheduler`
    python examples/mql_companion/compile_loop.py --source MyEA.mq5 --compile-only

    # full loop: up to 5 fix rounds with a local model
    python examples/mql_companion/compile_loop.py \\
        --source "<MT5 data folder>/MQL5/Experts/MyEA.mq5"

    # Linux/macOS through Wine, with an explicit editor + include dir
    python examples/mql_companion/compile_loop.py --source MyEA.mq5 \\
        --metaeditor ~/.wine/drive_c/MT5/metaeditor64.exe --wine \\
        --inc ~/.wine/drive_c/MT5/MQL5

    # let the agent patch the file itself with file_read / apply_patch
    python examples/mql_companion/compile_loop.py --source MyEA.mq5 --mode agent-tools

    # use the shipped preset (its model/engine win unless --model/--engine are set)
    python examples/mql_companion/compile_loop.py --source MyEA.mq5 \\
        --config configs/openjarvis/examples/mql-assistant.toml

Exit code is 0 only when the source compiles with zero errors *and* the binary
that compile owes is on disk: a zero-error log with no ``.ex5`` beside the source
is the CLI's silent failure, not a clean compile, and exits 2 like the other
toolchain problems (``--allow-missing-artifact`` relaxes it). 1 means the source
still does not compile after the round budget.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import click

# Allow running straight from a source checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from metaeditor import (  # noqa: E402
    CompileResult,
    compile_source,
    expects_artifact,
    extract_mql_source,
    find_metaeditor,
)

#: What a successful build leaves behind, for the message that says it is missing.
ARTIFACT_NAMES: tuple[str, ...] = (".ex5", ".ex4")

_REWRITE_TOOLS = ["file_read", "think"]
_AGENT_TOOLS = ["file_read", "file_write", "apply_patch", "think", "knowledge_search"]

_SYSTEM_RULES = """\
You are an expert MQL developer working on MetaTrader Expert Advisors.

Hard rules:
1. Answer with the COMPLETE corrected source file in a single ```mql5 fence.
   No commentary inside the fence, no partial snippets, no ellipses.
2. Keep every existing input, global, and event handler unless it is the
   actual cause of a compiler error.
3. This is MQL5 unless the file extension says otherwise. MQL5 has no
   predefined Ask/Bid/Point/Digits — use SymbolInfoDouble(_Symbol,
   SYMBOL_ASK), SymbolInfoDouble(_Symbol, SYMBOL_BID), _Point, _Digits.
4. Trade through <Trade\\Trade.mqh> (CTrade) unless the file already builds
   its own MqlTradeRequest.
5. Never widen risk to silence a compiler error: if a lot size, stop level,
   or margin check is in the way, fix the check — do not delete it.
6. Fix the reported diagnostics. Do not refactor anything else.
"""


def _echo(text: str = "") -> None:
    click.echo(text)


def _backup(source: Path, round_no: int) -> Path:
    """Snapshot the source before a round overwrites it."""
    backup = source.with_suffix(source.suffix + f".round{round_no}.bak")
    shutil.copy2(source, backup)
    return backup


def _report(result: CompileResult, round_no: int) -> None:
    status = "OK" if result.ok else "FAILED"
    _echo(f"  round {round_no}: {status}")
    if result.error_text:
        _echo(f"    ! {result.error_text}")
    if result.command:
        _echo(f"    cmd: {' '.join(result.command)}")
    if result.exit_code is not None:
        _echo(f"    exit code: {result.exit_code} (advisory only)")
    if result.diagnostics:
        for diag in result.errors[:15]:
            _echo(f"    error   {diag.format()}")
        for diag in result.warnings[:5]:
            _echo(f"    warning {diag.format()}")
    for line in result.other_lines[:5]:
        _echo(f"    | {line}")
    if result.artifact:
        _echo(f"    artifact: {result.artifact}")
    if result.note:
        _echo(f"    note: {result.note}")


def _build_rewrite_prompt(
    source: Path, result: CompileResult, context: list[str]
) -> str:
    body = source.read_text(encoding="utf-8", errors="replace")
    parts = [
        "Fix the MQL source below so it compiles with zero errors.",
        "",
        f"File: {source.name}",
        "",
        "Compiler output:",
        "```",
        result.format_for_prompt(),
        "```",
        "",
        "Current source:",
        "```mql5",
        body,
        "```",
    ]
    for extra in context:
        path = Path(extra).expanduser()
        if path.is_file():
            parts.extend(
                [
                    "",
                    f"Supporting file {path.name}:",
                    "```mql5",
                    path.read_text(encoding="utf-8", errors="replace"),
                    "```",
                ]
            )
    parts.extend(
        [
            "",
            "Return the complete corrected file in one ```mql5 fence.",
        ]
    )
    return "\n".join(parts)


def _build_agent_prompt(source: Path, result: CompileResult) -> str:
    return (
        "Fix the MQL source file in place so it compiles with zero errors.\n\n"
        f"File: {source}\n\n"
        "Compiler output:\n"
        f"```\n{result.format_for_prompt()}\n```\n\n"
        "Steps:\n"
        "1. file_read the source file.\n"
        "2. think through each reported diagnostic and its root cause.\n"
        "3. Apply the smallest correct fix with apply_patch (or file_write if "
        "the change is large).\n"
        "4. Reply with a one-line summary of what you changed.\n"
        "Do not run the compiler yourself; the harness recompiles after you."
    )


@click.command()
@click.option(
    "--source",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the .mq5 / .mq4 file to compile and fix.",
)
@click.option(
    "--metaeditor",
    default=None,
    help="Path to metaeditor64.exe (auto-detected otherwise; "
    "METAEDITOR_PATH also works).",
)
@click.option(
    "--inc",
    "include_dir",
    default=None,
    help="MQL5/MQL4 include directory passed to /inc (guessed otherwise).",
)
@click.option(
    "--wine/--no-wine",
    default=None,
    help="Force or forbid the Wine prefix on Linux/macOS (auto by default).",
)
@click.option(
    "--max-rounds",
    default=5,
    show_default=True,
    type=int,
    help="Maximum fix rounds before giving up.",
)
@click.option(
    "--mode",
    type=click.Choice(["rewrite", "agent-tools"]),
    default="rewrite",
    show_default=True,
    help="rewrite = the model returns the whole file and this script writes it; "
    "agent-tools = the agent patches the file itself via tools.",
)
@click.option(
    "--context",
    multiple=True,
    help="Extra file (e.g. an included .mqh) to show the model. Repeatable.",
)
@click.option(
    "--model",
    default=None,
    help="Model id for the fix step. Default: whatever the loaded config says.",
)
@click.option(
    "--engine",
    "engine_key",
    default=None,
    help="Engine backend (ollama, cloud, vllm, ...). Default: from the config.",
)
@click.option(
    "--agent",
    default="native_react",
    show_default=True,
    help="Agent used for the fix step.",
)
@click.option(
    "--config",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Config TOML to load instead of the default "
        "(e.g. configs/openjarvis/examples/mql-assistant.toml)."
    ),
)
@click.option(
    "--compile-only",
    is_flag=True,
    help="Compile and report only — never call a model.",
)
@click.option(
    "--syntax-only",
    is_flag=True,
    help="Pass /s to MetaEditor (syntax check, no .ex5 artifact).",
)
@click.option("--timeout", default=180, show_default=True, help="Per-compile timeout.")
@click.option(
    "--backup/--no-backup",
    default=True,
    show_default=True,
    help="Snapshot the source as <file>.roundN.bak before each fix.",
)
@click.option(
    "--allow-missing-artifact",
    is_flag=True,
    help="Call a zero-error log a success even when no .ex5/.ex4 appeared beside "
    "the source. Off by default: MetaEditor's CLI is documented to fail silently "
    "on large modular projects (0 errors, no artifact — "
    "mql5.com/en/forum/491543, fixed in build 5200), and a build that produced "
    "no binary is not a clean compile.",
)
@click.option(
    "--json-out",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a machine-readable round-by-round report (for CI/scheduler).",
)
def main(
    source: Path,
    metaeditor: str | None,
    include_dir: str | None,
    wine: bool | None,
    max_rounds: int,
    mode: str,
    context: tuple[str, ...],
    model: str | None,
    engine_key: str | None,
    agent: str,
    config: Path | None,
    compile_only: bool,
    syntax_only: bool,
    timeout: int,
    backup: bool,
    allow_missing_artifact: bool,
    json_out: Path | None,
) -> None:
    """Compile an MQL source with MetaEditor, fixing errors in a loop."""
    editor = find_metaeditor(metaeditor)
    if editor is None:
        click.echo(
            "Error: MetaEditor not found.\n"
            "  - Windows: install MetaTrader 5, or pass --metaeditor "
            "'C:\\Program Files\\MetaTrader 5\\metaeditor64.exe'\n"
            "  - Linux/macOS: install MetaTrader under Wine and pass "
            "--metaeditor ~/.wine/drive_c/.../metaeditor64.exe --wine\n"
            "  - Or set METAEDITOR_PATH in the environment.",
            err=True,
        )
        sys.exit(2)

    _echo(f"source:     {source}")
    _echo(f"metaeditor: {editor}")
    _echo(f"mode:       {'compile-only' if compile_only else mode}")
    if not compile_only:
        _echo(
            f"model:      {model or '(from config)'}  |  "
            f"engine: {engine_key or '(from config)'}  |  agent: {agent}"
        )
        if config:
            _echo(f"config:     {config}")
    _echo("-" * 68)

    jarvis = None
    if not compile_only:
        try:
            from openjarvis import Jarvis
        except ImportError:
            click.echo(
                "Error: openjarvis is not installed. "
                "Install it with:  uv sync --extra dev\n"
                "(or re-run with --compile-only to just compile)",
                err=True,
            )
            sys.exit(1)
        try:
            jarvis = Jarvis(
                config_path=str(config) if config else None,
                model=model,
                engine_key=engine_key,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced to the user verbatim
            click.echo(
                f"Error: could not initialize Jarvis — {exc}\n\n"
                "Make sure your engine is running (ollama serve), or pass "
                "--compile-only to skip the model entirely.",
                err=True,
            )
            sys.exit(1)

    rounds: list[dict] = []
    result: CompileResult | None = None
    previous_body = ""

    try:
        for round_no in range(1, max_rounds + 1):
            result = compile_source(
                source,
                metaeditor=editor,
                log_path=None,
                include_dir=include_dir,
                syntax_only=syntax_only,
                wine=wine,
                timeout=timeout,
            )
            _report(result, round_no)
            rounds.append(
                {
                    "round": round_no,
                    "ok": result.ok,
                    "exit_code": result.exit_code,
                    "error_count": result.error_count,
                    "warning_count": len(result.warnings),
                    "artifact": str(result.artifact) if result.artifact else "",
                    "duration_seconds": round(result.duration_seconds, 3),
                    "errors": [d.format() for d in result.errors],
                    "warnings": [d.format() for d in result.warnings],
                    "other_lines": list(result.other_lines),
                    "error_text": result.error_text,
                    "note": result.note,
                    "command": list(result.command),
                }
            )

            if result.ok:
                break

            if compile_only:
                break

            # An unusable toolchain (no log at all) is not something a model
            # can fix by editing source — stop and report instead of looping.
            if result.error_text and not result.diagnostics:
                _echo("  stopping: toolchain problem, not a source problem.")
                break

            if backup:
                _echo(f"  backup: {_backup(source, round_no)}")

            # Whatever the fix step claims, the file is the only evidence: an
            # agent that reports a patch it never wrote, and a model that echoes
            # the source straight back, both leave it byte-identical and would
            # otherwise spend the whole round budget on the same diagnostics.
            before_fix = source.read_bytes()

            if mode == "agent-tools":
                prompt = _build_agent_prompt(source, result)
                try:
                    answer = jarvis.ask(prompt, agent=agent, tools=_AGENT_TOOLS)
                except Exception as exc:  # noqa: BLE001
                    _echo(f"  agent failed: {exc}")
                    break
                _echo(
                    f"  agent: {str(answer).splitlines()[-1][:120] if answer else ''}"
                )
                if source.read_bytes() == before_fix:
                    _echo("  stopping: the agent changed nothing on disk.")
                    break
            else:
                # `Jarvis.ask()` takes no system_prompt kwarg, so the rules go
                # at the front of the user prompt.
                prompt = (
                    _SYSTEM_RULES
                    + "\n\n"
                    + _build_rewrite_prompt(source, result, list(context))
                )
                try:
                    answer = jarvis.ask(prompt, agent=agent, tools=_REWRITE_TOOLS)
                except Exception as exc:  # noqa: BLE001
                    _echo(f"  model failed: {exc}")
                    break

                body = extract_mql_source(str(answer))
                if not body:
                    _echo("  stopping: model returned no MQL source block.")
                    break
                if body == previous_body:
                    _echo("  stopping: model produced no change (loop guard).")
                    break
                previous_body = body
                source.write_text(body, encoding="utf-8")
                if source.read_bytes() == before_fix:
                    _echo("  stopping: the model returned the file unchanged.")
                    break
                _echo(f"  wrote {len(body.splitlines())} lines to {source.name}")
    finally:
        if jarvis is not None:
            jarvis.close()

    _echo("-" * 68)
    if result is None:
        _echo("no compile result")
        sys.exit(2)

    # A zero-error log that owes a binary and produced none is not a clean
    # compile. `result.ok` stays the log's verdict; this is the build's.
    missing_artifact = (
        result.ok
        and not syntax_only
        and not allow_missing_artifact
        and expects_artifact(source)
        and result.artifact is None
    )

    if missing_artifact:
        _echo(
            f"NO ARTIFACT after {len(rounds)} round(s) — the compiler reported "
            f"{result.error_count} error(s) but no "
            f"{'/'.join(ARTIFACT_NAMES)} appeared beside the source."
        )
        if result.note:
            _echo(f"  ! {result.note}")
        _echo(
            "  A toolchain problem, not a source problem: another model round "
            "cannot fix it."
        )
    elif result.ok:
        _echo(f"SUCCESS after {len(rounds)} round(s).")
        if result.artifact:
            _echo(f"artifact: {result.artifact}")
    else:
        _echo(f"FAILED after {len(rounds)} round(s) — {result.error_count} error(s).")
        if result.error_text:
            _echo(f"  ! {result.error_text}")
        for diag in result.errors[:20]:
            _echo(f"  {diag.format()}")

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps(
                {
                    "source": str(source),
                    "metaeditor": str(editor),
                    "mode": "compile-only" if compile_only else mode,
                    "model": "" if compile_only else model,
                    # The gate CI reads, and it means what the exit code means:
                    # a zero-error log with no binary is not a success. Each
                    # round keeps the compiler's own verdict under rounds[].ok.
                    "ok": bool(result.ok and not missing_artifact),
                    "artifact_missing": bool(missing_artifact),
                    "rounds": rounds,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        _echo(f"report: {json_out}")

    if missing_artifact:
        # Same reasoning as the unreadable-log case below: another model round
        # against a compiler that wrote nothing cannot help.
        sys.exit(2)
    if result.error_text and not result.diagnostics:
        # The documented contract: 2 is a toolchain problem, 1 is a source that
        # still does not compile. "FAILED — 0 error(s)" is neither when
        # MetaEditor left no log this parser could read, and CI should not
        # retry a model round against a broken toolchain.
        sys.exit(2)
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
