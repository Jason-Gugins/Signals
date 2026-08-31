"""Tests for tech-stack change detection (pure diff_technologies)."""

from src.sources.techstack.diff import diff_technologies

DOMAIN = "acme.com"
TODAY = "2026-08-31"


def _vendors(cands):
    return [(stype, cand.signal_type, cand.natural_key, cand.confidence, cand.evidence_data)
            for stype, cand in cands]


def test_added_become_tech_install_new():
    out = diff_technologies(set(), {"datadog", "snowflake"}, domain=DOMAIN, today=TODAY)
    assert len(out) == 2
    for stype, cand in out:
        assert stype == "tech_install_new"
        assert cand.signal_type == "tech_install_new"
        assert cand.confidence == 0.7
        assert cand.natural_key.startswith(f"techchg:{DOMAIN}:")
        assert cand.natural_key.endswith(f":{TODAY}")
        assert cand.evidence_data["change"] == "install"
        assert cand.evidence_data["vendor"] in {"datadog", "snowflake"}


def test_removed_become_tech_churn():
    out = diff_technologies({"oldgrid"}, set(), domain=DOMAIN, today=TODAY)
    assert len(out) == 1
    stype, cand = out[0]
    assert stype == "tech_churn"
    assert cand.signal_type == "tech_churn"
    assert cand.confidence == 0.6
    assert cand.natural_key == f"techchg:{DOMAIN}:oldgrid:{TODAY}"
    assert cand.evidence_data == {"vendor": "oldgrid", "change": "churn"}


def test_no_diff_returns_empty():
    both = {"a", "b"}
    assert diff_technologies(both, both, domain=DOMAIN, today=TODAY) == []


def test_flap_guard_suppresses_churn():
    out = diff_technologies({"flappy", "gone"}, {"flappy"}, domain=DOMAIN, today=TODAY,
                            flap_guard={"flappy"})
    vendors = {cand.evidence_data["vendor"] for _, cand in out}
    assert vendors == {"gone"}


def test_flap_guard_does_not_block_installs():
    out = diff_technologies({"a"}, {"a", "flappy"}, domain=DOMAIN, today=TODAY,
                            flap_guard={"flappy"})
    assert {cand.evidence_data["vendor"] for _, cand in out} == {"flappy"}
    assert out[0][0] == "tech_install_new"


def test_deterministic_ordering_by_vendor():
    out = diff_technologies({"zebra", "alpha"}, {"alpha", "mid"}, domain=DOMAIN, today=TODAY)
    vendors = [cand.evidence_data["vendor"] for _, cand in out]
    assert vendors == sorted(vendors)
    assert vendors == ["mid", "zebra"]  # installs and churn interleaved by vendor name
    assert [s for s, _ in out] == ["tech_install_new", "tech_churn"]


def test_empty_previous_first_run_installs_everything():
    current = {"x", "y", "z"}
    out = diff_technologies(set(), current, domain=DOMAIN, today=TODAY)
    assert {cand.evidence_data["vendor"] for _, cand in out} == current
    assert all(s == "tech_install_new" for s, _ in out)


def test_keys_are_stable():
    _, c1 = diff_technologies(set(), {"v"}, domain=DOMAIN, today=TODAY)[0]
    _, c2 = diff_technologies(set(), {"v"}, domain=DOMAIN, today=TODAY)[0]
    assert c1.natural_key == c2.natural_key == f"techchg:{DOMAIN}:v:{TODAY}"
