"""Shared test setup. Imported by conftest.py before the app so the module-level
DataManager reads a throwaway config, a stub static dir, a temp tile cache,
and an admin password."""
import os
import tempfile

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

AUTH = ("admin", "hunter2")


def base_cfg():
    return {
        "airports": [{"icao": "ksea", "name": "Seattle", "lat": 47.45, "lon": -122.31, "local_radius_mi": 5, "enabled": True}],
        "regions": [{"name": "Puget Sound", "center_lat": 47.44, "center_lon": -122.27, "radius_mi": 30, "enabled": True}],
    }


def put(client, body, **kw):
    return client.put("/api/config", json=body, auth=AUTH, **kw)
