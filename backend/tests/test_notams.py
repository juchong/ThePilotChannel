"""NOTAM views: selection by affected location, the time window, type
classification and order, decoding against the FAA tables, the FAA client, and
resilience. Environment and the `client` fixture come from support.py and
conftest.py; no FAA credentials are set, and tests that need the FAA client give
it a stub transport."""
import asyncio
import collections
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import httpx
import pytest
from support import AUTH, base_cfg, put, wait_until

from app import main as main_mod
from app import manager as mg
from app import notams
from app.notam_decode import (COMPASS, DATA_DIR, DAYS, KEYWORDS, NAVAIDS, PHRASES, ROUTE_KEYWORDS, SUN, TABLE_TERMS,
                              TERMS, TYPE_LABEL, TYPE_ORDER, UNIT_CODES, UNITS, WORDS, body_of, classify, decode, is_english,
                              is_word, joined_pieces, split_body, split_token, terms_for, token_roles)
from app.notam_decode import ENGLISH as DICTIONARY

NOW = 1_800_000_000  # a fixed "now" for the pure functions
H = 3600


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def feature(ident="1", location="S50", account="SEA", text="RWY 16/34 CLSD", start=-H, end=2 * H,
            issued=-2 * H, now=None, **extra):
    """One NOTAM in the FAA API's GeoJSON shape; times are offsets from `now`."""
    now = time.time() if now is None else now
    notam = {
        "id": f"NMS_ID_{ident}",
        "number": "11/012",
        "year": "2026",
        "type": "N",
        "issued": iso(now + issued),
        "lastUpdated": iso(now + issued),
        "location": location,
        "icaoLocation": "K" + location if len(location) == 3 else location,
        "effectiveStart": iso(now + start),
        "effectiveEnd": iso(now + end) if end is not None else "PERM",
        "estimated": "false",
        "text": text,
        "classification": "DOM",
        "accountId": account,
    }
    notam.update(extra)
    message = f"!{account} 11/012 {location} {text}"
    return {
        "type": "Feature",
        "properties": {"coreNOTAMData": {
            "notam": notam,
            "notamTranslation": [{"type": "LOCAL_FORMAT", "domestic_message": message}],
        }},
        "geometry": None,
    }


def record(**kw):
    return notams.normalize(feature(now=NOW, **kw))


def notams_cfg(idents=("KS50",), **over):
    body = base_cfg()
    body["notams"] = {
        "enabled": True,
        "source": "faa",
        "lookahead_h": 24,
        "locations": [{"ident": i, "name": "", "enabled": True} for i in idents],
        **over,
    }
    return body


def stub_faa(monkeypatch, by_ident):
    """Stand in for the FAA: `by_ident` maps a location to features, or to an
    exception to raise."""
    calls = []

    async def fetch(ident):
        calls.append(ident)
        result = by_ident.get(ident, [])
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(main_mod.manager.nms, "fetch", fetch)
    return calls


def shown(client, ident="KS50"):
    return client.get("/api/notams", params={"location": ident}).json()


# ---- selection by affected location ---------------------------------------------------------

def test_a_notam_is_selected_by_the_location_it_affects_not_by_its_issuer(client, monkeypatch):
    stub_faa(monkeypatch, {"KS50": [
        feature("1", location="S50", account="SEA", text="RWY 16/34 CLSD"),   # issued by SEA, affects S50
        feature("2", location="BFI", account="S50", text="TWY A CLSD"),       # issued by S50, affects BFI
    ]})
    assert put(client, notams_cfg()).status_code == 200
    assert wait_until(lambda: shown(client)["fetched"] is not None, 3)
    got = shown(client)["notams"]
    assert [(n["location"], n["accountability"]) for n in got] == [("S50", "SEA")]


def test_a_location_is_matched_under_its_domestic_and_icao_spelling():
    assert notams.query_forms("ks50") == ["KS50", "S50"]
    assert notams.query_forms("SEA") == ["SEA", "KSEA"]
    assert notams.query_forms("PANC") == ["PANC"]
    assert notams.affects(record(location="S50"), "KS50")
    assert notams.affects(record(location="ANC", icaoLocation="PANC"), "PANC")
    assert not notams.affects(record(location="BFI"), "KS50")


# ---- the FAA client ----------------------------------------------------------------------------

def faa_client(handler):
    return notams.NmsSource(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_the_api_is_asked_by_location_with_a_bearer_token_and_never_by_accountability(monkeypatch):
    monkeypatch.setenv("HANGAR_NMS_CLIENT_ID", "the-id")
    monkeypatch.setenv("HANGAR_NMS_CLIENT_SECRET", "the-secret")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/auth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": "1799"})
        location = request.url.params["location"]
        if location == "KS50":
            return httpx.Response(400, json={"status": "400", "message": "Bad Request"})
        return httpx.Response(200, json={"status": "Success", "data": {"geojson": [feature("1"), feature("2")]}})

    features = asyncio.run(faa_client(handler).fetch("KS50"))
    assert len(features) == 2
    token, *queries = seen
    assert token.headers["authorization"].startswith("Basic ") and b"grant_type=client_credentials" in token.content
    assert [q.url.params["location"] for q in queries] == ["KS50", "S50"]
    for q in queries:
        assert q.url.path == "/nmsapi/v1/notams" and "accountability" not in q.url.params
        assert q.headers["authorization"] == "Bearer tok" and q.headers["nmsresponseformat"] == "GEOJSON"


def test_a_location_the_api_knows_under_no_spelling_is_an_error(monkeypatch):
    monkeypatch.setenv("HANGAR_NMS_CLIENT_ID", "the-id")
    monkeypatch.setenv("HANGAR_NMS_CLIENT_SECRET", "the-secret")

    def handler(request):
        if request.url.path == "/v1/auth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": "1799"})
        return httpx.Response(400, json={"status": "400"})

    with pytest.raises(RuntimeError, match="does not know location KZZZ"):
        asyncio.run(faa_client(handler).fetch("KZZZ"))


def test_the_token_is_reused_and_renewed_once_when_the_api_answers_401(monkeypatch):
    monkeypatch.setenv("HANGAR_NMS_CLIENT_ID", "the-id")
    monkeypatch.setenv("HANGAR_NMS_CLIENT_SECRET", "the-secret")
    tokens, rejected = [], []

    def handler(request):
        if request.url.path == "/v1/auth/token":
            tokens.append(1)
            return httpx.Response(200, json={"access_token": f"tok{len(tokens)}", "expires_in": "1799"})
        if request.headers["authorization"] == "Bearer tok1" and len(rejected) < 1 and len(tokens) == 1 and request.url.params["location"] == "ZSE":
            rejected.append(1)
            return httpx.Response(401, json={"message": "Unauthorized"})
        return httpx.Response(200, json={"data": {"geojson": []}})

    async def run():
        source = faa_client(handler)
        await source.fetch("PANC")      # one token, reused below
        await source.fetch("PAFA")
        assert len(tokens) == 1
        await source.fetch("ZSE")       # 401 once: renewed and retried
        assert len(tokens) == 2

    asyncio.run(run())


def test_missing_credentials_are_reported_without_a_request():
    def handler(request):
        raise AssertionError("no request may be sent without credentials")

    with pytest.raises(notams.NotConfigured, match="HANGAR_NMS_CLIENT_ID"):
        asyncio.run(faa_client(handler).fetch("KSEA"))


def test_rejected_credentials_are_named_without_echoing_them(monkeypatch):
    monkeypatch.setenv("HANGAR_NMS_CLIENT_ID", "the-id")
    monkeypatch.setenv("HANGAR_NMS_CLIENT_SECRET", "the-secret")
    with pytest.raises(RuntimeError) as err:
        asyncio.run(faa_client(lambda request: httpx.Response(401, json={})).fetch("KSEA"))
    assert "rejected the credentials" in str(err.value) and "the-secret" not in str(err.value)


# ---- times and the window ------------------------------------------------------------------------

def test_times_are_the_api_times_as_utc_seconds():
    assert notams.parse_time("2025-03-17T17:08:00.000Z") == 1742231280
    assert notams.parse_time("2025-03-17T17:08:00Z") == 1742231280
    assert notams.parse_time("PERM") is None and notams.parse_time("") is None and notams.parse_time(None) is None
    r = record(start=-H, end=2 * H, issued=-3 * H, lastUpdated=iso(NOW - 600), estimated="true")
    assert (r["issued"], r["updated"], r["start"], r["end"]) == (NOW - 3 * H, NOW - 600, NOW - H, NOW + 2 * H)
    assert r["end_estimated"] is True and r["permanent"] is False


def utc(*parts) -> int:
    return int(datetime(*parts, tzinfo=timezone.utc).timestamp())


def nms_samples():
    """The FAA's own sample data for the NMS-API (tests/data/nms_samples.json)."""
    with open(os.path.join(os.path.dirname(__file__), "data", "nms_samples.json")) as fh:
        return json.load(fh)


def test_the_faa_specification_example_parses_field_for_field():
    r = notams.normalize(nms_samples()["spec_example"][0])
    assert (r["id"], r["number"], r["accountability"], r["location"], r["icao_location"]) == \
        ("NMS_ID_1234567812345678", "01/123", "XYZ", "CLT", "KCLT")
    assert (r["classification"], r["kind"], r["text"]) == ("DOM", "N", "27 RWY END ID LGT U/S")
    assert r["issued"] == r["updated"] == utc(2025, 3, 17, 17, 8)
    assert (r["start"], r["end"]) == (utc(2025, 3, 17, 17, 2), utc(2025, 9, 18, 21, 11))
    assert r["end_estimated"] is True and r["permanent"] is False and r["times_differ"] is False
    assert r["schedule"] == "DLY 1200-1230"


def test_the_faa_sample_notams_parse_in_the_formats_the_api_really_uses():
    a, b = [notams.normalize(f) for f in nms_samples()["sample_initial_load"]]
    # times given as 202508210234 / 202510012359, the number as 430 with the month only in the message
    assert (a["accountability"], a["number"], a["location"], a["type"]) == ("STL", "08/430", "8WC", "RWY")
    assert (a["start"], a["end"]) == (utc(2025, 8, 21, 2, 34), utc(2025, 10, 1, 23, 59))
    assert a["issued"] == a["updated"] == utc(2025, 8, 21, 2, 34)
    assert a["permanent"] is False and a["times_differ"] is False
    assert a["raw"] == "!STL 08/430 8WC RWY 20 RWY END ID LGT U/S 2508210234-2510012359"
    assert a["plain"]["text"] == "Runway 20 runway end identifier light unserviceable"
    assert (b["accountability"], b["number"], b["location"], b["icao_location"], b["type"]) == \
        ("BDR", "04/221", "ZBW", "KZBW", "AIRSPACE")
    assert (b["start"], b["end"], b["schedule"]) == (utc(2025, 5, 1, 11, 0), utc(2025, 11, 2, 0, 1), "DLY 1100-0001")
    assert all(notams.parse_time(c["lastUpdated"]) == utc(2025, 9, 12, 10, 21) for c in nms_samples()["checklist"]["data"]["checklist"])


def test_the_faa_sample_notams_belong_to_the_location_they_affect_not_to_their_issuer():
    a, b = [notams.normalize(f) for f in nms_samples()["sample_initial_load"]]
    assert notams.affects(a, "8WC") and not notams.affects(a, "STL") and not notams.affects(a, "KSTL")
    assert notams.affects(b, "ZBW") and notams.affects(b, "KZBW") and not notams.affects(b, "BDR")


def test_an_faa_sample_notam_is_shown_only_inside_its_validity():
    a = notams.normalize(nms_samples()["sample_initial_load"][0])   # 2025-08-21 0234Z to 2025-10-01 2359Z
    assert [n["status"] for n in notams.select([a], utc(2025, 9, 1, 0, 0), 24)] == ["active"]
    assert [n["status"] for n in notams.select([a], utc(2025, 8, 20, 12, 0), 24)] == ["upcoming"]
    assert notams.select([a], utc(2025, 8, 19, 0, 0), 24) == []      # starts beyond the look-ahead
    assert notams.select([a], utc(2025, 10, 2, 0, 0), 24) == []      # ended
    assert notams.select([a], time.time(), 24) == []


def test_every_time_format_the_faa_uses_is_read_as_the_same_instant():
    want = utc(2025, 8, 21, 2, 34)
    for text in ("2025-08-21T02:34:00.000Z", "2025-08-21T02:34:00Z", "202508210234", "2508210234", "2508210234EST", " 202508210234 "):
        assert notams.parse_time(text) == want, text
    for junk in ("PERM", "", "20250821", "2025-13-45T00:00:00Z", "202513450000", "202508212500", "tomorrow", None, 202508210234):
        assert notams.parse_time(junk) is None, junk


def with_message(f, message):
    f["properties"]["coreNOTAMData"]["notamTranslation"] = [{"type": "LOCAL_FORMAT", "simpleText": message}]
    return f


def test_time_fields_that_are_missing_come_from_the_validity_the_message_states():
    f = with_message(feature(effectiveStart="", effectiveEnd=""), "!SEA 11/012 S50 RWY 16/34 CLSD 2611081500-2611092300EST")
    r = notams.normalize(f)
    assert (r["start"], r["end"]) == (utc(2026, 11, 8, 15, 0), utc(2026, 11, 9, 23, 0))
    assert r["end_estimated"] is True and r["permanent"] is False and r["times_differ"] is False
    perm = notams.normalize(with_message(feature(effectiveStart="", effectiveEnd=""), "!SEA 11/012 S50 RWY 16/34 CLSD 2611081500-PERM"))
    assert perm["start"] == utc(2026, 11, 8, 15, 0) and perm["end"] is None and perm["permanent"] is True
    bare = notams.normalize(feature(effectiveStart="", effectiveEnd=""))    # no field and no validity in the message
    assert bare["start"] is None and bare["end"] is None and bare["permanent"] is False


def test_time_fields_that_disagree_with_the_message_are_flagged_not_silently_trusted():
    start, end = utc(2026, 11, 8, 15, 0), utc(2026, 11, 9, 23, 0)
    agree = with_message(feature(effectiveStart=iso(start), effectiveEnd=iso(end)), "!SEA 11/012 S50 RWY 16/34 CLSD 2611081500-2611092300")
    assert notams.normalize(agree)["times_differ"] is False
    later = with_message(feature(effectiveStart=iso(start), effectiveEnd=iso(end + 3600)), "!SEA 11/012 S50 RWY 16/34 CLSD 2611081500-2611092300")
    r = notams.normalize(later)
    assert r["times_differ"] is True and r["end"] == end + 3600
    perm = with_message(feature(effectiveStart=iso(start), effectiveEnd=iso(end)), "!SEA 11/012 S50 RWY 16/34 CLSD 2611081500-PERM")
    assert notams.normalize(perm)["times_differ"] is True


def test_a_notam_is_permanent_only_when_it_says_so():
    assert record(end=None)["permanent"] is True
    unstated = record(effectiveEnd="")
    assert unstated["end"] is None and unstated["permanent"] is False


def test_only_notams_in_effect_or_starting_within_the_lookahead_are_shown():
    records = [
        record(ident="active", start=-H, end=H),
        record(ident="soon", start=5 * H, end=9 * H),
        record(ident="edge", start=24 * H, end=30 * H),
        record(ident="later", start=24 * H + 60, end=30 * H),
        record(ident="expired", start=-5 * H, end=-60),
        record(ident="cancellation", type="C"),
        record(ident="cancelled", cancelationDate=iso(NOW - 60)),
        record(ident="permanent", start=-90 * 24 * H, end=None),
    ]
    got = {n["id"]: n["status"] for n in notams.select(records, NOW, 24)}
    assert got == {
        "NMS_ID_active": "active",
        "NMS_ID_soon": "upcoming",
        "NMS_ID_edge": "upcoming",
        "NMS_ID_permanent": "active",
    }
    assert [n["id"] for n in notams.select(records, NOW, 0)] == ["NMS_ID_active", "NMS_ID_permanent"]


def test_a_cached_notam_leaves_the_view_when_it_expires(client, monkeypatch):
    stub_faa(monkeypatch, {"KS50": [feature("short", end=1.5), feature("long", end=H)]})
    assert put(client, notams_cfg()).status_code == 200
    assert wait_until(lambda: shown(client)["fetched"] is not None, 3)
    assert wait_until(lambda: [n["id"] for n in shown(client)["notams"]] == ["NMS_ID_long"], 4)


# ---- type classification and order -------------------------------------------------------------

@pytest.mark.parametrize(
    "text, raw, qcode, expected",
    [
        ("RWY 16/34 CLSD", "", "", "RWY"),
        ("(U) RWY 07/25 UNSAFE ABANDONED VEHICLE", "", "", "RWY"),
        ("OBST CRANE (ASN 2026-ANM-1-NRA) 472000N1221300W 580FT NOT LGTD", "", "", "OBST"),
        ("IAP AUBURN MUNI, AUBURN, WA. RNAV (GPS) RWY 34, AMDT 1...", "", "", "IAP"),
        ("WA..AIRSPACE SEATTLE, WA..TEMPORARY FLIGHT RESTRICTIONS", "", "", "AIRSPACE"),
        ("27 END ID LGT U/S", "!SEA 11/012 S50 RWY 27 END ID LGT U/S 2611081500-2611082300", "", "RWY"),
        ("SOMETHING ELSE", "", "QMXLC", "TWY"),
        ("SOMETHING ELSE", "", "QOBCE", "OBST"),
        ("SOMETHING ELSE", "", "QNVAS", "NAV"),
        ("SOMETHING ELSE", "", "", "OTHER"),
        ("SEATTLE RWY 16L CLSD", "", "", "OTHER"),
    ],
)
def test_the_type_is_the_keyword_then_the_message_then_the_q_code(text, raw, qcode, expected):
    assert classify(text, raw, qcode) == expected


def test_notams_are_grouped_by_type_with_obstructions_last():
    records = [
        record(ident="crane", text="OBST CRANE 580FT NOT LGTD"),
        record(ident="approach", text="IAP AUBURN MUNI, AUBURN, WA. RNAV (GPS) RWY 34, AMDT 1..."),
        record(ident="taxiway", text="TWY A CLSD"),
        record(ident="runway-new", text="RWY 16/34 CLSD", issued=-H),
        record(ident="runway-old", text="RWY 16/34 WIP", issued=-9 * H),
        record(ident="runway-next", text="RWY 16/34 CLSD", start=3 * H, end=5 * H),
        record(ident="airport", text="AD AP CLSD"),
        record(ident="vor", text="NAV VOR U/S"),
    ]
    order = [n["id"].removeprefix("NMS_ID_") for n in notams.select(records, NOW, 24)]
    assert order == ["airport", "runway-new", "runway-old", "runway-next", "taxiway", "vor", "approach", "crane"]
    assert max(TYPE_ORDER, key=TYPE_ORDER.get) == "OBST"


# ---- decoding: every rule and every rendering comes from an FAA document ---------------------

def faa_examples(kind="examples"):
    """The example NOTAMs the FAA prints (tests/data/faa_examples.json): bodies
    from Order 7930.2, whole messages from the AIM keyword table."""
    with open(os.path.join(os.path.dirname(__file__), "data", "faa_examples.json")) as fh:
        return json.load(fh)[kind]


def table():
    with open(f"{DATA_DIR}/notam_terms.json") as fh:
        return json.load(fh)


def _plain(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower().replace("centre", "center").replace("metre", "meter"))


def sources():
    """What the generator read beside the FAA tables (tests/data/notam_sources.json):
    the state AIPs' lists of ICAO Doc 8400 abbreviations, the FAA AIP's list, and
    what the FAA's NOTAM documents write out."""
    with open(os.path.join(os.path.dirname(__file__), "data", "notam_sources.json")) as fh:
        return json.load(fh)


SIGNAL = re.compile(r"procedure signal|used to precede")


def forms_of(definition: str) -> set:
    """The wordings a definition lists, each reduced to its letters: the whole of
    it and each alternative, remarks in brackets left out."""
    bare = re.sub(r"\([^()]*\)|\[[^\]]*\]", " ", definition)
    return {_plain(part) for part in [bare] + re.split(r"\s+or\s+|[,;/]", bare)} - {""}


def related(first: str, second: str) -> bool:
    """Two definitions that are not different things: a wording in common, or a
    word of four letters or more."""
    words = [set(re.findall(r"[a-z]{4,}", d.lower().replace("centre", "center").replace("metre", "meter"))) for d in (first, second)]
    return bool(forms_of(first) & forms_of(second) or words[0] & words[1])


def test_every_spelled_out_contraction_restates_faa_text_it_cites():
    t = table()
    assert len(t["terms"]) > 150
    unsourced = []
    for group in ("terms", "phrases", "days", "compass", "sun", "runway_reports"):
        for term, entry in t[group].items():
            cited = [d for _where, d in entry.get("faa", [])] + [d for _usage, d in entry.get("jo_7340_2", [])]
            # X before the rest of a word: the Order's "X = Cross" and a word the FAA itself writes
            crossed = (entry.get("prefix") == "X" and "Cross" in cited and entry["text"] in WORDS
                       and term == "X" + entry["text"][len("cross"):].upper())
            # a name the FAA table writes out word by word: every word is the table's own or a cited definition's
            built = entry.get("built") and all(
                w.upper() in term.split() or any(_plain(w) in _plain(d) for d in cited) for w in entry["text"].split())
            if not entry.get("faa") or not (crossed or built or any(_plain(entry["text"]) in _plain(d) for d in cited)):
                unsourced.append(term)
    for unit, entry in t["units"].items():
        if not entry.get("faa") or not any(_plain(entry[form])[:4] in _plain(entry["faa"][0][1]) for form in ("singular", "plural")):
            unsourced.append(unit)
    assert unsourced == []


SOURCE = re.compile(
    r"AIM contractions table|JO 7930\.2 (?:TBL 3-3-1|TBL 5-5-1|TBL 6-1-1|appendix A|appendix C)|JO 7340\.2 \(ICAO usage\)(?:, X)?"
    r"|ICAO Doc 8400 \(AIP [A-Za-z ]+ GEN 2\.2(?:, AIP [A-Za-z ]+ GEN 2\.2)*\)|FAA AIP GEN 2\.2"
    r"|the FAA's NOTAM documents, written out")


def test_the_contraction_tables_come_from_the_documents_the_faa_names():
    """Order 7930.2 para 3-3-1 names Order 7340.2 and ICAO Doc 8400; the NOTAM
    tables, the FAA AIP and the FAA's own NOTAM text are the FAA's."""
    t = table()
    assert [term for term, entry in t["terms"].items() if not SOURCE.fullmatch(entry["faa"][0][0])] == []
    assert {entry["faa"][0][0].split(" (")[0].split(",")[0] for entry in t["terms"].values()} >= {
        "AIM contractions table", "JO 7340.2", "ICAO Doc 8400", "FAA AIP GEN 2.2", "the FAA's NOTAM documents"}
    # no English word of the FAA's own prose is treated as a contraction
    assert [term for term in t["terms"] if term.lower() in WORDS] == []
    assert set(UNIT_CODES) == set(t["units"])
    assert TYPE_LABEL == {**t["keywords"], "OTHER": "Other"} and set(t["keywords"]) == KEYWORDS


def test_the_rendering_of_every_contraction_is_one_of_the_wordings_its_sources_give():
    """What the decoder writes for a contraction is a wording the answer check
    accepts for it, also where the source's only wording is an -ing form (FLUC)."""
    t = table()
    for term, entry in t["terms"].items():
        assert entry["text"].lower() in [a.lower() for a in entry["accept"]], term
        assert not set(a.lower() for a in entry["accept"]) & set(p.lower() for p in entry.get("progressive", [])), term
    assert t["terms"]["FLUC"]["text"] == "fluctuating"


def test_a_contraction_the_faa_documents_do_not_settle_is_left_as_written():
    t = table()
    assert not set(t["undecoded"]) & set(TERMS)
    for term, entry in t["undecoded"].items():
        assert entry["reason"] and entry["faa"]
        if term.isalpha() and not is_word(term) and len(term) > 2:
            written = term.capitalize() if is_english(term) else term      # an English word spelled in full reads as that word
            assert decode(f"SVC {term} U/S") == f"{written} unserviceable", term


def test_every_faa_example_is_typed_by_its_keyword():
    bodies, messages = faa_examples(), faa_examples("aim_examples")
    assert len(bodies) > 300 and len(messages) >= 20
    assert [e for e in bodies if classify(e) != re.match(r"(?:\(U\) )?(\w+)", e).group(1)] == []
    for message in messages:
        first = body_of(message).split()[0]
        assert classify("", message) == (first if first in KEYWORDS else "OTHER"), message
    assert {classify("", m) for m in messages} >= {
        "RWY", "TWY", "APRON", "AD", "OBST", "NAV", "COM", "SVC", "AIRSPACE", "ODP", "SID", "STAR", "IAP", "VFP",
        "ROUTE", "SPECIAL"}


def test_decoding_an_faa_example_never_adds_drops_or_changes_a_number():
    number = re.compile(r"\d+(?:\.\d+)?|\.\d+")
    changed = [e for e in faa_examples() + faa_examples("aim_examples")
               if sorted(number.findall(body_of(e))) != sorted(number.findall(decode(e)))]
    assert changed == []


def test_decoding_an_faa_example_keeps_every_runway_and_taxiway_designator():
    designator = re.compile(r"(?:RWY (\d{1,2}[LRC]?(?:/\d{1,2}[LRC]?)*)|TWY ([A-Z]{1,2}\d{0,2}))(?=[ ,]|$)")
    lost = []
    for e in faa_examples() + faa_examples("aim_examples"):
        out = decode(e)
        lost += [(d, e) for d in (a or b for a, b in designator.findall(body_of(e)))
                 if not re.search(rf"(?<![A-Za-z0-9]){re.escape(d)}(?![A-Za-z0-9])", out)]
    assert lost == []


def test_decoding_changes_a_token_only_by_an_faa_rule():
    """Every token of every FAA example either survives as written (apart from
    letter case) or is replaced by text from the generated table."""
    renderings = {r.lower() for r in list(TERMS.values()) + list(PHRASES.values()) + list(DAYS.values())}
    renderings |= {form.lower() for entry in table()["terms"].values() for form in entry.get("forms", {})}      # the FAA's other forms
    allowed = set()
    for r in renderings | {"through", "to", "by", "unverified"} | set(COMPASS.values()) | set(SUN.values()):
        allowed.update(re.findall(r"[a-z]+", r.lower()))
    for forms in UNITS.values():
        for form in forms:
            allowed.update(form.split())
    for term in ("RWY", "TWY", "TXL"):      # written with a plural S in an FDC example ("RWYS 17C/R, 18L/R")
        allowed.add(TERMS[term] + "s")
    strangers = []
    for e in faa_examples() + faa_examples("aim_examples"):
        source = set(re.findall(r"[a-z]+", body_of(e).lower()))
        for word in re.findall(r"[A-Za-z]+", decode(e)):
            if word.lower() not in source and word.lower() not in allowed:
                strangers.append((word, e))
    assert strangers == []


@pytest.mark.parametrize(
    "text, expected",
    [
        ("TWY C CLSD", "Taxiway C closed"),
        ("TWY B3, C CLSD", "Taxiway B3, C closed"),
        ("TWY B CL LGT U/S", "Taxiway B centerline light unserviceable"),
        ("RWY 03L/21R CLSD", "Runway 03L/21R closed"),
        ("TWY SE, NE CLSD", "Taxiway SE, NE closed"),
        ("TWY WB BTN TWY SE AND RWY 13/31 CLSD", "Taxiway WB between taxiway SE and runway 13/31 closed"),
        ("RWY 13/31 NE 500FT CLSD", "Runway 13/31 north-east 500 feet closed"),
        ("TWY C BTN APCH END RWY 04R AND APCH END RWY 04L CLSD",
         "Taxiway C between approach end runway 04R and approach end runway 04L closed"),
    ],
)
def test_a_designator_is_never_read_as_a_contraction(text, expected):
    assert decode(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("RWY 16/34 CLSD TO ACFT WINGSPAN MORE THAN 70FT AND TAIL HGT MORE THAN 49FT",
         "Runway 16/34 closed to aircraft wingspan more than 70 feet and tail height more than 49 feet"),
        ("NAV VOR 010-035 BEYOND 35NM SFC-2000FT UNUSABLE",
         "VHF omni-directional radio range 010-035 beyond 35 nautical miles surface to 2000 feet unusable"),
        ("AD AP CLSD EXC PPR MON-FRI 0330-1430",
         "Airport closed except prior permission required Monday through Friday 0330-1430"),
        ("AD AP CLSD EXC 1HR PPR", "Airport closed except 1 hour prior permission required"),
        ("AD AP CLSD DLY SS-SR", "Airport closed daily sunset to sunrise"),
        ("TWY A FICON 1/2IN WET SN", "Taxiway A field condition 1/2 inch wet snow"),
        ("NAV ILS RWY 25 LOC UNUSABLE WI .5NM",
         "Instrument landing system runway 25 localizer unusable within .5 nautical miles"),
        ("RWY 13/31 E 500FT CLSD", "Runway 13/31 east 500 feet closed"),
        ("RWY 08/26 CHANGED TO 10000FT X 150FT", "Runway 08/26 changed to 10000 feet by 150 feet"),
        ("(U) RWY 07/25 UNSAFE", "Unverified: Runway 07/25 unsafe"),
        ("!SEA 11/012 S50 RWY 16/34 CLSD 2611081500-2611082300", "Runway 16/34 closed"),
        ("!BNA BNA RWY 18/36 CLSD", "Runway 18/36 closed"),
        ("RWY 01 DECLARED DIST: TORA 6827FT TODA 6827FT ASDA 6527FT LDA 6527FT.",
         "Runway 01 declared distance: take-off run available 6827 feet take-off distance available 6827 feet "
         "accelerate-stop distance available 6527 feet landing distance available 6527 feet."),
    ],
)
def test_contractions_units_levels_and_days_decode_in_the_faas_words(text, expected):
    assert decode(text) == expected


def test_phrases_the_faa_defines_as_a_whole_decode_as_a_whole():
    # Order 7930.2: NOT MNT for an unmonitored facility; TBL 5-5-1 PRECISION RWY MNT; TBL 6-1-1 LGT OUT TRG
    assert decode("NAV ILS RWY 32 NOT MNT DLY 0200-0900") == "Instrument landing system runway 32 not monitored daily 0200-0900"
    assert decode("SVC PRECISION RWY MNT U/S") == "Precision runway monitor unserviceable"
    assert decode("AIRSPACE LGT OUT TRG WI CLASS D SFC AREA") == "Lights-out training within class D surface area"


# ---- nothing the FAA defines for NOTAMs is left out, and no location is read as a word ---------------
#
# Order 7930.2 para 3-3-1: "Contractions and abbreviations designated for ICAO usage as specified in
# FAA Order JO 7340.2, Contractions ... must be used in the NOTAM system."

def order_table():
    with open(f"{DATA_DIR}/faa_contractions.json") as fh:
        return json.load(fh)["terms"]


def example_tokens():
    """(keyword, token, role) for every token of every FAA example NOTAM."""
    for e in faa_examples() + faa_examples("aim_examples"):
        _u, keyword, tokens = split_body(body_of(e))
        for token, role in zip(tokens, token_roles(tokens, (), keyword)):
            yield keyword, token, role


# Contractions left as written because their definition is a description, a sentence, a code table
# entry, a Q-code signal or the name of a message, not words to read in the contraction's place
# (ORD "order, indication of"; QNH; UTC; MU "friction value representing ...").
DESCRIBED = frozenset(
    "AIRMET ANC ARINC ARQ ARS ASHTAM ATP BARRICADED BCFG BRF CAVOK CIT DENEB DETRESFA FBL FOR/AT GAMET HYR IMI INCERFA "
    "LR MAS MIFG MSR MU NEG ORD QDL QDM QDR QFE QFU QGE QJH QNH QSP QTA QTE QTF QUJ RRA RTD RTF SITA SNOWTAM TAF UTC "
    "VOLMET VOR/DME WAC XX".split())
# One source gives two different things (REP: report, or reporting point; OCA: obstacle clearance
# altitude, or oceanic control area; GEO: geographic, or true; APV: approve, or an approach procedure).
TWO_MEANINGS = frozenset("ANS APV CFM CNL GEO OCA RAG REP RPT SPECI VAR".split())
# The FAA's NOTAM documents write the abbreviation out in other words than the ICAO list
# (STAR: standard terminal arrival; AIM: Aeronautical Information Manual).
WRITTEN_OTHERWISE = frozenset("AIM AIS STAR".split())
# Known from the FAA's own text, its AIP or ICAO Doc 8400 alone, while Order 7340.2 gives another meaning under
# a general or ATC usage, which U.S. text may use (ASR: Antenna Structure Registration in an obstruction NOTAM,
# airport surveillance radar in the Order; OTS: ICAO's organized track system, or out of service).
OTHER_IN_THE_ORDER = frozenset("ADF ADR AFTN APR ARAC ARO ASR ATA CCA DCS HAPI LRG MOD NAT OTS RCLR RON UNMON".split())
# In the AIM table only, with no ICAO list agreeing and no FAA NOTAM text showing it.
AIM_ALONE = frozenset("RX TX".split())

ENGLISH = "the contraction is also an English word in the FAA's prose"
NOT_NAMED = "two letters, and no FAA document names it for NOTAM or ICAO usage"
EITHER = "two letters, and another meaning is on record: it could be either"


def as_text_in_examples() -> set:
    return {piece for _k, token, role in example_tokens() if role == "text" for piece in split_token(token)[1].split("/")}


def on_record(term: str, s: dict, order: dict) -> list:
    """Every definition any publication gives for a contraction."""
    rows = [d for _usage, d in order.get(term, [])]
    rows += [d for entries in s["doc8400"].values() for d in entries.get(term, [])]
    rows += [s["faa_aip"][term]] if term in s["faa_aip"] else []
    rows += s["written_out"].get(term, [])
    return rows


def test_every_icao_contraction_of_the_order_is_spelled_out_or_left_for_a_reason_on_record():
    """Order 7930.2 para 3-3-1: the contractions Order 7340.2 marks for ICAO usage
    must be used in the NOTAM system. Each is spelled out in the Order's own
    words, or recorded with the reason it is left as written; a row that is a
    telegraphy signal is no NOTAM text."""
    order, t = order_table(), table()
    count, missing, wrong = 0, [], []
    for term, defs in order.items():
        icao = [d for usage, d in defs if usage == "ICAO" and not SIGNAL.search(d)]
        if not icao or len(term) < 2 or not re.fullmatch(r"[A-Z][A-Z/]*", term) or term in DAYS or term in UNIT_CODES or term in SUN:
            continue
        count += 1
        if term in TERMS:
            listed = t["terms"][term].get("listed_in")      # a NOTAM table's own wording takes precedence
            if not listed and not any(_plain(TERMS[term]) in _plain(d) for d in icao) \
                    and not t["terms"][term]["faa"][0][0].startswith("ICAO Doc 8400"):
                wrong.append(term)
        elif term not in t["undecoded"]:
            missing.append(term)
    assert count > 650 and missing == [] and wrong == []
    assert sum(1 for term in TERMS if any(usage == "ICAO" for usage, _d in order.get(term, []))) > 560
    # a signal of telegraphy is never read into a NOTAM: DE is "from" only before a call sign
    signals = {term for term, defs in order.items() if defs and all(SIGNAL.search(d) for _u, d in defs)}
    assert "DE" in signals and not signals & set(TERMS)
    assert decode("OBST CRANE (ORLA DA PRAIA DE BOA VIAGEM) 120FT") == "Crane (ORLA DA PRAIA DE BOA VIAGEM) 120 feet"


def test_what_is_taken_from_icao_doc_8400_is_given_by_two_publications_letter_for_letter():
    """Order 7930.2 para 3-3-1 names ICAO Doc 8400 beside Order 7340.2. The state
    AIPs that list its abbreviations carry misprints and national wordings of
    their own, so a wording is used only where two publications print it."""
    s, t, order = sources(), table(), order_table()
    assert len(s["doc8400"]) >= 3 and all(len(entries) > 250 for entries in s["doc8400"].values())
    taken = {term: entry for term, entry in t["terms"].items() if entry["faa"][0][0].startswith("ICAO Doc 8400")}
    assert len(taken) > 180
    # where Order 7340.2 marks a contraction for ICAO usage its wording is the one used, unless that wording
    # does not spell the contraction out and Doc 8400's does (MAR: "at sea" in the Order, March in Doc 8400)
    assert {term for term in taken if any(u == "ICAO" and not SIGNAL.search(d) for u, d in order.get(term, []))} == {"MAR", "METAR"}
    for term, entry in taken.items():
        said = _plain(entry["text"])
        witnesses = [name for name, entries in s["doc8400"].items() if any(said in forms_of(d) for d in entries.get(term, []))]
        witnesses += ["JO 7340.2"] if any(said in forms_of(d) for _usage, d in order.get(term, [])) else []
        assert len(witnesses) >= 2 and witnesses[0] != "JO 7340.2", (term, entry["text"], witnesses)
    # a wording one publication alone prints is used nowhere
    alone = []
    for name, entries in s["doc8400"].items():
        for term, defs in entries.items():
            for form in {f for d in defs for f in forms_of(d)}:
                others = [n for n, e in s["doc8400"].items() if n != name and any(form in forms_of(d) for d in e.get(term, []))]
                if not others and not any(form in forms_of(d) for _u, d in order.get(term, [])) and term in taken \
                        and _plain(taken[term]["text"]) == form:
                    alone.append((name, term, form))
    assert alone == []


def test_every_abbreviation_two_publications_agree_on_is_spelled_out_or_recorded_with_its_reason():
    s, t, order = sources(), table(), order_table()
    count, missing = 0, []
    for term in sorted({term for entries in s["doc8400"].values() for term in entries}):
        if len(term) < 2 or not re.fullmatch(r"[A-Z][A-Z/]*", term) or term in DAYS or term in UNIT_CODES or term in SUN:
            continue
        defs = [(name, d) for name, entries in s["doc8400"].items() for d in entries.get(term, [])]
        signal = {f for _n, d in defs + [("", d) for _u, d in order.get(term, [])] if SIGNAL.search(d) for f in forms_of(d)}
        given = collections.Counter(f for name in s["doc8400"] for f in {f for n, d in defs if n == name for f in forms_of(d)})
        for form in {f for _u, d in order.get(term, []) for f in forms_of(d)} & set(given):
            given[form] += 1
        if not any(n >= 2 for form, n in given.items() if form not in signal):
            continue
        count += 1
        if term not in t["terms"] and term not in t["undecoded"]:
            missing.append(term)
    assert count > 800 and missing == []
    # the abbreviations Order 7340.2 lists under a general usage only, which Doc 8400 gives for NOTAMs
    for text, expected in (
        ("OBST CRANE 300FT AMSL BRG 218 MAG 6.1NM FM ARP", "Crane 300 feet above mean sea level bearing 218 magnetic 6.1 nautical miles from aerodrome reference point"),
        ("REF AIP SUP 071/19", "Reference Aeronautical Information Publication supplement 071/19"),
        ("AIRSPACE DANGER AREA ACT AS FLW", "Danger area active as following"),
        ("AD AP CLSD TIL FURTHER NOTICE BFR 1200", "Airport closed until further notice before 1200"),
    ):
        assert decode(text) == expected


def test_a_contraction_is_left_as_written_only_for_a_reason_its_sources_show():
    s, t, order = sources(), table(), order_table()
    as_text = as_text_in_examples()
    found = collections.defaultdict(set)
    for term, entry in t["undecoded"].items():
        reason, cited = entry["reason"], [d for _w, d in entry["faa"]]
        assert cited and not set(t["undecoded"]) & set(TERMS)
        of_order = any(w.startswith("JO 7930.2") for w in entry.get("listed_in", []))
        if reason == ENGLISH:
            word = term.lower()
            assert (word in s["short_words"] if len(term) <= 3 else word in WORDS or word + "s" in WORDS) \
                or any(re.search(rf"(?i)(?<![a-z]){re.escape(term)}(?![a-z])", d) for d in cited), term
        elif reason == NOT_NAMED:
            assert len(term) == 2 and not entry.get("listed_in") and term not in as_text, term
            assert not any(usage == "ICAO" and not SIGNAL.search(d) for usage, d in order.get(term, [])), term
        elif reason == EITHER:
            assert len(term) == 2 and not of_order and term not in as_text, term
            listed = {f for d in cited for f in forms_of(d)}
            assert any(not forms_of(d) & listed for d in on_record(term, s, order)), term
        elif reason.startswith("the FAA's NOTAM documents write it out as"):
            assert reason.split('"')[1] in s["written_out"][term] and term in s["notam_text"], term
            found["written otherwise"].add(term)
        elif reason == "the definition describes the contraction; it does not spell it out":
            found["described"].add(term)
        elif reason.endswith("different meanings") or reason.endswith("gives a different meaning"):
            found["two meanings"].add(term)
        elif reason == "Order 7340.2 gives it another meaning":
            assert not any(u == "ICAO" and not SIGNAL.search(d) for u, d in order.get(term, [])) and not entry.get("listed_in"), term
            assert any(u in ("GEN", "ATC") and not forms_of(cited[0]) & forms_of(d) for u, d in order.get(term, [])), term
            found["other in the Order"].add(term)
        elif reason == "only the AIM table gives this meaning, and the FAA shows it in no NOTAM of its own":
            assert entry["listed_in"] == ["AIM contractions table"] and term not in s["notam_text"], term
            found["AIM alone"].add(term)
        else:
            raise AssertionError((term, reason))
    assert found == {"described": DESCRIBED, "two meanings": TWO_MEANINGS, "written otherwise": WRITTEN_OTHERWISE,
                     "other in the Order": OTHER_IN_THE_ORDER, "AIM alone": AIM_ALONE}
    # left as written, never half read; in capitals unless it is an English word spelled in full (BARRICADED)
    for term in sorted(DESCRIBED | TWO_MEANINGS | WRITTEN_OTHERWISE | OTHER_IN_THE_ORDER | AIM_ALONE):
        if term.isalpha() and not is_word(term):
            written = term.capitalize() if is_english(term) else term
            assert decode(f"SVC {term} U/S") == f"{written} unserviceable", term


def test_a_two_letter_group_is_spelled_out_only_when_the_faa_names_it_and_nothing_else_is_on_record():
    """Two letters are a taxiway, a state, a weather code or a signal as easily
    as a contraction (Order 7340.2 para 1-2-3 on short groups). One is spelled
    out when Order 7930.2 lists it or an FAA example NOTAM writes it as text;
    otherwise only when an FAA document names it for NOTAM or ICAO usage and no
    publication gives it any other meaning."""
    s, t, order = sources(), table(), order_table()
    as_text = as_text_in_examples()
    strict = []
    for term, entry in t["terms"].items():
        if len(term) != 2 or term in as_text or any(w.startswith("JO 7930.2") for w in entry.get("listed_in", [])):
            continue
        strict.append(term)
        assert entry.get("listed_in") or any(u == "ICAO" and not SIGNAL.search(d) for u, d in order.get(term, [])), term
        for row in on_record(term, s, order):
            assert any(related(row, d) for _w, d in entry["faa"]) or any(related(row, a) for a in entry["accept"]), (term, row)
    assert {"PN", "HJ", "HN", "CK", "MT", "WB", "SB", "EB", "DH", "PT", "LM", "IM"} <= set(strict) and len(strict) >= 25
    assert decode("AD AP CLSD EXC 90MIN PN HJ") == "Airport closed except 90 minutes prior notice required sunrise to sunset"
    assert decode("NAV ILS RWY 10 IM U/S") == "Instrument landing system runway 10 inner marker unserviceable"
    # weather codes and groups with a second meaning stay as written: FL is also a flash advisory, SA is
    # sand in the AIM table and "special authorization" in "SA CAT II", NR is number or near
    for text, expected in (
        ("NAV ILS RWY 18 SA CAT II NA", "Instrument landing system runway 18 SA category II not authorized"),
        ("AIRSPACE RESTRICTED AREA ACT SFC-FL 280", "Restricted area active SFC-FL 280"),
        ("OBST TOWER LGT U/S RA FG NR 3", "Tower light unserviceable RA FG NR 3"),
    ):
        assert decode(text) == expected


def test_every_contraction_an_faa_example_writes_is_spelled_out_where_the_faa_defines_it():
    order, t = order_table(), table()
    left = collections.Counter()
    for keyword, token, role in example_tokens():
        for piece in split_token(token)[1].split("/"):
            if (role != "text" or not re.fullmatch(r"[A-Z]{2,}", piece) or piece in terms_for(keyword) or is_word(piece)
                    or piece.lower() in WORDS or piece in DAYS or piece in UNIT_CODES or piece in SUN):
                continue
            if any(usage == "ICAO" for usage, _d in order.get(piece, [])) or piece in t["terms"] or piece in t["undecoded"]:
                left[piece] += 1
    # ARINC is a company's name, which the Order describes; REP is a report or a reporting point; ASR is a tower's
    # registration here and a radar in Order 7340.2; PER is "performance" in the lists and a plain word in prose
    assert set(left) == {"ARINC", "REP", "ASR", "PER"}, left
    for text, expected in (
        ("TWY U7 HLDG PSN SIGN FOR RWY 01L/19R LGT U/S", "Taxiway U7 holding position sign for runway 01L/19R light unserviceable"),
        ("SVC AUNICOM WX BCST NOT AVBL", "Automated UNICOM weather broadcast not available"),
        ("COM CLR DELIVERY 121.7 NOW 124.125", "Clearance delivery 121.7 now 124.125"),
        ("NAV ILS RWY 18 DME U/S", "Instrument landing system runway 18 distance measuring equipment unserviceable"),
        ("AD AP CARIBOU NEAR MOV AREAS S OF TERMINAL DLY SS-SR", "Airport caribou near movement areas south of terminal daily sunset to sunrise"),
        # written in FAA examples, and defined by a source the FAA names
        ("NAV RWY 22 GLS UNUSABLE BYD 15NM", "Runway 22 GBAS Landing System unusable beyond 15 nautical miles"),
        ("NAV ILS RWY 02 GP/OM/MM U/S", "Instrument landing system runway 02 glide path/outer marker/middle marker unserviceable"),
        ("NAV VORTAC ID CHANGED TO VHP", "VOR and TACAN identifier changed to VHP"),
        ("SVC WX BCST SYSTEM DTG UNREL", "Weather broadcast system date time group unreliable"),
        ("NAV GPS PRN 16 U/S", "Global positioning system pseudo random noise 16 unserviceable"),
    ):
        assert decode(text) == expected


def test_where_the_notam_tables_differ_the_meaning_another_document_gives_is_used():
    """PRN is "Pseudo-random Navigation" in the AIM table and "Pseudo Random
    Noise" in Order 7930.2 appendix C; Order 7340.2 has the second."""
    s, t, order = sources(), table(), order_table()
    settled = {term: entry for term, entry in t["terms"].items() if entry.get("differs")}
    assert "PRN" in settled
    for term, entry in settled.items():
        backing = [d for d in on_record(term, s, order)]
        assert any(forms_of(d) & forms_of(entry["faa"][0][1]) for d in backing), term
        for _where, other in entry["differs"]:
            assert not any(forms_of(d) & forms_of(other) for d in backing), term
    # tables that differ with no third document to settle it leave the contraction as written
    for term, entry in t["undecoded"].items():
        tables_ = [d for where, d in entry["faa"] if where.startswith(("AIM", "JO 7930.2"))]
        if entry["reason"].endswith("gives a different meaning") and len(tables_) > 1:
            assert not entry.get("differs"), term


def test_the_faa_aips_abbreviations_are_used_where_the_contractions_order_gives_the_same_meaning():
    """FAA AIP GEN 2.2 lists the abbreviations of U.S. AIS publications (BYD
    beyond, ARPT airport, REIL runway end identifier lights)."""
    s, t, order = sources(), table(), order_table()
    taken = {term: entry for term, entry in t["terms"].items() if entry["faa"][0][0] == "FAA AIP GEN 2.2"}
    assert len(taken) > 40 and {"BYD", "ARPT", "REIL", "TRACON", "DALGT"} <= set(taken)
    for term, entry in taken.items():
        assert entry["faa"][0][1] == s["faa_aip"][term] and any(related(entry["faa"][0][1], d) for _u, d in order.get(term, [])), term
        # a group of fewer than four letters only where the FAA prints it in NOTAM text (Order 7340.2 para 1-2-3:
        # three-character contractions conflict with location identifiers)
        assert len(re.sub(r"[^A-Z]", "", term)) >= 4 or term in s["notam_text"], term
    missing = []
    for term, definition in s["faa_aip"].items():
        same = any(forms_of(definition) & forms_of(d) for _u, d in order.get(term, []))
        named = len(re.sub(r"[^A-Z]", "", term)) >= 4 or term in s["notam_text"]
        if same and named and len(term) >= 2 and re.fullmatch(r"[A-Z][A-Z/]*", term) and term not in DAYS \
                and term not in UNIT_CODES and term not in SUN and term not in t["terms"] and term not in t["undecoded"]:
            missing.append(term)
    assert missing == []
    assert decode("RWY 16 REIL U/S") == "Runway 16 runway end identifier lights unserviceable"
    assert decode("NAV VOR UNUSABLE VIA THE 25 TAC ARC") == "VHF omni-directional radio range unusable via the 25 TAC ARC"
    # ICAO's meaning is not read into a contraction U.S. text uses otherwise
    assert decode("NAV ILS RWY 10 OTS") == "Instrument landing system runway 10 OTS"
    assert decode("APRON RON PRKG CLSD") == "Apron RON parking closed"


# Where the FAA's example NOTAMs name a location by its identifier (Order 7930.2 formats).
LOCATION_FORMS = {
    "the centre of a radius": r"RADIUS OF (?P<id>[A-Z0-9]{3,5})\b",
    "the end of a position reference": r"\(\d+(?:\.\d+)?NM [NSEW]{1,3} (?P<id>[A-Z0-9]{3,5})\)",
    "an identifier alone in parentheses": r"\((?P<id>[A-Z]{3,4})\)",
    "the identifier before a ground station": r"(?<![A-Z/])(?P<id>[A-Z]{3}) (?:VORTAC|VOR/DME|VOR|NDB|TACAN)\b",
    "a point of an area": r"(?<= TO )(?P<id>[A-Z]{3})(?= TO )",
}


def test_a_location_identifier_is_never_read_as_a_contraction_or_a_day():
    """Every contraction and day of up to four letters, written where an FAA
    example NOTAM names a location, stays as written: SAT is San Antonio, ACT is
    Waco, INT is Winston-Salem."""
    candidates = sorted(c for c in set(TERMS) | set(DAYS) if c.isalpha() and 3 <= len(c) <= 4)
    assert len(candidates) > 400
    seen, misread = collections.Counter(), []
    for e in faa_examples() + faa_examples("aim_examples"):
        body = body_of(e)
        for form, pattern in LOCATION_FORMS.items():
            for m in re.finditer(pattern, body):
                if m.group("id") in KEYWORDS or m.group("id") in NAVAIDS or is_word(m.group("id")):
                    continue
                seen[form] += 1
                for cand in candidates:
                    if cand in NAVAIDS and form == "the identifier before a ground station":
                        continue      # "ILS DME", "LOC DME": a facility, not an identifier
                    changed = body[:m.start("id")] + cand + body[m.end("id"):]
                    out = decode(changed)
                    at = len(decode(changed[:m.start("id")]).split()) if m.start("id") else 0
                    if not re.search(rf"(?<![A-Za-z]){cand}(?![A-Za-z])", out):
                        misread.append((form, cand, changed, out, at))
    assert misread == []
    assert all(seen[form] >= 2 for form in LOCATION_FORMS), seen
    # the same letters are still read where no location is named
    assert decode("AIRSPACE CLASS D SFC AREA HR CHANGED TO ACT MON-FRI 0615-2100, SAT 0830-1700") == \
        "Class D surface area hours changed to active Monday through Friday 0615-2100, Saturday 0830-1700"
    assert decode("TWY A INT TWY B CLSD") == "Taxiway A intersection taxiway B closed"


def test_a_procedure_notam_keeps_the_contractions_of_its_charts():
    """Order 7930.2 para 3-3-1: FDC NOTAMs on approach and departure procedures use
    the contractions published on the procedure charts. There a group known only
    from Order JO 7340.2 stays as written (MAP is the missed approach point, not
    "aeronautical maps and charts"); what the NOTAM tables list is still read."""
    heading = "IAP SOMEWHERE MUNI, SOMEWHERE, WA. ILS OR LOC RWY 16, AMDT 1..."
    kept = [term for term in TERMS if term not in TABLE_TERMS and term.isalpha()]
    assert len(kept) > 300
    for term in kept:
        assert re.search(rf"(?<![A-Za-z]){term}(?![A-Za-z])", decode(f"{heading} {term} 1200 NA.")), term
        assert not re.search(rf"(?<![A-Za-z]){term}(?![A-Za-z])", decode(f"SVC {term} U/S")), term
    assert decode(f"{heading} PROCEDURE NA.") == \
        "SOMEWHERE MUNI, SOMEWHERE, WA. Instrument landing system or localizer runway 16, amendment 1... procedure not authorized."


def test_a_contraction_of_several_forms_takes_the_form_its_neighbour_calls_for():
    """REC is "receive or receiver", MOV "move, or moving, or movement": the form
    is the one the FAA's own documents write beside that neighbour."""
    assert decode("NAV VOR AIRBORNE REC CHECKPOINT U/S") == "VHF omni-directional radio range airborne receiver checkpoint unserviceable"
    assert decode("AD AP CARIBOU NEAR MOV AREAS S OF TERMINAL DLY SS-SR").startswith("Airport caribou near movement areas ")
    assert decode("COM CLR DELIVERY 121.7 U/S") == "Clearance delivery 121.7 unserviceable"
    assert "75 feet wide remainder" in decode("RWY 16 FICON 4/4/4 100 PCT COMPACTED SN PLOWED 75FT WID REMAINDER 1/2IN DRY SN")
    t = table()["terms"]
    several = {term: entry["forms"] for term, entry in t.items() if entry.get("forms")}
    assert len(several) >= 15
    for term, forms in several.items():
        assert set(forms) <= set(t[term]["accept"]) and t[term]["text"] in forms, term      # only forms the FAA lists
        for form, context in forms.items():
            # a word written after this form alone calls for it
            for word in context["after"]:
                if sum(word in other["after"] for other in forms.values()) == 1 and is_word(word.upper()) and word.upper() not in TERMS:
                    got = decode(f"SVC {term} {word.upper()} U/S")
                    assert got.lower().startswith(form.lower() + " "), (term, form, word, got)
        # with no neighbour to go by, the usual form
        assert decode(f"SVC {term} 12 U/S").lower().startswith(t[term]["text"].lower() + " 12"), term


def test_a_designator_joined_to_a_list_is_never_read_as_a_contraction():
    """Every two-letter contraction, written where a taxiway designator stands:
    after TWY or TXL, after a comma, after AND, OR or &, and beside a slash."""
    groups = sorted(term for term in TERMS if re.fullmatch(r"[A-Z]{2}", term))
    assert len(groups) > 40 and {"PN", "CK", "HJ", "MT", "WB"} <= set(groups)
    forms = ("TWY {x} CLSD", "TXL {x} CLSD", "TWY A, {x} CLSD", "TWY A AND {x} CLSD", "TWY A OR {x} CLSD", "TWY A & {x} CLSD",
             "TWY {x}/B CLSD", "TWY B/{x} CLSD", "TWY A, B AND {x} CLSD", "TWY C BTN TWY A AND {x} CLSD")
    misread = [(form, x) for x in groups for form in forms
               if not re.search(rf"(?<![A-Za-z]){x}(?![A-Za-z])", decode(form.format(x=x)))]
    assert misread == []
    # two of them joined by a slash, where each alone would be spelled out
    assert [(x, y) for x in groups for y in groups if decode(f"TWY {x}/{y} CLSD") != f"Taxiway {x}/{y} closed"] == []
    assert decode("SVC HJ/PN U/S") == "Sunrise to sunset/prior notice required unserviceable"
    # what follows the list is text again, and a word after AND is a word
    assert decode("TWY A AND CK CLSD CK FREQ") == "Taxiway A and CK closed check frequency"
    assert decode("TWY A AND ALL APRONS CLSD") == "Taxiway A and all aprons closed"
    assert decode("RWY 09 AND 27 CLSD") == "Runway 09 and 27 closed"


def test_every_unit_reads_after_a_number_and_apart_from_one_only_where_the_faa_writes_it_so():
    t, s = table(), sources()
    assert {"FT", "NM", "M", "KM", "KG", "MHZ"} <= set(t["units"])
    for unit, entry in t["units"].items():
        one, many = entry["singular"], entry["plural"]
        assert decode(f"RWY 18 CLSD 5{unit}") == f"Runway 18 closed 5 {many}", unit
        assert decode(f"RWY 18 CLSD 5 {unit}") == f"Runway 18 closed 5 {many}", unit
        assert decode(f"RWY 18 CLSD 1{unit}") == f"Runway 18 closed 1 {one}", unit
        assert decode(f"RWY 18 CLSD 1 {unit}") == f"Runway 18 closed 1 {one}", unit
        assert decode(f"RWY 18 CLSD 2.5{unit}") == f"Runway 18 closed 2.5 {many}", unit
    assert decode("OBST CRANE 63.0M AMSL (206.7 FT) 109.7MHZ SFC-300M") == \
        "Crane 63.0 meters above mean sea level (206.7 feet) 109.7 megahertz surface to 300 meters"
    # apart from a number: only the unit an FAA example NOTAM writes that way, and never one that is an English word
    apart = set()
    for e in faa_examples() + faa_examples("aim_examples"):
        tokens = e.split()
        apart |= {split_token(tok)[1] for n, tok in enumerate(tokens)
                  if split_token(tok)[1] in t["units"] and not re.search(r"\d", tokens[n - 1] if n else "")}
    assert "IN" in apart and "HR" in apart
    alone = {unit for unit, entry in t["units"].items() if entry.get("alone")}
    assert alone == {unit for unit in apart if unit.lower() not in s["short_words"]} == {"HR"}
    for unit, entry in t["units"].items():
        got = decode(f"RWY 18 CLSD {unit} CHANGED")
        want = entry["plural"] if unit in alone else (unit.lower() if is_word(unit) else unit)
        assert got == f"Runway 18 closed {want} changed", unit
    # after a token that only holds a figure, a unit is not guessed at
    assert decode("AIRSPACE AREAS MMR-101 SEC W") == "Areas MMR-101 SEC W"
    for unit in alone:
        assert decode(f"AIRSPACE AREAS MMR-101 {unit} W") == f"Areas MMR-101 {unit} W"
    # a contraction that holds a figure is left alone: the numbers of the line are the NOTAM's
    assert decode("SVC FIRE AND RESCUE HR OF SER H24") == "Fire and rescue hours of service H24"
    assert [term for group in ("terms", "undecoded") for term in t[group] if re.search(r"\d", term)] == []


def test_a_short_token_is_lower_cased_only_as_a_word_the_faa_writes_and_never_a_contraction_of_the_lists():
    s, t = sources(), table()
    short = {w for w in WORDS if len(w) <= 3}
    either = {term.lower() for term, entry in t["undecoded"].items() if entry.get("either")}
    assert short == set(s["short_words"]) - either and len(short) > 60
    assert {"do", "up", "top", "aid", "day", "it", "so", "not", "the"} <= short
    # IF is the intermediate approach fix, PER performance, MAP maps and charts, ARC the area chart in the ICAO
    # lists, and each is a plain word too: only the sentence tells, so they stay as written
    assert {"if", "per", "map", "arc"} <= either and not {word for word in either if len(word) <= 3} & set(WORDS)
    for word in sorted(short):
        token = word.upper()
        if token in DAYS or token in t["units"] or token in COMPASS or token in SUN:
            continue
        assert decode(f"RWY 18 CLSD {token} 5") == f"Runway 18 closed {word} 5", word
    for word in sorted(word for word in either if len(word) <= 3):
        assert decode(f"RWY 18 CLSD {word.upper()} 5") == f"Runway 18 closed {word.upper()} 5", word
    assert decode("NAV ILS RWY 16R NOT AVBL DO NOT USE UP TO 17NM") == \
        "Instrument landing system runway 16R not available do not use up to 17 nautical miles"
    # in the FAA's own example NOTAMs every such word is written as a word
    seen = collections.Counter()
    for keyword, token, role in example_tokens():
        core = split_token(token)[1]
        if role == "text" and core.lower() in short and core not in DAYS and core not in t["units"] and core not in COMPASS:
            seen[core] += 1
    assert len(seen) > 25 and seen["DO"] and seen["AID"]


# ---- plain English beyond the FAA's own prose, and tokens made of several pieces ------------------------
#
# A NOTAM is written in capitals, so the letters alone do not tell a word from a name, a fix or a
# contraction. A word of the FAA's prose reads in lower case; so does a word of four letters or more
# that two published English word lists (SCOWL's American and British lists) enter in lower case and
# not also as a name, unless some source lists it as a contraction or it is a word of the phonetic
# alphabet. A five-letter word may be a fix name: it stays as written in a NOTAM on a procedure or a
# route and beside a route element.

def english_tokens():
    """(keyword, tokens, roles, index) of every dictionary word in a text position of an FAA example."""
    for e in faa_examples() + faa_examples("aim_examples"):
        _u, keyword, tokens = split_body(body_of(e))
        roles = token_roles(tokens, (), keyword)
        for i, token in enumerate(tokens):
            if roles[i] == "text" and is_english(split_token(token)[1]):
                yield keyword, tokens, roles, i


def fix_names() -> set:
    """Five-letter groups of the FAA examples that are no contraction and no word (LARRN, KMART)."""
    return {core for _k, token, role in example_tokens() for core in [split_token(token)[1]]
            if role == "text" and re.fullmatch(r"[A-Z]{5}", core) and core not in TERMS and not is_word(core) and not is_english(core)}


def test_the_dictionary_words_are_ordinary_words_and_no_name_contraction_spelling_word_or_numeral():
    s, t, order = sources(), table(), order_table()
    english = s["english"]
    assert len(english["lists"]) == 2 and english["words"] > len(DICTIONARY) > 50000
    assert all(len(word) >= 4 and word.isalpha() and word.islower() for word in DICTIONARY) and not DICTIONARY & WORDS
    # a word the lists also enter with a capital is a name as well: it stays as written
    assert not DICTIONARY & set(english["also_names"]) and {"china", "japan", "turkey", "green", "march"} <= set(english["also_names"])
    for name in ("CHINA", "JAPAN", "TURKEY"):
        assert decode(f"AD AP {name} CLSD") == f"Airport {name} closed"
    # nor is a contraction of any list a plain word, whatever the list says it means
    listed = set(order) | set(t["terms"]) | set(s["faa_aip"]) | {term for entries in s["doc8400"].values() for term in entries}
    assert not {word for word in DICTIONARY if word.upper() in listed} and len(english["also_contractions"]) > 50
    assert "STAR" in t["undecoded"] and decode("SVC STAR U/S") == "STAR unserviceable"
    # the words of the phonetic alphabet name letters (AIM TBL 4-2-7): Alfa and Juliett also in their English spelling
    assert sorted({entry["letter"] for entry in t["spelling"].values()}) == [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    assert set(t["spelling"]) >= {"ALFA", "ALPHA", "BRAVO", "JULIETT", "JULIET", "XRAY", "ZULU"} and len(t["spelling"]) == 28
    assert not {word for word in DICTIONARY if word.upper() in t["spelling"]}
    for word in t["spelling"]:      # also where the FAA's prose has the word (uniform, echo, alpha)
        assert decode(f"APRON STAND {word} CLSD") == f"Apron stand {word} closed"
        assert decode(f"APRON STAND {word}/{word} CLSD") == f"Apron stand {word}/{word} closed"
    # a Roman numeral is a number
    assert not {"viii", "xiii", "xvii", "xxiii"} & DICTIONARY and decode("NAV ILS RWY 08L CAT VIII NA").count("VIII") == 1
    # a word of three letters or fewer comes from the FAA's prose alone: the letters of a location identifier spell many
    assert decode("AD AP SEA ASH CLSD") == "Airport " + ("sea" if is_word("SEA") else "SEA") + " ASH closed" and "ash" not in WORDS


def test_every_plain_word_of_an_faa_example_reads_in_lower_case_and_no_other_group_of_letters_does():
    lowered, kept = set(), set()
    fixes = fix_names()
    for keyword, tokens, roles, i in english_tokens():
        cores = [split_token(token)[1] for token in tokens]
        core = cores[i]
        beside_route = any(0 <= j < len(cores) and roles[j] == "text" and (cores[j] == "DCT" or re.fullmatch(r"[A-Z]{1,2}\d{1,4}", cores[j])
                                                                           or cores[j] in fixes) for j in (i - 1, i + 1))
        words = re.findall(r"[A-Za-z]+", decode(" ".join(tokens)))
        if len(core) == 5 and (keyword in ROUTE_KEYWORDS or beside_route):
            assert core in words, (core, tokens)
            kept.add(core)
        else:
            assert core.lower() in words or core.capitalize() in words, (core, tokens)
            lowered.add(core)
    assert {"ABANDONED", "CRACKS", "ENGINEERED", "HYDRANT", "INSIDE", "RESURFACING", "TRENCHING", "VEHICLES", "FLAGGED", "PILES"} <= lowered
    assert len(lowered) > 30
    # a group of four letters or more that is neither a word nor spelled out by the table stays as written
    names = set()
    for keyword, token, role in example_tokens():
        core = split_token(token)[1]
        if role == "text" and re.fullmatch(r"[A-Z]{4,}", core) and core not in TERMS and not is_word(core) and not is_english(core) \
                and not (core.endswith("S") and core[:-1] in TERMS):
            assert decode(f"AD AP {core} CLSD") == f"Airport {core} closed", core
            names.add(core)
    assert {"FEDEX", "WINDCONE", "EAGLECREST", "LARRN", "KMART", "MOGAS"} <= names


def test_a_five_letter_word_stays_as_written_where_it_may_be_a_fix_name():
    """Fix names have five letters. In a NOTAM on a procedure or a route, and beside
    DCT, an airway or a five-letter name, a five-letter word of the dictionaries
    stays as written; elsewhere it reads as the word."""
    five = sorted(word for word in DICTIONARY if len(word) == 5)
    fixes = sorted(fix_names())
    assert len(five) > 3000 and {"LARRN", "KMART", "PODDE", "DRUZZ", "FINGR"} <= set(fixes)
    for word in five:
        token = word.upper()
        assert decode(f"AD AP {token} NEAR RWY 01") == f"Airport {word} near runway 01", word
        assert f"cross {token} at 3000" in decode(f"IAP SOMEWHERE MUNI, SOMEWHERE, WA. ILS RWY 16, AMDT 1... CROSS {token} AT 3000."), word
        assert f" {token} to " in decode(f"ROUTE ZSE ZLC. V4 SOMEWHERE (SWR) VORTAC, WA TO ANYWHERE (AWR) VORTAC, ID VIA {token} TO BOISE MEA 9000."), word
        assert decode(f"AD AP TFC DCT {token} CLSD") == f"Airport traffic direct {token} closed", word
        assert decode(f"AD AP TFC V23 {token} CLSD") == f"Airport traffic V23 {token} closed", word
        assert decode(f"AD AP TFC {token} J501 CLSD") == f"Airport traffic {token} J501 closed", word
    for fix in fixes:
        for word in five[::40]:
            token = word.upper()
            assert decode(f"AD AP TFC {fix} {token} CLSD") == f"Airport traffic {fix} {token} closed", (fix, word)
            assert decode(f"AD AP TFC {token} {fix} CLSD") == f"Airport traffic {token} {fix} closed", (fix, word)
    # words of the FAA's own prose are words wherever they stand
    assert "cross tower at 3000" in decode("IAP SOMEWHERE MUNI, SOMEWHERE, WA. ILS RWY 16, AMDT 1... CROSS TOWER AT 3000.")


def test_a_token_of_pieces_joined_by_marks_reads_piece_by_piece_only_when_every_piece_is_settled():
    # in the FAA's own examples
    assert decode("SVC PCL RWY 18/36 MEDIUM/HIGH INTST U/S") == "Pilot controlled lighting runway 18/36 medium/high intensity unserviceable"
    assert decode("SVC AUTOMATED WX BCST SYSTEM CEILING/WIND UNREL") == "Automated weather broadcast system ceiling/wind unreliable"
    assert decode("NAV VORTAC 116.2/CH 109 COMMISSIONED") == "VOR and TACAN 116.2/channel 109 commissioned"
    assert "13L/R, original...weather minimums 3000 foot" in decode(
        "VFP JOHN F KENNEDY INTL, NEW YORK, NY. PARKWAY VISUAL RWY 13L/R, ORIG...WEATHER MINIMUMS 3000 FOOT CEILING AND 3 MILES VISIBILITY.")
    # every spelled-out contraction reads the same joined to a number, a word or another contraction
    for term, text in TERMS.items():
        if not re.fullmatch(r"[A-Z/]+", term):
            continue
        assert decode(f"AD AP 1.{term} CLSD") == f"Airport 1.{text} closed", term
        assert decode(f"AD AP (2){term} CLSD") == f"Airport (2){text} closed", term
        assert decode(f"AD AP {term},FALSE CLSD") == f"Airport {text},false closed", term
        assert decode(f"AD AP 19R-{term} CLSD") == f"Airport 19R-{text} closed", term
        # a piece nothing settles leaves the whole token as written: it is a code, not text
        for unsettled in (f"{term}/ZZZZZ", f"ZZZZZ/{term}", f"{term}/B", f"{term}.QQ7"):
            assert decode(f"AD AP {unsettled} CLSD") == f"Airport {unsettled} closed", unsettled
        # a hyphen between a letter and a figure joins a designation
        assert decode(f"AD AP {term}-2 CLSD") == f"Airport {term}-2 closed", term
    # two marks in a row join like one
    assert decode("AD MOV AREA(RWY 06/24,ALL TWY AND ALL APN)-CLSD DUE TO SN REMOVAL") == \
        "Movement area(runway 06/24,all taxiway and all apron)-closed due to snow removal"
    # words joined by hyphens that the dictionaries enter as one word read as written
    assert decode("AD AP DE-ICING NON-STANDARD HYDRO-ELECTRIC TAKE-OFF") == "Airport de-icing non-standard hydro-electric take-off"
    # RWY joined to its designator
    assert decode("AD AP ARRIVALS FOR RWY01R AND RWY19 CLSD") == "Airport arrivals for runway 01R and runway 19 closed"
    # numbers, designations, single letters and addresses stay as written
    for token in ("134.5/303-627-3001", "5/5/5", "R/B/W/T", "LDA-2", "ALSF-2", "W-181", "R-046", "2013-AGL-5-NRA", "D-ATIS", "ADS-B",
                  "CTR/C", "U.S", "WWW.FAA.GOV/AIR_TRAFFIC", "HTTPS://NOTAMS.AIM.FAA.GOV", "9-ATOR-HQ-SOSC@FAA.GOV", "RTS/PKS"):
        assert decode(f"SVC SEE {token} NOW") == f"See {token} now", token
    # and in every FAA example every figure survives where it stood
    for e in faa_examples() + faa_examples("aim_examples"):
        body = body_of(e)
        assert re.findall(r"\d+", decode(body)) == re.findall(r"\d+", " ".join(split_body(body)[2])), e
    assert joined_pieces("RMK/1.FALSE", TERMS) == [("RMK", "term", "remark"), ("/", "mark", "/"), ("1", "as written", "1"),
                                                   (".", "mark", "."), ("FALSE", "word", "false")]
    # figures joined by a mark are one fact and stay one piece
    assert joined_pieces("21-06)(INCLUDING", TERMS) == [("21-06", "as written", "21-06"), (")(", "mark", ")("), ("INCLUDING", "word", "including")]
    assert joined_pieces("01L/19R-CLSD", TERMS) == [("01L/19R", "as written", "01L/19R"), ("-", "mark", "-"), ("CLSD", "term", "closed")]
    # a point, a comma or a colon between two figures is part of a number, never a place to part the token
    assert joined_pieces("316/4.8NM", TERMS) == [("316", "as written", "316"), ("/", "mark", "/"), ("4.8NM", "unit", "4.8 nautical miles")]
    assert decode("OBST CRANE RDL 316/4.8NM AD") == "Crane radial 316/4.8 nautical miles aerodrome"
    for token in ("3,000M(10,000FT)", "1043,0M", "12:30/CLSD", "RMK/1.5"):
        assert all(written != "." and written != "," and written != ":" for written, kind, _r in joined_pieces(token, TERMS) or [] if kind == "mark"), token
    assert decode("AD AP ELEV 3,000M(10,000FT) CLSD") == "Airport elevation 3,000M(10,000FT) closed"


def test_a_single_compass_letter_after_a_distance_is_a_direction():
    assert decode("AIRSPACE UNMANNED ROCKET WI AN AREA DEFINED AS 1NM RADIUS OF 424238N0902332W (2.5NM E PVB) SFC-4500FT").count(
        "(2.5 nautical miles east PVB)") == 1
    for letter, word in COMPASS.items():
        assert decode(f"OBST TOWER 345140N0804506W (12NM {letter} PWK) 1049FT") == f"Tower 345140N0804506W (12 nautical miles {word} PWK) 1049 feet"
        assert decode(f"OBST TOWER 6 NM {letter} PWK") == f"Tower 6 nautical miles {word} PWK"
        # not after a figure that is no distance
        assert decode(f"APRON GATE 12 {letter} CLSD") == f"Apron gate 12 {letter} closed"
        assert decode(f"AD AP 250KT {letter} CLSD") == f"Airport 250 knots {letter} closed"


def test_a_surface_written_with_a_plural_s_reads_as_the_plural_and_no_other_group_does():
    assert decode("TWY D BTN TWYS A-2 AND D-1 CLSD") == "Taxiway D between taxiways A-2 and D-1 closed"
    assert "runways 17C/R, 18L/R: do not exceed" in decode(
        "SID DALLAS/FORT WORTH INTL, DALLAS, TX. PODDE THREE DEPARTURE... CHANGE NOTES TO READ: RWYS 17C/R, 18L/R: DO NOT EXCEED 240KT UNTIL LARRN.")
    assert token_roles(["RWYS", "17C,", "18L"]) == ["surface", "designator", "designator"]
    assert decode("TWYS B, C CLSD") == "Taxiways B, C closed" and decode("AD AP TXLS CLSD") == f"Airport {TERMS['TXL']}s closed"
    # a contraction with an S after it may be a contraction or a name of its own: MSAS is a satellite system, not altitudes
    t = table()
    for term in TERMS:
        token = term + "S"
        if term in ("RWY", "TWY", "TXL") or not term.isalpha() or len(term) < 3 or token in t["terms"] or token in t["undecoded"] \
                or is_word(token) or is_english(token) or token in DAYS or token in t["units"]:
            continue
        assert decode(f"AD AP {token} CLSD") == f"Airport {token} closed", token
    assert "MSA" in TERMS and decode("AD AP MSAS NOT AVBL") == "Airport MSAS not available"
    # a contraction of its own is not the plural of a shorter one
    assert "RCLS" in TERMS and "RCL" in TERMS and decode("RWY 12 RCLS U/S") == f"Runway 12 {TERMS['RCLS']} unserviceable"


def test_a_level_range_may_name_the_datum_of_its_lower_level():
    assert decode("AIRSPACE DRUM MOA ACT 500FT AGL-4999FT") == "Drum military operations area active 500 feet above ground level to 4999 feet"
    for datum in ("AGL", "MSL", "AMSL"):
        assert decode(f"AIRSPACE UAS WI AN AREA DEFINED AS 2NM RADIUS OF SYR 200FT {datum}-FL180") == \
            f"Unmanned aircraft system within an area defined as 2 nautical miles radius of SYR 200 feet {TERMS[datum]} to FL180"
        # only after a height
        assert decode(f"AD AP {datum}-4999FT CLSD") == f"Airport {datum}-4999FT closed"


def test_a_quantity_is_never_read_as_a_location_identifier():
    assert decode("AIRSPACE BLASTING WI A RADIUS OF 5NM CENTERED ON 542111N1301149W") == \
        "Blasting within A radius of 5 nautical miles centered on 542111N1301149W"
    assert decode("AIRSPACE UAS WI AN AREA DEFINED AS 5NM RADIUS OF SAT") == "Unmanned aircraft system within an area defined as 5 nautical miles radius of SAT"


def test_two_letters_after_a_comma_are_kept_as_a_state_code_only_when_they_are_one():
    assert decode("ROUTE ZAB. V12 (ABC) VORTAC, NM TO (DEF) VORTAC, WI MEA 4000").count(", NM to") == 1
    for code in ("WI", "NE", "MT", "ID", "FL", "OR", "IN", "ME", "OK"):      # within, north-east, mountain, identifier, ... elsewhere
        assert decode(f"AIRSPACE UAS WI AN AREA DEFINED AS 2NM RADIUS OF OSHKOSH, {code} SFC-400FT").count(f"OSHKOSH, {code} surface") == 1, code
    # any other two letters after a comma are read as anywhere else
    assert decode("NAV ILS RWY 25 ON TEST, DO NOT USE, IS UNRELIABLE") == "Instrument landing system runway 25 on test, do not use, is unreliable"
    assert decode("TWY A CLSD, AP OPN") == "Taxiway A closed, airport open"


def test_x_reads_by_only_between_two_dimensions():
    # the FAA's own example: each dimension is a quantity and its name
    assert decode("RWY 06/24 W 1000FT IRREGULAR SFC 4IN DPT X 12IN WID X 24FT LEN") == \
        "Runway 06/24 west 1000 feet irregular surface 4 inches depth by 12 inches wide by 24 feet length"
    assert decode("RWY 01L/19R 10301FT X 150FT CONC") == "Runway 01L/19R 10301 feet by 150 feet concrete"
    # a dimension on one side only: X is a designator or a mark of the NOTAM's own
    assert decode("AD AP RAMP X 12IN CRACKS") == "Airport ramp X 12 inches cracks"
    assert decode("AD AP CRACKS 12IN X RAMP CLSD") == "Airport cracks 12 inches X ramp closed"
    assert decode("AD AP 12IN DEEP CRACKS X 24FT CLSD") == "Airport 12 inches deep cracks X 24 feet closed"


def test_an_ellipsis_before_a_word_is_punctuation_and_a_point_may_begin_a_number():
    assert split_token("...SPECIAL") == ("...", "SPECIAL", "") and split_token(".5NM") == ("", ".5NM", "") and split_token("(U)") == ("(", "U", ")")
    assert decode("SECURITY ...SPECIAL NOTICE... PILOTS ARE REMINDED") == "...special notice... pilots are reminded"
    assert decode("NAV ILS RWY 25 LOC UNUSABLE WI .5NM") == "Instrument landing system runway 25 localizer unusable within .5 nautical miles"


def test_the_words_before_the_article_are_those_the_faa_writes_before_it():
    before = set(table()["before_article"])
    assert {"is", "within", "with", "of", "as", "for", "to", "in", "by"} <= before and len(before) > 80
    assert not {"jet", "index", "class", "group", "taxiway", "runway", "category"} & before


def test_the_letter_a_before_a_distance_is_the_article_and_a_designator_elsewhere():
    assert decode("NAV GPS MAY NOT BE AVBL WI A 276NM RADIUS CENTERED AT 332347N1142221W") == \
        "Global positioning system may not be available within a 276 nautical miles radius centered at 332347N1142221W"
    assert decode("AD AP ARFF INDEX A NOT AVBL") == f"Airport {TERMS['ARFF']} index A not available"
    assert decode("AD AP MOBILE JET A FUEL NOT AVBL") == "Airport mobile jet A fuel not available"
    assert decode("APRON RAMP E OF A CLSD") == "Apron ramp east of A closed"


def test_days_joined_by_slashes_are_each_read():
    days = sorted(DAYS)
    for first in days:
        for second in days:
            assert decode(f"AD AP CLSD {first}/{second} 1200-1400") == f"Airport closed {DAYS[first]}/{DAYS[second]} 1200-1400"
    assert decode("AD AP CLSD MON/WED/FRI 1200-1400") == "Airport closed Monday/Wednesday/Friday 1200-1400"
    # a day joined to something else is not half read
    assert decode("AD AP CLSD MON/XYZ") == "Airport closed MON/XYZ"


def test_what_opens_a_runway_report_takes_the_states_meaning_only_before_a_runway():
    """Canada's NOTAMs on runway conditions open with RSC and the runway
    (Transport Canada AC 300-019); in the ICAO list RSC is a rescue sub-centre."""
    s, t = sources(), table()
    assert set(t["runway_reports"]) == set(s["runway_reports"]) >= {"RSC"}
    for term, entry in t["runway_reports"].items():
        words, where = s["runway_reports"][term]
        assert entry == {"text": words.lower(), "faa": [[where, words]]} and _plain(words) != _plain(TERMS[term])
        for runway in ("08", "26L", "09/27"):
            assert decode(f"{term} {runway} 6/6/6 100 PCT DRY") == f"{entry['text'].capitalize()} {runway} 6/6/6 100 percent dry"
        assert decode(f"{term} 08 5/5/5 WET. {term} 26 5/5/5 WET.") == \
            f"{entry['text'].capitalize()} 08 5/5/5 wet. {entry['text'].capitalize()} 26 5/5/5 wet."
        # anywhere else it is what the lists say
        assert decode(f"SVC {term} HALIFAX U/S") == f"{TERMS[term].capitalize()} HALIFAX unserviceable"
        assert decode(f"RWY 12 {term} NOT AVBL") == f"Runway 12 {TERMS[term]} not available"


def test_a_phrase_the_faa_writes_in_words_beside_its_notam_wording_reads_in_those_words():
    """Order 7930.2 gives lighting aids and frequencies in words and, in
    parentheses, as they are written in a NOTAM: "Runway end identifier light
    (RWY END ID LGT)"."""
    s, t = sources(), table()
    assert len(s["side_by_side"]) >= 10 and "RWY END ID LGT" in s["side_by_side"]
    for phrase, words in s["side_by_side"].items():
        tokens, said = phrase.split(), words.split()
        assert len(tokens) == len(said) > 1
        for token, word in zip(tokens, said):      # each word is the token itself, or one holds the other's letters in order
            short, long_ = sorted((re.sub(r"[^a-z]", "", token.lower()), re.sub(r"[^a-z]", "", word.lower())), key=len)
            rest = iter(long_)
            assert all(letter in rest for letter in short), (phrase, word)
        assert phrase in t["phrases"]
        if t["phrases"][phrase]["faa"][0][0] == "the FAA's NOTAM documents, written out":
            assert t["phrases"][phrase]["text"] == words.lower()
        assert decode(f"SVC {phrase} U/S") == f"{t['phrases'][phrase]['text']} unserviceable".capitalize(), phrase
    assert decode("RWY 18 RWY END ID LGT U/S") == "Runway 18 runway end identifier light unserviceable"


def test_a_leading_x_reads_cross_where_the_faa_gives_the_word_and_never_alone():
    """Order JO 7340.2: X = Cross (ICAO usage). Checked over every contraction of
    the Order that begins with X and every word of the FAA's prose that begins
    with "cross"."""
    with open(f"{DATA_DIR}/faa_contractions.json") as fh:
        order = json.load(fh)["terms"]
    assert ["ICAO", "Cross"] in order["X"]
    assert decode("RWY 01/19 CLSD EXC XNG") == "Runway 01/19 closed except crossing"      # an FAA example NOTAM
    assert decode("RWY 09/27 CLSD EXC XWIND OPS") == "Runway 09/27 closed except crosswind operations"
    crossed = {term: entry["text"] for term, entry in table()["terms"].items() if term.startswith("X") and entry["text"].startswith("cross")}
    # every ICAO contraction of the Order that begins with X and means a word beginning with "cross"
    in_order = {t for t, defs in order.items() if re.fullmatch(r"X[A-Z]{2,}", t) and all(u == "ICAO" for u, _d in defs)
                and all(d.lower().startswith("cross") for _u, d in defs)}
    assert in_order and in_order <= set(crossed)
    # X before the rest of every word of the FAA's prose that begins with "cross"
    for word in WORDS:
        if word.startswith("cross") and len(word) >= len("cross") + 3:
            assert crossed["X" + word[len("cross"):].upper()] == word
    for term, word in crossed.items():
        assert word.startswith("cross") and decode(f"SVC {term} U/S") == f"{word} unserviceable".capitalize(), term
    # a contraction of the Order in which X means something else is never read as "cross"
    for term in order:
        if term.startswith("X") and term not in crossed:
            assert "cross" not in decode(f"SVC {term} U/S").lower(), term
    # X alone is not "cross": it stays as written, or is "by" between two dimensions
    assert decode("TWY A X TWY B CLSD") == "Taxiway A X taxiway B closed"
    assert decode("RWY 01L/19R 10301FT X 150FT CONC") == "Runway 01L/19R 10301 feet by 150 feet concrete"
    # a word that only begins with X is not a contraction of "cross"
    assert decode("SVC XRAY XPDR XYZ U/S") == "XRAY XPDR XYZ unserviceable"


def test_names_identifiers_state_codes_and_unsettled_contractions_stay_as_written():
    # the NOTAM's own location is not the word "sea"
    assert decode("NAV SEA VORTAC U/S", protect=("SEA",)) == "SEA VOR and TACAN unserviceable"
    # a procedure NOTAM's "AIRPORT, CITY, ST." heading is names, not contractions or words
    assert decode("IAP PORTLAND INTL, PORTLAND, OR. ILS RWY 10R NA") == \
        "PORTLAND INTL, PORTLAND, OR. Instrument landing system runway 10R not authorized"
    assert decode("VFP JOHN F KENNEDY INTL, NEW YORK, NY. PARKWAY VISUAL RWY 13L/R").startswith("JOHN F KENNEDY INTL, NEW YORK, NY. ")
    # a state code after a comma is not "taxilane", "nautical mile" or "within"
    assert decode("ROUTE ZAB ZKC. V12 PANHANDLE (PNH) VORTAC, TX TO GAGE (GAG) VORTAC, OK MOCA 5000.").count("TX to GAGE") == 1
    assert "VORTAC, NM to" in decode("ROUTE ZAB. V12 (ABC) VORTAC, NM TO (DEF) VORTAC, WI MEA 4000").replace("VOR and TACAN", "VORTAC")
    # REP is a report or a reporting point; ASR is a tower's registration or a radar
    assert decode("SVC WX REP U/S") == "Weather REP unserviceable"
    assert decode("OBST TOWER LGT (ASR 1234567) U/S") == "Tower light (ASR 1234567) unserviceable"


def test_every_sample_notam_is_a_typed_dated_record():
    records = [notams.normalize(f) for f in asyncio.run(notams.SampleSource().fetch("KS50"))]
    assert len(records) >= 12 and all(records)
    assert all(r["type"] != "OTHER" and r["location"] == "S50" and r["issued"] and r["start"] for r in records)
    assert {"RWY", "TWY", "APRON", "AD", "NAV", "COM", "SVC", "AIRSPACE", "IAP", "OBST"} <= {r["type"] for r in records}
    assert all(notams.affects(r, "KS50") for r in records)


# ---- views, API and admin ------------------------------------------------------------------------

def test_each_enabled_location_gets_a_view_after_the_other_views(client, monkeypatch):
    stub_faa(monkeypatch, {})
    body = notams_cfg(("KS50", "KSEA", "ZSE"), label="NOTAMs", dwell_s=45, lookahead_h=12)
    body["notams"]["locations"][1]["enabled"] = False
    assert put(client, body).status_code == 200
    views = client.get("/api/views").json()["views"]
    mine = [v for v in views if v["type"] == "notams"]
    assert [v["id"] for v in mine] == ["notams:KS50", "notams:ZSE"] and views[-2:] == mine
    assert mine[0] == {"id": "notams:KS50", "type": "notams", "label": "KS50 - NOTAMs", "dwell_s": 45,
                       "location": "KS50", "name": "", "lookahead_h": 12}
    assert client.get("/api/notams", params={"location": "KSEA"}).status_code == 404
    assert client.get("/api/notams", params={"location": "KZZZ"}).status_code == 404
    body["notams"]["enabled"] = False
    assert put(client, body).status_code == 200
    assert not [v for v in client.get("/api/views").json()["views"] if v["type"] == "notams"]


@pytest.mark.parametrize(
    "mutate, loc",
    [
        (lambda n: n["locations"].append({"ident": "ks50"}), ["notams"]),
        (lambda n: n["locations"].append({"ident": "TOOLONG"}), ["notams", "locations", "1", "ident"]),
        (lambda n: n["locations"].append({"ident": "K S"}), ["notams", "locations", "1", "ident"]),
        (lambda n: n.update(lookahead_h=-1), ["notams", "lookahead_h"]),
        (lambda n: n.update(refresh_s=30), ["notams", "refresh_s"]),
        (lambda n: n.update(source="scraper"), ["notams", "source"]),
    ],
)
def test_bad_notam_settings_are_rejected(client, mutate, loc):
    body = notams_cfg()
    mutate(body["notams"])
    r = put(client, body)
    assert r.status_code == 422, r.text
    assert loc in [e["loc"][: len(loc)] for e in r.json()["detail"]], r.json()


def test_checking_a_location_needs_the_admin_password_and_saves_nothing(client):
    assert put(client, base_cfg()).status_code == 200
    assert client.post("/api/notams/check", json={"ident": "KS50", "source": "sample"}).status_code == 401
    r = client.post("/api/notams/check", json={"ident": "ks50", "source": "sample"}, auth=AUTH).json()
    assert r["ok"] is True and r["asked_as"] == ["KS50", "S50"] and r["total"] >= 12 and 0 < r["shown"] < r["total"]
    r = client.post("/api/notams/check", json={"ident": "KS50", "source": "faa"}, auth=AUTH).json()
    assert r["ok"] is False and "HANGAR_NMS_CLIENT_ID" in r["error"]
    assert client.get("/api/notams", params={"location": "KS50"}).status_code == 404


def test_status_reports_notam_state_and_never_the_credentials(client, monkeypatch):
    monkeypatch.setenv("HANGAR_NMS_CLIENT_ID", "the-id")
    monkeypatch.setenv("HANGAR_NMS_CLIENT_SECRET", "the-secret")
    stub_faa(monkeypatch, {"KS50": RuntimeError("FAA NMS rejected the credentials (HTTP 401)")})
    assert put(client, notams_cfg()).status_code == 200
    assert wait_until(lambda: client.get("/api/status").json()["notams"]["error"], 3)
    status = client.get("/api/status")
    block = status.json()["notams"]
    assert block["enabled"] is True and block["source"] == "faa" and block["credentials"] is True and block["locations"] == 1
    assert "the-secret" not in status.text and "the-id" not in status.text
    assert "the-secret" not in client.get("/api/config").text


# ---- speed and reliability -------------------------------------------------------------------------

def test_the_notam_view_never_waits_on_the_faa(client, monkeypatch):
    async def hung(ident):
        await asyncio.sleep(30)

    monkeypatch.setattr(main_mod.manager.nms, "fetch", hung)
    assert put(client, notams_cfg(("KPWT",))).status_code == 200   # a location no other test has fetched
    t0 = time.time()
    r = shown(client, "KPWT")
    assert time.time() - t0 < 1.0
    assert r["pending"] is True and r["notams"] == [] and r["fetched"] is None


def state_changes(caplog):
    """The refresh loop's failed / recovered log lines in order, from the first
    failure on (the shared manager may start a test in a failed state)."""
    lines = [x.getMessage() for x in caplog.records if "NOTAM refresh" in x.getMessage()]
    changes = ["failed" if "failed" in line else "recovered" for line in lines]
    return changes[changes.index("failed"):] if "failed" in changes else []


def test_the_last_notams_survive_an_outage_which_is_logged_once(client, monkeypatch, caplog):
    answers = {"KS50": [feature("1")]}
    calls = stub_faa(monkeypatch, answers)
    caplog.set_level(logging.INFO, logger="hangar")
    assert put(client, notams_cfg()).status_code == 200
    assert wait_until(lambda: shown(client)["count"] == 1, 3)
    answers["KS50"] = httpx.ConnectError("Temporary failure in name resolution")
    for n in range(3):   # each saved change wakes the refresh loop
        before = len(calls)
        assert put(client, notams_cfg(label=f"NOTAMs {n}")).status_code == 200
        assert wait_until(lambda: len(calls) > before, 3)
    assert wait_until(lambda: shown(client)["error"] is not None, 3)
    r = shown(client)
    assert r["count"] == 1 and "name resolution" in r["error"] and r["pending"] is False
    assert state_changes(caplog) == ["failed"]
    answers["KS50"] = [feature("1"), feature("2")]
    assert put(client, notams_cfg(label="NOTAMs again")).status_code == 200
    assert wait_until(lambda: shown(client)["count"] == 2 and shown(client)["error"] is None, 3)
    assert state_changes(caplog) == ["failed", "recovered"]


def test_one_failing_location_does_not_hide_the_others(client, monkeypatch):
    stub_faa(monkeypatch, {"KS50": [feature("1")], "KBFI": RuntimeError("FAA NMS does not know location KBFI or BFI")})
    assert put(client, notams_cfg(("KBFI", "KS50"))).status_code == 200
    assert wait_until(lambda: shown(client)["count"] == 1, 3)
    bad = shown(client, "KBFI")
    assert bad["count"] == 0 and "does not know" in bad["error"] and bad["pending"] is False


def test_sample_notams_are_never_shown_as_faa_data_or_kept_on_disk(client, monkeypatch):
    stub_faa(monkeypatch, {})
    assert put(client, notams_cfg(source="sample")).status_code == 200
    assert wait_until(lambda: shown(client)["count"] > 0, 3)
    assert shown(client)["source"] == "sample"
    assert put(client, notams_cfg(source="faa")).status_code == 200
    r = shown(client)   # at once, before the FAA has answered
    assert r["source"] == "faa" and r["notams"] == []
    assert wait_until(lambda: shown(client)["fetched"] is not None, 3)
    assert shown(client)["notams"] == []
    assert wait_until(lambda: "KS50" in json.load(open(mg.NOTAM_STORE))["locations"], 3)
    assert "SAMPLE" not in open(mg.NOTAM_STORE).read()


def test_the_notam_cache_is_restored_as_faa_data(tmp_path, monkeypatch):
    store = str(tmp_path / "notam-cache.json")
    monkeypatch.setattr(mg, "NOTAM_STORE", store)
    saved = {"ts": NOW, "locations": {"KS50": {"ts": NOW, "records": [record()]}, "KBAD": {"ts": 0, "records": []}}}
    json.dump(saved, open(store, "w"))
    fresh = mg.DataManager.__new__(mg.DataManager)
    fresh._notam_cache, fresh._notam_ts = {}, None
    fresh._load_notam_store()
    assert list(fresh._notam_cache) == ["KS50"] and fresh._notam_ts == NOW
    entry = fresh._notam_cache["KS50"]
    assert entry["source"] == "faa" and entry["error"] is None and entry["records"][0]["type"] == "RWY"
