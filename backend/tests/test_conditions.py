"""The weather readout for automations (GET /api/conditions): the humidity against the
values the National Weather Service publishes, the altimeter against the inches a
report states, the ceiling's three states from both METAR sources, every field of an
airport's record, and the list of monitored airports."""
import json
import os
import re
import time

import pytest
from support import CONTROL, base_cfg, put, wait_until

from app import conditions as cond
from app import main as main_mod
from app import nws_gateway as gw
from app.metar_decode import decode
from app.weather import normalize_metar

DATA = os.path.join(os.path.dirname(__file__), "data")
NWS = json.load(open(os.path.join(DATA, "nws_observations.json")))["observations"]
CASES = json.load(open(os.path.join(DATA, "metar_reports.json")))
KS50_RAIN = "KS50 132255Z AUTO 00000KT 2 1/2SM -RA BR FEW055 OVC070 11/11 A2934 RMK AO2 VIS 2V4 P0002 T01150115"
FIELDS = ["name", "reported", "category", "observed", "age_s", "stale", "ceiling_ft", "ceiling_known",
          "visibility_sm", "wind_dir_deg", "wind_variable", "wind_speed_kt", "wind_gust_kt", "temperature_c",
          "dewpoint_c", "humidity_pct", "altimeter_inhg", "raw"]


def gateway(report, stamp="2026/11/13 22:55", name="Auburn Muni"):
    rec, obs = gw.parse_station_file(f"{stamp}\n{report}\n", report.split()[0])
    return gw.to_metar(rec, obs, name, 47.3276, -122.2267, 4500), obs


# ---- humidity ----------------------------------------------------------------------------

TOLERANCE = 1e-6


@pytest.mark.parametrize("obs", NWS, ids=lambda o: f"{o['station']} {o['temperature_c']}/{o['dewpoint_c']}")
def test_humidity_is_the_one_the_national_weather_service_publishes(obs):
    ours = cond.relative_humidity(obs["temperature_c"], obs["dewpoint_c"])
    assert abs(ours - obs["relative_humidity"]) <= TOLERANCE


def test_the_published_values_cover_frost_to_warmth_and_tell_other_formulas_apart():
    temps = [o["temperature_c"] for o in NWS]
    humid = [o["relative_humidity"] for o in NWS]
    assert min(temps) < 0 < 25 < max(temps) and min(humid) < 50 < 90 < max(humid)

    def tetens(t, td):   # the vapor pressure worksheet's form, 6.11 x 10^(7.5 T / (237.3 + T))
        return 100 * 10 ** (7.5 * td / (237.3 + td)) / 10 ** (7.5 * t / (237.3 + t))

    for other in (tetens, lambda t, td: 100 - 5 * (t - td)):
        assert any(abs(other(o["temperature_c"], o["dewpoint_c"]) - o["relative_humidity"]) > TOLERANCE for o in NWS)


def test_humidity_needs_both_temperatures_and_never_exceeds_saturation():
    assert cond.relative_humidity(None, 5) is None and cond.relative_humidity(5, None) is None
    assert cond.relative_humidity(11.5, 11.5) == 100.0
    assert cond.relative_humidity(10, 11) == 100.0          # a dew point above the temperature: a mistyped report


# ---- altimeter ---------------------------------------------------------------------------

def test_the_altimeter_gives_back_the_inches_the_report_states():
    for a in range(2700, 3201):
        rec = decode(f"KXXX 132255Z 00000KT 10SM CLR 10/05 A{a}")
        assert cond.readout({"altimeter_hpa": rec["altim"]}, "", 0, 4500)["altimeter_inhg"] == a / 100, a


@pytest.mark.parametrize("hpa,inhg", [(986, 29.12), (1008, 29.77), (1013, 29.91), (1029, 30.39)])
def test_an_altimeter_in_hectopascals_is_converted_as_the_national_weather_service_does(hpa, inhg):
    # inHg = 0.0295300 x hPa (NWS pressure conversion); aviationweather.gov's 33.8667 hPa per inch differs here
    m = gateway(f"EGLL 132220Z 24008KT 9999 FEW030 08/04 Q{hpa:04d}", "2026/11/13 22:20", "")[0]
    assert cond.readout(m, "", 0, 4500)["altimeter_inhg"] == inhg


def test_the_altimeter_from_aviationweather_gives_back_the_inches_of_its_report():
    checked = 0
    for case in CASES:
        body = case["raw"].split(" RMK")[0]
        a = re.findall(r" A(\d{4})(?: |$)", body)
        if a and case["record"].get("altim") is not None and not re.search(r" Q\d{4}(?: |$)", body):
            m = normalize_metar(case["record"], 4500)
            assert cond.readout(m, "", 0, 4500)["altimeter_inhg"] == int(a[0]) / 100, case["raw"]
            checked += 1
    assert checked >= 20


# ---- ceiling -----------------------------------------------------------------------------

@pytest.mark.parametrize("sky,ceiling,known", [
    ("FEW020 SCT180", None, True),          # no ceiling
    ("BKN030 OVC050", 3000, True),
    ("VV002", 200, True),                   # an indefinite ceiling
    ("FEW025 BKN///", None, False),         # a ceiling without its height
    ("BKN012 //////TCU", None, False),      # a group that may be a lower ceiling
    ("", None, False),                      # no sky condition
])
def test_the_gateway_ceiling_is_a_height_none_or_unknown(sky, ceiling, known):
    report = f"RJBE 132200Z 06007KT 9999 {sky} 15/12 Q1020".replace("  ", " ")
    m, obs = gateway(report, "2026/11/13 22:00", "")
    r = cond.readout(m, "", obs, 4500)
    assert (r["ceiling_ft"], r["ceiling_known"]) == (ceiling, known)


def test_cavok_rules_a_ceiling_out():
    m, obs = gateway("RJBE 132200Z 06007KT CAVOK 15/12 Q1020", "2026/11/13 22:00", "")
    r = cond.readout(m, "", obs, 4500)
    assert (r["ceiling_ft"], r["ceiling_known"]) == (None, True)


@pytest.mark.parametrize("sky,ceiling,known", [
    ("CLR", None, True),
    ("FEW020", None, True),
    ("SCT009 BKN015 OVC025", 1500, True),
    ("", None, False),                      # no sky condition
])
def test_the_aviationweather_ceiling_is_read_from_its_report(sky, ceiling, known):
    raw = f"KBFI 132253Z 16007KT 10SM {sky} 08/06 A3012".replace("  ", " ")
    r = cond.readout(normalize_metar({"icaoId": "KBFI", "rawOb": raw}, 4500), "", 0, 4500)
    assert (r["ceiling_ft"], r["ceiling_known"]) == (ceiling, known)


@pytest.mark.parametrize("raw,clouds,ceiling,known", [
    ("BGBW 122150Z AUTO 13001KT 0350NDV FG VV002/// 01/01 Q1011", None, 200, True),          # left out whole
    ("LFBA 132230Z AUTO 13004KT 0550 FG VV/// 04/04 Q1013", [], None, False),                 # obscured, height unknown
    ("KMWN 131153Z 01019KT 100SM BKN/// SCT010 BKN030 OVC050 M07/M16", [
        {"cover": "SCT", "base": 1000}, {"cover": "BKN", "base": 3000}, {"cover": "OVC", "base": 5000}], None, False),
])
def test_a_slashed_group_aviationweather_leaves_out_still_counts(raw, clouds, ceiling, known):
    record = {"icaoId": raw[:4], "rawOb": raw}
    if clouds is not None:
        record["clouds"] = clouds
    r = cond.readout(normalize_metar(record, 4500), "", 0, 4500)
    assert (r["ceiling_ft"], r["ceiling_known"]) == (ceiling, known)


SKY_GROUP = re.compile(r"^(FEW|SCT|BKN|OVC|VV|///)")


def test_the_ceiling_is_aviationweathers_wherever_its_record_holds_every_cloud_group():
    checked = set()
    for case in CASES:
        body = case["raw"].split(" RMK")[0].split()
        if "clouds" not in case["compare"] or any("/" in t for t in body if SKY_GROUP.match(t)):
            continue
        clouds = case["record"].get("clouds")
        expected = (min((c["base"] for c in clouds or [] if c["cover"] in ("BKN", "OVC", "OVX")), default=None),
                    clouds is not None)
        r = cond.readout({"raw": case["raw"]}, "", 0, 4500)
        assert (r["ceiling_ft"], r["ceiling_known"]) == expected, case["raw"]
        checked.add((expected[0] is not None, expected[1]))
    assert checked == {(True, True), (False, True), (False, False)}       # a ceiling, none, and no sky condition


def test_without_the_report_text_only_a_stated_height_is_known():
    assert cond.ceiling({"ceiling_ft": 1200}) == (1200, True)
    assert cond.ceiling({"ceiling_ft": None}) == (None, False) and cond.ceiling({}) == (None, False)


# ---- an airport's record -----------------------------------------------------------------

def test_a_report_gives_every_field():
    m, obs = gateway(KS50_RAIN)
    assert cond.readout(m, "Auburn", obs + 600, 4500) == {
        "name": "Auburn", "reported": True, "category": "IFR", "observed": obs, "age_s": 600, "stale": False,
        "ceiling_ft": 7000, "ceiling_known": True, "visibility_sm": 2.5,
        "wind_dir_deg": None, "wind_variable": False, "wind_speed_kt": 0, "wind_gust_kt": None,   # calm
        "temperature_c": 11.5, "dewpoint_c": 11.5, "humidity_pct": 100, "altimeter_inhg": 29.34, "raw": KS50_RAIN,
    }
    assert list(cond.readout(m, "", obs, 4500)) == FIELDS
    assert cond.readout(m, "", obs, 4500)["name"] == "Auburn Muni"          # the report's own name without a label


def test_wind_direction_speed_and_gust():
    def wind(**record):
        r = cond.readout(normalize_metar({"icaoId": "KBFI", **record}, 4500), "", 0, 4500)
        return r["wind_dir_deg"], r["wind_variable"], r["wind_speed_kt"], r["wind_gust_kt"]

    assert wind(wdir=230, wspd=12, wgst=22) == (230, False, 12, 22)
    assert wind(wdir=40, wspd=7) == (40, False, 7, None)
    assert wind(wdir="VRB", wspd=4) == (None, True, 4, None)
    assert wind(wdir=0, wspd=0) == (None, False, 0, None)                  # calm has no direction
    assert wind() == (None, False, None, None)                             # no wind group


def test_humidity_is_given_in_whole_percent_rounded_half_up():
    for t, td in ((20, 10), (25.6, 3.3), (-2.8, -7.2), (31, 24)):
        r = cond.readout(normalize_metar({"icaoId": "KBFI", "temp": t, "dewp": td}, 4500), "", 0, 4500)
        assert r["humidity_pct"] == int(cond.relative_humidity(t, td) + 0.5) and (r["temperature_c"], r["dewpoint_c"]) == (t, td)
    assert cond.readout(normalize_metar({"icaoId": "KBFI", "temp": 20}, 4500), "", 0, 4500)["humidity_pct"] is None


def test_age_and_staleness_are_worked_out_when_asked():
    now = time.time()
    m = normalize_metar({"icaoId": "KBFI", "obsTime": now - 4000}, 4500)
    assert cond.readout(m, "", now, 4500)["stale"] is False
    assert cond.readout(m, "", now + 600, 4500)["stale"] is True and cond.readout(m, "", now + 600, 4500)["age_s"] == 4600


def test_a_report_that_settles_no_category_reads_unknown():
    m = normalize_metar({"icaoId": "KBFI", "visib": "10+"}, 4500)          # no sky condition, so no category
    r = cond.readout(m, "", 0, 4500)
    assert r["category"] == "UNKNOWN" and r["reported"] is True and r["ceiling_known"] is False


def test_an_airport_without_a_report_has_every_field_and_no_values():
    r = cond.readout(None, "Auburn", time.time(), 4500)
    assert list(r) == FIELDS
    assert r["name"] == "Auburn" and r["reported"] is False and r["category"] == "UNKNOWN" and r["ceiling_known"] is False
    assert r["wind_variable"] is False
    assert all(r[f] is None for f in FIELDS if f not in ("name", "reported", "category", "ceiling_known", "wind_variable"))


# ---- the route -----------------------------------------------------------------------------

def test_the_readout_lists_the_enabled_airports_in_order_from_the_cache(client, monkeypatch):
    manager = main_mod.manager
    now = time.time()
    report = normalize_metar({
        "icaoId": "KBFI", "name": "Seattle/Boeing Fld", "wdir": 160, "wspd": 7, "wgst": 18, "visib": "10+",
        "clouds": [{"cover": "FEW", "base": 1200}, {"cover": "BKN", "base": 2500}], "temp": 8.3, "dewp": 6.1,
        "altim": 1020.1, "obsTime": now - 600, "fltCat": "MVFR", "rawOb": "KBFI 132253Z 16007G18KT 10SM FEW012 BKN025 08/06 A3012",
    }, 4500)

    async def fetch(icaos, stale):
        return {k: report for k in ("KBFI",) if k in icaos}

    async def no_gateway(airports, stale):
        return {}, None

    monkeypatch.setattr(manager, "_weather_cache", {})
    monkeypatch.setattr(manager.metar, "fetch", fetch)
    monkeypatch.setattr(manager.gateway, "fetch", no_gateway)
    cfg = base_cfg()
    cfg["airports"] = [
        {"icao": "ks50", "name": "Auburn", "lat": 47.33, "lon": -122.23},
        {"icao": "KBFI", "name": "Boeing Field", "lat": 47.53, "lon": -122.30},
        {"icao": "KSEA", "name": "Seattle", "lat": 47.45, "lon": -122.31, "enabled": False},
    ]
    assert put(client, cfg).status_code == 200
    read = lambda: client.get("/api/conditions", auth=CONTROL).json()          # noqa: E731
    assert wait_until(lambda: read()["airports"]["KBFI"]["reported"], 5)
    body = read()
    assert list(body) == ["monitored", "airports"]
    assert body["monitored"] == list(body["airports"]) == ["KS50", "KBFI"]     # in the settings' order; KSEA is off
    bfi = body["airports"]["KBFI"]
    assert 600 <= bfi["age_s"] <= 660
    assert {k: v for k, v in bfi.items() if k != "age_s"} == {
        "name": "Boeing Field", "reported": True, "category": "MVFR", "observed": report["obs_time"], "stale": False,
        "ceiling_ft": 2500, "ceiling_known": True, "visibility_sm": 10.0,
        "wind_dir_deg": 160, "wind_variable": False, "wind_speed_kt": 7, "wind_gust_kt": 18,
        "temperature_c": 8.3, "dewpoint_c": 6.1, "humidity_pct": 86, "altimeter_inhg": 30.12, "raw": report["raw"],
    }
    assert body["airports"]["KS50"]["reported"] is False and body["airports"]["KS50"]["name"] == "Auburn"
