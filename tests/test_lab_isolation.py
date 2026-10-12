"""Task 1 (decide-lab): the scope guard + package skeleton checks.

``src/lab`` is dev-only tooling. The intel pipeline, signal layer, and source
adapters must never import it — otherwise lab-only dependencies (and the lab
itself) could leak into production installs. This test walks the ASTs of
``src/pipeline``, ``src.signals``, and ``src/sources`` and fails on any
``src.lab`` import (absolute, ``from src import lab``, or relative), plus a
text scan for dynamic (string) imports of the module.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from src.lab.__main__ import build_parser

REPO = Path(__file__).resolve().parents[1]
GUARDED_DIRS = ("src/pipeline", "src/signals", "src/sources")

_DYNAMIC_IMPORT_RE = re.compile(r"['\"]src\.lab")


def _py_files() -> list[Path]:
    files: list[Path] = []
    for rel in GUARDED_DIRS:
        files.extend((REPO / rel).rglob("*.py"))
    return files


def _ast_imports_lab(tree: ast.AST) -> list[str]:
    """Every import statement in `tree` that binds src.lab, as source text."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "src.lab" or alias.name.startswith("src.lab."):
                    offenders.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = getattr(node, "level", 0) or 0
            first = module.split(".")[0] if module else None
            binds_lab = (
                module == "src.lab"
                or module.startswith("src.lab.")
                or (module == "src" and any(a.name == "lab" for a in node.names))
                # from .lab import x / from ..lab import x / from .. import lab
                or (level >= 1 and first == "lab")
                or (level >= 1 and module == "" and any(a.name == "lab" for a in node.names))
            )
            if binds_lab:
                offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
    return offenders


def test_pipeline_never_imports_lab() -> None:
    files = _py_files()
    assert files, "guard found no source files — the guard itself is broken"
    failures: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        offenders = _ast_imports_lab(ast.parse(text, filename=str(path)))
        # Dynamic imports (importlib.import_module("src.lab"), __import__) live
        # in strings the AST cannot see; scan import-bearing lines for the
        # quoted module path so prose comments mentioning src.lab stay legal.
        for i, line in enumerate(text.splitlines(), start=1):
            if "import" in line.lower() and _DYNAMIC_IMPORT_RE.search(line):
                offenders.append(f"line {i}: {line.strip()}")
        if offenders:
            rel = path.relative_to(REPO)
            failures.append(f"{rel}:\n  " + "\n  ".join(offenders))
    assert not failures, (
        "src.lab is dev-only and must never be imported by the intel "
        "pipeline/signals/sources:\n" + "\n".join(failures)
    )


def test_lab_runner_help_names_planned_subcommands() -> None:
    help_text = build_parser().format_help()
    for sub in ("run", "report", "corpus"):
        assert sub in help_text
