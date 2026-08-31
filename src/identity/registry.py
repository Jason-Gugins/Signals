"""Account registry: coalescing upsert + alias resolution."""

from __future__ import annotations

from typing import Optional

from src.core.db import Database
from src.core.models import Account
from src.identity.domains import root_domain
from src.identity.names import name_similarity, normalize_name
from src.identity.resolve import fuzzy_match, normalize_entity

_ORDER_WHITELIST = {
    "score DESC": "score DESC",
    "score ASC": "score ASC",
    "domain": "domain",
    "domain ASC": "domain ASC",
    "name": "name",
    "tier": "tier",
    "updated_at DESC": "updated_at DESC",
}


class AccountRegistry:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, account: Account, *, source: str | None = None) -> Account:
        domain = root_domain(account.domain) or account.domain.casefold()
        account.domain = domain
        if source and not account.seed_source:
            account.seed_source = source
        self.db.upsert("accounts", account.to_db_row(), pk="domain")
        if account.name:
            self.add_alias(account.name, "name", domain, source=source)
        if account.linkedin_slug:
            self.add_alias(account.linkedin_slug, "linkedin_slug", domain, source=source)
        if account.ticker:
            self.add_alias(account.ticker, "ticker", domain, source=source)
        if account.cik:
            self.add_alias(account.cik, "cik", domain, source=source)
        if account.ats_token:
            self.add_alias(account.ats_token, "ats_token", domain, source=source)
        self.add_alias(domain, "domain", domain, source=source)
        return self.get(domain)

    def get(self, domain: str) -> Optional[Account]:
        key = root_domain(domain) or domain.casefold()
        row = self.db.one("SELECT * FROM accounts WHERE domain = ?", (key,))
        return Account.from_db_row(row) if row else None

    def add_alias(
        self,
        alias: str,
        kind: str,
        domain: str,
        *,
        confidence: float = 1.0,
        source: str | None = None,
    ) -> None:
        if kind == "name":
            key = normalize_name(alias)
        elif kind in {"domain", "subdomain"}:
            key = root_domain(alias) or alias.casefold().strip()
        else:
            key = alias.strip().casefold()
        if not key:
            return
        self.db.upsert(
            "account_aliases",
            {
                "alias": key,
                "alias_kind": kind,
                "domain": domain,
                "confidence": confidence,
                "source": source,
            },
            pk=("alias", "alias_kind"),
        )

    def _by_alias(self, alias: str, kind: str) -> Optional[Account]:
        if kind == "name":
            key = normalize_name(alias)
        elif kind == "domain":
            key = root_domain(alias) or alias.casefold()
        else:
            key = alias.strip().casefold()
        if not key:
            return None
        row = self.db.one(
            "SELECT domain FROM account_aliases WHERE alias = ? AND alias_kind = ?",
            (key, kind),
        )
        return self.get(row["domain"]) if row else None

    def resolve(
        self,
        *,
        domain: str | None = None,
        name: str | None = None,
        linkedin_slug: str | None = None,
        ticker: str | None = None,
        cik: str | None = None,
        url: str | None = None,
        min_similarity: float = 0.86,
    ) -> Optional[Account]:
        if domain:
            hit = self.get(domain) or self._by_alias(domain, "domain")
            if hit:
                return hit
        if url:
            rd = root_domain(url)
            if rd:
                hit = self.get(rd) or self._by_alias(rd, "domain")
                if hit:
                    return hit
        if cik:
            hit = self._by_alias(cik, "cik")
            if hit:
                return hit
        if ticker:
            hit = self._by_alias(ticker, "ticker")
            if hit:
                return hit
        if linkedin_slug:
            hit = self._by_alias(linkedin_slug, "linkedin_slug")
            if hit:
                return hit
        if name:
            hit = self._by_alias(name, "name")
            if hit:
                return hit
            # Normalized entity-alias lookup (exact + normalize_entity
            # equality) — strictly higher priority than fuzzy guessing.
            hit = self._by_entity_alias(name)
            if hit:
                return hit
            # Normalized match against known name-aliases before falling
            # back to fuzzy similarity.
            hit = self._normalized_name(name)
            if hit:
                return hit
            return self._fuzzy_name(name, min_similarity)
        return None

    def _normalized_name(self, name: str) -> Optional[Account]:
        """Match a name against known name-aliases by normalize_entity equality."""
        key = normalize_entity(name)
        if not key:
            return None
        rows = self.db.query(
            "SELECT alias, domain FROM account_aliases WHERE alias_kind = 'name'"
        )
        for row in rows:
            if normalize_entity(row["alias"]) == key:
                return self.get(row["domain"])
        return None

    def add_entity_alias(self, alias: str, domain: str) -> None:
        """Manually map a company-name alias to a canonical account domain.

        entity_aliases entries are ONLY written here (or via
        load_entity_aliases_from_config) — never auto-derived from
        scraping/discovery, because a bad alias silently re-points one
        company's signals at another's account.
        """
        key = normalize_entity(alias)
        if not key:
            return
        self.db.upsert(
            "entity_aliases",
            {"alias": key, "domain": domain},
            pk="alias",
        )

    def load_entity_aliases_from_config(self, entries: dict[str, str]) -> None:
        """Populate entity_aliases from a config mapping {alias: domain}.

        Config-driven population path — the caller decides the mapping
        (e.g. an `entity_aliases:` section in a config YAML). Never called
        automatically by scraping code.
        """
        for alias, domain in entries.items():
            self.add_entity_alias(alias, domain)

    def _by_entity_alias(self, name: str) -> Optional[Account]:
        """entity_aliases lookup: exact normalized key first, then a
        full-table normalized-equality pass (handles legacy rows written
        with a different normalization). Higher priority than fuzzy."""
        key = normalize_entity(name)
        if not key:
            return None
        row = self.db.one(
            "SELECT domain FROM entity_aliases WHERE alias = ?", (key,)
        )
        if row:
            return self.get(row["domain"])
        rows = self.db.query("SELECT alias, domain FROM entity_aliases")
        for r in rows:
            if normalize_entity(r["alias"]) == key:
                return self.get(r["domain"])
        return None

    def _fuzzy_name(self, name: str, min_similarity: float) -> Optional[Account]:
        rows = self.db.query(
            "SELECT alias, domain FROM account_aliases WHERE alias_kind = 'name'"
        )
        hits: dict[str, float] = {}
        for row in rows:
            score = name_similarity(name, row["alias"])
            if score >= min_similarity:
                prev = hits.get(row["domain"])
                if prev is None or score > prev:
                    hits[row["domain"]] = score
        if len(hits) == 1:
            return self.get(next(iter(hits)))
        return None

    def list_accounts(
        self,
        *,
        cohort: str | None = None,
        tier: int | None = None,
        disqualified: bool = False,
        limit: int | None = None,
        order_by: str = "score DESC",
    ) -> list[Account]:
        clauses = ["disqualified = ?"]
        params: list = [1 if disqualified else 0]
        if cohort is not None:
            clauses.append("cohort = ?")
            params.append(cohort)
        if tier is not None:
            clauses.append("tier = ?")
            params.append(tier)
        order = _ORDER_WHITELIST.get(order_by, "score DESC")
        sql = f"SELECT * FROM accounts WHERE {' AND '.join(clauses)} ORDER BY {order}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [Account.from_db_row(r) for r in self.db.query(sql, params)]

    def set_scores(
        self,
        domain: str,
        *,
        score,
        tier,
        buying_window,
        scored_at,
    ) -> None:
        key = root_domain(domain) or domain.casefold()
        self.db.upsert(
            "accounts",
            {
                "domain": key,
                "score": score,
                "tier": tier,
                "buying_window": buying_window,
                "scored_at": scored_at,
            },
            pk="domain",
            overwrite={"score", "tier", "buying_window", "scored_at"},
        )

    def count(self, **filters) -> int:
        clauses = ["1=1"]
        params: list = []
        for key, val in filters.items():
            if key not in {"cohort", "tier", "disqualified", "buying_window"}:
                continue
            clauses.append(f"{key} = ?")
            params.append(int(val) if key == "disqualified" else val)
        row = self.db.one(
            f"SELECT COUNT(*) AS n FROM accounts WHERE {' AND '.join(clauses)}",
            params,
        )
        return int(row["n"]) if row else 0
