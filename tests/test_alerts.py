from datetime import date
from pathlib import Path

from src.core.db import Database
from src.core.models import Account, Signal
from src.export.alerts import Alert, build_alerts, format_slack_text, post_webhook, write_jsonl
from src.identity.registry import AccountRegistry
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy


def _sig(domain, typ, seen):
    spec = Taxonomy.load().get(typ)
    return Signal(
        signal_id=f"{domain}-{typ}-{seen}",
        domain=domain, signal_type=typ, category=spec.category, origin=spec.origin,
        catalyst=spec.catalyst, polarity=spec.polarity, observed_at=seen, source="t",
        confidence=0.9, first_seen_at=seen, last_seen_at=seen, evidence="raised Series B",
    )


def test_alerts_filters_jsonl_webhook(tmp_path):
    db = Database(tmp_path / "s.db")
    reg = AccountRegistry(db)
    reg.upsert(Account(domain="acme.com", name="Acme", tier=1, score=80))
    reg.upsert(Account(domain="old.com", name="Old", tier=4, score=5))
    store = SignalStore(db)
    store.upsert(_sig("acme.com", "funding_round", "2026-08-10"))
    store.upsert(_sig("acme.com", "award", "2026-08-10"))
    store.upsert(_sig("acme.com", "funding_round", "2026-07-01"))
    store.upsert(_sig("old.com", "funding_round", "2026-08-10"))
    alerts = build_alerts(db, since="2026-08-01", min_tier=2, primary_only=True)
    types = {a.signal_type for a in alerts}
    assert "funding_round" in types
    assert "award" not in types
    assert all(a.domain != "old.com" for a in alerts)
    path = tmp_path / "a.jsonl"
    write_jsonl(alerts, str(path))
    write_jsonl(alerts, str(path))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 * len(alerts)
    import json
    json.loads(lines[0])
    text = format_slack_text(alerts[0])
    assert "Acme" in text and "raised" in text

    class Resp:
        status_code = 500

    class Client:
        def __init__(self):
            self.n = 0
        def post(self, url, json=None, timeout=None):
            self.n += 1
            return Resp()
        def close(self):
            pass

    c = Client()
    sent = post_webhook(alerts[:3] or [Alert("x","x",1,1,"t","e",None,"growth_pitch",1,"2026-08-16")], "http://x", client=c, batch=2)
    assert sent == 0
    assert c.n >= 1
