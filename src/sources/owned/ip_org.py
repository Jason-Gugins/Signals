"""PTR / IP to org helper."""

def default_ptr_lookup(ip: str) -> str | None:
    try:
        import dns.reversename
        import dns.resolver
        name = dns.reversename.from_address(ip)
        ans = dns.resolver.resolve(name, "PTR")
        return str(ans[0]).rstrip(".")
    except Exception:
        return None
