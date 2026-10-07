"""Tests for the bundled mql5-expert example skill.

The skill lives in ``examples/mql_companion/skills/mql5-expert/`` and is
installed with ``examples/mql_companion/install_skill.py``. These tests guard
the two things that are easy to break without noticing:

* the manifest must survive the framework's strict parser (agentskills.io
  naming rules) and remain a *hybrid* skill — structured steps **and**
  markdown instructions;
* the pipeline must stay runnable offline: every step tool must be registered,
  and every ``arguments_template`` must render to valid JSON. SkillExecutor
  substitutes ``{key}`` placeholders raw, so interpolating a whole source file
  into a template produces invalid JSON and aborts the pipeline at that step.

The last test runs the mql-bench MQL4-ism scanner over the shipped EA template:
the skill tells agents what correct MQL5 looks like, so the template must not
contain the idioms the benchmark penalizes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openjarvis.skills.executor import SkillExecutor
from openjarvis.skills.loader import discover_skills, load_skill_directory
from openjarvis.skills.parser import SkillParser
from openjarvis.skills.security import DANGEROUS_CAPABILITIES
from openjarvis.skills.types import SkillManifest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "examples" / "mql_companion" / "skills" / "mql5-expert"


@pytest.fixture(scope="module")
def manifest() -> SkillManifest:
    return load_skill_directory(SKILL_DIR)


class TestManifest:
    def test_skill_directory_exists(self) -> None:
        assert (SKILL_DIR / "SKILL.md").is_file()
        assert (SKILL_DIR / "skill.toml").is_file()

    def test_loads_as_hybrid_skill(self, manifest: SkillManifest) -> None:
        assert manifest.name == "mql5-expert"
        assert manifest.version == "0.1.0"
        assert manifest.steps, "expected a structured pipeline"
        assert manifest.markdown_content.strip(), "expected SKILL.md instructions"

    def test_passes_strict_frontmatter_validation(self) -> None:
        raw = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert raw.startswith("---")
        body = raw[3:]
        frontmatter_text = body[: body.find("\n---")]
        import yaml

        frontmatter = yaml.safe_load(frontmatter_text)
        # Raises SkillParseError on any naming/length violation.
        parsed = SkillParser().parse_frontmatter(frontmatter, markdown_content="body")
        assert parsed.name == "mql5-expert"
        assert len(parsed.description) <= 1024

    def test_metadata(self, manifest: SkillManifest) -> None:
        assert "mql5" in manifest.tags
        assert "metatrader" in manifest.tags
        assert manifest.author == "openjarvis"

    def test_capabilities_are_canonical_and_not_dangerous(
        self, manifest: SkillManifest
    ) -> None:
        from openjarvis.security.capabilities import Capability

        valid = {c.value for c in Capability}
        for capability in manifest.required_capabilities:
            assert capability in valid, f"unknown capability {capability!r}"
        assert not (set(manifest.required_capabilities) & DANGEROUS_CAPABILITIES), (
            "dangerous capabilities would gate `jarvis skill install` behind "
            "--yes-dangerous"
        )

    def test_discovered_from_the_skills_root(self) -> None:
        names = {m.name for m in discover_skills(SKILL_DIR.parent)}
        assert "mql5-expert" in names


class TestPipeline:
    def test_step_tools_are_registered(self, manifest: SkillManifest) -> None:
        # The autouse `_clean_registries` fixture clears ToolRegistry per test
        # and a plain `import openjarvis.tools` cannot re-trigger the class
        # decorators, so reload the two modules this pipeline needs.
        import importlib
        import sys

        import openjarvis.tools.file_read  # noqa: F401
        import openjarvis.tools.think  # noqa: F401
        from openjarvis.core.registry import ToolRegistry

        for mod_name in ("openjarvis.tools.file_read", "openjarvis.tools.think"):
            importlib.reload(sys.modules[mod_name])

        for step in manifest.steps:
            assert step.tool_name, "every step must name a tool"
            assert ToolRegistry.contains(step.tool_name), (
                f"step tool {step.tool_name!r} is not registered; "
                "`jarvis skill run` would refuse to start the pipeline"
            )
            assert step.output_key, "every step must store its output"

    def test_pipeline_stays_within_common_presets(
        self, manifest: SkillManifest
    ) -> None:
        # `jarvis skill run` refuses skills whose tools are not in
        # tools.enabled, so the pipeline is deliberately limited to the two
        # tools enabled by essentially every preset.
        used = {step.tool_name for step in manifest.steps}
        assert used <= {"file_read", "think"}

    @pytest.mark.parametrize(
        "context",
        [
            {"file_path": "MyEA.mq5"},
            # Forward slashes: SkillExecutor substitutes {key} raw, so a
            # backslash Windows path lands in the JSON string as an invalid
            # escape (see the xfail test below).
            {"file_path": "C:/MQL5/Experts/Grid EA.mq5", "focus": "lot sizing"},
            {"file_path": "/home/trader/mql5/Experts/MyEA.mq5"},
            {},  # missing placeholders must still render to valid JSON
        ],
    )
    def test_templates_render_to_valid_json(
        self, manifest: SkillManifest, context: dict
    ) -> None:
        for step in manifest.steps:
            rendered = SkillExecutor._render_template(step.arguments_template, context)
            parsed = json.loads(rendered)
            assert isinstance(parsed, dict)

    @pytest.mark.xfail(
        reason=(
            "Known framework limitation: SkillExecutor._render_template does raw "
            "{key} substitution, so backslashes in a Windows path produce "
            "invalid JSON. Pass forward-slash paths to `jarvis skill run`, or "
            "use examples/mql_companion/compile_loop.py, which takes argv. "
            "Remove this xfail if the renderer ever escapes values."
        ),
        strict=False,
        raises=json.JSONDecodeError,
    )
    def test_backslash_windows_path_is_a_known_gap(
        self, manifest: SkillManifest
    ) -> None:
        context = {"file_path": "C:\\MQL5\\Experts\\MyEA.mq5"}
        for step in manifest.steps:
            rendered = SkillExecutor._render_template(step.arguments_template, context)
            json.loads(rendered)

    def test_no_step_interpolates_source_code(self, manifest: SkillManifest) -> None:
        # Raw {key} substitution cannot carry multi-line file content: the
        # rendered JSON would be invalid and the executor aborts the pipeline.
        for step in manifest.steps:
            assert "{source_code}" not in step.arguments_template

    def test_first_step_reads_the_file(self, manifest: SkillManifest) -> None:
        first = manifest.steps[0]
        assert first.tool_name == "file_read"
        assert json.loads(first.arguments_template)["path"] == "{file_path}"


class TestInstructions:
    def test_covers_the_core_rules(self, manifest: SkillManifest) -> None:
        body = manifest.markdown_content
        for expected in (
            "compiler is the ground truth",
            "compile_loop.py",
            "knowledge_search",
            "MQL4-isms",
            "references/mql4-to-mql5.md",
            "references/compile-errors.md",
            "references/optimization.md",
        ):
            assert expected in body, f"SKILL.md lost the {expected!r} guidance"

    def test_documents_forward_slash_paths(self, manifest: SkillManifest) -> None:
        """`jarvis skill run` interpolates raw JSON, so paths must use /.

        Pairs with test_backslash_windows_path_is_a_known_gap below: that test
        pins the limitation, this one pins the user-facing documentation of it.
        """
        body = manifest.markdown_content
        assert "forward slashes" in body
        assert "jarvis skill run mql5-expert -a file_path=" in body

    def test_ships_its_references(self) -> None:
        references = SKILL_DIR / "references"
        assert (references / "compile-errors.md").is_file()
        assert (references / "mql4-to-mql5.md").is_file()
        assert (references / "mql5-api-cheatsheet.md").is_file()
        assert (references / "optimization.md").is_file()
        for path in references.glob("*.md"):
            assert path.stat().st_size > 500, f"{path.name} looks truncated"

    def test_ships_the_ea_template(self) -> None:
        template = SKILL_DIR / "templates" / "ea-template.mq5"
        assert template.is_file()
        body = template.read_text(encoding="utf-8")
        for expected in (
            "#property",
            "#include <Trade\\Trade.mqh>",
            "int OnInit()",
            "void OnTick()",
            "void OnDeinit(const int reason)",
            "SymbolInfoDouble",
            "SYMBOL_VOLUME_STEP",
            "PositionGetInteger(POSITION_MAGIC)",
            "INIT_PARAMETERS_INCORRECT",
        ):
            assert expected in body, f"template lost {expected!r}"


class TestTemplateIsCleanMql5:
    def test_template_has_no_mql4_isms(self) -> None:
        from openjarvis.evals.scorers.mql_bench import (
            find_mql4_isms,
            strip_comments_and_strings,
        )

        template = SKILL_DIR / "templates" / "ea-template.mq5"
        code = strip_comments_and_strings(template.read_text(encoding="utf-8"))
        hits = find_mql4_isms(code)
        assert hits == [], f"EA template contains MQL4 idioms: {hits}"

    def test_template_would_pass_the_bench_style_checks(self) -> None:
        """The template must satisfy the checks the benchmark demands."""
        template = (SKILL_DIR / "templates" / "ea-template.mq5").read_text(
            encoding="utf-8"
        )
        for required in (
            "AccountInfoDouble",
            "ACCOUNT_EQUITY",
            "SYMBOL_TRADE_TICK_VALUE",
            "SYMBOL_TRADE_TICK_SIZE",
            "SYMBOL_VOLUME_STEP",
            "SYMBOL_VOLUME_MIN",
            "SYMBOL_VOLUME_MAX",
            "SYMBOL_TRADE_STOPS_LEVEL",
            "CopyBuffer",
            "ArraySetAsSeries",
            "INVALID_HANDLE",
            "IndicatorRelease",
            "PositionOpen",
            "BarsCalculated",
        ):
            assert required in template, f"template lost {required!r}"


def _function_body(code: str, name: str) -> str:
    """Return the ``{...}`` body of MQL5 function ``name`` (comments stripped)."""
    import re

    match = re.search(rf"^[A-Za-z_][\w ]*\b{name}\s*\(", code, re.MULTILINE)
    assert match, f"template has no function {name}()"
    start = code.index("{", match.end())
    depth = 0
    for pos in range(start, len(code)):
        depth += {"{": 1, "}": -1}.get(code[pos], 0)
        if depth == 0:
            return code[start : pos + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


@pytest.fixture(scope="module")
def template_code() -> str:
    from openjarvis.evals.scorers.mql_bench import strip_comments_and_strings

    raw = (SKILL_DIR / "templates" / "ea-template.mq5").read_text(encoding="utf-8")
    return strip_comments_and_strings(raw)


def _squash(text: str) -> str:
    return "".join(text.split())


class TestTemplateTradeFlow:
    """The order path the template teaches, pinned against the MQL5 reference.

    Each test names one claim from a vendor page; the numeric ones also show
    the arithmetic the claim rests on, so the rationale cannot drift from the
    code unnoticed.
    """

    def test_stops_are_measured_from_the_closing_price(
        self, template_code: str
    ) -> None:
        # mql5.com/en/articles/2555: buy -> Bid, sell -> Ask, for SL *and* TP.
        body = _squash(_function_body(template_code, "StopsAreValid"))
        buy, sell = body.split("else", 1)
        for needle in ("bid-sl<", "tp-bid<"):
            assert needle in buy, f"buy branch lost {needle!r}"
        for needle in ("sl-ask<", "ask-tp<"):
            assert needle in sell, f"sell branch lost {needle!r}"

    @pytest.mark.parametrize("buy", [True, False])
    def test_an_sl_typed_from_the_entry_is_a_spread_short(self, buy: bool) -> None:
        # Both sides: a buy enters at the Ask but closes at the Bid, a sell
        # enters at the Bid but closes at the Ask. The template builds SL from
        # the entry price, so the naive `n >= level` test is off by the spread.
        point, level, spread, n = 0.00001, 300, 20, 300
        bid = 1.10000
        ask = bid + spread * point
        if buy:
            sl = ask - n * point
            real = round((bid - sl) / point)
        else:
            sl = bid + n * point
            real = round((sl - ask) / point)
        assert n >= level  # what a distance-only check sees
        assert real == level - spread < level  # what the server measures

    def test_open_position_validates_the_real_prices_not_the_typed_distance(
        self, template_code: str
    ) -> None:
        body = _squash(_function_body(template_code, "OpenPosition"))
        assert "StopsAreValid(type,sl,tp)" in body
        assert "sl_points*point<min_distance" not in body
        assert body.index("StopsAreValid(") < body.index("PositionOpen(")

    def test_the_server_verdict_comes_from_the_retcode(
        self, template_code: str
    ) -> None:
        # CTrade::PositionOpen's bool is "basic structures checked", not "done".
        body = _squash(_function_body(template_code, "OpenPosition"))
        assert "RetcodeIsSuccess(retcode)" in body
        assert "if(!sent)" not in body
        import re

        helper = _function_body(template_code, "RetcodeIsSuccess")
        accepted = set(re.findall(r"TRADE_RETCODE_\w+", helper))
        assert accepted == {
            "TRADE_RETCODE_PLACED",  # 10008
            "TRADE_RETCODE_DONE",  # 10009
            "TRADE_RETCODE_DONE_PARTIAL",  # 10010
        }

    def test_a_transient_refusal_does_not_consume_the_bar(
        self, template_code: str
    ) -> None:
        body = _function_body(template_code, "OnTick")
        flat = _squash(body)
        assert "IsNewBar" not in _squash(template_code)
        order = [
            "HasUnhandledBar()",
            "TradingIsAllowed()",
            "SpreadIsAcceptable()",
            "BarsCalculated(g_fast_handle)",
            "CopyBuffer(g_slow_handle",
            "MarkBarHandled();",
        ]
        positions = [flat.index(item) for item in order[:-1]]
        positions.append(flat.rindex(order[-1]))  # the final mark, not the early one
        assert positions == sorted(positions), dict(zip(order, positions))
        # the only other mark is the own-position branch, which is final
        assert flat.count("MarkBarHandled();") == 2
        assert "HasOwnPosition()){MarkBarHandled();return;}" in flat

    def test_volume_floor_carries_an_epsilon(self, template_code: str) -> None:
        import math

        assert math.floor(0.3 / 0.1) == 2  # the double-precision trap
        assert math.floor(0.3 / 0.1 + 1e-8) == 3
        body = _squash(_function_body(template_code, "NormalizeVolume"))
        assert "MathFloor(volume/lot_step+1e-8)" in body

    def test_references_teach_what_the_template_does(self) -> None:
        refs = SKILL_DIR / "references"
        cheat = " ".join((refs / "mql5-api-cheatsheet.md").read_text("utf-8").split())
        errors = " ".join((refs / "compile-errors.md").read_text("utf-8").split())
        for needle in (
            "Bid - SL >= level",
            "SL - Ask >= level",
            "TP - Bid >= level",
            "Ask - TP >= level",
            "successful check of the basic structures",
            "MathFloor(volume / lot_step + 1e-8)",
        ):
            assert needle in cheat, f"cheatsheet lost {needle!r}"
        assert "Bid for a buy, Ask for a sell" in errors


class TestTemplateRiskSizing:
    def test_a_stop_is_sized_with_the_loss_side_tick_value(
        self, template_code: str
    ) -> None:
        # SYMBOL_TRADE_TICK_VALUE is documented as the *profit* value
        # (SYMBOL_TRADE_TICK_VALUE_PROFIT); a stop-loss is a losing tick, which
        # has SYMBOL_TRADE_TICK_VALUE_LOSS. The plain one is only the fallback
        # for a server that leaves the loss value at 0.
        body = _squash(_function_body(template_code, "RiskVolume"))
        loss = body.index("SYMBOL_TRADE_TICK_VALUE_LOSS")
        fallback = body.index(
            "tick_value=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_VALUE)"
        )
        assert loss < fallback
        assert "if(tick_value<=0.0)tick_value=" in body
        # ...and the loss figure is what feeds the per-lot loss.
        assert "loss_per_lot=sl_points*point/tick_size*tick_value" in body


class TestMql5NamesAreNotCalledGone:
    """``OrderSelect(ticket)``, ``Point()`` and ``Digits()`` exist in MQL5.

    The review checklist feeds an LLM, so telling it they are MQL4-isms makes it
    flag correct code. The MQL4 *forms* are what is gone: ``OrderSelect(index,
    SELECT_BY_POS)`` and the bare ``Point``/``Digits`` variables.
    """

    FILES = (
        SKILL_DIR / "SKILL.md",
        SKILL_DIR / "skill.toml",
        SKILL_DIR / "references" / "mql4-to-mql5.md",
        REPO_ROOT / "docs" / "tutorials" / "mql-companion.md",
    )

    MARKDOWN = [p for p in FILES if p.suffix == ".md"]

    @pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: p.name)
    def test_orderselect_ticket_form_is_named_as_valid(self, path: Path) -> None:
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "OrderSelect(ticket)" in text, f"{path.name}: which OrderSelect is gone?"
        assert "SELECT_BY_POS" in text, path.name

    @pytest.mark.parametrize("path", FILES[:1] + FILES[2:], ids=lambda p: p.name)
    def test_point_and_digits_functions_are_acknowledged(self, path: Path) -> None:
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "Point()" in text and "Digits()" in text, path.name

    def test_the_review_checklist_names_both_exceptions(self) -> None:
        text = (SKILL_DIR / "skill.toml").read_text(encoding="utf-8")
        assert "Point() and Digits() are real MQL5 functions" in text
        assert "single ticket argument" in text
