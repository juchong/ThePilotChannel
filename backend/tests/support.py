"""Shared test setup. Imported by conftest.py before the app so the module-level
DataManager reads a throwaway config, a stub static dir, a temp tile cache,
and an admin password."""
import os
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="tpc-test-")
CFG = os.path.join(TMP, "config.yaml")
STATIC = os.path.join(TMP, "static")
os.makedirs(os.path.join(STATIC, "assets"))
with open(os.path.join(STATIC, "admin.html"), "w") as fh:
    fh.write("<html>admin</html>")
with open(os.path.join(STATIC, "index.html"), "w") as fh:
    fh.write("<html>display</html>")
with open(os.path.join(STATIC, "assets", "main-abc123.js"), "w") as fh:
    fh.write("// bundle")
os.environ["HANGAR_CONFIG"] = CFG
os.environ["HANGAR_STATIC"] = STATIC
os.environ["HANGAR_ADMIN_PASSWORD"] = "hunter2"
os.environ["HANGAR_TILE_CACHE"] = os.path.join(TMP, "tiles")
os.environ["HANGAR_GO2RTC"] = "http://127.0.0.1:9"  # a closed port: the relay is unreachable in tests
# no FAA credentials: the NOTAM source raises before it would reach the network
os.environ.pop("HANGAR_NMS_CLIENT_ID", None)
os.environ.pop("HANGAR_NMS_CLIENT_SECRET", None)

AUTH = ("admin", "hunter2")


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
