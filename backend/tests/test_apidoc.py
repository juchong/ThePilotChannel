"""The display API reference (app/data/display_api.md) against the API it describes:
it documents every display-control route and only the reads a client needs, its
examples and field tables have the fields the API answers with, its errors and
limits are the ones the API applies, and the copy made for a display carries that
display's address and views."""
import asyncio
import json
import re
import time

import pytest
from support import AUTH, CONTROL, Bearer, base_cfg, sign_in
from test_access import CONTROL_CLASS, OPEN, SESSION, ROUTES, app_routes, declared_class

from app import apidoc, conditions, nws_gateway
from app import main as main_mod
from app.access import LOCKED_DETAIL
from app.config import Config
from app.manager import MAX_SUBSCRIBERS, TooManySubscribers
from app.store import STORE
from app.views import build_views

DOC = apidoc.TEMPLATE
FLAT = " ".join(DOC.split())          # the text with its line breaks undone
HEADING_RE = re.compile(r"^### (GET|POST) (/\S+)$", re.M)
READS = {("GET", "/api/display"), ("GET", "/api/views"), ("GET", "/api/stream")}
MOVES = ("/api/display/next", "/api/display/previous", "/api/display/view", "/api/display/blackout", "/api/display/restore")


def documented():
    return set(HEADING_RE.findall(DOC))


def section(method, path):
    """The text under a route's heading, up to the next heading."""
    start = DOC.index(f"### {method} {path}\n")
    following = re.compile(r"^#{2,3} ", re.M).search(DOC, start + 4)
    return DOC[start:following.start() if following else len(DOC)]


def json_example(method, path):
    return json.loads(re.search(r"```json\n(.*?)\n```", section(method, path), re.S).group(1))


def table_fields(text):
    """The field names in the first column of a table's rows: "`nav.ts`, `nav.age_s`"."""
    names = []
    for row in re.findall(r"^\| (.+?) \|", text, re.M):
        names += re.findall(r"`([^`]+)`", row)
    return set(names)


def paths(value, prefix=""):
    """Every field of an answer as a dotted path."""
    out = set()
    for key, inner in value.items():
        out.add(prefix + key)
        if isinstance(inner, dict):
            out |= paths(inner, prefix + key + ".")
    return out


def number(pattern):
    found = re.findall(pattern, FLAT)
    assert len(found) == 1, (pattern, found)
    return float(found[0])


@pytest.fixture()
def configured(client):
    assert client.put("/api/config", json=base_cfg(), auth=AUTH).status_code == 200
    assert client.post("/api/display/showing", json={"view": "local:KSEA", "index": 0}).status_code == 200
    yield client
    client.post("/api/display/restore", auth=CONTROL)


# ---- what it documents ------------------------------------------------------------------

def test_it_documents_every_display_control_route_and_only_the_reads_a_client_needs():
    control = {key for key in app_routes() if declared_class(*key) == CONTROL_CLASS}
    assert control and control <= documented()
    assert documented() - control == READS
    for method, path in documented():
        text = section(method, path)
        assert ("Key needed" in text) != ("No key" in text), (method, path)
        assert declared_class(method, path) == (CONTROL_CLASS if "Key needed" in text else OPEN), (method, path)


def test_the_route_that_serves_it_is_for_a_signed_in_browser():
    assert ROUTES[("GET", "/api/docs/display")][0] == SESSION
    assert declared_class("GET", "/api/docs/display") == SESSION


def test_the_weather_example_and_table_have_exactly_the_fields_of_the_answer_and_its_values(configured):
    real = configured.get("/api/conditions", auth=CONTROL).json()
    example = json_example("GET", "/api/conditions")
    assert list(example) == list(real) == ["monitored", "airports"] and example["monitored"] == list(example["airports"])
    fields = [list(a) for a in list(real["airports"].values()) + list(example["airports"].values())]
    assert real["airports"] and all(f == fields[0] for f in fields)
    assert table_fields(section("GET", "/api/conditions")) == {"monitored", "airports"} | set(fields[0])
    # the example is what the display gives for its report, and for an airport without one
    bfi = example["airports"]["KBFI"]
    rec, obs = nws_gateway.parse_station_file(f"2026/11/16 05:53\n{bfi['raw']}\n", "KBFI")
    assert obs == bfi["observed"]
    record = nws_gateway.to_metar(rec, obs, "", 0.0, 0.0, 4500)
    assert conditions.readout(record, bfi["name"], obs + bfi["age_s"], 4500) == bfi
    assert conditions.readout(None, "Auburn", 0, 4500) == example["airports"]["KS50"]
    r = configured.get("/api/conditions")
    assert r.status_code == 401 and r.json()["detail"] == "sign in, or send an API key"


def test_the_display_state_example_and_table_have_exactly_the_fields_of_the_answer(configured):
    configured.post("/api/display/view", json={"view": "region:Puget Sound"}, auth=CONTROL)
    real = configured.get("/api/display").json()
    assert paths(json_example("GET", "/api/display")) == paths(real)
    leaves = {p for p in paths(real) if p not in ("nav", "showing")}
    assert table_fields(section("GET", "/api/display")) == leaves


def test_every_move_and_blackout_answers_with_the_display_state(configured):
    shape = paths(configured.get("/api/display").json())
    for path in MOVES:
        body = {"view": "local:KSEA"} if path.endswith("/view") else None
        r = configured.post(path, json=body, auth=CONTROL)
        assert r.status_code == 200 and paths(r.json()) == shape, path


def test_the_view_example_fields_are_on_every_view_and_the_types_are_all_there_are(configured):
    cfg = Config.model_validate({
        **base_cfg(),
        "satellite": {"enabled": True},
        "cameras": {"enabled": True, "tiles": [{"stream": "cam1", "cols": 2}]},
        "notams": {"enabled": True, "source": "sample", "locations": [{"ident": "KS50"}]},
    })
    views = build_views(cfg)
    documented_fields = table_fields(section("GET", "/api/views"))
    assert documented_fields == {"id", "type", "label", "dwell_s"}
    assert all(documented_fields <= set(v) for v in views)
    for example in json_example("GET", "/api/views")["views"]:
        assert set(example) == documented_fields
    types = re.search(r"\| `type` \| (.+?) \|", DOC).group(1)
    assert set(re.findall(r"`(\w+)`", types)) == {v["type"] for v in views}
    assert set(json_example("GET", "/api/views")) == set(configured.get("/api/views").json())


def test_the_documented_view_ids_have_the_forms_the_display_gives_them(configured):
    ids = {v["id"] for v in configured.get("/api/views").json()["views"]}
    assert {"local:KSEA", "region:Puget Sound"} <= ids
    for form in ("local:<airport>", "region:<region name>", "notams:<location>", "satellite", "cameras"):
        assert f"`{form}`" in DOC


# ---- errors and limits ---------------------------------------------------------------------

def test_the_documented_errors_are_the_ones_the_api_gives(configured, monkeypatch):
    table = DOC[DOC.index("## Errors"):DOC.index("## Reading")]
    stated = set(re.findall(r'`"([^"`]+)"`', table))
    seen = set()

    r = configured.post("/api/display/next")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    seen.add(r.json()["detail"])
    for auth in (Bearer("tpc_mistyped"), ("admin", "hunter2")):
        r = configured.post("/api/display/next", auth=auth)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        seen.add(r.json()["detail"])
    r = configured.post("/api/display/view", json={"view": "no-such-view"}, auth=CONTROL)
    assert r.status_code == 404
    seen.add(r.json()["detail"])
    for bad in ({"content": b"{not json", "headers": {"content-type": "application/json"}}, {"json": {"seconds": 0}}):
        r = configured.post("/api/display/blackout", auth=CONTROL, **bad)
        assert r.status_code == 422
        assert all({"loc", "msg", "type"} <= set(e) for e in r.json()["detail"])
    assert configured.post("/api/display/blackout", json={"seconds": 0}, auth=CONTROL).json()["detail"][0]["loc"] == ["body", "seconds"]
    for kind in (None, "application/x-www-form-urlencoded", "text/plain"):           # a JSON body not sent as JSON
        headers = {"content-type": kind} if kind else {}
        r = configured.post("/api/display/view", content=b'{"view": "local:KSEA"}', headers=headers, auth=CONTROL)
        assert r.status_code == 422, kind
    assert configured.post("/api/display/view", content=b'{"view": "local:KSEA"}', auth=CONTROL,
                           headers={"content-type": "application/json; charset=utf-8"}).status_code == 200

    def full():
        raise TooManySubscribers()
    monkeypatch.setattr(main_mod.manager, "subscribe", full)
    r = configured.get("/api/stream")
    assert r.status_code == 503
    seen.add(r.json()["detail"])
    monkeypatch.setattr(STORE, "error", "data/secrets.json: Expecting value")
    r = configured.post("/api/display/next", auth=CONTROL)
    assert r.status_code == 503 and r.json()["detail"] == LOCKED_DETAIL

    assert stated == seen
    assert set(re.findall(r"^\| (\d{3}) \|", table, re.M)) == {"401", "404", "422", "503"}


def test_the_documented_limits_are_the_ones_the_api_applies(configured):
    def blackout(**body):
        return configured.post("/api/display/blackout", json=body, auth=CONTROL).status_code

    floor = number(r"a number above (\d+), at most \d+")
    ceiling = number(r"a number above \d+, at most (\d+)")
    assert blackout(seconds=floor) == 422 and blackout(seconds=floor + 0.5) == 200
    assert blackout(seconds=ceiling) == 200 and blackout(seconds=ceiling + 0.5) == 422
    reason = int(number(r"a string of at most (\d+) characters"))
    assert blackout(reason="r" * reason) == 200 and blackout(reason="r" * (reason + 1)) == 422
    view = int(number(r"a view id from `GET /api/views` of at most (\d+) characters"))
    move = lambda v: configured.post("/api/display/view", json={"view": v}, auth=CONTROL).status_code  # noqa: E731
    assert move("v" * view) == 404 and move("v" * (view + 1)) == 422
    assert number(r"a keep-alive comment, sent after (\d+) seconds without an event") == main_mod.SSE_KEEPALIVE_S
    assert number(r"At most (\d+) event streams can be open at once") == MAX_SUBSCRIBERS


# ---- behaviour it states --------------------------------------------------------------------

def test_the_event_stream_opens_with_connected_and_keeps_alive_at_the_interval_it_states(monkeypatch):
    monkeypatch.setattr(main_mod, "SSE_KEEPALIVE_S", 0.05)

    class Open:
        async def is_disconnected(self):
            return False

    async def read():
        body = (await main_mod.stream(Open())).body_iterator
        first = await body.__anext__()
        started = time.monotonic()
        second = await asyncio.wait_for(body.__anext__(), 2)
        waited = time.monotonic() - started
        await body.aclose()
        return first, second, waited

    first, second, waited = asyncio.run(read())
    assert first.startswith("data: ") and json.loads(first[6:])["event"] == "connected"
    assert second == ": keep-alive\n\n" and waited >= 0.05


def test_a_blackout_without_seconds_holds_until_restored_whatever_the_empty_body(configured):
    for body in ({}, {"content": b""}, {"content": b"", "headers": {"content-type": "application/json"}}, {"json": {}},
                 {"json": {"seconds": None}}):
        configured.post("/api/display/restore", auth=CONTROL)
        r = configured.post("/api/display/blackout", auth=CONTROL, **body)
        assert r.status_code == 200, (body, r.text)
        assert r.json()["active"] is True and r.json()["until"] is None and r.json()["remaining_s"] is None, body


def test_a_blackout_replaces_the_one_in_progress_end_time_included(configured):
    configured.post("/api/display/blackout", json={"reason": "movie"}, auth=CONTROL)
    s = configured.post("/api/display/blackout", json={"seconds": 30, "reason": "doorbell"}, auth=CONTROL).json()
    assert s["active"] is True and s["reason"] == "doorbell" and 29 <= s["remaining_s"] <= 30
    s = configured.post("/api/display/restore", auth=CONTROL).json()
    assert s["active"] is False and s["until"] is None and s["reason"] is None
    again = configured.post("/api/display/restore", auth=CONTROL)
    assert again.status_code == 200 and again.json()["active"] is False and again.json()["until"] is None


def test_a_move_answers_with_the_request_recorded_and_the_view_that_was_on_screen(configured):
    before = configured.get("/api/display").json()
    for action, body in (("next", None), ("previous", None), ("view", {"view": "region:Puget Sound"})):
        s = configured.post(f"/api/display/{action}", json=body, auth=CONTROL).json()
        assert s["nav"]["seq"] == before["nav"]["seq"] + 1 and s["nav"]["action"] == action
        assert s["nav"]["view"] == (body or {}).get("view")
        assert s["showing"]["view"] == "local:KSEA"          # until the TV reports its switch
        before = s
    blacked = configured.post("/api/display/blackout", auth=CONTROL).json()
    s = configured.post("/api/display/next", auth=CONTROL).json()
    assert s["active"] is True and s["until"] == blacked["until"]                 # a move does not end a blackout


def test_saving_settings_clears_the_view_reported_until_the_tv_reports_again(configured):
    assert configured.get("/api/display").json()["showing"]["view"] == "local:KSEA"
    assert configured.put("/api/config", json=base_cfg(), auth=AUTH).status_code == 200
    showing = configured.get("/api/display").json()["showing"]
    assert showing["view"] is None and showing["label"] is None and showing["index"] is None


# ---- the copy made for a display ---------------------------------------------------------

VIEWS = [
    {"id": "local:KSEA", "type": "local", "label": "KSEA - Local 5mi", "dwell_s": 20},
    {"id": "region:Puget | Sound", "type": "regional", "label": "Region: a | b\nc", "dwell_s": 15},
    {"id": "local:KBFI", "type": "local", "label": "KBFI - Local 5mi", "dwell_s": 20.5},
    {"id": "region:Puget | Sound", "type": "regional", "label": "Region: a | b\nc", "dwell_s": 15},
    {"id": "region:tick`s", "type": "regional", "label": "ticks", "dwell_s": 15},
]


def view_rows(text):
    start = text.index("## Views on this display")
    return [line for line in text[start:text.index("\n## ", start + 4)].splitlines() if line.startswith("| ") and "---" not in line][1:]


def cells(row):
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)[1:-1]]


def test_the_copy_carries_the_address_it_was_asked_from_and_the_views_once_each_in_cycle_order():
    text = apidoc.render("http://192.168.1.50:8000/", VIEWS)
    assert apidoc.PLACEHOLDER not in text and text.count("http://192.168.1.50:8000") == DOC.count(apidoc.PLACEHOLDER)
    assert apidoc.VIEWS_START not in text and apidoc.VIEWS_END not in text and "For example" not in text
    rows = [cells(r) for r in view_rows(text)]
    assert [r[0] for r in rows] == ["`local:KSEA`", "`region:Puget | Sound`", "`local:KBFI`", "`` region:tick`s ``"]
    assert [r[4] for r in rows] == ["1", "2", "1", "1"]
    assert rows[1][2] == "Region: a | b c" and rows[2][3] == "20.5"
    assert all(len(r) == 5 for r in rows)
    restored = text.replace("http://192.168.1.50:8000", apidoc.PLACEHOLDER)     # nothing else changed
    assert restored.startswith(DOC.split(apidoc.VIEWS_START)[0]) and restored.endswith(DOC.split(apidoc.VIEWS_END)[1])


@pytest.mark.parametrize("base", ["http://evil.example/<script>", "javascript:alert(1)", "http://a b:8000",
                                  "http://host:8000/path", "ftp://host", ""])
def test_an_address_that_is_not_a_plain_scheme_host_and_port_leaves_the_placeholder(base):
    text = apidoc.render(base, VIEWS)
    assert text.count(apidoc.PLACEHOLDER) == DOC.count(apidoc.PLACEHOLDER)


@pytest.mark.parametrize("base", ["http://hangar.local:8000", "http://192.168.1.50:8000", "https://tv.example.net",
                                  "http://[fe80::1]:8000", "http://localhost:8000"])
def test_a_plain_address_is_written_in(base):
    assert apidoc.PLACEHOLDER not in apidoc.render(base + "/", VIEWS)


def test_a_display_without_views_says_so():
    text = apidoc.render("http://192.168.1.50:8000", [])
    assert not view_rows(text) and "no views yet" in text


def test_the_admin_page_gets_the_copy_for_this_display(configured):
    sign_in(configured)
    r = configured.get("/api/docs/display")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "content-disposition" not in r.headers
    assert "Base URL: `http://testserver`" in r.text and apidoc.PLACEHOLDER not in r.text
    ids = list(dict.fromkeys(v["id"] for v in configured.get("/api/views").json()["views"]))
    assert len(ids) >= 2 and [cells(row)[0] for row in view_rows(r.text)] == [f"`{i}`" for i in ids]
    r = configured.get("/api/docs/display?download=true")
    assert r.headers["content-disposition"] == 'attachment; filename="display-api.md"'
