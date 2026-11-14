"""METARs for airports aviationweather.gov does not serve: the raw-report decoder, the
flight category under missing elements (of a gateway report and of an
aviationweather.gov record), the NWS gateway source, and the fallback in the weather
refresh. Reports are real ones; the decoded values in data/metar_reports.json are
aviationweather.gov's own (backend/tools/check_metar_decode.py --fixture)."""
import asyncio
import itertools
import json
import logging
import os
import time

import pytest
from support import base_cfg, put, wait_until

from app import main as main_mod
from app import nws_gateway as gw
from app.metar_decode import UNLIMITED_FT, UNLIMITED_SM, decode, observed_at
from app.weather import category_in_range, derive_category, normalize_metar

CASES = json.load(open(os.path.join(os.path.dirname(__file__), "data", "metar_reports.json")))


def same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= 0.005
    return a == b


def decoded(raw, anchor=0.0):
    """The decoder's fields named as aviationweather.gov's record names them."""
    dec = decode(raw)
    out = dict(dec)
    out["obsTime"] = observed_at(dec["time"], anchor)
    out["fltCat"] = category_in_range(dec["ceiling_range"], dec["visibility_range"])
    return out


def category(raw):
    """The flight category the display gives a gateway report."""
    return gw.to_metar(decode(raw), 0.0, "", 0.0, 0.0, 4500)["category"]


# ---- the decoder against aviationweather.gov's decode -------------------------------------

@pytest.mark.parametrize("case", CASES, ids=[c["raw"][:60] for c in CASES])
def test_decoder_reads_a_real_report_as_aviationweather_does(case):
    record = case["record"]
    ours = decoded(case["raw"], record["obsTime"])
    for field in case["compare"]:
        assert same(ours.get(field), record.get(field)), (field, ours.get(field), record.get(field))


def test_fixture_covers_each_field_and_flight_category():
    compared = {f for c in CASES for f in c["compare"] if c["record"].get(f) is not None}
    assert compared == {"wdir", "wspd", "wgst", "visib", "altim", "temp", "dewp", "clouds", "obsTime", "fltCat"}
    assert {c["record"].get("fltCat") for c in CASES} == {"VFR", "MVFR", "IFR", "LIFR", None}


# ---- forms the code defines and aviationweather.gov's record leaves out --------------------

def test_visibility_with_ndv_is_ten_kilometres_or_more():
    d = decode("METAR ENDR 132250Z AUTO 24012KT 9999NDV BKN043/// 05/M04 Q0999 W///S4")
    assert d["visib"] == "6+" and d["clouds"] == [{"cover": "BKN", "base": 4300}]
    assert category("METAR ENDR 132250Z AUTO 24012KT 9999NDV BKN043/// 05/M04 Q0999 W///S4") == "VFR"


def test_temperature_is_read_when_the_dew_point_is_slashed_out():
    d = decode("METAR SBCX 132200Z 20003KT CAVOK 22/// Q1018")
    assert d["temp"] == 22 and "dewp" not in d and d["altim"] == 1018


def test_every_cloud_layer_is_kept_and_a_base_at_the_surface_is_a_ceiling():
    d = decode("METAR CYIK 132200Z 18018KT 2 1/2SM -SG BR DRSN FEW004 SCT005 BKN007 BKN012 OVC016 M02/M02 A3016 RMK ST2ST2ST2ST1SC1")
    assert [(c["cover"], c["base"]) for c in d["clouds"]] == [("FEW", 400), ("SCT", 500), ("BKN", 700), ("BKN", 1200), ("OVC", 1600)]
    d = decode("METAR RJSK 132200Z 16003KT 0100 R28/0200N FG BKN000 BKN001 08/08 Q1020")
    assert d["clouds"][0] == {"cover": "BKN", "base": 0} and d["ceiling_range"] == (0, 0)


def test_a_wind_group_that_is_not_in_code_form_gives_no_wind():
    for raw in (
        "METAR KOWI 132250Z AUTO 4.83333305KT 10SM CLR 06/M02 A3012 RMK A01",
        "METAR KTAH 132150Z AUTO 0100915KT 9SM CLR 23/11 A2994 RMK A01",
        "METAR KMWA 132245Z 3308KT 10SM BKN040 OVC050 06/M02 A3010",
        "METAR KWHP 131955Z 070705KT 8SM SCT015 BKN030 OVC050 15/15 A2982",
    ):
        d = decode(raw)
        assert "wdir" not in d and "wspd" not in d and d["visib"] in (8, 9, "10+")


def test_the_trend_forecast_and_a_second_report_are_not_read_as_the_observation():
    d = decode("METAR LFBT 132230Z AUTO 05003KT 8000 OVC004 10/10 Q1011 TEMPO 0600 FG VV///")
    assert d["visib"] == 4.97 and d["ceiling_range"] == (400, 400)
    d = decode("MDJB 131400Z 18010KT 9999 SCT018 SCT300 30/26 Q1013 METAR MDSD 131400Z 16008KT 9999 FEW018CB SCT018 30/25 Q1014 CB/NE/W")
    assert d["icaoId"] == "MDJB" and d["wdir"] == 180 and d["temp"] == 30 and d["dewp"] == 26 and d["altim"] == 1013
    assert [(c["cover"], c["base"]) for c in d["clouds"]] == [("SCT", 1800), ("SCT", 30000)]


def test_text_that_is_not_a_dated_report_is_refused():
    assert decode("METAR TLPL 1332200Z 10018KT 9999 BKN023 28/26 Q1014 NOSIG") is None
    assert decode("KS50 131655Z NIL") is None
    assert decode("<html><title>404 Not Found</title></html>") is None
    assert decode("") is None


def test_decoder_does_not_raise_on_a_damaged_report():
    for case in CASES:
        tokens = case["raw"].split()
        for i, tok in enumerate(tokens):
            for damaged in (tokens[:i] + tokens[i + 1:], tokens[:i] + [tok[:-1]] + tokens[i + 1:], tokens[:i] + [tok, tok] + tokens[i + 1:]):
                assert decode(" ".join(damaged)) is None or True
    assert "visib" not in decode("KS50 132255Z AUTO 00000KT 2 1/0SM -RA BR FEW055 OVC070 11/11 A2934")


# ---- the flight category when the report leaves something open ----------------------------

LEVELS_FT = [0, 100, 400, 500, 900, 1000, 2900, 3000, 3100, 9000, UNLIMITED_FT]
LEVELS_SM = [0.0, 0.25, 0.99, 1.0, 2.99, 3.0, 5.0, 5.01, 6.0, 10.0, UNLIMITED_SM]


def test_category_is_given_exactly_when_every_value_in_the_ranges_agrees():
    for c_lo, c_hi in itertools.combinations_with_replacement(LEVELS_FT, 2):
        for v_lo, v_hi in itertools.combinations_with_replacement(LEVELS_SM, 2):
            possible = {
                derive_category(c, v)
                for c in LEVELS_FT if c_lo <= c <= c_hi
                for v in LEVELS_SM if v_lo <= v <= v_hi
            }
            got = category_in_range((c_lo, c_hi), (v_lo, v_hi))
            assert got == (possible.pop() if len(possible) == 1 else None), ((c_lo, c_hi), (v_lo, v_hi), got)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # vertical visibility without a height: the ceiling is open
        ("METAR LFBA 132300Z AUTO 10004KT 0350 R29/0450N FG VV/// 05/05 Q1012", "LIFR"),
        ("METAR LFSX 132230Z AUTO 28001KT 9000 4200 VV/// M02/M03 Q1018", None),
        # a broken layer without a height is no lower than the layer before it
        ("METAR RJBB 132230Z 09004KT 9999 FEW020 SCT180 BKN/// 15/13 Q1021 NOSIG", "VFR"),
        ("METAR RJBE 132200Z 06007KT 9999 FEW025 BKN/// 15/12 Q1020", None),
        # convective cloud of unknown amount and height may be a ceiling anywhere
        ("METAR RJDB 132200Z AUTO 18012KT 9999 FEW090 SCT100 OVC110 //////TCU 17/14 Q1019", None),
        ("METAR ROIG 132200Z AUTO 18004KT 130V200 9999 BKN012 BKN150 OVC190 //////TCU 24/23 Q1016", None),
        # CAVOK rules a ceiling out whatever is slashed after it
        ("METAR OODQ 132250Z AUTO 33010KT CAVOK ////// 25/14 Q1017 NOSIG", "VFR"),
        # a layer whose amount is missing may or may not be a ceiling
        ("METAR UUOK 132230Z AUTO 22004MPS 4800 // ///005/// 12/12 Q0994 RMK QNH745 QFE728/0971", "IFR"),
        ("METAR URWW 132200Z AUTO 22006MPS 8000 // ///038/// ///// Q////", "MVFR"),
        ("METAR ULPB 132200Z AUTO VRB01MPS 9999 // ///016/// 00/00 Q0986 RMK QFE736/0981", None),
        # no sky condition: only a visibility below one mile settles the category
        ("METAR KBLU 132252Z AUTO 18019G32KT 1/4SM RA FG 02/02 A2961 RMK AO2 PK WND 17046/2229 SLP007 P0028 T00170017 $", "LIFR"),
        ("KS50 131415Z AUTO 00000KT 10SM 09/09 A2925 RMK AO2 T00850085 $", None),
        ("METAR EGUN 132255Z AUTO 00000KT 9999 M M01/M03 A3009 RMK AO2 SLP193 T10091033 CHINO RWY10 $", None),
        # no visibility: only a ceiling below 500 feet would settle it
        ("METAR KMIV 132254Z AUTO 33007KT OVC025 06/02 A2973 RMK AO2 SLP066 T00560022 $", None),
        ("METAR PANN 132253Z AUTO 07006KT OVC007 M07/M09 A2949 RMK AO2 SLP998 T10721089 TSNO $", None),
        # a cloud group that is not in code form but names a ceiling cover
        ("KSWF 130045Z 06006KT 6SM -DZ BR BKN015 OVC25 07/07 A2948", "MVFR"),
        ("METAR NFTF 132200Z 09006KT 9999 FEW025TCU BKN1230 26/22 Q1018", None),
        # nothing reported at all
        ("KS50 130815Z AUTO RMK AO2 PWINO PNO $", None),
    ],
)
def test_category_of_reports_with_an_element_missing_or_slashed(raw, expected):
    assert category(raw) == expected


WITHOUT_CATEGORY = [c for c in CASES if not c["record"].get("fltCat")]


def carries(record):
    if record.get("clouds") is not None:
        return "sky" if record.get("visib") is None else "both"
    return "visibility" if record.get("visib") is not None else "neither"


@pytest.mark.parametrize("case", WITHOUT_CATEGORY, ids=[c["raw"][:60] for c in WITHOUT_CATEGORY])
def test_record_without_a_category_is_given_only_the_one_its_report_settles(case):
    record = case["record"]
    shown = normalize_metar(record, 4500)["category"]
    report = decoded(case["raw"])
    assert shown in (None, report["fltCat"])
    same_elements = {"visib", "clouds"} <= set(case["compare"])
    same_ceiling = record.get("clouds") is not None or report["ceiling_range"] == (0, UNLIMITED_FT)
    if same_elements and same_ceiling:
        assert shown == report["fltCat"]


def test_fixture_covers_records_whose_one_element_does_and_does_not_settle_the_category():
    seen = {(carries(c["record"]), normalize_metar(c["record"], 4500)["category"]) for c in WITHOUT_CATEGORY}
    assert seen == {("visibility", "LIFR"), ("visibility", None), ("sky", "LIFR"), ("sky", None), ("neither", None)}


# ---- observation time ------------------------------------------------------------------

def epoch(text):
    import calendar

    return calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M"))


def test_report_time_resolves_to_the_occurrence_nearest_the_anchor():
    assert observed_at((13, 22, 55), epoch("2026-11-13 22:57")) == epoch("2026-11-13 22:55")
    assert observed_at((31, 23, 55), epoch("2026-11-01 00:05")) == epoch("2026-10-31 23:55")
    assert observed_at((1, 0, 5), epoch("2026-10-31 23:58")) == epoch("2026-11-01 00:05")
    assert observed_at((31, 12, 0), epoch("2026-12-02 00:00")) == epoch("2026-12-31 12:00")  # November has no 31st
    assert observed_at((1, 0, 10), epoch("2026-12-31 23:59")) == epoch("2027-01-01 00:10")
    assert observed_at((29, 6, 0), epoch("2028-02-28 12:00")) == epoch("2028-02-29 06:00")


# ---- gateway station files ----------------------------------------------------------------

KS50_RAIN = "KS50 132255Z AUTO 00000KT 2 1/2SM -RA BR FEW055 OVC070 11/11 A2934 RMK AO2 VIS 2V4 P0002 T01150115"
KS50_EMPTY = "KS50 130815Z AUTO RMK AO2 PWINO PNO $"


def test_station_file_gives_the_report_and_its_time():
    rec, obs = gw.parse_station_file(f"2026/11/13 22:55\n{KS50_RAIN}\n", "KS50")
    assert obs == epoch("2026-11-13 22:55") and rec["icaoId"] == "KS50" and rec["rawOb"] == KS50_RAIN
    assert rec["wdir"] == 0 and rec["wspd"] == 0 and rec["visib"] == 2.5
    assert rec["temp"] == 11.5 and rec["dewp"] == 11.5 and rec["altim"] == 993.6
    assert rec["clouds"] == [{"cover": "FEW", "base": 5500}, {"cover": "OVC", "base": 7000}]


def test_station_file_date_decides_the_month_of_the_report():
    rec, obs = gw.parse_station_file("2011/09/09 16:09\nA302 091609Z 00000KT 9999 SKC 30/20 Q1010\n", "A302")
    assert obs == epoch("2011-09-09 16:09")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "2026/11/13 22:55\n",
        f"{KS50_RAIN}\n",
        f"2026/13/45 22:55\n{KS50_RAIN}\n",
        "2026/11/13 22:55\nKSEA 132253Z 00000KT 6SM -RA BR FEW040 BKN070 OVC090 11/11 A2933\n",
        "<!DOCTYPE HTML PUBLIC \"-//IETF//DTD HTML 2.0//EN\">\n<html><head>\n<title>404 Not Found</title>\n",
        "2026/11/13 22:55\nKS50 132255Z NIL\n",
    ],
)
def test_station_file_that_is_not_a_dated_report_of_the_station_is_refused(text):
    assert gw.parse_station_file(text, "KS50") is None


def test_gateway_report_becomes_the_display_record_of_the_configured_airport():
    rec, obs = gw.parse_station_file(f"2026/11/13 22:55\n{KS50_RAIN}\n", "KS50")
    m = gw.to_metar(rec, obs, "Auburn Muni", 47.3276, -122.2267, 4500)
    assert m["icao"] == "KS50" and m["name"] == "Auburn Muni" and (m["lat"], m["lon"]) == (47.3276, -122.2267)
    assert m["source"] == "nws" and m["raw"] == KS50_RAIN and m["obs_time"] == obs
    assert m["wind"] == {"dir": 0, "variable": False, "speed_kt": 0, "gust_kt": None, "calm": True}
    assert m["visibility_sm"] == 2.5 and m["ceiling_ft"] == 7000
    assert m["category"] == "IFR" and m["category_color"] == "#ef4444"  # visibility below three miles
    assert m["temp_c"] == 11.5 and m["dewpoint_c"] == 11.5 and m["altimeter_hpa"] == 993.6


def test_report_without_elements_shows_no_values_and_no_category():
    rec, obs = gw.parse_station_file(f"2026/11/13 08:15\n{KS50_EMPTY}\n", "KS50")
    m = gw.to_metar(rec, obs, "Auburn Muni", 47.3276, -122.2267, 4500)
    assert m["category"] is None and m["category_color"] is None and m["ceiling_ft"] is None
    assert m["wind"]["speed_kt"] is None and m["wind"]["calm"] is False
    assert m["visibility_sm"] is None and m["temp_c"] is None and m["altimeter_hpa"] is None


def test_ceiling_is_shown_only_when_the_report_settles_it():
    for body, ceiling in (("FEW025 BKN///", None), ("BKN012 //////TCU", None), ("FEW020 SCT180", None), ("BKN030 OVC050", 3000)):
        rec, obs = gw.parse_station_file(f"2026/11/13 22:00\nRJBE 132200Z 06007KT 9999 {body} 15/12 Q1020\n", "RJBE")
        assert gw.to_metar(rec, obs, "", 0.0, 0.0, 4500)["ceiling_ft"] == ceiling, body


# ---- the gateway source -------------------------------------------------------------------

class Airport:
    def __init__(self, icao, name="", lat=47.3276, lon=-122.2267):
        self.icao, self.name, self.lat, self.lon = icao, name, lat, lon


class Reply:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Gateway:
    """A stand-in for the gateway: station files by identifier, a reply or an error."""

    def __init__(self, files):
        self.files = files
        self.asked = []

    async def get(self, url, timeout=None):
        icao = url.rsplit("/", 1)[1].split(".")[0]
        self.asked.append(icao)
        reply = self.files.get(icao, Reply(404, "<html>404 Not Found</html>"))
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def station_file(report_after_time, age_s=600, icao="KS50"):
    """A station file for a report observed age_s ago: the gateway's date line and the
    report with its day and time group set to that moment."""
    t = time.gmtime(time.time() - age_s)
    return Reply(200, f"{time.strftime('%Y/%m/%d %H:%M', t)}\n{icao} {time.strftime('%d%H%M', t)}Z {report_after_time}\n")


RAIN = KS50_RAIN.split(" ", 2)[2]


def test_gateway_gives_none_for_an_unknown_station_and_for_a_report_past_the_age_limit(monkeypatch):
    src = gw.GatewaySource(Gateway({"KS50": station_file(RAIN, age_s=gw.MAX_AGE_S + 3600)}))
    assert asyncio.run(src.fetch_one(Airport("KXXX"), 4500)) is None
    assert asyncio.run(src.fetch_one(Airport("KS50"), 4500)) is None
    fresh = gw.GatewaySource(Gateway({"KS50": station_file(RAIN, age_s=gw.MAX_AGE_S - 3600)}))
    m = asyncio.run(fresh.fetch_one(Airport("KS50"), 4500))
    assert m["icao"] == "KS50" and m["stale"] is True


def test_gateway_retries_a_transient_error_once_and_raises_other_failures(monkeypatch):
    import httpx

    monkeypatch.setattr(gw, "RETRY_DELAY_S", 0)
    src = gw.GatewaySource(Gateway({"KS50": [httpx.ConnectError("name resolution"), station_file(RAIN)]}))
    assert asyncio.run(src.fetch_one(Airport("KS50"), 4500))["icao"] == "KS50"
    assert src.client.asked == ["KS50", "KS50"]
    down = gw.GatewaySource(Gateway({"KS50": Reply(503)}))
    with pytest.raises(RuntimeError):
        asyncio.run(down.fetch_one(Airport("KS50"), 4500))


def test_one_station_failing_does_not_keep_the_others_from_being_read():
    src = gw.GatewaySource(Gateway({"KS50": station_file(RAIN), "K1L8": Reply(503), "K68S": station_file("AUTO 00000KT 10SM CLR 05/01 A3010 RMK AO2", icao="K68S")}))
    found, error = asyncio.run(src.fetch([Airport("KS50"), Airport("K1L8"), Airport("KXXX"), Airport("K68S")], 4500))
    assert sorted(found) == ["K68S", "KS50"] and error == "K1L8: HTTP 503"
    assert found["K68S"]["category"] == "VFR" and found["KS50"]["category"] == "IFR"


# ---- the fallback in the weather refresh --------------------------------------------------

def with_ks50():
    cfg = base_cfg()
    cfg["airports"].append({"icao": "KS50", "name": "Auburn Muni", "lat": 47.3276, "lon": -122.2267, "local_radius_mi": 5, "enabled": True})
    return cfg


def serves(*icaos, calls=None):
    async def fetch(asked, stale):
        if calls is not None:
            calls.append(sorted(asked))
        return {i: {"icao": i, "obs_time": time.time(), "wind": {}, "lat": 47.45, "lon": -122.31} for i in icaos if i in asked}

    return fetch


def weather(client, icao):
    return client.get("/api/weather", params={"ids": icao}).json()["metars"].get(icao)


@pytest.fixture()
def manager(client, monkeypatch):
    m = main_mod.manager
    monkeypatch.setattr(m, "_weather_cache", {})
    monkeypatch.setattr(m, "_gateway_error", None)
    return m


def test_airport_aviationweather_lacks_is_read_from_the_gateway(client, manager, monkeypatch):
    gateway = Gateway({"KS50": station_file(RAIN)})
    monkeypatch.setattr(manager.metar, "fetch", serves("KSEA"))
    monkeypatch.setattr(manager.gateway, "client", gateway)
    assert put(client, with_ks50()).status_code == 200
    assert wait_until(lambda: weather(client, "KS50"), 6)
    m = weather(client, "KS50")
    assert m["source"] == "nws" and m["name"] == "Auburn Muni" and m["category"] == "IFR"
    assert m["visibility_sm"] == 2.5 and m["ceiling_ft"] == 7000 and m["wind"]["calm"] is True
    assert 500 <= m["age_s"] <= 700 and m["stale"] is False
    assert weather(client, "KSEA").get("source") is None
    assert set(gateway.asked) == {"KS50"}  # the airport aviationweather.gov answered for is not asked
    assert client.get("/api/status").json()["weather"]["gateway"] == {"stations": ["KS50"], "error": None}


def test_gateway_is_not_asked_when_aviationweather_serves_every_airport(client, manager, monkeypatch):
    gateway = Gateway({})
    calls = []
    monkeypatch.setattr(manager.metar, "fetch", serves("KSEA", "KS50", calls=calls))
    monkeypatch.setattr(manager.gateway, "client", gateway)
    assert put(client, with_ks50()).status_code == 200
    assert wait_until(lambda: weather(client, "KS50") and calls, 6)
    assert gateway.asked == [] and weather(client, "KS50").get("source") is None


def test_older_gateway_report_does_not_replace_a_newer_cached_one(client, manager, monkeypatch):
    newer = {"icao": "KS50", "obs_time": time.time() - 300, "wind": {}, "raw": "newer"}
    calls = []
    gateway = Gateway({"KS50": station_file(RAIN, age_s=1500)})
    monkeypatch.setattr(manager.metar, "fetch", serves("KSEA", calls=calls))
    monkeypatch.setattr(manager.gateway, "client", gateway)
    manager._weather_cache["KS50"] = newer
    assert put(client, with_ks50()).status_code == 200
    assert wait_until(lambda: gateway.asked and weather(client, "KSEA"), 6)
    assert weather(client, "KS50")["raw"] == "newer"


def test_failing_gateway_keeps_the_cached_report_and_is_logged_once(client, manager, monkeypatch, caplog):
    gateway = Gateway({"KS50": station_file(RAIN)})
    monkeypatch.setattr(manager.metar, "fetch", serves("KSEA"))
    monkeypatch.setattr(manager.gateway, "client", gateway)
    assert put(client, with_ks50()).status_code == 200
    assert wait_until(lambda: weather(client, "KS50"), 6)
    gateway.files["KS50"] = Reply(503)
    with caplog.at_level(logging.INFO, logger="hangar"):
        for n in (2, 3):
            before = len(gateway.asked)
            assert put(client, with_ks50()).status_code == 200  # a config save wakes the weather loop
            assert wait_until(lambda: len(gateway.asked) > before, 6)
        assert wait_until(lambda: client.get("/api/status").json()["weather"]["gateway"]["error"] == "KS50: HTTP 503", 6)
        assert weather(client, "KS50")["category"] == "IFR"  # the last report stays
        gateway.files["KS50"] = station_file(RAIN, age_s=60)
        assert put(client, with_ks50()).status_code == 200
        assert wait_until(lambda: client.get("/api/status").json()["weather"]["gateway"]["error"] is None, 6)
    failed = [r for r in caplog.records if "gateway weather fetch failed" in r.getMessage()]
    recovered = [r for r in caplog.records if "gateway weather fetch recovered" in r.getMessage()]
    assert len(failed) == 1 and len(recovered) == 1


def test_gateway_airports_refresh_while_aviationweather_is_down(client, manager, monkeypatch):
    from app import manager as mg

    monkeypatch.setattr(mg, "WEATHER_RETRY_S", 0.2)
    gateway = Gateway({"KS50": station_file(RAIN, age_s=1800)})
    monkeypatch.setattr(manager.metar, "fetch", serves("KSEA"))
    monkeypatch.setattr(manager.gateway, "client", gateway)
    assert put(client, with_ks50()).status_code == 200
    assert wait_until(lambda: weather(client, "KS50"), 6)
    first = weather(client, "KS50")["obs_time"]
    refreshed_at = client.get("/api/status").json()["weather"]["last_refresh_ts"]

    async def down(asked, stale):
        raise OSError("[Errno -3] Temporary failure in name resolution")

    monkeypatch.setattr(manager.metar, "fetch", down)
    gateway.files["KS50"] = station_file(RAIN, age_s=120)
    assert put(client, with_ks50()).status_code == 200
    assert wait_until(lambda: weather(client, "KS50")["obs_time"] > first, 6)
    status = client.get("/api/status").json()["weather"]
    assert "name resolution" in status["error"] and status["last_refresh_ts"] == refreshed_at
    assert "KSEA" not in gateway.asked  # an airport aviationweather.gov serves waits for it


def test_station_sets_include_a_gateway_airport_inside_their_area(client, manager, monkeypatch):
    async def bbox(*a):
        return [{"icao": "KSEA", "lat": 47.45, "lon": -122.31, "obs_time": time.time(), "wind": {}}]

    monkeypatch.setattr(manager.metar, "fetch_bbox", bbox)
    monkeypatch.setattr(manager.metar, "fetch", serves("KSEA"))
    monkeypatch.setattr(manager.gateway, "client", Gateway({"KS50": station_file(RAIN)}))
    assert put(client, with_ks50()).status_code == 200
    assert wait_until(lambda: weather(client, "KS50"), 6)
    around = {"min_lat": 47.1, "min_lon": -122.6, "max_lat": 47.6, "max_lon": -122.0}
    north = {"min_lat": 47.4, "min_lon": -122.6, "max_lat": 47.9, "max_lon": -122.0}
    stations = client.get("/api/weather/bbox", params=around).json()["stations"]
    assert [s["icao"] for s in stations] == ["KSEA", "KS50"] and stations[1]["category"] == "IFR" and "age_s" in stations[1]
    assert [s["icao"] for s in client.get("/api/weather/bbox", params=north).json()["stations"]] == ["KSEA"]
    near = client.get("/api/weather/area", params={"lat": 47.33, "lon": -122.23, "radius_nm": 15}).json()["stations"]
    far = client.get("/api/weather/area", params={"lat": 47.6, "lon": -122.3, "radius_nm": 10}).json()["stations"]
    assert "KS50" in [s["icao"] for s in near] and "KS50" not in [s["icao"] for s in far]


def test_station_set_lists_an_airport_once_and_a_set_never_fetched_stays_empty(client, manager, monkeypatch):
    async def bbox_with_ks50(*a):
        return [{"icao": "KS50", "lat": 47.3276, "lon": -122.2267, "obs_time": time.time(), "wind": {}, "raw": "from aviationweather"}]

    async def broken(*a):
        raise OSError("[Errno -3] Temporary failure in name resolution")

    manager._weather_cache["KS50"] = {"icao": "KS50", "lat": 47.3276, "lon": -122.2267, "obs_time": time.time(), "wind": {}, "source": "nws"}
    monkeypatch.setattr(manager.metar, "fetch_bbox", bbox_with_ks50)
    box = {"min_lat": 47.11, "min_lon": -122.61, "max_lat": 47.61, "max_lon": -122.01}
    stations = client.get("/api/weather/bbox", params=box).json()["stations"]
    assert [s.get("raw") for s in stations] == ["from aviationweather"]
    monkeypatch.setattr(manager.metar, "fetch_bbox", broken)
    cold = {"min_lat": 47.12, "min_lon": -122.62, "max_lat": 47.62, "max_lon": -122.02}
    r = client.get("/api/weather/bbox", params=cold).json()
    assert r["stations"] == [] and "name resolution" in r["error"]
