"""Build helper for the native signals_antibot engine (Rust + BoringSSL + PyO3).

Builds the cdylib wheel with maturin and installs it into the Signals venv.
One-time prerequisites (already installed on this machine):
  - Rust (rustc/cargo) at C:/Users/Jason/.cargo/bin
  - Go 1.24+ at C:/Program Files/Go/bin          (BoringSSL's build system)
  - NASM at C:/Users/Jason/nasm/nasm-2.16.03
  - CMake
  - LLVM/Clang (libclang for bindgen) at C:/Program Files/LLVM/bin

Usage (from repo root):
  .venv/Scripts/python.exe tools/build_antibot.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENGINE_DIR = REPO / "src" / "antibot" / "engine"

# Windows toolchain paths needed by boring-sys (Go build, NASM, libclang/bindgen).
EXTRA_PATH = [
    r"C:\Program Files\Go\bin",
    r"C:\Program Files\LLVM\bin",
    r"C:\Users\Jason\nasm\nasm-2.16.03",
    r"C:\Users\Jason\.cargo\bin",
]


def main() -> int:
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(EXTRA_PATH) + os.pathsep + env.get("PATH", "")
    env["LIBCLANG_PATH"] = r"C:\Program Files\LLVM\bin"

    venv_maturin = REPO / ".venv" / "Scripts" / "maturin.exe"
    maturin = str(venv_maturin) if venv_maturin.exists() else "maturin"

    print(f"Building engine in {ENGINE_DIR} ...")
    result = subprocess.run([maturin, "develop", "--release"], cwd=ENGINE_DIR, env=env)
    if result.returncode != 0:
        print("Build FAILED — see src/antibot/BUILD_NOTES.md for troubleshooting.", file=sys.stderr)
        return result.returncode

    print("\nSanity check:")
    check = subprocess.run(
        [str(REPO / ".venv" / "Scripts" / "python.exe"), "-c",
         "import signals_antibot as m; print('engine_version:', m.engine_version()); "
         "print('tls:', m.tls_library_version())"]
    )
    return check.returncode


if __name__ == "__main__":
    raise SystemExit(main())
