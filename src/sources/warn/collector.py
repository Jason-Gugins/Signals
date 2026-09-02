from src.sources.warn.states.ca import CaWarn
from src.sources.warn.states.fl import FlWarn
from src.sources.warn.states.il import IlWarn
from src.sources.warn.states.nj import NjWarn
from src.sources.warn.states.ny import NyWarn
from src.sources.warn.states.oh import OhWarn
from src.sources.warn.states.tx import TxWarn
from src.sources.warn.states.wa import WaWarn

WARN_JURISDICTIONS = {
    "NY": NyWarn(),
    "CA": CaWarn(),
    "WA": WaWarn(),
    "TX": TxWarn(),
    "IL": IlWarn(),
    "NJ": NjWarn(),
    "FL": FlWarn(),
    "OH": OhWarn(),
    # "MI": JS-rendered, browser tier required — see P3 research; deferred.
}
