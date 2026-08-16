"""AST purity guard: parsers must not do I/O or read the clock."""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_IMPORTS = {"httpx", "requests", "sqlite3", "playwright"}
FORBIDDEN_CALLS = {
    ("datetime", "now"),
    ("date", "today"),
    ("time", "time"),
}
FORBIDDEN_NAMES = {"open"}
FORBIDDEN_PATH_READS = {"read_text", "read_bytes", "read_text", "open"}


def _module_name(path: Path) -> str:
    return path.stem


def _defines_parse(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "parse":
            return True
    return False


def _should_scan(path: Path, tree: ast.AST) -> bool:
    return _module_name(path).startswith("parse") or _defines_parse(tree)


def _attr_chain(node: ast.AST) -> list[str]:
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return list(reversed(parts))


def scan_purity(root: str | Path) -> list[tuple[str, int, str]]:
    """Return (file, line, offending_name) for every purity violation."""
    root = Path(root)
    hits: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
        if not _should_scan(path, tree):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in FORBIDDEN_IMPORTS:
                        hits.append((str(path), node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".")[0]
                if mod in FORBIDDEN_IMPORTS:
                    hits.append((str(path), node.lineno, node.module or mod))
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_NAMES:
                    hits.append((str(path), node.lineno, "open"))
                chain = _attr_chain(node.func)
                if len(chain) >= 2:
                    pair = (chain[-2], chain[-1])
                    if pair in FORBIDDEN_CALLS:
                        hits.append((str(path), node.lineno, f"{pair[0]}.{pair[1]}"))
                    if chain[-2] in {"Path", "path"} and chain[-1].startswith("read_"):
                        hits.append((str(path), node.lineno, f"{chain[-2]}.{chain[-1]}"))
    return hits


def test_purity_scanner_flags_synthetic_module(tmp_path):
    bad = tmp_path / "parse_evil.py"
    bad.write_text(
        "import httpx\n"
        "from datetime import datetime\n"
        "def parse(doc, account, meta):\n"
        "    datetime.now()\n"
        "    return []\n",
        encoding="utf-8",
    )
    hits = scan_purity(tmp_path)
    names = {h[2] for h in hits}
    assert "httpx" in names
    assert "datetime.now" in names
    assert all(h[0].endswith("parse_evil.py") for h in hits)


def test_real_sources_tree_is_pure():
    root = Path(__file__).resolve().parents[1] / "src" / "sources"
    hits = scan_purity(root)
    assert hits == []
