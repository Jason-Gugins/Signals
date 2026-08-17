from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.crtsh.subdomains import infer_from_subdomains, parse_crtsh
from src.sources.registry import register


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
        return out
