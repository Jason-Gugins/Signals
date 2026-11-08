"""PTR / IP to org helper."""

def default_ptr_lookup(ip: str) -> str | None:
    try:
        import dns.resolver
        import dns.reversename
        name = dns.reversename.from_address(ip)
        ans = dns.resolver.resolve(name, "PTR")
        return str(ans[0]).rstrip(".")
    except Exception:  # noqa: BLE001 -- fail-open tolerance: no PTR record, no dnspython, or resolver failure all mean "no org attribution" — the visitor row still aggregates
        return None
