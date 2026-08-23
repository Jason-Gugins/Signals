"""README must match the live CLI and a portable clone path."""

from __future__ import annotations

from pathlib import Path

import src.cli as cli_mod

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_has_clone_url():
    assert "github.com/Jason-Gugins/Signals" in README


def test_readme_install_is_not_hardcoded_machine_path():
    assert "C:\\Users\\Jason\\Documents\\AI\\Signals" not in README


def test_readme_invokes_module_not_signals_binary():
    assert "python -m src.cli" in README or ".venv\\Scripts\\python.exe -m src.cli" in README
    assert "signals status" not in README
    assert "signals doctor" not in README


def test_readme_lists_every_cli_command():
    names = sorted(cli_mod.main.commands)
    missing = [n for n in names if f"`{n}`" not in README and n not in README]
    assert not missing, missing
