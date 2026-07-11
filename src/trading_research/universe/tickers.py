"""Verified ticker universe.

The universe is the ONLY authority on which symbols exist. Ticker extraction
(and any LLM-proposed symbol) must validate against it — a symbol not in the
universe is rejected, never guessed. Symbols that collide with common English
words are additionally flagged ambiguous and require contextual confirmation
before a bare (non-cashtag) mention counts (see analysis/ticker_extractor.py).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

# Symbols that are also common English words. A bare-text match on one of
# these is NOT a mention unless contextually confirmed. Superset of the
# examples required by the spec (AI, IT, ON, ALL, SO, A, FOR, ARE).
AMBIGUOUS_SYMBOLS: frozenset[str] = frozenset(
    {
        "A", "AI", "ALL", "AN", "ARE", "AT", "BE", "BIG", "BY", "CAN", "CAR",
        "COST", "DAY", "DID", "EAT", "FOR", "FUN", "GO", "GOOD", "HAS", "HE",
        "IT", "LOVE", "LOW", "MAN", "NEW", "NOW", "ON", "ONE", "OPEN", "OR",
        "OUT", "PLAY", "REAL", "RUN", "SEE", "SHE", "SO", "STAY", "TELL",
        "TWO", "VERY", "WELL", "YOU",
    }
)


class UnknownSymbolError(ValueError):
    """Raised when a symbol is not in the verified universe (fail closed)."""


def normalize_symbol(raw: str) -> str:
    """Canonical form: uppercase, whitespace-stripped, leading '$' removed.

    Class-share dots/hyphens (BRK.B) are preserved as-is — the universe is
    the authority on which spelling exists. Raises UnknownSymbolError for
    input that cannot possibly be a symbol (empty, embedded whitespace).
    """
    symbol = raw.strip().lstrip("$").upper()
    if not symbol or any(c.isspace() for c in symbol):
        raise UnknownSymbolError(f"not a possible ticker symbol: {raw!r}")
    return symbol


@dataclass(frozen=True)
class Security:
    symbol: str
    name: str
    exchange: str
    sector: str = ""
    is_otc: bool = False
    is_active: bool = True
    source: str = "seed"


class TickerUniverse:
    """In-memory verified symbol set with ambiguity metadata."""

    def __init__(self, securities: list[Security]):
        self._by_symbol = {s.symbol.upper(): s for s in securities}

    def __len__(self) -> int:
        return len(self._by_symbol)

    def __contains__(self, symbol: str) -> bool:
        return symbol.upper() in self._by_symbol

    def get(self, symbol: str) -> Security | None:
        return self._by_symbol.get(symbol.upper())

    def is_valid(self, symbol: str) -> bool:
        """True if the symbol is verified, active, and not OTC."""
        try:
            sec = self.get(normalize_symbol(symbol))
        except UnknownSymbolError:
            return False
        return sec is not None and not sec.is_otc and sec.is_active

    def require(self, symbol: str) -> Security:
        """Return the Security for a valid symbol; raise UnknownSymbolError otherwise."""
        normalized = normalize_symbol(symbol)
        if not self.is_valid(normalized):
            raise UnknownSymbolError(
                f"{normalized!r} is not in the verified ticker universe — rejected"
            )
        return self._by_symbol[normalized]

    def is_ambiguous(self, symbol: str) -> bool:
        """True if bare-text mentions of this symbol need context confirmation."""
        return symbol.upper() in AMBIGUOUS_SYMBOLS

    def name_tokens(self, symbol: str) -> set[str]:
        """Lowercased company-name tokens usable for contextual confirmation.

        Excludes the symbol's own spelling (e.g. "ON Semiconductor" for ticker
        ON): a company name that happens to start with its own ticker would
        otherwise let any bare mention of that word self-confirm as a ticker
        mention, defeating the ambiguity check it's meant to gate.
        """
        sec = self.get(symbol)
        if sec is None:
            return set()
        stop = {"inc", "corp", "co", "the", "company", "group", "holdings", "ltd", "plc"}
        stop.add(sec.symbol.lower())
        return {t.strip(".,").lower() for t in sec.name.split()} - stop

    @classmethod
    def from_csv(cls, path: str | Path) -> "TickerUniverse":
        """Load a universe from CSV: symbol,name,exchange[,sector,is_otc,is_active,source].

        This is the intended replacement path for the embedded seed — point it
        at a full exchange-listing export and everything downstream (extraction,
        screening) picks it up unchanged.
        """
        def truthy(value: str, default: bool) -> bool:
            v = value.strip().lower()
            return default if v == "" else v in ("1", "true", "yes")

        securities = []
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                securities.append(
                    Security(
                        symbol=normalize_symbol(row["symbol"]),
                        name=row.get("name", "").strip(),
                        exchange=row.get("exchange", "").strip(),
                        sector=row.get("sector", "").strip(),
                        is_otc=truthy(row.get("is_otc", ""), False),
                        is_active=truthy(row.get("is_active", ""), True),
                        source=row.get("source", "").strip() or f"csv:{Path(path).name}",
                    )
                )
        return cls(securities)


# Small embedded seed for the PoC and tests. Real runs should load a full
# exchange listing via from_csv() — this seed deliberately includes the
# ambiguous-word symbols so extraction rules are exercised.
_SEED = [
    Security("AAPL", "Apple Inc", "NASDAQ", "Technology"),
    Security("MSFT", "Microsoft Corp", "NASDAQ", "Technology"),
    Security("NVDA", "NVIDIA Corp", "NASDAQ", "Technology"),
    Security("AMD", "Advanced Micro Devices Inc", "NASDAQ", "Technology"),
    Security("TSLA", "Tesla Inc", "NASDAQ", "Consumer Cyclical"),
    Security("PLTR", "Palantir Technologies Inc", "NASDAQ", "Technology"),
    Security("SOFI", "SoFi Technologies Inc", "NASDAQ", "Financial Services"),
    Security("F", "Ford Motor Company", "NYSE", "Consumer Cyclical"),
    Security("T", "AT&T Inc", "NYSE", "Communication Services"),
    Security("AI", "C3.ai Inc", "NYSE", "Technology"),
    Security("IT", "Gartner Inc", "NYSE", "Technology"),
    Security("ON", "ON Semiconductor Corp", "NASDAQ", "Technology"),
    Security("ALL", "Allstate Corp", "NYSE", "Financial Services"),
    Security("SO", "Southern Company", "NYSE", "Utilities"),
    Security("A", "Agilent Technologies Inc", "NYSE", "Healthcare"),
    Security("ARE", "Alexandria Real Estate Equities Inc", "NYSE", "Real Estate"),
    Security("FOR", "Forestar Group Inc", "NYSE", "Real Estate"),
    Security("OPEN", "Opendoor Technologies Inc", "NASDAQ", "Real Estate"),
    Security("RUN", "Sunrun Inc", "NASDAQ", "Technology"),
    Security("NOW", "ServiceNow Inc", "NYSE", "Technology"),
    Security("SIRI", "Sirius XM Holdings Inc", "NASDAQ", "Communication Services"),
    Security("LUMN", "Lumen Technologies Inc", "NYSE", "Communication Services"),
    Security("SHELCO", "Example Shell Co", "OTC", "", is_otc=True),
    Security("GONEQ", "Example Delisted Corp", "NYSE", "", is_active=False),
]


def default_universe() -> TickerUniverse:
    return TickerUniverse(list(_SEED))
