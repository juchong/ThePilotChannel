"""Stored credentials: the secrets API, where each credential is used, and the
rule that no stored secret ever leaves in a response, a header or a log line.
Environment and the `client` fixture come from support.py and conftest.py."""
import asyncio
import json
import logging
import os

import httpx
import pytest
import yaml
from support import AUTH, Bearer, base_cfg, put, sign_in, wait_until
from test_access import OPEN, ROUTES
from test_cameras import Relay, cams_cfg

from app import access, cameras, config, llm
from app import main as main_mod
from app import manager as mg
from app import notams
from app.access import ACCESS, SESSION_COOKIE
from app.store import STORE, SecretStore


def test_the_secrets_api_stores_and_clears_and_never_returns_a_value(client):
    assert client.get("/api/secrets", auth=AUTH).status_code == 403          # not even a settings key
    sign_in(client)
    empty = {"set": False, "changed": None}
    assert client.get("/api/secrets").json()["secrets"] == {
        "llm_api_key": empty, "aggregator_api_key": empty, "nms": {"production": empty, "staging": empty, "fit": empty}}
    r = client.put("/api/secrets/llm_api_key", json={"value": "  sk-pasted-with-spaces \n"})
    assert r.status_code == 200 and "sk-pasted" not in r.text
    assert STORE.credential("llm_api_key") == "sk-pasted-with-spaces" and llm.api_key() == "sk-pasted-with-spaces"
    r = client.put("/api/secrets/nms/staging", json={"client_id": " the-id ", "client_secret": "the-secret"})
    assert r.status_code == 200 and "the-secret" not in r.text and "the-id" not in r.text
    state = client.get("/api/secrets").json()["secrets"]
    assert state["llm_api_key"]["set"] is True and state["llm_api_key"]["changed"] > 0
    assert state["nms"]["staging"]["set"] is True and state["nms"]["staging"]["changed"] > 0
    assert state["nms"]["production"] == empty and state["nms"]["fit"] == empty
    assert notams.credentials("staging") == ("the-id", "the-secret") and notams.credentials("production") == ("", "")
    for bad in ({"client_id": "", "client_secret": "x"}, {"client_id": "x", "client_secret": "   "}, {"client_id": "x"}, {}):
        assert client.put("/api/secrets/nms/staging", json=bad).status_code == 422
    assert client.put("/api/secrets/nms/moon", json={"client_id": "x", "client_secret": "x"}).status_code == 422
    assert client.put("/api/secrets/nms", json={"value": "x"}).status_code == 404
    assert notams.credentials("staging") == ("the-id", "the-secret")
    assert client.put("/api/secrets/llm_api_key", json={"value": "   "}).status_code == 422
    assert client.put("/api/secrets/admin_password", json={"value": "x"}).status_code == 404
    assert client.delete("/api/secrets/session").status_code == 404
    assert client.delete("/api/secrets/nms").status_code == 404 and client.delete("/api/secrets/nms/moon").status_code == 422
    assert client.delete("/api/secrets/nms/staging").status_code == 200 and client.delete("/api/secrets/llm_api_key").status_code == 200
    assert client.get("/api/secrets").json()["secrets"]["nms"]["staging"] == empty and llm.api_key() == ""
    assert client.delete("/api/secrets/nms/staging").status_code == 200                      # nothing stored: nothing to do


def test_each_environment_keeps_its_own_faa_keys_and_each_pair_goes_only_to_its_environment(client):
    sign_in(client)
    pairs = {env: (f"{env}-id", f"{env}-secret") for env in ("production", "staging", "fit")}
    for env, (client_id, secret) in pairs.items():
        assert client.put(f"/api/secrets/nms/{env}", json={"client_id": client_id, "client_secret": secret}).status_code == 200
    assert {env: notams.credentials(env) for env in pairs} == pairs
    state = client.get("/api/secrets").json()["secrets"]["nms"]
    assert all(state[env]["set"] for env in pairs) and "-secret" not in json.dumps(state) and "-id" not in json.dumps(state)
    # replacing one pair and removing another leaves the third as it was
    assert client.put("/api/secrets/nms/production", json={"client_id": "new-id", "client_secret": "new-secret"}).status_code == 200
    assert client.delete("/api/secrets/nms/fit").status_code == 200
    assert notams.credentials("production") == ("new-id", "new-secret")
    assert notams.credentials("staging") == pairs["staging"] and notams.credentials("fit") == ("", "")
    stored = SecretStore(STORE.path)                               # and so it is on disk
    assert {env: stored.nms_pair(env) for env in pairs} == {"production": ("new-id", "new-secret"),
                                                            "staging": pairs["staging"], "fit": ("", "")}


def test_a_single_faa_pair_stored_by_an_earlier_version_is_its_environments_pair(tmp_path):
    path = tmp_path / "secrets.json"
    for env in ("production", "staging", "fit"):
        path.write_text(json.dumps({"version": 1, "credentials": {"nms": {
            "client_id": "old-id", "client_secret": "old-secret", "environment": env, "changed": 5.0}}}))
        store = SecretStore(str(path))
        assert store.nms_pair(env) == ("old-id", "old-secret")
        assert all(store.nms_pair(other) == ("", "") for other in ("production", "staging", "fit") if other != env)
        assert store.credential_state()["nms"][env] == {"set": True, "changed": 5.0}
        store.set_nms("new-id", "new-secret", "production" if env != "production" else "staging")
        again = SecretStore(str(path))
        assert again.nms_pair(env) == ("old-id", "old-secret") and len(again.data["credentials"]["nms"]) == 2
        assert "environment" not in json.dumps(again.data["credentials"]["nms"])
    for broken in ({"client_id": "id", "client_secret": "s", "environment": "moon"}, "a string",
                   {"moon": {"client_id": "id", "client_secret": "s"}}, {"staging": "not a pair"}):
        path.write_text(json.dumps({"version": 1, "credentials": {"nms": broken, "llm_api_key": {"value": "k"}}}))
        store = SecretStore(str(path))
        assert all(store.nms_pair(env) == ("", "") for env in ("production", "staging", "fit"))
        assert store.credential("llm_api_key") == "k" and store.error is None


def test_only_new_keys_for_the_environment_in_use_drop_its_token(client):
    sign_in(client)
    nms = main_mod.manager.nms
    assert nms.environment == "production"
    nms._token, nms._token_until = "token-of-the-old-keys", nms.gate.clock() + 1000
    for env in ("staging", "fit"):
        assert client.put(f"/api/secrets/nms/{env}", json={"client_id": "other", "client_secret": "other"}).status_code == 200
        assert client.delete(f"/api/secrets/nms/{env}").status_code == 200
    assert nms._token == "token-of-the-old-keys"
    assert client.put("/api/secrets/nms/production", json={"client_id": "new", "client_secret": "new"}).status_code == 200
    assert nms._token is None
    nms._token, nms._token_until = "again", nms.gate.clock() + 1000
    assert client.delete("/api/secrets/nms/production").status_code == 200
    assert nms._token is None

def test_the_aggregator_key_lives_in_the_store_and_not_in_the_config(client):
    sign_in(client)
    body = base_cfg()
    body["data_source"] = {"mode": "aggregator", "aggregator": "airplaneslive", "api_key": "s3cr3t"}
    r = put(client, body)
    assert r.status_code == 422 and r.json()["detail"] == [{"loc": ["data_source", "api_key"], "msg": "unknown field"}]
    del body["data_source"]["api_key"]
    assert put(client, body).status_code == 200
    assert client.put("/api/secrets/aggregator_api_key", json={"value": "s3cr3t"}).status_code == 200
    assert main_mod.manager._aggregator_source().api_key == "s3cr3t"
    assert "api_key" not in client.get("/api/config").json()["data_source"]
    assert "s3cr3t" not in client.get("/api/config").text and "s3cr3t" not in open(config.CONFIG_PATH).read()


def test_a_config_file_that_still_carries_the_aggregator_key_gives_it_to_the_store_and_loses_it(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({**base_cfg(), "data_source": {"mode": "aggregator", "api_key": "key-in-the-old-file"}}))
    store = SecretStore(str(tmp_path / "secrets.json"))
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(mg, "STORE", store)
    for name in ("WEATHER_STORE", "NOTAM_STORE"):
        monkeypatch.setattr(mg, name, str(tmp_path / name))
    monkeypatch.setattr(notams, "USAGE_PATH", str(tmp_path / "nms-usage.jsonl"))
    manager = mg.DataManager()
    try:
        assert store.credential("aggregator_api_key") == "key-in-the-old-file"
        assert manager.cfg.data_source.mode == "aggregator"
        assert "key-in-the-old-file" not in cfg.read_text() and "api_key" not in cfg.read_text()
        assert manager.config_version == config.config_version(str(cfg))
    finally:
        asyncio.run(manager.client.aclose())


def test_checking_the_faa_keys_is_for_a_session_and_is_not_repeated_at_once(client, monkeypatch):
    sign_in(client)
    monkeypatch.setitem(main_mod._credential_check, "at", 0.0)
    r = client.post("/api/secrets/nms/staging/check")
    assert r.status_code == 200 and r.json() == {
        "ok": False, "environment": "staging", "error": "no FAA credentials are stored for the staging environment"}
    r = client.post("/api/secrets/nms/production/check")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


# ---- nothing stored ever leaves ------------------------------------------------------------------

SECRETS = {
    "password": "Sentinel-Password-9d1f",
    "nms_id": "sentinel-nms-id-4c7a",
    "nms_secret": "sentinel-nms-secret-b2e9",
    "nms_staging_id": "sentinel-staging-id-61d0",
    "nms_staging_secret": "sentinel-staging-secret-e83b",
    "llm": "sk-sentinel-llm-key-7f30",
    "aggregator": "sentinel-aggregator-key-51cd",
    "stream_token": "SentinelStreamToken8a2f",
    "stream_userinfo": "sentineluser:sentinelpass",
}
STREAMS = [
    f"rtsps://192.168.1.1:7441/{SECRETS['stream_token']}",
    f"rtsp://{SECRETS['stream_userinfo']}@192.168.1.9:554/live?auth={SECRETS['stream_token']}2",
]
GET_PARAMS = {
    "/api/traffic": {"view": "local:KSEA"},
    "/api/weather/area": {"lat": 47.4, "lon": -122.3, "radius_nm": 20},
    "/api/weather/bbox": {"min_lat": 47, "min_lon": -123, "max_lat": 48, "max_lon": -122},
    "/api/notams": {"location": "KS50"},
    "/tiles/{z}/{x}/{y}.png": None, "/radar/{layer}/{z}/{x}/{y}.png": None,   # images
    "/api/stream": None,                                                       # an endless stream
    "/api/satellite": None,                                                    # asks NOAA
    "/docs/oauth2-redirect": None,
}


def leaks(text: str, extra=()):
    return [name for name, value in [*SECRETS.items(), *extra] if value in text]


def test_no_stored_secret_is_in_any_response_header_or_log_line(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    relay = Relay()
    relay.install(monkeypatch)
    relay.dead.add("192.168.1.9:554")
    assert put(client, cams_cfg()).status_code == 200

    # every slot of the store holds a value that would be recognised anywhere
    ACCESS.set_password("admin", SECRETS["password"])
    token = client.post("/api/auth/login", json={"username": "admin", "password": SECRETS["password"]}).cookies[SESSION_COOKIE]
    key = client.post("/api/keys", json={"name": "sentinel", "scope": "settings"}).json()["key"]
    found = [("session token", token), ("api key", key)]
    assert client.put("/api/secrets/nms/production", json={"client_id": SECRETS["nms_id"],
                                                           "client_secret": SECRETS["nms_secret"]}).status_code == 200
    assert client.put("/api/secrets/nms/staging", json={"client_id": SECRETS["nms_staging_id"],
                                                        "client_secret": SECRETS["nms_staging_secret"]}).status_code == 200
    assert client.put("/api/secrets/llm_api_key", json={"value": SECRETS["llm"]}).status_code == 200
    assert client.put("/api/secrets/aggregator_api_key", json={"value": SECRETS["aggregator"]}).status_code == 200
    made = [client.post("/api/cameras/streams", json={"name": f"Sentinel {n}", "url": url}) for n, url in enumerate(STREAMS)]
    assert [r.status_code for r in made] == [200, 200]
    assert wait_until(lambda: len(relay.streams) == 2, 3)          # the running sync loop gave them to the relay

    # the paths on which something goes wrong, where a value is most likely to be echoed
    assert client.post("/api/cameras/streams/test", json={"id": made[1].json()["id"]}).json()["ok"] is False
    assert client.post("/api/cameras/streams/test", json={"url": STREAMS[1] + "x"}).json()["ok"] is False
    assert client.post("/api/cameras/streams/test", json={"url": "exec:" + SECRETS["stream_token"]}).status_code == 422
    assert client.put(f"/api/cameras/streams/{made[0].json()['id']}", json={"url": STREAMS[0] + "#frag"}).status_code == 422
    relay.down = True
    asyncio.run(cameras.sync())
    cameras._status_cache["value"] = None
    monkeypatch.setitem(main_mod._credential_check, "at", 0.0)
    refuse = httpx.MockTransport(lambda request: httpx.Response(401, json={"ErrorCode": "invalid_client"}))
    monkeypatch.setattr(main_mod.manager.nms, "client", httpx.AsyncClient(transport=refuse))
    assert client.post("/api/secrets/nms/production/check").json()["ok"] is False
    monkeypatch.setitem(main_mod._credential_check, "at", 0.0)
    assert client.post("/api/secrets/nms/staging/check").json()["ok"] is False
    assert client.post("/api/auth/login", json={"username": "admin", "password": SECRETS["password"] + "x"}).status_code == 401
    assert client.post("/api/display/next", auth=Bearer(key + "x")).status_code == 401

    # every route that answers a GET, asked by the signed-in browser so that it answers in full
    asked = 0
    for (method, path), (level, _probe) in sorted(ROUTES.items()):
        if method != "GET" or GET_PARAMS.get(path, {}) is None:
            continue
        r = client.get(path, params=GET_PARAMS.get(path))
        assert r.status_code in (200, 404), (path, r.status_code)
        assert leaks(r.text, found) == [] and leaks(json.dumps(dict(r.headers)), found) == [], path
        asked += 1
    assert asked >= 20
    for r in made:
        assert leaks(r.text) == [] and r.json()["host"] in ("192.168.1.1:7441", "192.168.1.9:554")
    logged = "\n".join(record.getMessage() + str(record.exc_text or "") for record in caplog.records)
    assert logged and leaks(logged, found) == []
    # the values are where they belong: in the store, and nowhere else on disk
    stored = open(STORE.path).read()
    assert SECRETS["nms_secret"] in stored and SECRETS["nms_staging_secret"] in stored and SECRETS["stream_token"] in stored
    assert SECRETS["password"] not in stored and token not in stored and key not in stored      # only their hashes
    data_dir = os.path.dirname(config.CONFIG_PATH)
    for name in os.listdir(data_dir):
        path = os.path.join(data_dir, name)
        if os.path.isfile(path) and not name.startswith("secrets.json"):
            assert leaks(open(path, errors="replace").read(), found) == [], name


def test_every_secret_slot_of_the_store_is_covered_by_the_leak_check():
    """The check above plants a value in each place the store keeps one; a new
    kind of stored secret must be added to it."""
    assert set(STORE.data) == {"version", "account", "sessions", "api_keys", "credentials", "streams", "imported"}
    assert set(access.LEGACY_ENV[n][0] for n in range(4)) == {
        "HANGAR_ADMIN_PASSWORD", "HANGAR_NMS_CLIENT_ID", "HANGAR_NMS_CLIENT_SECRET", "HANGAR_LLM_API_KEY"}
    from app.store import NMS, SIMPLE_CREDENTIALS
    assert (NMS, *SIMPLE_CREDENTIALS) == ("nms", "llm_api_key", "aggregator_api_key")
