"""Tests for the ported stealth script (no browser launch)."""

from src.core.stealth import STEALTH_INIT_SCRIPT, get_stealth_init_script, validate_stealth_script


def test_validate_stealth_script_valid_with_all_sections():
    result = validate_stealth_script()
    assert result["valid"] is True
    required = {
        "navigator_webdriver",
        "navigator_plugins",
        "canvas_spoofing",
        "webgl_spoofing",
        "extension_probe_defense",
        "permission_api",
        "chrome_runtime",
    }
    assert required.issubset(set(result["sections_included"]))


def test_script_overrides_navigator_prototype():
    script = get_stealth_init_script()
    assert script == STEALTH_INIT_SCRIPT
    assert "Navigator.prototype" in script
    assert "webdriver" in script
