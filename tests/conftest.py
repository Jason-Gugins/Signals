"""Autouse: block live HTTP unless a test uses respx."""

from __future__ import annotations

import inspect

import pytest


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if request.node.get_closest_marker("allow_network"):
        return
    if "respx_mock" in request.fixturenames:
        return
    func = getattr(request.node, "obj", None)
    src = ""
    try:
        src = inspect.getsource(func) if func else ""
    except (OSError, TypeError):
        src = ""
    if "@respx" in src or "respx.mock" in src:
        return

    def boom(*a, **k):
        raise RuntimeError("network access in tests")

    monkeypatch.setattr("httpx.Client.send", boom, raising=False)
    monkeypatch.setattr("httpx.HTTPTransport.handle_request", boom, raising=False)
