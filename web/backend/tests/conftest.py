"""Make the web backend importable where the production-only ``v2/data``
package is absent (sandbox / CI checkout).

``v2/data/client.py`` and friends ship on the VPS and are git-ignored (see
the root ``.gitignore``).  Several v2 modules import them at module level,
so without them ``app.main`` cannot even be imported.  When — and only
when — the real package is missing, install minimal stand-ins exposing the
names those modules import.  Tests that need real data behaviour
monkeypatch the call sites; nothing here performs I/O.  On a machine with
the real package this file is a no-op.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_DATA_DIR = _REPO / "v2" / "data"

try:  # real production package present → do nothing
    import v2.data.client  # noqa: F401
except ImportError:
    class _Unavailable(RuntimeError):
        pass

    class FDClient:  # noqa: D401 — stub
        """Stand-in for the production Financial Datasets client."""

        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def __getattr__(self, name):
            raise _Unavailable(f"v2.data.FDClient.{name} is production-only; monkeypatch the caller")

    class CachedFDClient(FDClient):
        pass

    class ProviderRequestError(RuntimeError):
        pass

    @dataclass
    class Price:
        open: float
        close: float
        high: float
        low: float
        volume: int
        time: str

    @dataclass
    class EarningsRecord:
        ticker: str = ""
        report_date: str = ""

    @dataclass
    class EarningsData:
        ticker: str = ""

    class NewsProvider:  # noqa: D401 — stub
        def get_news(self, *args, **kwargs):
            return []

    def default_news_provider() -> NewsProvider:
        return NewsProvider()

    class YFinanceClient(FDClient):
        pass

    KNOWN_ADRS: set[str] = set()

    pkg = types.ModuleType("v2.data")
    pkg.__path__ = [str(_DATA_DIR)]  # keep the real price_source.py importable as a submodule
    pkg.FDClient, pkg.CachedFDClient = FDClient, CachedFDClient
    pkg.Price, pkg.EarningsRecord, pkg.EarningsData = Price, EarningsRecord, EarningsData

    client = types.ModuleType("v2.data.client")
    client.FDClient, client.ProviderRequestError = FDClient, ProviderRequestError
    models = types.ModuleType("v2.data.models")
    models.Price, models.EarningsRecord, models.EarningsData = Price, EarningsRecord, EarningsData
    news = types.ModuleType("v2.data.news_provider")
    news.NewsProvider, news.default_news_provider = NewsProvider, default_news_provider
    yf = types.ModuleType("v2.data.yfinance_client")
    yf.YFinanceClient, yf.KNOWN_ADRS = YFinanceClient, KNOWN_ADRS

    for name, module in (("v2.data", pkg), ("v2.data.client", client), ("v2.data.models", models),
                         ("v2.data.news_provider", news), ("v2.data.yfinance_client", yf)):
        sys.modules[name] = module
    pkg.client, pkg.models, pkg.news_provider, pkg.yfinance_client = client, models, news, yf


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_lab_stores(tmp_path, monkeypatch):
    """Every test gets fresh lab.db / personas.db instead of the repo's data/."""
    monkeypatch.setenv("WEB_LAB_DB", str(tmp_path / "lab.db"))
    monkeypatch.setenv("WEB_PERSONAS_DB", str(tmp_path / "personas.db"))
    from app.routers import committee, workspace

    monkeypatch.setattr(workspace, "_LAB_STORE", None)
    monkeypatch.setattr(committee, "_STORE", None)
    yield
