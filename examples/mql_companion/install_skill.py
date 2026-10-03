#!/usr/bin/env python3
"""Install the bundled ``mql5-expert`` skill into your OpenJarvis skills dir.

This goes through the framework's own :class:`SkillImporter`, so the skill gets
the same treatment as one pulled from Hermes/OpenClaw/GitHub: strict frontmatter
validation, external→internal tool-name translation, symlink rejection, trust
classification, and a ``.source`` provenance sidecar.

One difference, deliberately: ``SkillImporter`` copies ``SKILL.md`` plus
``references/``, ``assets/``, ``templates/`` (and ``scripts/`` only when opted
in) — it does **not** copy ``skill.toml``, which would silently downgrade a
hybrid skill to instruction-only. This script copies the manifest afterwards so
``jarvis skill run mql5-expert`` keeps working.

Usage::

    python examples/mql_companion/install_skill.py
    python examples/mql_companion/install_skill.py --force
    python examples/mql_companion/install_skill.py --target ./skills --dry-run
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import click

SKILL_DIR = Path(__file__).resolve().parent / "skills" / "mql5-expert"


@click.command()
@click.option(
    "--target",
    default=None,
    type=click.Path(path_type=Path),
    help="Skills root to install into (default: ~/.openjarvis/skills).",
)
@click.option("--force", is_flag=True, help="Overwrite an existing install.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate the skill and show what would be copied, without writing.",
)
def main(target: Path | None, force: bool, dry_run: bool) -> None:
    """Validate and install the mql5-expert skill."""
    try:
        from openjarvis.core.paths import get_config_dir
        from openjarvis.skills.importer import SkillImporter
        from openjarvis.skills.loader import load_skill_directory
        from openjarvis.skills.parser import SkillParser
        from openjarvis.skills.sources.base import ResolvedSkill
        from openjarvis.skills.tool_translator import ToolTranslator
    except ImportError:
        click.echo(
            "Error: openjarvis is not installed. Install it with:  uv sync --extra dev",
            err=True,
        )
        sys.exit(1)

    if not (SKILL_DIR / "SKILL.md").is_file():
        click.echo(f"Error: no SKILL.md under {SKILL_DIR}", err=True)
        sys.exit(1)

    # 1. Validate locally first — fail before touching the user's config dir.
    manifest = load_skill_directory(SKILL_DIR)
    click.echo(f"skill:       {manifest.name} v{manifest.version}")
    click.echo(f"description: {manifest.description}")
    click.echo(f"tags:        {', '.join(manifest.tags) or '-'}")
    caps = ", ".join(manifest.required_capabilities) or "-"
    click.echo(f"capabilities: {caps}")
    click.echo(f"steps:       {len(manifest.steps)}")
    for step in manifest.steps:
        label = step.tool_name or f"skill:{step.skill_name}"
        click.echo(f"  - {label} -> {step.output_key or '(no output key)'}")
    n_lines = len(manifest.markdown_content.splitlines())
    click.echo(f"instructions: {n_lines} lines")

    target_root = Path(target).expanduser() if target else get_config_dir() / "skills"
    install_dir = target_root / "local" / manifest.name
    click.echo(f"target:      {install_dir}")

    if dry_run:
        click.echo("\n[dry-run] would copy:")
        for path in sorted(SKILL_DIR.rglob("*")):
            if path.is_file():
                click.echo(f"  {path.relative_to(SKILL_DIR)}")
        return

    # 2. Install through the framework importer.
    importer = SkillImporter(
        SkillParser(),
        ToolTranslator(),
        target_root=target_root,
    )
    resolved = ResolvedSkill(
        name=manifest.name,
        source="local",
        path=SKILL_DIR,
        category="coding",
        description=manifest.description,
        commit="",
    )
    result = importer.import_skill(resolved, force=force)

    for warning in result.warnings:
        click.echo(f"  ! {warning}")
    if result.skipped:
        click.echo("Already installed. Re-run with --force to overwrite.")
        return
    if not result.success:
        click.echo("Install failed.", err=True)
        sys.exit(1)

    # 3. Copy the structured pipeline manifest the importer does not handle.
    toml_src = SKILL_DIR / "skill.toml"
    if toml_src.is_file() and result.target_path is not None:
        shutil.copy2(toml_src, result.target_path / "skill.toml")
        click.echo("  + copied skill.toml (pipeline manifest)")

    # 4. Verify the installed copy loads as a hybrid (TOML + markdown) skill.
    installed = load_skill_directory(install_dir)
    click.echo(f"\ninstalled:   {install_dir}")
    click.echo(
        f"verified:    steps={len(installed.steps)} "
        f"instructions={'yes' if installed.markdown_content else 'no'}"
    )
    click.echo(
        "\nNext:\n"
        "  jarvis skill list\n"
        "  jarvis skill info mql5-expert\n"
        "  jarvis skill run mql5-expert -a file_path=MyEA.mq5\n"
        '  jarvis ask "Use the mql5-expert skill to review MyEA.mq5"'
    )


if __name__ == "__main__":
    main()
