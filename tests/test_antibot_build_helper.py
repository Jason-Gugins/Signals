"""Platform-aware build helper for the native engine (P10 port).

The ALPS registry patch is load-bearing (stale codepoint = detectable
fingerprint mismatch), so its logic gets direct tests with an injected
registry root — runnable on Windows without a real cargo registry.
"""

from pathlib import Path

import tools.build_antibot as helper


def _fake_registry(tmp_path: Path) -> Path:
    header_dir = (
        tmp_path / "registry" / "src" / "index.crates.io-6f17d22bba15001f"
        / "boring-sys-4.22.0" / "deps" / "boringssl" / "src" / "include" / "openssl"
    )
    header_dir.mkdir(parents=True)
    header = header_dir / "tls1.h"
    header.write_text(
        "#define TLSEXT_TYPE_application_settings 17513\n", encoding="utf-8"
    )
    return tmp_path / "registry"


def test_registry_patch_rewrites_and_is_idempotent(tmp_path, capsys):
    root = _fake_registry(tmp_path)
    helper.apply_registry_patches(root)
    first = capsys.readouterr().out
    assert "Patched ALPS codepoint" in first
    header = next(root.glob("src/*/boring-sys-*/deps/boringssl/src/include/openssl/tls1.h"))
    assert "17613" in header.read_text(encoding="utf-8")
    helper.apply_registry_patches(root)
    assert "already patched" in capsys.readouterr().out


def test_registry_patch_noop_when_no_boring_sys(tmp_path, capsys):
    (tmp_path / "registry" / "src").mkdir(parents=True)
    helper.apply_registry_patches(tmp_path / "registry")
    assert "already patched (or not found)" in capsys.readouterr().out


def test_venv_paths_follow_platform():
    posix = helper.venv_paths(Path("/repo"), platform="posix")
    nt = helper.venv_paths(Path("/repo"), platform="nt")
    assert posix == (Path("/repo/.venv/bin/maturin"), Path("/repo/.venv/bin/python"))
    assert nt[0] == Path("/repo/.venv/Scripts/maturin.exe")
    assert nt[1] == Path("/repo/.venv/Scripts/python.exe")
