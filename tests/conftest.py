import pytest


@pytest.fixture(autouse=True)
def _no_live_massive(monkeypatch):
    """Unit tests never call the live Massive API (tests inject fetchers)."""
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    import marketdata

    marketdata._MASSIVE_CACHE.clear()
