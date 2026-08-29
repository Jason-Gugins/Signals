"""Tests for the solve-and-bounce orchestrator (mocked ghosts, no real browser)."""

from src.antibot.python.ghost import (
    BounceResult,
    GhostResult,
    extract_clearance_cookies,
    solve_and_bounce,
)
from src.antibot.python.transport import AntibotResponse


class FakeTransport:
    """Records fetch calls; replays canned responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.fetch_calls = []  # (url, cookies)

    def fetch(self, url, *, cookies=None, **kwargs):
        self.fetch_calls.append((url, cookies))
        return self.responses.pop(0)


class FakeGhost:
    def __init__(self, clearance_cookies=None, error=False):
        self.clearance_cookies = clearance_cookies or []
        self.error = error
        self.solve_calls = 0

    def solve(self, url):
        self.solve_calls += 1
        if self.error:
            raise RuntimeError("browser boom")
        return GhostResult(clearance_cookies=self.clearance_cookies, rendered_html=None)


def _resp(status=200, body=b"<html>ok</html>"):
    return AntibotResponse(status=status, body=body, headers={}, url="https://x.test")


# --- clearance filter -------------------------------------------------------


def test_clearance_filter_keeps_only_clearance():
    jar = extract_clearance_cookies(
        [
            {"name": "_ga", "value": "x", "domain": ".x.test"},
            {"name": "_gid", "value": "y", "domain": ".x.test"},
            {"name": "_fbp", "value": "z", "domain": ".x.test"},
            {"name": "cf_clearance", "value": "a", "domain": ".x.test"},
            {"name": "datadome", "value": "b", "domain": ".x.test"},
            {"name": "_px3", "value": "c", "domain": ".x.test"},
            {"name": "__cf_bm", "value": "d", "domain": ".x.test"},
            {"name": "_abck", "value": "e", "domain": ".x.test"},
        ]
    )
    names = [c["name"] for c in jar]
    assert names == ["cf_clearance", "datadome", "_px3", "__cf_bm", "_abck"]


def test_clearance_filter_empty():
    assert extract_clearance_cookies([]) == []
    assert extract_clearance_cookies([{"name": "session_id", "value": "x"}]) == []


# --- solve_and_bounce --------------------------------------------------------


def test_solve_and_bounce_passes_through_when_no_challenge():
    t = FakeTransport([_resp(200)])
    ghost = FakeGhost(clearance_cookies=[{"name": "datadome", "value": "tok", "domain": ".g2.com"}])
    out = solve_and_bounce(t, ghost, "https://www.g2.com/x")
    assert out.via == "tier1_direct"
    assert out.response.status == 200
    assert out.clearance_cookies == []
    assert ghost.solve_calls == 0  # browser never woke up


def test_solve_and_bounce_hands_cookies_to_tier1():
    # First fetch is a DataDome challenge, second is clean.
    t = FakeTransport([_resp(403, b"datadome captcha"), _resp(200)])
    ghost = FakeGhost(
        clearance_cookies=[{"name": "datadome", "value": "tok", "domain": ".g2.com"}]
    )
    out = solve_and_bounce(t, ghost, "https://www.g2.com/products/x/reviews")
    assert out.via == "tier1_with_ghost_cookies"
    assert out.response.status == 200
    assert out.clearance_cookies == [
        {"name": "datadome", "value": "tok", "domain": ".g2.com"}
    ]
    assert ghost.solve_calls == 1  # solved exactly once
    assert len(t.fetch_calls) == 2  # tier1 fetched content both times
    # Second fetch received the harvested clearance cookies.
    assert t.fetch_calls[1][1] == [
        {"name": "datadome", "value": "tok", "domain": ".g2.com"}
    ]


def test_solve_and_bounce_uses_custom_challenge_check():
    t = FakeTransport([_resp(200), _resp(200)])
    ghost = FakeGhost(clearance_cookies=[{"name": "cf_clearance", "value": "c"}])
    out = solve_and_bounce(
        t,
        ghost,
        "https://x.test/",
        challenge_check=lambda r: r.status == 200,  # treat anything as challenged
    )
    assert out.via == "tier1_with_ghost_cookies"
    assert ghost.solve_calls == 1


def test_bounce_returns_solve_failed_when_ghost_errors():
    t = FakeTransport([_resp(403, b"datadome")])
    ghost = FakeGhost(error=True)
    out = solve_and_bounce(t, ghost, "https://www.g2.com/x")
    assert isinstance(out, BounceResult)
    assert out.via == "solve_failed"
    assert out.response is None  # documented: no refetch happened
    assert out.clearance_cookies == []


def test_bounce_returns_solve_failed_when_no_cookies_harvested():
    t = FakeTransport([_resp(403, b"datadome")])
    ghost = FakeGhost(clearance_cookies=[])
    out = solve_and_bounce(t, ghost, "https://www.g2.com/x")
    assert out.via == "solve_failed"
    assert out.response is None