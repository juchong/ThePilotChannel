import time

import support  # noqa: F401  (environment must be prepared before app.main is imported)
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    """App client with every upstream stubbed: no test touches the network."""
    from app import main as main_mod

    async def fake_metar(icaos, stale):
        return {i.upper(): {"icao": i.upper(), "obs_time": time.time(), "wind": {}} for i in icaos}

    async def fake_traffic(lat, lon, nm):
        return [
            {"hex": "a1", "lat": lat, "lon": lon, "seen_pos": 1},
            {"hex": "a2", "lat": lat, "lon": lon, "seen_pos": 999},  # stale position, dropped
        ]

    async def fake_tile(z, x, y):
        return b"\x89PNG\r\n\x1a\n" + b"0" * 16

    monkeypatch.setattr(main_mod.manager.metar, "fetch", fake_metar)
    monkeypatch.setattr(main_mod.manager, "_fetch_traffic", fake_traffic)
    # the lifespan warms the basemap tile cache for the configured views: never from OSM in tests
    monkeypatch.setattr(main_mod.manager.tiles, "_fetch_upstream", fake_tile)
    with TestClient(main_mod.app) as c:
        yield c
