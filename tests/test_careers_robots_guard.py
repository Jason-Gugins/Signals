"""Config guard for the careers robots override (the cf1c99 pattern).

The 2026-11-06 operator-approved override covers careers-FAMILY paths on 7
DTC prospect domains only — this pin makes any widening a deliberate,
reviewed change.
"""

import yaml

OVERRIDDEN_HOSTS = {
    "blenderseyewear.com", "comfrt.com", "hexclad.com",
    "lumedeodorant.com", "meritbeauty.com", "ridge.com", "simplemodern.com",
}
CAREERS_PREFIXES = {"/careers", "/pages/careers", "/about/careers", "/jobs"}


def _allow() -> list[dict]:
    cfg = yaml.safe_load(open("config/default.yaml", encoding="utf-8"))
    return (cfg.get("http") or {}).get("robots_allow") or []


def test_all_seven_hosts_have_the_four_careers_prefixes():
    rules = _allow()
    for host in OVERRIDDEN_HOSTS:
        got = {r.get("path_prefix") for r in rules if r.get("host") == host}
        assert CAREERS_PREFIXES <= got, f"{host} missing prefixes: {CAREERS_PREFIXES - got}"


def test_the_override_stays_scoped_to_careers_family_paths():
    rules = _allow()
    for r in rules:
        if r.get("host") in OVERRIDDEN_HOSTS:
            assert r.get("path_prefix") in CAREERS_PREFIXES, (
                f"override widened beyond careers-family paths: {r}"
            )


def test_operator_approval_is_recorded_in_the_comment():
    text = open("config/default.yaml", encoding="utf-8").read()
    assert "operator-approved 2026-11-06" in text
    assert "CAREERS_HARVEST_20261106" in text
