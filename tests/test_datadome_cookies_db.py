from src.core.db import Database


def test_datadome_cookies_table_exists(tmp_path):
    db = Database(tmp_path / "s.db")
    cols = db.table_columns("datadome_cookies")
    expected = {"domain", "user_agent", "proxy", "cookies", "expires_at", "solve_method"}
    assert expected.issubset(cols)


def test_datadome_cookie_store_put_get(tmp_path):
    from src.core.db import DataDomeCookieStore
    db = Database(tmp_path / "s.db")
    store = DataDomeCookieStore(db)
    store.put("g2.com", user_agent="Chrome/147", proxy="http://proxy:8080",
              cookies=[{"name": "datadome", "value": "abc123"}],
              expires_at="2030-08-26T00:00:00+00:00", solve_method="2captcha")
    cached = store.get("g2.com", user_agent="Chrome/147", proxy="http://proxy:8080")
    assert cached is not None
    import json
    cookies = json.loads(cached["cookies"])
    assert cookies[0]["name"] == "datadome"
    assert cookies[0]["value"] == "abc123"


def test_datadome_cookie_store_miss(tmp_path):
    from src.core.db import DataDomeCookieStore
    db = Database(tmp_path / "s.db")
    store = DataDomeCookieStore(db)
    assert store.get("g2.com", user_agent="Chrome/147", proxy="direct") is None


def test_datadome_cookie_store_clear(tmp_path):
    from src.core.db import DataDomeCookieStore
    db = Database(tmp_path / "s.db")
    store = DataDomeCookieStore(db)
    store.put("g2.com", user_agent="UA", proxy="direct",
              cookies=[{"name": "datadome", "value": "x"}],
              expires_at="2030-08-26T00:00:00+00:00", solve_method="browser")
    store.clear("g2.com")
    assert store.get("g2.com", user_agent="UA", proxy="direct") is None


def test_datadome_cookie_store_expired_returns_none(tmp_path):
    from src.core.db import DataDomeCookieStore
    db = Database(tmp_path / "s.db")
    store = DataDomeCookieStore(db)
    store.put("g2.com", user_agent="UA", proxy="direct",
              cookies=[{"name": "datadome", "value": "x"}],
              expires_at="2020-01-01T00:00:00+00:00", solve_method="browser")
    assert store.get("g2.com", user_agent="UA", proxy="direct") is None
