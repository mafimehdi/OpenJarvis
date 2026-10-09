"""Smoke test: every shipped preset config must load cleanly.

Presets are installed via `jarvis init --preset <name>`, which copies
`configs/openjarvis/examples/<name>.toml` to `~/.openjarvis/config.toml`.
A preset that fails to parse via `load_config()` would break first-time
setup, so we validate the whole set on every commit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from openjarvis.core.config import JarvisConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
PRESETS_DIR = REPO_ROOT / "configs" / "openjarvis" / "examples"


def _preset_paths() -> list[Path]:
    return sorted(PRESETS_DIR.glob("*.toml"))


def test_presets_directory_exists() -> None:
    assert PRESETS_DIR.is_dir(), f"presets dir missing: {PRESETS_DIR}"


def test_at_least_one_preset_ships() -> None:
    assert _preset_paths(), f"no preset .toml files in {PRESETS_DIR}"


@pytest.mark.parametrize(
    "preset_path",
    _preset_paths(),
    ids=lambda p: p.stem,
)
def test_preset_loads(preset_path: Path) -> None:
    cfg = load_config(path=preset_path)
    assert isinstance(cfg, JarvisConfig)
    # A preset must at least name an engine and an agent — those are the two
    # slots `jarvis init` expects to be populated for a working first run.
    assert cfg.engine.default, f"{preset_path.stem}: engine.default is empty"
    assert cfg.agent.default_agent, f"{preset_path.stem}: agent.default_agent is empty"


def _documented_preset_names() -> dict[str, list[str]]:
    """Every preset name the repository tells users to install.

    Prose and config comments both count: ``mql-assistant`` was advertised from
    three places (the example README, its tutorial, and a comment inside its own
    config file).
    """
    files = [REPO_ROOT / "README.md", REPO_ROOT / "CHANGELOG.md"]
    for sub in ("docs", "examples", "configs"):
        root = REPO_ROOT / sub
        files.extend(sorted(root.rglob("*.md")))
        files.extend(sorted(root.rglob("*.toml")))
    found: dict[str, list[str]] = {}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in re.findall(r"--preset[= ]+([A-Za-z0-9][A-Za-z0-9_-]*)", text):
            found.setdefault(name, []).append(str(path.relative_to(REPO_ROOT)))
    return found


def test_every_preset_the_docs_install_is_an_accepted_choice() -> None:
    """A preset the repository tells you to install must be installable.

    ``--preset`` validates against a hardcoded ``click.Choice`` list while the
    presets themselves are files in a directory, so a preset can ship, parse and
    load cleanly — everything above passes — and still be unselectable.
    ``mql-assistant`` did exactly that: the file was here, ``load_config()`` was
    happy with it, and the first command the example's own README printed died
    with ``Error: Invalid value for '--preset': 'mql-assistant' is not one of
    ...`` — the worst possible place for that failure, and invisible to a test
    that only checks the file.

    This is the missing half of the smoke test: it checks the file is reachable
    from the command, not just that the file is valid.
    """
    from openjarvis.cli.init_cmd import init

    preset_param = next(p for p in init.params if p.name == "preset")
    choices = {str(choice) for choice in getattr(preset_param.type, "choices", ())}
    assert choices, "no --preset choices found; the option changed shape"

    documented = _documented_preset_names()
    assert documented, "the docs scan found nothing — the regex or paths broke"

    rejected = {
        name: sorted(set(where))
        for name, where in documented.items()
        if name not in choices
    }
    assert not rejected, (
        "the repository documents `jarvis init --preset <name>` for presets the "
        f"command rejects: {rejected}"
    )

    missing_file = sorted(
        name for name in documented if not (PRESETS_DIR / f"{name}.toml").is_file()
    )
    assert not missing_file, (
        f"documented presets with no config file to copy: {missing_file}"
    )
