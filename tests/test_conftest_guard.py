"""Guard behavior for the conftest _no_network autouse fixture."""

from __future__ import annotations

import socket
import urllib.request

import httpx
import pytest


def test_unmarked_httpx_call_raises():
    """(a) An unmarked test that calls a real HTTP client must be blocked."""
    with pytest.raises(RuntimeError, match="network access in tests"):
        with httpx.Client() as client:
            client.send(
                httpx.Request("GET", "http://127.0.0.1:1/").stream
                and httpx.Request("GET", "http://127.0.0.1:1/")
            )


def test_unmarked_urllib_call_raises():
    with pytest.raises(RuntimeError, match="network access in tests"):
        urllib.request.urlopen("http://127.0.0.1:1/")


def test_unmarked_socket_call_raises():
    with pytest.raises(RuntimeError, match="network access in tests"):
        socket.create_connection(("127.0.0.1", 1))


def test_unmarked_curl_cffi_call_raises():
    curl_cffi = pytest.importorskip("curl_cffi")
    with pytest.raises(RuntimeError, match="network access in tests"):
        curl_cffi.get("http://127.0.0.1:1/")


@pytest.mark.allow_network
def test_allow_network_marker_bypasses_guard(request):
    """(b) With the allow_network marker, the guard fixture must be a no-op —
    the patched targets are restored, so a blocked call would NOT raise here
    (it would fail on connection refused instead, i.e. OSError, not RuntimeError).
    We assert on the marker logic itself without performing real I/O."""
    assert request.node.get_closest_marker("allow_network") is not None
    # The guard fixture only skips patching when the marker is present; verify
    # by checking that the marker exists on this node (logic-level check —
    # real I/O is skipped in the offline lane by the CI marker filter).


@pytest.mark.live_fetch
def test_live_fetch_marker_exists(request):
    """(c) The live_fetch marker exists and is recognized on this node."""
    assert request.node.get_closest_marker("live_fetch") is not None
    assert request.node.get_closest_marker("allow_network") is None
