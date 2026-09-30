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
