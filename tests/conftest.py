"""Autouse: block live HTTP/network unless a test uses respx or is marked.

Blocked surfaces:
- httpx.Client.send + httpx.HTTPTransport.handle_request
- curl_cffi (module-level requests.get / Session.request, and Curl.perform)
- urllib.request.urlopen
- socket.create_connection

Escape hatches: the ``allow_network`` / ``live_fetch`` markers, or respx
(the ``respx_mock`` fixture or ``@respx`` / ``respx.mock`` decorators).
"""

from __future__ import annotations

import inspect

import pytest


def _escape(request) -> bool:
    if request.node.get_closest_marker("allow_network"):
        return True
    if request.node.get_closest_marker("live_fetch"):
        return True
    if "respx_mock" in request.fixturenames:
        return True
    func = getattr(request.node, "obj", None)
    try:
        src = inspect.getsource(func) if func else ""
    except (OSError, TypeError):
        src = ""
    return "@respx" in src or "respx.mock" in src


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if _escape(request):
        return

    def boom(*a, **k):
        raise RuntimeError("network access in tests")

    # httpx
    monkeypatch.setattr("httpx.Client.send", boom, raising=False)
    monkeypatch.setattr("httpx.HTTPTransport.handle_request", boom, raising=False)

    # curl_cffi: module-level helpers and session methods (curl_fetcher calls
    # curl_requests.get with a call-time import, so patching the module attr works).
    monkeypatch.setattr("curl_cffi.requests.get", boom, raising=False)
    monkeypatch.setattr("curl_cffi.requests.post", boom, raising=False)
    monkeypatch.setattr("curl_cffi.requests.head", boom, raising=False)
    monkeypatch.setattr("curl_cffi.requests.request", boom, raising=False)
    monkeypatch.setattr("curl_cffi.requests.Session.request", boom, raising=False)
    monkeypatch.setattr("curl_cffi.Curl.impersonate", boom, raising=False)

    # stdlib
    monkeypatch.setattr("urllib.request.urlopen", boom, raising=False)
    monkeypatch.setattr("socket.create_connection", boom, raising=False)
    monkeypatch.setattr("socket.socket.connect", boom, raising=False)
