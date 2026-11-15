"""Shared test setup. Imported by conftest.py before the app so the module-level
DataManager reads a throwaway config, a stub static dir, a temp tile cache and a
temp secrets store. The retired HANGAR_ADMIN_PASSWORD variable is set, so the
app starts with the account it imports from it."""
import asyncio
import os
import tempfile
import time

import httpx

TMP = tempfile.mkdtemp(prefix="tpc-test-")
CFG = os.path.join(TMP, "config.yaml")
STATIC = os.path.join(TMP, "static")
os.makedirs(os.path.join(STATIC, "assets"))
for page, body in (("admin.html", "admin"), ("login.html", "login"), ("index.html", "display")):
    with open(os.path.join(STATIC, page), "w") as fh:
        fh.write(f"<html>{body}</html>")
with open(os.path.join(STATIC, "assets", "main-abc123.js"), "w") as fh:
    fh.write("// bundle")
os.environ["HANGAR_CONFIG"] = CFG
os.environ["HANGAR_STATIC"] = STATIC
os.environ["HANGAR_SECRETS"] = os.path.join(TMP, "secrets.json")
os.environ["HANGAR_ADMIN_PASSWORD"] = "hunter2"
os.environ["HANGAR_TILE_CACHE"] = os.path.join(TMP, "tiles")
os.environ["HANGAR_GO2RTC"] = "http://127.0.0.1:9"  # a closed port: the relay is unreachable in tests
for name in ("HANGAR_NMS_CLIENT_ID", "HANGAR_NMS_CLIENT_SECRET", "HANGAR_NMS_BASE", "HANGAR_LLM_API_KEY"):
    os.environ.pop(name, None)
for n in range(1, 13):
    os.environ.pop(f"HANGAR_CAM{n}", None)

from app.access import ACCESS  # noqa: E402  (the environment above must be set first)
from app.store import STORE  # noqa: E402

USER = {"username": "admin", "password": "hunter2"}


class Bearer(httpx.Auth):
    """Sends an API key the way an automation does."""

    def __init__(self, key: str):
        self.key = key

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self.key}"
        yield request


AUTH = Bearer(ACCESS.create_key("tests", "settings")["key"])          # may change settings
CONTROL = Bearer(ACCESS.create_key("tests control", "control")["key"])  # may control the display only


def sign_in(client):
    """Sign the test client's browser in; its cookie jar then carries the session."""
    r = client.post("/api/auth/login", json=USER)
    assert r.status_code == 200, r.text
    return r


def set_llm_key(monkeypatch, value: str) -> None:
    """Store a model endpoint key for one test."""
    monkeypatch.setitem(STORE.data["credentials"], "llm_api_key", {"value": value, "changed": 0})


def set_nms_keys(monkeypatch, client_id="the-id", secret="the-secret", environment="production") -> None:
    """Store FAA credentials for one test."""
    monkeypatch.setitem(STORE.data["credentials"], "nms", {
        "client_id": client_id, "client_secret": secret, "environment": environment, "changed": 0,
    })


class FakeClock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


class OpenGate:
    """A pull gate whose turn has always come."""
    limited = False

    def wait_s(self) -> float:
        return 0.0

    async def wait_turn(self) -> None:
        await asyncio.sleep(0)


class StubNms:
    """Stands in for the FAA client in manager tests. `by_ident` maps a location
    to its GeoJSON features, to None (the API does not know it), to an exception
    to raise, or to a coroutine function to await. A pull of changes returns
    every location's features."""
    name = "faa"
    configured = True

    def __init__(self, by_ident):
        from app.notams import UsageLog
        self.by_ident = by_ident
        self.calls = []
        self.environment = "production"
        self.gate = OpenGate()
        self.usage = UsageLog(os.path.join(tempfile.mkdtemp(prefix="tpc-usage-"), "nms-usage.jsonl"))

    def use(self, environment):
        self.environment = environment

    def reset_token(self):
        pass

    async def _answer(self, ident):
        result = self.by_ident.get(ident, [])
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return await result()
        return result

    async def baseline(self, form, ident):
        self.calls.append(ident)
        return await self._answer(ident)

    async def changes(self, since):
        self.calls.append("changes")
        out = []
        for ident in list(self.by_ident):
            out += await self._answer(ident) or []
        return out

    async def check_credentials(self):
        return {"ok": True, "environment": self.environment, "error": None}


def stub_faa(monkeypatch, by_ident):
    """Give the app's manager a stand-in for the FAA and a model endpoint that is
    down (live NOTAM views need the rewrite enabled). -> the stand-in; its
    `calls` lists the pulls made."""
    from app import main as main_mod
    from app.llm import ModelError

    async def no_model(*a, **kw):
        raise ModelError("no model in this test")

    stub = StubNms(by_ident)
    manager = main_mod.manager
    # the manager is shared by every test: start from no NOTAMs held and no pull made
    manager._notam_cache.clear()
    manager._notam_job, manager._delta_tried, manager._last_kind, manager._nms_hold = None, 0.0, None, 0.0
    monkeypatch.setattr(manager, "nms", stub)
    monkeypatch.setattr(main_mod.manager.llm, "complete", no_model)
    return stub


REWRITE = {"enabled": True, "base_url": "http://model.test/v1", "model": "stub", "description": "Stub 1B"}


def base_cfg():
    return {
        "airports": [{"icao": "ksea", "name": "Seattle", "lat": 47.45, "lon": -122.31, "local_radius_mi": 5, "enabled": True}],
        "regions": [{"name": "Puget Sound", "center_lat": 47.44, "center_lon": -122.27, "radius_mi": 30, "enabled": True}],
    }


def put(client, body, **kw):
    return client.put("/api/config", json=body, auth=AUTH, **kw)


def wait_until(cond, timeout: float) -> bool:
    """Poll `cond` every 100 ms until it holds or `timeout` seconds pass."""
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.1)
    return cond()
