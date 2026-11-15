"""Access: every route's access class, the admin account, sessions, the sign-in
throttle, API keys, the secrets store's durability, the command-line tool, and
the one-time import of what used to live in the environment. Environment and
the `client` fixture come from support.py and conftest.py; the app starts with
the account imported from HANGAR_ADMIN_PASSWORD (admin / hunter2)."""
import io
import json
import logging
import os
import stat
import time

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount, WebSocketRoute
from support import AUTH, CONTROL, USER, Bearer, FakeClock, base_cfg, sign_in

from app import access
from app import main as main_mod
from app.access import ACCESS, SESSION_COOKIE, Access, AccessError, LoginThrottle
from app.store import STORE, SecretStore, StoreLocked

OPEN, CONTROL_CLASS, SETTINGS, SESSION = "open", "control", "settings", "session"

# Every route of the app, with its access class and, for a protected route, a
# request that passes the access check and then changes nothing (it is refused
# for its body, names nothing that exists, or only reads).
ROUTES = {
    ("GET", "/healthz"): (OPEN, None),
    ("GET", "/admin"): (OPEN, None),              # a page: it sends a browser without a session to /login
    ("GET", "/login"): (OPEN, None),
    ("GET", "/api/config"): (OPEN, None),
    ("PUT", "/api/config"): (SETTINGS, {"json": {"no_such_section": 1}}),
    ("GET", "/api/views"): (OPEN, None),
    ("GET", "/api/traffic"): (OPEN, None),
    ("GET", "/api/weather"): (OPEN, None),
    ("GET", "/api/weather/area"): (OPEN, None),
    ("GET", "/api/weather/bbox"): (OPEN, None),
    ("GET", "/api/satellite"): (OPEN, None),
    ("GET", "/api/notams"): (OPEN, None),
    ("POST", "/api/notams/check"): (SETTINGS, {"json": {}}),
    ("POST", "/api/notams/model-test"): (SETTINGS, {"json": {}}),
    ("GET", "/api/notams/usage"): (SETTINGS, {}),
    ("POST", "/api/secrets/nms/check"): (SESSION, {}),
    ("GET", "/api/status"): (OPEN, None),
    ("WS", "/api/cameras/ws"): (OPEN, None),
    ("GET", "/api/cameras/streams"): (SESSION, {}),
    ("POST", "/api/cameras/streams"): (SESSION, {"json": {}}),
    ("PUT", "/api/cameras/streams/{stream_id}"): (SESSION, {"url": "/api/cameras/streams/cam99", "json": {}}),
    ("DELETE", "/api/cameras/streams/{stream_id}"): (SESSION, {"url": "/api/cameras/streams/cam99"}),
    ("POST", "/api/cameras/streams/test"): (SESSION, {"json": {}}),
    ("GET", "/tiles/{z}/{x}/{y}.png"): (OPEN, None),
    ("GET", "/radar/{layer}/{z}/{x}/{y}.png"): (OPEN, None),
    ("POST", "/api/test-source"): (SETTINGS, {"json": {"mode": "no-such-mode"}}),
    ("GET", "/api/display"): (OPEN, None),
    ("POST", "/api/display/blackout"): (CONTROL_CLASS, {"json": {"seconds": -1}}),
    ("POST", "/api/display/restore"): (CONTROL_CLASS, {}),
    ("POST", "/api/display/next"): (CONTROL_CLASS, {}),
    ("POST", "/api/display/previous"): (CONTROL_CLASS, {}),
    ("POST", "/api/display/view"): (CONTROL_CLASS, {"json": {"view": "no-such-view"}}),
    ("POST", "/api/display/showing"): (OPEN, None),
    ("GET", "/api/docs/display"): (SESSION, {}),
    ("GET", "/api/stream"): (OPEN, None),
    ("GET", "/api/auth/session"): (OPEN, None),
    ("POST", "/api/auth/setup"): (OPEN, None),
    ("POST", "/api/auth/login"): (OPEN, None),
    ("POST", "/api/auth/logout"): (SESSION, None),          # ends the session it is sent with: tested on its own
    ("POST", "/api/auth/logout-all"): (SESSION, None),
    ("PUT", "/api/auth/account"): (SESSION, {"json": {}}),
    ("GET", "/api/keys"): (SESSION, {}),
    ("POST", "/api/keys"): (SESSION, {"json": {"scope": "no-such-scope"}}),
    ("DELETE", "/api/keys/{key_id}"): (SESSION, {"url": "/api/keys/no-such-key"}),
    ("GET", "/api/secrets"): (SESSION, {}),
    ("PUT", "/api/secrets/nms"): (SESSION, {"json": {}}),
    ("PUT", "/api/secrets/{name}"): (SESSION, {"url": "/api/secrets/no-such-secret", "json": {"value": "x"}}),
    ("DELETE", "/api/secrets/{name}"): (SESSION, {"url": "/api/secrets/no-such-secret"}),
    # FastAPI's own description of the API
    ("GET", "/openapi.json"): (OPEN, None),
    ("GET", "/docs"): (OPEN, None),
    ("GET", "/docs/oauth2-redirect"): (OPEN, None),
    ("GET", "/redoc"): (OPEN, None),
}
PROTECTED = {key: value for key, value in ROUTES.items() if value[0] != OPEN and value[1] is not None}


def all_routes(routes=None):
    """Every route of the app, those of an included router too."""
    for route in main_mod.app.routes if routes is None else routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from all_routes(included.routes)
        else:
            yield route


def app_routes():
    found = set()
    for route in all_routes():
        if isinstance(route, WebSocketRoute):
            found.add(("WS", route.path))
        elif isinstance(route, Mount):
            continue                      # the built frontend
        else:
            found |= {(method, route.path) for method in (route.methods or ()) if method not in ("HEAD", "OPTIONS")}
    return found


def declared_class(method, path):
    """The access class the app declares for a route: that of the access
    dependency it carries, "open" when it carries none."""
    for route in all_routes():
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            found = [d.call.access_class for d in route.dependant.dependencies if hasattr(d.call, "access_class")]
            assert len(found) <= 1, (method, path, found)
            return found[0] if found else OPEN
    return OPEN


def probe(client, key, **kw):
    method, path = key
    spec = dict(ROUTES[key][1])
    return client.request(method, spec.pop("url", path), **spec, **kw)


def test_every_route_is_in_exactly_one_access_class():
    assert app_routes() == set(ROUTES)        # a new route must be classified here before it ships
    for (method, path), (expected, _probe) in ROUTES.items():
        if method != "WS":
            assert declared_class(method, path) == expected, (method, path)


@pytest.mark.parametrize("key", sorted(PROTECTED), ids=lambda k: " ".join(k))
def test_a_protected_route_refuses_everyone_its_class_does_not_admit(client, key, monkeypatch):
    level = ROUTES[key][0]
    allowed = lambda r: r.status_code not in (401, 403, 503)   # noqa: E731  (it got past the access check)
    assert probe(client, key).status_code == 401                                   # no credentials
    assert probe(client, key, auth=Bearer("tpc_not-a-key")).status_code == 401     # an unknown key
    assert probe(client, key, auth=("admin", "hunter2")).status_code == 401        # the password itself, HTTP Basic
    revoked = ACCESS.create_key("to be revoked", "settings")
    ACCESS.revoke_key(revoked["id"])
    assert probe(client, key, auth=Bearer(revoked["key"])).status_code == 401      # a revoked key
    r = probe(client, key, auth=CONTROL)
    assert allowed(r) if level == CONTROL_CLASS else r.status_code == 403, r.text  # a display-control key
    r = probe(client, key, auth=AUTH)
    assert allowed(r) if level in (CONTROL_CLASS, SETTINGS) else r.status_code == 403, r.text   # a settings key
    sign_in(client)
    assert allowed(probe(client, key)), probe(client, key).text                    # a signed-in browser: every class
    if key[0] != "GET":
        r = probe(client, key, headers={"Origin": "http://evil.example"})          # another site's page, same cookie
        assert r.status_code == 403 and "cross-origin" in r.text
        assert allowed(probe(client, key, headers={"Origin": "http://testserver"}))
    monkeypatch.setattr(ACCESS, "clock", lambda: time.time() + access.SESSION_TTL_S + 60)
    assert probe(client, key).status_code == 401                                   # the session has expired


def test_the_display_reads_everything_it_shows_without_credentials(client):
    assert client.put("/api/config", json=base_cfg(), auth=AUTH).status_code == 200
    for path in ("/", "/healthz", "/api/config", "/api/views", "/api/status", "/api/display", "/api/weather",
                 "/api/traffic?view=local:KSEA", "/api/weather/area?lat=47.4&lon=-122.3&radius_nm=20",
                 "/api/weather/bbox?min_lat=47&min_lon=-123&max_lat=48&max_lon=-122", "/tiles/8/41/89.png",
                 "/api/auth/session"):
        assert client.get(path).status_code == 200, path
    assert client.post("/api/display/showing", json={"view": "local:KSEA", "index": 0}).status_code == 200


# ---- passwords -----------------------------------------------------------------------------

def rfc7914():
    with open(os.path.join(os.path.dirname(__file__), "data", "rfc7914_scrypt.json")) as fh:
        return json.load(fh)["vectors"]


@pytest.mark.parametrize("vector", [v for v in rfc7914() if v["n"] <= 16384], ids=lambda v: f"N={v['n']} r={v['r']} p={v['p']}")
def test_password_hashing_is_scrypt_as_rfc_7914_defines_it(vector):
    # the vector with N=1048576 needs a gigabyte of memory and is left out
    got = access.scrypt(vector["password"].encode(), vector["salt"].encode(), vector["n"], vector["r"], vector["p"], vector["dklen"])
    assert got.hex() == vector["hex"]


def test_a_password_is_stored_as_a_salted_hash_and_only_the_right_one_verifies():
    one, two = access.hash_password("correct horse"), access.hash_password("correct horse")
    assert one["algo"] == "scrypt" and (one["n"], one["r"], one["p"]) == (2 ** 15, 8, 1)
    assert one["salt"] != two["salt"] and one["hash"] != two["hash"] and "correct horse" not in json.dumps(one)
    assert access.verify_password("correct horse", one) and access.verify_password("correct horse", two)
    for wrong in ("correct horsf", "Correct horse", "", "correct horse "):
        assert not access.verify_password(wrong, one)
    for broken in (None, {}, {**one, "hash": "not base64!"}, {**one, "n": "many"}):
        assert not access.verify_password("correct horse", broken)


# ---- signing in ------------------------------------------------------------------------------

def test_the_admin_page_is_served_to_a_session_only_and_never_as_a_static_file(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/admin", auth=AUTH, follow_redirects=False).status_code == 303      # a key is not a browser session
    assert client.get("/admin.html").status_code == 404
    assert client.get("/login").text == "<html>login</html>"
    assert client.get("/api/auth/session").json() == {
        "authenticated": False, "username": None, "setup_required": False, "locked": False, "detail": None}
    assert client.post("/api/auth/login", json={"username": "admin", "password": "hunter3"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "root", "password": "hunter2"}).status_code == 401
    r = sign_in(client)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=") and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert f"Max-Age={access.SESSION_TTL_S}" in cookie and "Path=/" in cookie and "hunter2" not in cookie
    assert client.get("/admin").text == "<html>admin</html>"
    assert client.get("/api/auth/session").json()["username"] == "admin"
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/admin", follow_redirects=False).status_code == 303
    assert client.get("/api/keys").status_code == 401


def test_a_session_survives_a_restart_and_is_stored_as_a_hash(client):
    token = sign_in(client).cookies[SESSION_COOKIE]
    saved = open(STORE.path).read()
    assert token not in saved and "hunter2" not in saved
    again = Access(SecretStore(STORE.path))          # the app starts again and reads the file
    assert again.session(token) == "admin" and again.session(token + "x") is None


def test_the_first_visitor_creates_the_account_and_nobody_creates_a_second(client, monkeypatch):
    monkeypatch.setitem(STORE.data, "account", None)
    monkeypatch.setitem(STORE.data, "sessions", [])
    assert client.get("/api/auth/session").json()["setup_required"] is True
    assert client.get("/api/keys", auth=AUTH).status_code == 403        # a key still cannot reach session routes
    for bad in ({"username": "bad name!", "password": "long enough"}, {"username": "pilot", "password": "short"}):
        assert client.post("/api/auth/setup", json=bad).status_code == 422
    assert client.post("/api/auth/setup", json={"username": "pilot", "password": "long enough"},
                       headers={"Origin": "http://evil.example"}).status_code == 403
    r = client.post("/api/auth/setup", json={"username": "pilot", "password": "long enough"})
    assert r.status_code == 200 and SESSION_COOKIE in r.cookies
    assert client.get("/api/auth/session").json() == {
        "authenticated": True, "username": "pilot", "setup_required": False, "locked": False, "detail": None}
    assert client.post("/api/auth/setup", json={"username": "other", "password": "long enough"}).status_code == 409
    assert STORE.data["account"]["username"] == "pilot"


def test_changing_the_password_needs_the_current_one_and_ends_every_other_session(client):
    from fastapi.testclient import TestClient
    other = TestClient(main_mod.app)                 # a second browser, signed in too
    sign_in(other)
    sign_in(client)
    try:
        change = {"current_password": "wrong", "new_password": "a new password"}
        assert client.put("/api/auth/account", json=change).status_code == 403
        assert client.put("/api/auth/account", json={"current_password": "hunter2", "new_password": "short"}).status_code == 422
        r = client.put("/api/auth/account", json={"current_password": "hunter2", "new_password": "a new password", "username": "juan"})
        assert r.status_code == 200 and r.json()["username"] == "juan"
        assert client.get("/api/auth/session").json()["username"] == "juan"       # this browser stays signed in
        assert other.get("/api/keys").status_code == 401                          # the other one does not
        assert other.post("/api/auth/login", json=USER).status_code == 401
        assert other.post("/api/auth/login", json={"username": "juan", "password": "a new password"}).status_code == 200
        assert client.post("/api/auth/logout-all").status_code == 200
        assert other.get("/api/keys").status_code == 401 and client.get("/api/keys").status_code == 401
    finally:
        ACCESS.set_password("admin", "hunter2")      # the account the other tests sign in with (min length waived below)
        ACCESS.throttle.success("testclient")


@pytest.fixture(autouse=True)
def _short_test_password(monkeypatch):
    """The suite's account uses the password the retired variable carried."""
    monkeypatch.setattr(access, "PASSWORD_MIN", 7)


# ---- the sign-in throttle -------------------------------------------------------------------

def test_failed_sign_ins_lock_further_attempts_for_a_growing_time():
    clock = FakeClock()
    throttle = LoginThrottle(clock)
    for _ in range(LoginThrottle.FREE - 1):
        throttle.failure("10.0.0.5")
        assert throttle.retry_after("10.0.0.5") == 0
    waits = []
    for _ in range(12):
        throttle.failure("10.0.0.5")
        waits.append(throttle.retry_after("10.0.0.5"))
    assert waits[0] == LoginThrottle.BASE_S and waits == sorted(waits) and waits[-1] == LoginThrottle.MAX_S
    assert throttle.retry_after("10.0.0.6") == 0             # another client is not locked by this one
    clock.now += LoginThrottle.MAX_S
    assert throttle.retry_after("10.0.0.5") == 0
    throttle.success("10.0.0.5")
    throttle.failure("10.0.0.5")
    assert throttle.retry_after("10.0.0.5") == 0             # a success starts the count again


def test_many_clients_failing_lock_sign_in_for_everyone_and_the_table_stays_bounded():
    clock = FakeClock()
    throttle = LoginThrottle(clock)
    for n in range(LoginThrottle.GLOBAL_FREE - 1):
        throttle.failure(f"10.0.{n}.1")
    assert throttle.retry_after("10.9.9.9") == 0
    throttle.failure("10.0.99.1")
    assert throttle.retry_after("10.9.9.9") == LoginThrottle.BASE_S
    for n in range(3 * LoginThrottle.MAX_CLIENTS):
        throttle.failure(f"c{n}")
    assert len(throttle._clients) <= LoginThrottle.MAX_CLIENTS
    clock.now += LoginThrottle.FORGET_S + LoginThrottle.MAX_S + 1
    assert throttle.retry_after("10.9.9.9") == 0             # old failures are forgotten


def test_a_locked_out_client_is_refused_even_with_the_right_password(client, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(ACCESS, "throttle", LoginThrottle(clock))
    for _ in range(LoginThrottle.FREE):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "guess"}).status_code == 401
    r = client.post("/api/auth/login", json=USER)
    assert r.status_code == 429 and int(r.headers["retry-after"]) == LoginThrottle.BASE_S
    clock.now += LoginThrottle.BASE_S
    assert client.post("/api/auth/login", json=USER).status_code == 200


# ---- API keys --------------------------------------------------------------------------------

def test_a_key_is_shown_once_stored_as_a_hash_and_dead_when_revoked(client):
    sign_in(client)
    r = client.post("/api/keys", json={"name": "  Home   Assistant ", "scope": "control"})
    assert r.status_code == 200
    made = r.json()
    key = made["key"]
    assert key.startswith("tpc_") and len(key) >= 40 and made["name"] == "Home Assistant" and made["prefix"] == key[:8]
    listed = client.get("/api/keys")
    mine = next(k for k in listed.json()["keys"] if k["id"] == made["id"])
    assert mine == {"id": made["id"], "name": "Home Assistant", "prefix": key[:8], "scope": "control",
                    "created": made["created"], "last_used": None}
    assert key not in listed.text and key not in open(STORE.path).read()
    assert client.post("/api/display/next", auth=Bearer(key)).status_code == 200
    assert next(k for k in client.get("/api/keys").json()["keys"] if k["id"] == made["id"])["last_used"] is not None
    assert client.put("/api/config", json=base_cfg(), auth=Bearer(key)).status_code == 403
    assert client.delete(f"/api/keys/{made['id']}").status_code == 200
    assert client.post("/api/display/next", auth=Bearer(key)).status_code == 401
    assert client.delete(f"/api/keys/{made['id']}").status_code == 404
    for bad in ({"name": "", "scope": "control"}, {"name": "x" * 61, "scope": "control"}):
        assert client.post("/api/keys", json=bad).status_code == 422


def test_keys_are_limited_in_number(tmp_path):
    acc = Access(SecretStore(str(tmp_path / "secrets.json")))
    keys = [acc.create_key(f"key {n}", "control") for n in range(access.MAX_KEYS)]
    assert len({k["key"] for k in keys}) == access.MAX_KEYS
    with pytest.raises(AccessError) as err:
        acc.create_key("one too many", "control")
    assert err.value.status == 409


# ---- the store ----------------------------------------------------------------------------------

def test_the_store_is_private_written_whole_and_keeps_one_backup(tmp_path):
    path = str(tmp_path / "secrets.json")
    store = SecretStore(path)
    assert not store.exists and store.error is None and not os.path.exists(path)     # reading creates nothing
    store.set_credential("llm_api_key", "first")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and not os.path.exists(path + ".tmp")
    store.set_credential("llm_api_key", "second")
    assert stat.S_IMODE(os.stat(path + ".bak").st_mode) == 0o600
    assert json.load(open(path + ".bak"))["credentials"]["llm_api_key"]["value"] == "first"
    assert SecretStore(path).credential("llm_api_key") == "second"


def test_an_unreadable_store_falls_back_to_its_backup_and_otherwise_locks(tmp_path, caplog):
    path = str(tmp_path / "secrets.json")
    store = SecretStore(path)
    store.set_credential("llm_api_key", "first")
    store.set_credential("llm_api_key", "second")
    open(path, "w").write("{ not json")
    fallen = SecretStore(path)
    assert fallen.error is None and fallen.credential("llm_api_key") == "first"       # the backup
    os.remove(path)                                   # a crash between the two renames leaves only the backup
    assert SecretStore(path).credential("llm_api_key") == "first"
    open(path, "w").write("[1, 2]")
    open(path + ".bak", "w").write("")
    locked = SecretStore(path)
    assert locked.error and not locked.exists
    with pytest.raises(StoreLocked):
        locked.set_credential("llm_api_key", "third")
    assert open(path).read() == "[1, 2]"              # the unreadable file is left for repair
    acc = Access(locked)
    assert acc.state() == {"locked": True, "account": False, "username": None}
    with pytest.raises(StoreLocked):
        acc.create_account("admin", "long enough")    # a locked store is never "no account yet"
    os.remove(path)
    os.remove(path + ".bak")
    assert acc.state() == {"locked": False, "account": False, "username": None}       # removed: a fresh start


def test_a_locked_store_closes_the_admin_instead_of_opening_it(client, monkeypatch):
    sign_in(client)
    monkeypatch.setattr(STORE, "error", "data/secrets.json: Expecting value")
    assert client.get("/api/keys").status_code == 503 and "set-password" in client.get("/api/keys").json()["detail"]
    assert client.post("/api/display/next", auth=AUTH).status_code == 503
    assert client.get("/admin", follow_redirects=False).status_code == 303
    state = client.get("/api/auth/session").json()
    assert state["locked"] is True and state["setup_required"] is False and state["authenticated"] is False
    assert client.post("/api/auth/setup", json={"username": "pilot", "password": "long enough"}).status_code == 503
    assert client.post("/api/auth/login", json=USER).status_code == 503
    assert client.get("/api/status").status_code == 200           # the display is not affected


def test_a_change_made_outside_the_app_is_seen_at_once(tmp_path):
    path = str(tmp_path / "secrets.json")
    running = Access(SecretStore(path))
    running.create_account("admin", "first password")
    token = running.login("admin", "first password", "10.0.0.5")
    Access(SecretStore(path)).set_password(None, "second password")          # the command-line tool, another process
    assert running.session(token) is None                                     # every session ended
    with pytest.raises(AccessError):
        running.login("admin", "first password", "10.0.0.5")
    assert running.session(running.login("admin", "second password", "10.0.0.5")) == "admin"


def test_the_command_line_tool_sets_a_password_and_reports_the_account(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(access, "ACCESS", Access(SecretStore(str(tmp_path / "secrets.json"))))
    assert access.main(["status"]) == 1 and "no account" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert access.main(["set-password"]) == 1                       # refused: too short
    monkeypatch.setattr("sys.stdin", io.StringIO("a good password\n"))
    assert access.main(["set-password", "--username", "juan"]) == 0
    out = capsys.readouterr().out
    assert "juan" in out and "a good password" not in out
    assert access.main(["status"]) == 0 and "account: juan" in capsys.readouterr().out
    assert access.ACCESS.session(access.ACCESS.login("juan", "a good password", "cli")) == "juan"
    assert access.main([]) == 2 and access.main(["set-password", "--bogus"]) == 2


# ---- the one-time import of what the environment used to carry -------------------------------------

CAM_URL = "rtsps://192.168.1.1:7441/AbCdEfGhIjKlMnOp"
LEGACY = {
    "HANGAR_ADMIN_PASSWORD": "pw-from-env",
    "HANGAR_NMS_CLIENT_ID": "nms-id-from-env",
    "HANGAR_NMS_CLIENT_SECRET": "nms-secret-from-env",
    "HANGAR_LLM_API_KEY": "llm-key-from-env",
    **{f"HANGAR_CAM{n}": f"{CAM_URL}{n}?enableSrtp" for n in range(1, 13)},
}
RAW_CONFIG = {
    "data_source": {"mode": "aggregator", "api_key": "aggregator-key-from-config"},
    "cameras": {"tiles": [{"name": "Gate", "stream": "cam1"}, {"name": "", "stream": "cam2"}]},
}


def fresh(tmp_path):
    return SecretStore(str(tmp_path / "secrets.json"))


def test_every_retired_variable_is_imported_into_its_place(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="hangar")
    store = fresh(tmp_path)
    done = access.import_legacy(store, LEGACY, RAW_CONFIG)
    assert sorted(done) == sorted([*LEGACY, access.CONFIG_API_KEY]) and len(done) == 17
    assert store.data["account"]["username"] == "admin"
    assert access.verify_password("pw-from-env", store.data["account"]["password"])
    assert store.nms_pair("production") == ("nms-id-from-env", "nms-secret-from-env")
    assert store.credential("llm_api_key") == "llm-key-from-env"
    assert store.credential("aggregator_api_key") == "aggregator-key-from-config"
    streams = store.streams()
    assert [s["id"] for s in streams] == [f"cam{n}" for n in range(1, 13)]              # in number order, not cam1, cam10, ...
    assert [s["url"] for s in streams] == [f"{CAM_URL}{n}" for n in range(1, 13)]       # Protect's enableSrtp ending is gone
    assert [s["name"] for s in streams[:3]] == ["Gate", "Camera 2", "Camera 3"]         # a tile's caption names its stream
    assert sorted(store.data["imported"]) == sorted(done)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert all(name in logged for name in LEGACY)
    assert not any(value in logged for value in (*LEGACY.values(), "aggregator-key-from-config", "AbCdEf"))


@pytest.mark.parametrize("name", [*LEGACY, access.CONFIG_API_KEY])
def test_a_value_imported_once_is_never_imported_again(tmp_path, name):
    store = fresh(tmp_path)
    access.import_legacy(store, LEGACY, RAW_CONFIG)
    # the owner clears or replaces what was imported
    if name == "HANGAR_ADMIN_PASSWORD":
        Access(store).set_password("admin", "changed in the app")
    elif name.startswith("HANGAR_NMS"):
        store.clear_credential("nms")
    elif name == "HANGAR_LLM_API_KEY":
        store.clear_credential("llm_api_key")
    elif name == access.CONFIG_API_KEY:
        store.clear_credential("aggregator_api_key")
    else:
        store.remove_stream(name.replace("HANGAR_CAM", "cam"))
    before = json.dumps(store.data, sort_keys=True)
    changed = {**LEGACY, name: "another-value" if "CAM" not in name else "rtsps://10.9.9.9:7441/other"}
    raw = {**RAW_CONFIG, "data_source": {"api_key": "another-aggregator-key"}}
    for environ in (LEGACY, changed):                 # a restart with the old value, then with a changed one
        assert access.import_legacy(SecretStore(store.path), environ, raw) == []
    assert json.dumps(SecretStore(store.path).data, sort_keys=True) == before
    assert not access.verify_password("another-value", SecretStore(store.path).data["account"]["password"])


def test_values_already_stored_are_not_replaced_and_a_bad_camera_address_is_not_recorded(tmp_path, caplog):
    store = fresh(tmp_path)
    Access(store).create_account("juan", "set in the app")
    store.set_credential("llm_api_key", "set in the app")
    store.add_stream("Mine", "rtsps://10.0.0.9:7441/mine", "cam2")
    environ = {**LEGACY, "HANGAR_CAM3": "exec:rm -rf /", "HANGAR_CAM4": "http://192.168.1.1/video"}
    done = access.import_legacy(store, environ, {})
    assert "HANGAR_ADMIN_PASSWORD" not in done and "HANGAR_LLM_API_KEY" not in done
    assert not {"HANGAR_CAM2", "HANGAR_CAM3", "HANGAR_CAM4"} & set(done)
    assert store.data["account"]["username"] == "juan" and store.credential("llm_api_key") == "set in the app"
    assert store.stream("cam2")["url"] == "rtsps://10.0.0.9:7441/mine"
    assert store.stream("cam3") is None and store.stream("cam4") is None
    assert "rm -rf" not in caplog.text and "192.168.1.1/video" not in caplog.text
    # corrected in the environment, the address is imported at the next start
    assert access.import_legacy(store, {**environ, "HANGAR_CAM3": CAM_URL}, {}) == ["HANGAR_CAM3"]


def test_the_faa_keys_need_both_halves_and_take_their_environment_from_the_retired_host_setting(tmp_path):
    for n, (base, environment) in enumerate([
        (None, "production"), ("https://api-nms.aim.faa.gov", "production"),
        ("https://api-staging.cgifederal-aim.com/", "staging"), ("https://api-fit.cgifederal-aim.com", "fit"),
        ("https://example.net", "production"),
    ]):
        store = SecretStore(str(tmp_path / f"s{n}.json"))
        environ = {"HANGAR_NMS_CLIENT_ID": "id", "HANGAR_NMS_CLIENT_SECRET": "secret"}
        if base:
            environ["HANGAR_NMS_BASE"] = base
        assert access.import_legacy(store, {"HANGAR_NMS_CLIENT_ID": "id"}, {}) == []      # half a pair is nothing
        access.import_legacy(store, environ, {})
        assert store.credential_state()["nms"]["environment"] == environment
        assert store.nms_pair(environment) == ("id", "secret")
        assert all(store.nms_pair(other) == ("", "") for other in ("production", "staging", "fit") if other != environment)


def test_the_older_password_variable_is_imported_and_leftovers_are_named(tmp_path):
    store = fresh(tmp_path)
    assert access.import_legacy(store, {"ADMIN_PASSWORD": "older-name"}, {}) == ["HANGAR_ADMIN_PASSWORD"]
    assert access.verify_password("older-name", store.data["account"]["password"])
    assert access.legacy_leftovers({}) == []
    assert access.legacy_leftovers({"HANGAR_CAM7": "x", "HANGAR_LLM_API_KEY": "", "ADMIN_PASSWORD": "y", "PATH": "/bin"}) == [
        "HANGAR_ADMIN_PASSWORD", "HANGAR_CAM7"]


def test_the_app_started_with_the_account_from_the_retired_password_variable(client):
    assert "HANGAR_ADMIN_PASSWORD" in STORE.data["imported"]
    sign_in(client)
    listing = client.get("/api/secrets").json()
    assert listing["imported"] == ["HANGAR_ADMIN_PASSWORD"] and listing["leftovers"] == ["HANGAR_ADMIN_PASSWORD"]
