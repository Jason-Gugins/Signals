from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.crtsh.subdomains import SUBDOMAIN_HINTS, infer_from_subdomains, parse_crtsh
from src.sources.registry import register

# Bound a subdomain flood (wildcard cert leaks etc.): at most 50
# new_subdomain candidates per cycle, hint-labeled names first.
MAX_NEW_SUBDOMAINS_PER_CYCLE = 50


@register
class CrtshSource(SourceAdapter):
    key = "crtsh"
    tier = "http"
    cadence_hours = 336

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://crt.sh/?q=%25.{account.domain}&output=json", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        if not doc.body:
            return []
        names = parse_crtsh(doc.body)
        today = task_meta["today"]
        out = []
        for m in infer_from_subdomains(names, {}):
            out.append(
                SignalCandidate(
                    signal_type="tech_install_new",
                    observed_at=today,
                    natural_key=f"crt:{m.vendor}",
                    title=m.display,
                    confidence=m.confidence,
                    evidence_data={"vendor": m.vendor},
                )
            )
        # Cycle-over-cycle delta: the runner injects the most recent prior
        # crt.sh doc's parsed name list as prev_crtsh_names (mirrors the
        # wayback prev_pricing_html/prev_homepage_html mechanism). No prev
        # (first run) or an empty prev (a prior doc that parsed to zero
        # names) -> no signal: first run never guesses.
        prev = task_meta.get("prev_crtsh_names")
        if isinstance(prev, list) and prev:
            prev_names = {str(n).casefold() for n in prev}
            fresh = [n for n in names if n not in prev_names]
            # Stable order under the cap: hint-labeled names (app./status./
            # jobs./... — the product/GTM motions) first, then the sorted
            # remainder (names is already sorted).
            hinted = [n for n in fresh if n.split(".")[0] in SUBDOMAIN_HINTS]
            rest = [n for n in fresh if n.split(".")[0] not in SUBDOMAIN_HINTS]
            for name in (hinted + rest)[:MAX_NEW_SUBDOMAINS_PER_CYCLE]:
                out.append(
                    SignalCandidate(
                        signal_type="new_subdomain",
                        observed_at=today,
                        # PERMANENT key: a subdomain that disappears and
                        # re-appears is not new again — dedupe is per key
                        # forever, not per cycle.
                        natural_key=f"subdomain:{account.domain}:{name}",
                        title=name,
                        confidence=0.5,
                        evidence_data={"subdomain": name},
                    )
                )
        return out
