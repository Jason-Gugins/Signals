"""Stubs — enabled: false pending recon (2026-08-16)."""

from src.sources.warn import WarnNotice


class _Stub:
    fmt = "html"

    def discover(self, body: bytes) -> list[str]:
        return []

    def parse(self, body: bytes) -> list[WarnNotice]:
        return []


class WaWarn(_Stub):
    code = "WA"
    index_url = "https://www.lni.wa.gov/"
