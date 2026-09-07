"""Named stock universes for the Lab screener.

Three index lists ship with the repo as a dated snapshot; ``load_universe``
prefers a fresher copy in ``data/universes.json`` when one exists.  Refresh
that file on a machine with open internet (the VPS) with::

    python -m v2.screening.universes --refresh            # all three, from Wikipedia
    python -m v2.screening.universes --show sp500          # print what will be used

A stale snapshot is harmless for screening: a delisted ticker simply fails
the metrics fetch and is skipped, a newcomer is missing until the next
refresh.  The snapshot date is reported alongside every screening run.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_PATH = PROJECT_ROOT / "data" / "universes.json"

#: date of the bundled snapshot below
BUNDLED_AS_OF = "2025-12-31"

DOW30: list[str] = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "GS", "HD", "HON", "IBM", "JNJ",
    "JPM", "KO", "MCD", "MMM", "MRK", "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
]

NASDAQ100: list[str] = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN", "AMZN", "ANSS", "APP", "ARM", "ASML",
    "AVGO", "AXON", "AZN", "BIIB", "BKNG", "BKR", "CCEP", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT",
    "CRWD", "CSCO", "CSGP", "CSX", "CTAS", "CTSH", "DASH", "DDOG", "DXCM", "EA", "EXC", "FANG", "FAST", "FTNT",
    "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX", "INTC", "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN",
    "LRCX", "LULU", "MAR", "MCHP", "MDLZ", "MELI", "META", "MNST", "MRVL", "MSFT", "MSTR", "MU", "NFLX", "NVDA",
    "NXPI", "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PLTR", "PYPL", "QCOM", "REGN", "ROP",
    "ROST", "SBUX", "SNPS", "TEAM", "TMUS", "TSLA", "TTD", "TTWO", "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]

SP500: list[str] = [
    # Information technology
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "AMD", "CSCO", "ACN", "IBM", "INTU", "NOW", "QCOM", "TXN",
    "AMAT", "PANW", "ANET", "MU", "ADI", "LRCX", "KLAC", "APH", "CRWD", "CDNS", "SNPS", "MSI", "ADSK", "FTNT", "ROP",
    "WDAY", "NXPI", "MCHP", "TEL", "IT", "GLW", "CTSH", "FICO", "HPQ", "DELL", "MPWR", "KEYS", "ON", "CDW", "HPE",
    "TYL", "NTAP", "PTC", "TDY", "WDC", "STX", "ZBRA", "GDDY", "FSLR", "TER", "TRMB", "JBL", "AKAM", "SWKS", "GEN",
    "FFIV", "VRSN", "JNPR", "ENPH", "EPAM", "QRVO", "SMCI", "PLTR",
    # Communication services
    "GOOGL", "GOOG", "META", "NFLX", "DIS", "TMUS", "CMCSA", "VZ", "T", "CHTR", "EA", "WBD", "TTWO", "OMC", "LYV",
    "IPG", "FOXA", "FOX", "NWSA", "NWS", "MTCH", "PARA",
    # Consumer discretionary
    "AMZN", "TSLA", "HD", "MCD", "BKNG", "LOW", "TJX", "SBUX", "NKE", "ORLY", "CMG", "MAR", "GM", "HLT", "ABNB",
    "AZO", "ROST", "F", "DHI", "RCL", "YUM", "LEN", "TSCO", "EBAY", "GRMN", "DECK", "NVR", "PHM", "ULTA", "EXPE",
    "LULU", "DRI", "APTV", "CCL", "BBY", "POOL", "TPR", "KMX", "LVS", "DPZ", "GPC", "LKQ", "RL", "HAS", "MGM",
    "NCLH", "WYNN", "CZR", "MHK", "BWA", "ETSY",
    # Consumer staples
    "WMT", "COST", "PG", "KO", "PEP", "PM", "MDLZ", "MO", "CL", "TGT", "KMB", "MNST", "KDP", "KVUE", "GIS", "SYY",
    "STZ", "KHC", "HSY", "ADM", "KR", "CHD", "MKC", "K", "DG", "DLTR", "CLX", "EL", "TSN", "HRL", "CAG", "SJM", "CPB",
    "TAP", "BG", "LW", "BF.B", "WBA",
    # Health care
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "ISRG", "AMGN", "DHR", "PFE", "SYK", "BSX", "VRTX", "GILD",
    "MDT", "BMY", "ELV", "CI", "REGN", "ZTS", "CVS", "BDX", "MCK", "HCA", "COR", "EW", "A", "IDXX", "IQV", "RMD",
    "GEHC", "HUM", "CNC", "DXCM", "MTD", "CAH", "BIIB", "WST", "ZBH", "STE", "WAT", "LH", "HOLX", "BAX", "DGX", "ALGN",
    "PODD", "MOH", "COO", "VTRS", "RVTY", "TECH", "INCY", "CRL", "UHS", "HSIC", "DVA", "SOLV", "MRNA",
    # Financials
    "BRK.B", "JPM", "V", "MA", "BAC", "WFC", "GS", "AXP", "MS", "SPGI", "BLK", "C", "SCHW", "PGR", "MMC", "CB", "FI",
    "ICE", "CME", "KKR", "BX", "PYPL", "AON", "MCO", "USB", "PNC", "AJG", "COF", "TFC", "APO", "TRV", "AFL", "BK",
    "AMP", "ALL", "MET", "AIG", "MSCI", "PRU", "ACGL", "HIG", "NDAQ", "FIS", "DFS", "WTW", "MTB", "STT", "BRO", "FITB",
    "TROW", "RJF", "GPN", "HBAN", "SYF", "CINF", "NTRS", "RF", "CFG", "CPAY", "WRB", "CBOE", "PFG", "KEY", "FDS", "L",
    "EG", "JKHY", "AIZ", "GL", "ERIE", "IVZ", "BEN", "MKTX",
    # Industrials
    "GE", "CAT", "RTX", "UNP", "HON", "ETN", "BA", "DE", "LMT", "ADP", "UPS", "GEV", "TT", "PH", "WM", "GD", "CTAS",
    "MMM", "ITW", "NOC", "TDG", "CSX", "EMR", "FDX", "CARR", "NSC", "PCAR", "URI", "JCI", "CPRT", "GWW", "PWR", "LHX",
    "CMI", "FAST", "PAYX", "AME", "ODFL", "VRSK", "IR", "RSG", "OTIS", "EFX", "DAL", "XYL", "WAB", "AXON", "HWM", "DOV",
    "ROK", "BR", "UAL", "FTV", "LDOS", "VLTO", "HUBB", "BLDR", "EXPD", "MAS", "J", "TXT", "IEX", "SNA", "LUV", "SWK",
    "PNR", "NDSN", "CHRW", "JBHT", "ALLE", "ROL", "DAY", "GNRC", "PAYC", "AOS", "HII",
    # Energy
    "XOM", "CVX", "COP", "EOG", "WMB", "SLB", "PSX", "MPC", "OKE", "KMI", "VLO", "HES", "OXY", "FANG", "BKR", "TRGP",
    "HAL", "DVN", "CTRA", "EQT", "APA",
    # Materials
    "LIN", "SHW", "APD", "ECL", "FCX", "NEM", "CTVA", "DD", "MLM", "VMC", "NUE", "DOW", "PPG", "IFF", "LYB", "BALL",
    "AVY", "PKG", "STLD", "CF", "IP", "AMCR", "ALB", "EMN", "CE", "MOS",
    # Real estate
    "PLD", "AMT", "EQIX", "WELL", "SPG", "DLR", "PSA", "O", "CCI", "CBRE", "EXR", "VICI", "IRM", "CSGP", "AVB", "VTR",
    "SBAC", "EQR", "WY", "INVH", "ESS", "MAA", "ARE", "KIM", "DOC", "UDR", "HST", "CPT", "REG", "BXP", "FRT",
    # Utilities
    "NEE", "SO", "DUK", "CEG", "SRE", "AEP", "VST", "D", "PCG", "PEG", "EXC", "XEL", "ED", "ETR", "WEC", "EIX", "DTE",
    "PPL", "AWK", "ES", "FE", "AEE", "CNP", "ATO", "CMS", "NRG", "NI", "LNT", "EVRG", "PNW", "AES",
]

_BUNDLED: dict[str, list[str]] = {"sp500": SP500, "nasdaq100": NASDAQ100, "dow30": DOW30}
UNIVERSE_LABELS: dict[str, str] = {"sp500": "标普 500", "nasdaq100": "纳斯达克 100", "dow30": "道琼斯 30"}

_WIKI = {
    "sp500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", ("symbol", "ticker")),
    "nasdaq100": ("https://en.wikipedia.org/wiki/Nasdaq-100", ("ticker", "symbol")),
    "dow30": ("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average", ("symbol", "ticker")),
}


def _dedupe(tickers: list[str]) -> list[str]:
    out: list[str] = []
    for t in tickers:
        t = t.strip().upper().replace(".", ".")
        if t and t not in out:
            out.append(t)
    return out


def load_universe(name: str) -> tuple[list[str], str]:
    """``(tickers, as_of)`` — the refreshed file when present, else the bundled snapshot."""
    if name not in _BUNDLED:
        raise KeyError(f"unknown universe {name!r}; known: {', '.join(_BUNDLED)}")
    if DATA_PATH.exists():
        try:
            data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
            entry = data.get(name)
            if entry and entry.get("tickers"):
                return _dedupe(list(entry["tickers"])), str(entry.get("as_of") or "")
        except (OSError, ValueError) as exc:
            logger.warning("universes.json unreadable (%s); using bundled snapshot", exc)
    return _dedupe(_BUNDLED[name]), BUNDLED_AS_OF


def universe_status() -> dict[str, dict[str, object]]:
    return {name: {"size": len(load_universe(name)[0]), "as_of": load_universe(name)[1], "label": UNIVERSE_LABELS[name]} for name in _BUNDLED}


# ----------------------------------------------------------------- refresh from Wikipedia

class _Tables(HTMLParser):
    """Collect every table on the page as rows of cell text (nesting-safe)."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []
        self._saved: list[tuple[list[str] | None, list[str] | None]] = []  # outer row/cell while inside a nested table
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
            self._saved.append((self._row, self._cell))
            self._row, self._cell = None, None
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            if self._row:
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())
            self._row, self._cell = self._saved.pop() if self._saved else (None, None)

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


_TICKER_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,7}")


def _symbols_from_table(rows: list[list[str]], header_names: tuple[str, ...]) -> list[str]:
    for h, header in enumerate(rows[:3]):  # header may follow a caption row
        cols = [c.strip().lower() for c in header]
        col = next((i for i, c in enumerate(cols) if c in header_names), None)
        if col is None:
            continue
        out = []
        for row in rows[h + 1:]:
            if len(row) > col:
                cell = row[col].replace(" ", "").strip()
                if _TICKER_RE.fullmatch(cell):
                    out.append(cell)
        return out
    return []


def parse_constituents(html: str, header_names: tuple[str, ...]) -> list[str]:
    """Symbols from whichever table has a matching header and the most valid tickers."""
    parser = _Tables()
    parser.feed(html)
    best: list[str] = []
    for rows in parser.tables:
        found = _symbols_from_table(rows, header_names)
        if len(found) > len(best):
            best = found
    return _dedupe(best)


def describe_tables(html: str) -> list[str]:
    """One line per table: row count and header cells — for ``--dump`` debugging."""
    parser = _Tables()
    parser.feed(html)
    return [f"table {i}: {len(rows)} rows · header {rows[0][:8] if rows else []}" for i, rows in enumerate(parser.tables)]


def _fetch(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "ai-hedge-fund-altdata/2026 (+https://github.com/YuhanWang03/ai-hedge-fund-altdata)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def refresh_from_wikipedia(names: list[str] | None = None, *, path: Path = DATA_PATH, timeout: float = 30.0) -> dict[str, object]:
    """Fetch current constituents and write ``data/universes.json``.

    Each index is independent: a page whose layout defeats the parser is
    reported under ``errors`` and its previous entry (or the bundled list)
    stays in force; the others are still written.
    """
    names = names or list(_WIKI)
    data: dict[str, dict[str, object]] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    sizes: dict[str, int] = {}
    errors: dict[str, str] = {}
    minimum = {"sp500": 480, "nasdaq100": 90, "dow30": 28}
    for name in names:
        url, headers = _WIKI[name]
        try:
            tickers = parse_constituents(_fetch(url, timeout), headers)
            if len(tickers) < minimum[name]:
                raise RuntimeError(f"parsed only {len(tickers)} symbols (expected ≥ {minimum[name]}); run --dump {name} to see the tables")
        except Exception as exc:  # noqa: BLE001 — one bad page must not block the rest
            errors[name] = f"{type(exc).__name__}: {exc}"
            logger.warning("universe refresh %s failed: %s", name, exc)
            continue
        data[name] = {"tickers": tickers, "as_of": date.today().isoformat(), "source": url}
        sizes[name] = len(tickers)
    if sizes:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"written": str(path) if sizes else None, "sizes": sizes, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m v2.screening.universes")
    parser.add_argument("--refresh", action="store_true", help="fetch current constituents from Wikipedia into data/universes.json")
    parser.add_argument("--show", choices=sorted(_BUNDLED), help="print the tickers that will be used for one universe")
    parser.add_argument("--dump", choices=sorted(_BUNDLED), help="print every table header found on the Wikipedia page (parser debugging)")
    args = parser.parse_args(argv)
    if args.dump:
        for line in describe_tables(_fetch(_WIKI[args.dump][0], 30.0)):
            print(line)
    if args.refresh:
        report = refresh_from_wikipedia()
        print(json.dumps(report, ensure_ascii=False, indent=1))
        if report["errors"]:
            return 1
    if args.show:
        tickers, as_of = load_universe(args.show)
        print(f"{args.show} · {len(tickers)} tickers · as of {as_of}")
        print(" ".join(tickers))
    if not (args.refresh or args.show or args.dump):
        print(json.dumps(universe_status(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
