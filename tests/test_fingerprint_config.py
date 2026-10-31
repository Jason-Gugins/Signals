"""fingerprints.yaml completeness invariants — config-as-contract."""
import pytest

from src.sources.techstack.fingerprint import load_fingerprint_rules


@pytest.fixture(scope="module")
def rules():
    return load_fingerprint_rules()


def test_every_vendor_has_category_and_match(rules):
    for key, spec in rules["vendors"].items():
        assert spec.get("category"), f"{key}: missing category[]"
        assert spec.get("match"), f"{key}: empty match block"


def test_enterprise_vendors_have_contract_years(rules):
    missing = [k for k, s in rules["vendors"].items()
               if s.get("tier") == "enterprise" and not isinstance(s.get("contract_years"), int)]
    assert missing == [], f"enterprise vendors missing contract_years: {missing}"


def test_contract_years_sane(rules):
    for k, s in rules["vendors"].items():
        cy = s.get("contract_years")
        if cy is not None:
            assert 1 <= cy <= 5, f"{k}: contract_years {cy} outside 1-5"
