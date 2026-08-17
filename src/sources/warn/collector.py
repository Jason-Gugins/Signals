from src.sources.warn.states.ca import CaWarn
from src.sources.warn.states.il import IlWarn
from src.sources.warn.states.ny import NyWarn
from src.sources.warn.states.tx import TxWarn
from src.sources.warn.states.wa import WaWarn

WARN_JURISDICTIONS = {
    "NY": NyWarn(),
    "CA": CaWarn(),
    "WA": WaWarn(),
    "TX": TxWarn(),
    "IL": IlWarn(),
}
