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


def apply_registry_patches() -> None:
    """Apply our Chrome-parity patches to the vendored boring-sys BoringSSL.

    The cargo registry source is ephemeral (refreshed by `cargo update` /
    `cargo clean` of the registry), so re-apply on every build. Idempotent.

    Patches (see BUILD_NOTES.md 'Vendored BoringSSL patch'):
      - TLSEXT_TYPE_application_settings 17513 -> 17613 (Chrome 151+ emits the
        final IANA codepoint; upstream BoringSSL still sends the legacy draft).
    """
    import glob
    import re

    pattern = os.path.expanduser(
        r"~\.cargo\registry\src\*\boring-sys-*\deps\boringssl\src\include\openssl\tls1.h"
    )
    for header in glob.glob(pattern):
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


def main() -> int:
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(EXTRA_PATH) + os.pathsep + env.get("PATH", "")
    env["LIBCLANG_PATH"] = r"C:\Program Files\LLVM\bin"

    apply_registry_patches()

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
