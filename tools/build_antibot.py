"""Build helper for the native signals_antibot engine (Rust + BoringSSL + PyO3).

Cross-platform (Windows + Linux/Ubuntu). Linux prereqs:
  rustup (stable), Go 1.24+ tarball, build-essential, cmake, ninja-build,
  clang, libclang-dev, perl. NASM is Windows-only (Linux asm is perl-based).

Usage (from repo root):
  Windows (VS dev prompt): .venv\\Scripts\\python.exe tools\\build_antibot.py
  Linux/WSL2:              .venv/bin/python tools/build_antibot.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENGINE_DIR = REPO / "src" / "antibot" / "engine"

# Windows-only toolchain paths (Go/NASM/LLVM outside the default PATH).
WINDOWS_EXTRA_PATH = [
    r"C:\Program Files\Go\bin",
    r"C:\Program Files\LLVM\bin",
    r"C:\Users\Jason\nasm\nasm-2.16.03",
    r"C:\Users\Jason\.cargo\bin",
]


def apply_registry_patches(cargo_registry_root: Path | None = None) -> None:
    """Apply our Chrome-parity patches to the vendored boring-sys BoringSSL.

    ``cargo_registry_root`` is the cargo REGISTRY root — the directory whose
    ``src/`` subtree holds the vendored crate sources (default: the real
    ``~/.cargo/registry``). The registry source is ephemeral (refreshed by
    `cargo update` / registry clean), so re-apply on every build. Idempotent.
    Patches (see BUILD_NOTES.md 'Vendored BoringSSL patch'):
      - TLSEXT_TYPE_application_settings 17513 -> 17613 (Chrome 151+ emits the
        final IANA codepoint; upstream BoringSSL still sends the legacy draft).
    Forward-slash globs work on both platforms.
    """
    import glob
    import re

    root = (
        Path(cargo_registry_root)
        if cargo_registry_root
        else Path.home() / ".cargo" / "registry"
    )
    pattern = str(
        root / "src" / "*" / "boring-sys-*"
        / "deps" / "boringssl" / "src" / "include" / "openssl" / "tls1.h"
    )
    headers = glob.glob(pattern)
    if not headers:
        print(f"ALPS codepoint already patched (or not found): no boring-sys sources under {root}")
        return
    for header in headers:
        text = Path(header).read_text(encoding="utf-8", errors="replace")
        patched = re.sub(
            r"#define TLSEXT_TYPE_application_settings 17513\b",
            "#define TLSEXT_TYPE_application_settings 17613",
            text,
        )
        if patched != text:
            Path(header).write_text(patched, encoding="utf-8")
            print(f"Patched ALPS codepoint 17513 -> 17613 in {header}")
        else:
            print(f"ALPS codepoint already patched (or not found): {header}")


def venv_paths(repo: Path, platform: str | None = None) -> tuple[Path, Path]:
    """(maturin, python) executable paths inside the repo venv, per OS."""
    plat = platform if platform is not None else os.name
    if plat == "nt":
        return repo / ".venv" / "Scripts" / "maturin.exe", repo / ".venv" / "Scripts" / "python.exe"
    return repo / ".venv" / "bin" / "maturin", repo / ".venv" / "bin" / "python"


def main() -> int:
    env = os.environ.copy()
    if os.name == "nt":
        env["PATH"] = os.pathsep.join(WINDOWS_EXTRA_PATH) + os.pathsep + env.get("PATH", "")
        env["LIBCLANG_PATH"] = r"C:\Program Files\LLVM\bin"

    apply_registry_patches()

    maturin, venv_python = venv_paths(REPO)
    maturin = str(maturin) if maturin.exists() else "maturin"

    print(f"Building engine in {ENGINE_DIR} ...")
    result = subprocess.run([maturin, "develop", "--release"], cwd=ENGINE_DIR, env=env)
    if result.returncode != 0:
        print("Build FAILED — see src/antibot/README.md for troubleshooting.", file=sys.stderr)
        return result.returncode

    print("\nSanity check:")
    check = subprocess.run(
        [str(venv_python), "-c",
         "import signals_antibot as m; print('engine_version:', m.engine_version()); "
         "print('tls:', m.tls_library_version())"]
    )
    return check.returncode


if __name__ == "__main__":
    raise SystemExit(main())
