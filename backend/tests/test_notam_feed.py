"""The NOTAM feed under the FAA's limits: the scheduler that decides which pull to
make, run for two simulated days against a stand-in for the NMS-API whose NOTAMs
are created, changed, cancelled, replaced and left to expire; the merge of pulled
changes; the comparison of a merged set with a whole one; and how the display
names the source. No test here waits in real time or touches the network."""
import asyncio
import copy
import json
import logging
import os
import random
import tempfile
import time
from datetime import datetime, timezone

import httpx
import pytest
import yaml
from support import AUTH, REWRITE, FakeClock, base_cfg, put, set_nms_keys, stub_faa, wait_until
from test_notams import (FAA_BAD_REQUEST, FAA_INTERNAL_ERROR, FAA_PULL_INTERVAL_S, FAA_REQUEST_SPACING_S, FAA_TOKEN_LIFETIME_S,
                         faa_answers, faa_request_faults, feature, notams_cfg, shown)

from app import config
from app import main as main_mod
from app import manager as mg
from app import notams
from app.config import Config, live_notams_problem, load_config, notam_source_label
from app.views import build_views

H = 3600.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def minute(ts: float) -> str:
    """A time as the API gives update and cancellation times: to the minute."""
    return iso(ts - ts % 60)


class Faa:
    """A stand-in for the NMS-API over simulated time. It holds NOTAMs in the
    shape of the FAA's own GeoJSON example, files each under a domestic location
    (and its ICAO spelling where the facility has one), and answers the two
    queries the display makes the way the FAA's documents describe them and its
    staging environment answers them: by `location`, under either spelling, every
    NOTAM in effect or still to come; by `lastUpdatedDate`, every NOTAM changed
    since, cancelled ones included and expired ones not. Update and cancellation
    times are given to the minute; which NOTAMs changed is decided by the time
    they really changed (`modified`).
    A request the FAA's specification does not allow is answered 400 and kept in
    `violations`. `faults` maps the number of a data request to what goes wrong
    with it: an HTTP status, "outage", or "failure" (answered 200, stating a
    failure)."""

    def __init__(self, clock, facilities, seed=7, faults=None):
        self.clock = clock
        self.facilities = facilities            # domestic identifier -> ICAO identifier or ""
        self.template = faa_answers()["spec_example"][0]
        self.notams = {}                        # id -> the "notam" member of a feature
        self.modified = {}                      # id -> when it last changed, to the second
        self.random = random.Random(seed)
        self.faults = faults or {}
        self.requests = []                      # (time, kind, status)
        self.violations = []                    # (time, what the request carried that is not allowed)
        self.pulls = 0
        self.tokens = []                        # issue times
        self.serial = 0
        self.outage = None                      # (from, to) in simulated time

    # -- the world
    def file(self, location, text="RWY 16/34 CLSD", start=None, hours=None, kind="N"):
        now = self.clock()
        self.serial += 1
        start = now if start is None else start
        notam = {
            **copy.deepcopy(self.template["properties"]["coreNOTAMData"]["notam"]),
            "id": f"NMS_{self.serial:06d}", "number": f"11/{self.serial:03d}", "type": kind,
            "issued": iso(now), "lastUpdated": minute(now), "location": location, "icaoLocation": self.facilities[location],
            "accountId": location, "effectiveStart": iso(start),
            "effectiveEnd": iso(start + hours * H) if hours else "PERM", "estimated": "false", "text": text,
        }
        notam.pop("cancelationDate", None)
        self.notams[notam["id"]] = notam
        self.modified[notam["id"]] = now
        return notam["id"]

    def change(self, key, text):
        self.notams[key].update(text=text, lastUpdated=minute(self.clock()))
        self.modified[key] = self.clock()

    def cancel(self, key):
        self.notams[key].update(cancelationDate=minute(self.clock()), lastUpdated=minute(self.clock()))
        self.modified[key] = self.clock()

    def replace(self, key, text):
        old = self.notams[key]
        self.cancel(key)
        return self.file(old["location"], text, kind="R")

    def live(self, location, at):
        """Ids of the NOTAMs of a location that are not cancelled and not ended at `at`."""
        out = set()
        for key, n in self.notams.items():
            if n["location"] != location or "cancelationDate" in n:
                continue
            end = n["effectiveEnd"]
            if end != "PERM" and notams.parse_time(end) <= at:
                continue
            out.add(key)
        return out

    def churn(self):
        """One random event somewhere: a new NOTAM, a change, a cancellation, a replacement."""
        location = self.random.choice(sorted(self.facilities))
        mine = sorted(self.live(location, self.clock()))
        roll = self.random.random()
        if roll < 0.45 or not mine:
            self.file(location, f"TWY {self.random.choice('ABCDEF')} CLSD", hours=self.random.choice([None, 0.5, 2, 9, 30]),
                      start=self.clock() + self.random.choice([0, 0, 0, 1800, 4 * H]))
        elif roll < 0.65:
            self.change(self.random.choice(mine), f"RWY 16/34 CLSD EXC XNG {self.random.randint(1, 99)}")
        elif roll < 0.85:
            self.cancel(self.random.choice(mine))
        else:
            self.replace(self.random.choice(mine), f"APRON MAIN RAMP CLSD {self.random.randint(1, 99)}")

    # -- the API
    def _feature(self, notam):
        message = f"!{notam['accountId']} {notam['number']} {notam['location']} {notam['text']}"
        return {"type": "Feature", "geometry": None, "properties": {"coreNOTAMData": {
            "notam": copy.deepcopy(notam), "notamTranslation": [{"type": "LOCAL_FORMAT", "simpleText": message}]}}}

    def _answer(self, status, body):
        """An answer of the data API: each carries the id the API gave the request."""
        return httpx.Response(status, headers={"x-request-id": f"00000000-0000-4000-8000-{self.pulls:012d}"}, json=body)

    def _served(self, features):
        return self._answer(200, {"status": "Success", "data": {"geojson": features}})

    def handler(self, request: httpx.Request) -> httpx.Response:
        now = self.clock()
        down = self.outage is not None and self.outage[0] <= now < self.outage[1]
        if request.url.path == "/v1/auth/token":
            if down:
                self.requests.append((now, "token", 0))
                raise httpx.ConnectError("Temporary failure in name resolution", request=request)
            self.tokens.append(now)
            self.requests.append((now, "token", 200))
            return httpx.Response(200, json={**faa_answers()["token_answer"], "access_token": f"tok{len(self.tokens)}"})
        self.pulls += 1
        fault = self.faults.get(self.pulls)
        used = int(request.headers["authorization"].removeprefix("Bearer tok"))
        expired = now - self.tokens[used - 1] >= FAA_TOKEN_LIFETIME_S
        if down or fault == "outage":
            self.requests.append((now, "data", 0))
            raise httpx.ConnectError("Temporary failure in name resolution", request=request)
        if fault == 401 or expired:
            assert not expired, "a data pull was sent with an expired token"
            self.requests.append((now, "data", 401))
            return self._answer(401, faa_answers()["token_expired"])
        if fault in (408, 429, 503):
            self.requests.append((now, "data", fault))
            return self._answer(fault, {"status": str(fault)})
        if fault == "failure":
            self.requests.append((now, "data", "failure"))
            return self._answer(200, {"status": "Failure", "errors": [FAA_INTERNAL_ERROR]})
        not_allowed = faa_request_faults(request, now)
        if not_allowed:
            self.violations.append((now, not_allowed))
            self.requests.append((now, "data", 400))
            return self._answer(400, FAA_BAD_REQUEST)
        self.requests.append((now, "data", 200))
        q = request.url.params
        if "location" in q:
            asked = q["location"]
            known = {d for d in self.facilities} | {i for i in self.facilities.values() if i}
            if asked not in known:
                return self._answer(400, faa_answers()["invalid_location"])
            mine = [n for n in self.notams.values()
                    if asked in (n["location"], n["icaoLocation"]) and n["id"] in self.live(n["location"], now)]
            return self._served([self._feature(n) for n in mine])
        since = notams.parse_time(q["lastUpdatedDate"])
        changed = [n for key, n in self.notams.items() if self.modified[key] >= since]
        return self._served([self._feature(n) for n in changed])


def simulated_manager(clock, faa, idents, refresh_s=180, wall=None):
    """A manager whose NOTAM scheduler runs on the simulated clock against the
    stand-in. Nothing else of it is started. `wall` is the time of day, where it
    is not the clock that measures intervals."""
    wall = wall or clock
    usage = notams.UsageLog(os.path.join(tempfile.mkdtemp(prefix="tpc-usage-"), "nms-usage.jsonl"))
    gate = notams.PullGate(clock=clock, wall=wall, sleep=clock.sleep)
    m = object.__new__(mg.DataManager)
    m.cfg = Config.model_validate({"notams": {
        "enabled": True, "source": "faa", "environment": "staging", "refresh_s": refresh_s, "translate": dict(REWRITE),
        "locations": [{"ident": i} for i in idents]}})
    m.nms = notams.NmsSource(httpx.AsyncClient(transport=httpx.MockTransport(faa.handler)), gate=gate, usage=usage,
                             environment="staging")
    m._notam_cache, m._notam_job, m._notam_error, m._notam_ts = {}, None, None, None
    m._delta_tried, m._last_kind, m._nms_hold, m._notam_saved = 0.0, None, 0.0, 0.0
    m._now = wall
    m.saves = 0
    m._save_notam_store = lambda: setattr(m, "saves", m.saves + 1)
    m._broadcast = lambda msg: None
    m._kick_rewrites = lambda: None
    return m


async def run_feed(m, clock, faa, until, churn_every=None, check=None):
    """Drive the scheduler the way its loop does, to simulated time `until`."""
    next_churn = clock() + churn_every if churn_every else None
    while clock() < until:
        pulls = faa.pulls
        wait = await m._nms_step()
        if faa.pulls != pulls and check:
            check()
        if wait > 0:
            clock.now += wait
        while next_churn is not None and next_churn <= clock():
            faa.churn()
            next_churn += churn_every


def shown_ids(m, ident, at):
    return {n["id"] for n in notams.select(m._notam_cache[ident]["records"], at, 24)} if ident in m._notam_cache else set()


def due_ids(faa, location, at, lookahead_h=24):
    """What the display must show for a location at `at`, from the stand-in's
    own books: the live NOTAMs that started or start within the look-ahead."""
    return {k for k in faa.live(location, at)
            if notams.parse_time(faa.notams[k]["effectiveStart"]) <= at + lookahead_h * H}


FACILITIES = {"S50": "", "SEA": "KSEA", "BFI": "KBFI", "PWT": "KPWT", "TIW": "KTIW", "RNT": "KRNT", "PAE": "KPAE",
              "OLM": "KOLM", "ZSE": "", "TCM": "KTCM", "GRF": "KGRF", "AWO": "KAWO"}
MONITORED = ["KS50", "KSEA", "KBFI", "KPWT", "KTIW", "KRNT", "KPAE", "KOLM", "ZSE", "KTCM", "KGRF", "KAWO"]


@pytest.mark.parametrize("count", [1, 2, 5, 12])
def test_two_days_of_pulls_stay_inside_the_faa_limits_and_keep_every_location_right(monkeypatch, count):
    set_nms_keys(monkeypatch, environment="staging")
    clock = FakeClock(start=1_800_000_000.0)
    start = clock()
    # one of each failure the FAA's FAQ and specification name, an outage of single pulls, and one longer than a
    # set stays patchable
    faults = {7: 401, 15: 429, 16: 429, 40: 503, 41: "outage", 90: 401, 120: 408, 150: "failure", 151: "failure", 200: 429}
    faa = Faa(clock, FACILITIES, seed=count, faults=faults)
    for location in FACILITIES:
        for n in range(3):
            faa.file(location, f"RWY 0{n + 1}/1{n + 9} CLSD", hours=[None, 5, 40][n])
    idents = MONITORED[:count]
    m = simulated_manager(clock, faa, idents)
    faa.outage = (start + 20 * H, start + 21.6 * H)
    wrong = []

    def check():
        # after every pull that was served: a location complete through this pull shows exactly what it should
        at = clock()
        if faa.requests[-1] != (faa.requests[-1][0], "data", 200):
            return
        for ident in idents:
            entry = m._notam_cache.get(ident)
            if entry and entry["ts"] > 0 and entry["synced"] >= faa.requests[-1][0] - 2:
                location = min(notams.query_forms(ident), key=len)
                if shown_ids(m, ident, at) != due_ids(faa, location, at):
                    wrong.append((ident, at - start, sorted(shown_ids(m, ident, at) ^ due_ids(faa, location, at))))

    asyncio.run(run_feed(m, clock, faa, start + 48 * H, churn_every=400.0, check=check))

    assert faa.violations == []                                    # every request was one the specification allows
    data = [(t, status) for t, kind, status in faa.requests if kind == "data"]
    gaps = [b[0] - a[0] for a, b in zip(data, data[1:])]
    assert min(gaps) >= FAA_PULL_INTERVAL_S                        # never two data pulls inside three minutes
    assert len(data) <= 48 * H / FAA_PULL_INTERVAL_S + 1
    times = [t for t, _k, _s in faa.requests]
    assert min(b - a for a, b in zip(times, times[1:])) >= FAA_REQUEST_SPACING_S
    # the display keeps pulling: outside the outage and the waits a 429 imposes, no long silence
    assert len(data) > 0.8 * 48 * H / FAA_PULL_INTERVAL_S
    # each 401 is followed by a token request and never by a second pull in the same turn
    statuses = [(kind, status) for _t, kind, status in faa.requests]
    for n, entry in enumerate(statuses):
        if entry == ("data", 401):
            assert statuses[n + 1] == ("token", 200)
    assert sum(1 for s in statuses if s == ("data", 401)) == 2      # only the two injected: no token was used expired
    # a 429 lengthens the wait before the next pull, and a second one in a row lengthens it further
    after_429 = [gaps[n] for n, (_t, status) in enumerate(data[:-1]) if status == 429]
    assert len(after_429) == 3 and after_429[0] >= 2 * FAA_PULL_INTERVAL_S and after_429[1] >= 4 * FAA_PULL_INTERVAL_S
    # what was shown was right after every served pull, and right at the end
    assert wrong == []
    assert all(m._notam_cache[i]["ts"] > 0 for i in idents)
    # whenever a whole set arrived, the set kept current by pulls of changes had missed nothing
    compares = [r for r in m.nms.usage.rows if r["kind"] == "compare"]
    assert len(compares) >= count * 6
    assert all(v == 0 for r in compares for k, v in r["diff"].items() if k.startswith("missed_")), compares
    # a difference is left unsettled only where the outage put it out of a pull's reach: in the whole
    # fetches that follow it (after the wait a failing location is given, at most two spellings per
    # location, taking turns with pulls of changes)
    recovery = mg.NOTAM_RETRY_MAX_S + 4 * count * FAA_PULL_INTERVAL_S + 60
    for r in compares:
        if r["diff"].get("unverified"):
            assert faa.outage[1] <= r["t"] <= faa.outage[1] + recovery, r
    summary = m.nms.usage.summary(clock(), window_s=None)
    assert summary["shortest_gap_s"] >= FAA_PULL_INTERVAL_S and summary["status_429"] == 3 and summary["mismatches"] == 0
    assert summary["pulls"] == len(data) == summary["baselines"] + summary["deltas"]
    # the outage outlasted the reach of a pull of changes: afterwards whole sets were fetched again
    rows = [r for r in m.nms.usage.rows if r["kind"] in ("baseline", "delta")]
    assert all(bool(r.get("rid")) == (r["status"] > 0) for r in rows)   # every answered pull under the id the API gave it
    after = [r for r in rows if r["t"] >= faa.outage[1] and r["status"] == 200]
    assert {r["what"] for r in after[: 4 * count] if r["kind"] == "baseline"} == set(idents)


def test_a_new_location_is_fetched_whole_before_changes_are_pulled_and_both_spellings_are_asked(monkeypatch):
    set_nms_keys(monkeypatch, environment="staging")
    clock = FakeClock(start=1_800_000_000.0)
    faa = Faa(clock, {"S50": "", "SEA": "KSEA"})
    a = faa.file("S50", hours=5)
    b = faa.file("SEA", hours=5)
    m = simulated_manager(clock, faa, ["KS50", "KSEA"])
    asyncio.run(run_feed(m, clock, faa, clock() + 0.5 * H))
    rows = [r for r in m.nms.usage.rows if r["kind"] in ("baseline", "delta")]
    first = [(r["kind"], r.get("form"), r["status"]) for r in rows[:4]]
    # KS50 is asked as KS50 (unknown to the API) and then as S50; then changes are due; then KSEA, once: the API
    # answers both of its spellings with the same set
    assert first == [("baseline", "KS50", 400), ("baseline", "S50", 200), ("delta", None, 200), ("baseline", "KSEA", 200)]
    assert shown_ids(m, "KS50", clock()) == {a} and shown_ids(m, "KSEA", clock()) == {b}
    assert m._notam_cache["KS50"]["unknown_forms"] == ["KS50"] and m._notam_cache["KSEA"]["unknown_forms"] == []
    # from then on: one pull of changes per turn, for both locations together
    assert {r["kind"] for r in rows[4:]} == {"delta"} and len(rows) >= 8
    # and a location's whole set fetched again is asked under the spelling the API knows only
    m._notam_cache["KS50"]["requested"] = m._notam_cache["KSEA"]["requested"] = False
    m._notam_cache["KS50"]["ts"] -= notams.BASELINE_S
    asyncio.run(run_feed(m, clock, faa, clock() + 0.2 * H))
    again = [r.get("form") for r in list(m.nms.usage.rows)[len(rows):] if r["kind"] == "baseline"]
    assert again == ["S50"]


def test_a_location_the_api_knows_under_no_spelling_is_reported_and_not_asked_every_turn(monkeypatch):
    set_nms_keys(monkeypatch, environment="staging")
    clock = FakeClock(start=1_800_000_000.0)
    faa = Faa(clock, {"SEA": "KSEA"})
    m = simulated_manager(clock, faa, ["KZZZ"])
    asyncio.run(run_feed(m, clock, faa, clock() + 3 * H))
    asked = [r for r in m.nms.usage.rows if r["kind"] == "baseline"]
    assert [r["form"] for r in asked] == ["KZZZ", "ZZZ"]           # once under each spelling, then not again for hours
    entry = m._notam_cache["KZZZ"]
    assert entry["unknown"] is True and entry["records"] == []
    assert "does not know location KZZZ or ZZZ" in m.get_notams("KZZZ")["error"]


def test_a_wait_set_before_the_clock_was_put_back_does_not_outlast_its_length(monkeypatch):
    set_nms_keys(monkeypatch, environment="staging")
    clock = FakeClock(start=1_800_000_000.0)
    skew = [0.0]
    wall = lambda: clock() + skew[0]                                 # noqa: E731
    faa = Faa(wall, {"SEA": "KSEA"}, faults={1: 503})
    filed = faa.file("SEA", hours=30)
    m = simulated_manager(clock, faa, ["KSEA"], wall=wall)
    asyncio.run(run_feed(m, clock, faa, clock() + 60))               # the first whole fetch fails: the location waits
    entry = m._notam_cache["KSEA"]
    assert entry["failures"] == 1 and 0 < entry["retry_at"] - wall() <= mg.NOTAM_RETRY_MAX_S
    assert m._needs_whole(entry, wall()) is False
    skew[0] = -6 * H
    asyncio.run(run_feed(m, clock, faa, clock() + mg.NOTAM_RETRY_MAX_S))
    assert shown_ids(m, "KSEA", wall()) == {filed}                   # asked again within the longest wait, not six hours on
    # the hold after refused keys ends the same way, and one that is running holds
    m._nms_hold = wall() + mg.NOTAM_RETRY_MAX_S
    assert m._holding(m._nms_hold, wall()) is True and m._holding(m._nms_hold, wall() - 6 * H) is False
    assert m._holding(0.0, wall()) is False and m._holding(wall() - 1, wall()) is False


def test_refused_keys_stop_the_pulls_until_the_keys_are_replaced(monkeypatch):
    set_nms_keys(monkeypatch, environment="staging")
    clock = FakeClock(start=1_800_000_000.0)
    seen = []

    def handler(request):
        seen.append((clock(), request.url.path))
        return httpx.Response(401, json=faa_answers()["invalid_client"])

    faa = Faa(clock, {"SEA": "KSEA"})
    faa.handler = handler
    m = simulated_manager(clock, faa, ["KSEA", "KBFI", "KPAE"])
    asyncio.run(run_feed(m, clock, faa, clock() + 2 * H))
    assert all(path == "/v1/auth/token" for _t, path in seen)       # never a data pull without a token
    assert 1 <= len(seen) <= 5                                       # and the token is not asked for turn after turn
    assert "rejected the credentials" in m._notam_error
    m.secret_changed("nms")                                          # the owner stores new keys
    assert m._nms_hold == 0.0


def test_no_pull_is_made_without_keys_for_the_environment_or_while_live_views_are_blocked(monkeypatch):
    clock = FakeClock(start=1_800_000_000.0)
    faa = Faa(clock, {"SEA": "KSEA"})
    m = simulated_manager(clock, faa, ["KSEA"])
    set_nms_keys(monkeypatch, environment="production")              # keys for another environment than the configured one
    asyncio.run(run_feed(m, clock, faa, clock() + H))
    assert faa.requests == [] and "no FAA credentials are stored for the staging environment" in m._notam_error
    set_nms_keys(monkeypatch, environment="staging")
    m.cfg = m.cfg.model_copy(update={"notams": m.cfg.notams.model_copy(update={
        "translate": m.cfg.notams.translate.model_copy(update={"enabled": False})})})
    assert m._notam_idents() == [] and m.notams_blocked()
    assert m._nms_pick(clock()) is None and faa.requests == []


def test_a_failure_the_api_states_in_an_answer_never_empties_a_locations_set(monkeypatch):
    set_nms_keys(monkeypatch, environment="staging")
    clock = FakeClock(start=1_800_000_000.0)
    faa = Faa(clock, {"SEA": "KSEA"})
    filed = {faa.file("SEA", f"TWY {c} CLSD", hours=30) for c in "ABC"}
    m = simulated_manager(clock, faa, ["KSEA"])
    asyncio.run(run_feed(m, clock, faa, clock() + 0.3 * H))
    assert shown_ids(m, "KSEA", clock()) == filed
    # the whole fetch asked for now, under both spellings, and the pulls of changes between them: each is
    # answered 200 with a stated failure and no NOTAMs
    faa.faults = {faa.pulls + n: "failure" for n in range(1, 5)}
    m._notam_cache["KSEA"]["requested"] = True
    seen = []
    asyncio.run(run_feed(m, clock, faa, clock() + 2 * H,
                         check=lambda: seen.append((faa.requests[-1][2], shown_ids(m, "KSEA", clock()), m._notam_error))))
    assert [status for status, _ids, _error in seen[:4]] == ["failure"] * 4
    assert all(ids == filed for _status, ids, _error in seen)        # after every pull, failed or served
    assert all("reported a failure: 1 Encountered unexpected query parameter format" in error for _s, _i, error in seen[:4])
    assert m._notam_error is None and m._notam_cache["KSEA"]["error"] is None and m._notam_cache["KSEA"]["requested"] is False
    assert {r["kind"] for r in list(m.nms.usage.rows)[-4:]} >= {"delta"} and faa.violations == []


@pytest.mark.parametrize("last_pull", ["changes", "whole"])
def test_a_clock_set_back_fetches_whole_sets_again_and_never_asks_for_changes_since_a_time_to_come(monkeypatch, last_pull):
    set_nms_keys(monkeypatch, environment="staging")
    clock = FakeClock(start=1_800_000_000.0)
    skew = [0.0]
    wall = lambda: clock() + skew[0]                                 # noqa: E731
    faa = Faa(wall, {"SEA": "KSEA"})
    first = faa.file("SEA", hours=30)
    m = simulated_manager(clock, faa, ["KSEA"], wall=wall)
    errors, state = [], m._notam_state
    m._notam_state = lambda error: (errors.append(error), state(error))[1]

    async def until_fetched_whole():
        while m._notam_cache.get("KSEA", {}).get("ts", 0) <= 0:
            await m._nms_step()

    # the clock is put back after pulls of changes, and right after the whole fetch, before any
    asyncio.run(run_feed(m, clock, faa, clock() + 0.5 * H) if last_pull == "changes" else until_fetched_whole())
    assert shown_ids(m, "KSEA", wall()) == {first} and m._last_kind == last_pull
    served = len([r for r in m.nms.usage.rows if r["kind"] in ("baseline", "delta")])
    skew[0] = -2 * H                                                 # the time of day is set back two hours
    later = faa.file("SEA", "TWY B CLSD", hours=30)
    asyncio.run(run_feed(m, clock, faa, clock() + 0.5 * H))
    assert faa.violations == []                                      # no lastUpdatedDate after the present was sent,
    assert [e for e in errors if e] == []                            # and none was held back at the last moment
    rows = [r for r in m.nms.usage.rows if r["kind"] in ("baseline", "delta")][served:]
    assert [r["kind"] for r in rows[:2]] == ["baseline", "delta"]    # fetched whole, then patched again at once
    assert all(r["status"] == 200 for r in rows) and len(rows) >= 0.5 * H / FAA_PULL_INTERVAL_S - 1   # a pull every turn
    assert shown_ids(m, "KSEA", wall()) == {first, later}
    # what the set held from before is not held against the pulls of changes
    assert m.nms.usage.summary(wall(), window_s=None)["mismatches"] == 0 and m._notam_cache["KSEA"]["diff"] is None


# ---- merging pulled changes ----------------------------------------------------------------------

def test_pulled_changes_update_cancel_and_add_by_notam_id_and_only_for_the_location_they_affect(monkeypatch):
    set_nms_keys(monkeypatch, environment="staging")
    clock = FakeClock(start=1_800_000_000.0)
    faa = Faa(clock, {"S50": "", "SEA": "KSEA"})
    keep, edit, gone, old = (faa.file("S50", f"TWY {c} CLSD", hours=30) for c in "ABCD")
    other = faa.file("SEA", hours=30)
    m = simulated_manager(clock, faa, ["KS50"])
    asyncio.run(run_feed(m, clock, faa, clock() + 0.2 * H))
    assert shown_ids(m, "KS50", clock()) == {keep, edit, gone, old}
    faa.change(edit, "TWY B CLSD EXC XNG")
    faa.cancel(gone)
    new = faa.replace(old, "TWY D CLSD WEF NOW")
    faa.change(other, "RWY 16L/34R CLSD")                            # another location's NOTAM changes too
    late = faa.file("S50", "APRON CLSD", start=clock() + 30 * H, hours=2)   # starts beyond the look-ahead
    asyncio.run(run_feed(m, clock, faa, clock() + 0.2 * H))
    records = {r["id"]: r for r in m._notam_cache["KS50"]["records"]}
    assert shown_ids(m, "KS50", clock()) == {keep, edit, new}
    assert records[edit]["text"] == "TWY B CLSD EXC XNG" and records[gone]["cancelled"] and records[old]["cancelled"]
    assert other not in records and late in records                 # held, and shown once its start comes within a day
    clock.now += 7 * H
    assert late in shown_ids(m, "KS50", clock())
    assert len(records) == len(m._notam_cache["KS50"]["records"])    # one record per NOTAM: a change replaces, never doubles


def test_a_changed_notam_that_names_no_location_is_not_put_under_every_location():
    bare = feature("x")
    core = bare["properties"]["coreNOTAMData"]
    del core["notam"]["location"], core["notam"]["icaoLocation"]
    core["notamTranslation"] = []                                    # and no message whose header would name one
    nameless = notams.normalize(bare)
    assert nameless["location"] == "" and nameless["icao_location"] == ""
    assert notams.affects(nameless, "KS50") is True                  # asked for by location: the API chose it
    assert notams.affects(nameless, "KS50", strict=True) is False    # among all changed NOTAMs: it is nobody's
    assert notams.affects(notams.normalize(feature("y")), "KS50", strict=True) is True


NOW = 1_800_000_000


def rec(key, updated=0, end=10 * H, cancelled=None, kind="N"):
    return {"id": key, "updated": NOW + updated, "end": None if end is None else NOW + end,
            "cancelled": None if cancelled is None else NOW + cancelled, "kind": kind}


@pytest.mark.parametrize("merged, fresh, expected", [
    ([rec("a")], [rec("a")], {"same": 1}),
    ([rec("a", end=-60)], [], {"ended": 1}),                                   # ended: the API stopped listing it
    ([rec("a", cancelled=-60)], [], {"ended": 1}),
    ([rec("a", kind="C")], [], {"ended": 1}),
    ([rec("a", end=None)], [], {"gone": 1}),                                   # in effect here, not listed there: settled later
    ([rec("a")], [], {"gone": 1}),
    ([], [rec("a", updated=-3600)], {"appeared": 1}),                          # updated before the last pull: settled later
    ([], [rec("a", updated=-30)], {"appeared": 1}),                            # in the minute of the last pull, too
    ([], [rec("a", updated=0)], {"newer": 1}),                                 # updated at or after the last pull
    ([], [rec("a", updated=30)], {"newer": 1}),
    ([rec("a", updated=-7200)], [rec("a", updated=-3600)], {"changed": 1}),
    ([rec("a", updated=-7200)], [rec("a", updated=-7200, cancelled=-100)], {"changed": 1}),
    ([rec("a", updated=-7200)], [rec("a", updated=30)], {"newer": 1}),
    ([rec("a"), rec("b", end=-1), rec("c")], [rec("a"), rec("c", updated=-50), rec("d", updated=-50)],
     {"same": 1, "ended": 1, "changed": 1, "appeared": 1}),
])
def test_a_whole_set_is_compared_with_the_merged_one_class_by_class(merged, fresh, expected):
    zero = {"same": 0, "ended": 0, "newer": 0, "gone": 0, "appeared": 0, "changed": 0}
    assert notams.compare_sets(merged, fresh, now=NOW, synced=NOW) == {**zero, **expected}
    pending = notams.unsettled_ids(merged, fresh, NOW, NOW)
    assert {k: len(v) for k, v in pending.items()} == {k: expected.get(k, 0) for k in notams.UNSETTLED}


def test_an_unsettled_notam_is_newer_if_the_next_pull_of_changes_carries_it_and_missed_if_not():
    pending = {"gone": ["g1", "g2"], "appeared": ["a1"], "changed": ["c1", "c2"]}
    assert notams.settle(pending, {"g1", "c2", "other"}) == {
        "newer": 2, "missed_removal": 1, "missed_new": 1, "missed_change": 1}
    assert notams.settle(pending, set()) == {"newer": 0, "missed_removal": 2, "missed_new": 1, "missed_change": 2}
    assert notams.settle({k: [] for k in notams.UNSETTLED}, {"x"}) == {
        "newer": 0, "missed_removal": 0, "missed_new": 0, "missed_change": 0}


def test_a_withdrawal_is_missed_only_if_the_next_pull_of_changes_does_not_report_it(monkeypatch, caplog):
    set_nms_keys(monkeypatch, environment="staging")
    clock = FakeClock(start=1_800_000_000.0)
    faa = Faa(clock, {"SEA": "KSEA"})
    stays, cancelled, vanished = (faa.file("SEA", f"TWY {c} CLSD", hours=30) for c in "ABC")
    m = simulated_manager(clock, faa, ["KSEA"])
    asyncio.run(run_feed(m, clock, faa, clock() + 0.3 * H))
    last_pull = max(t for t, kind, _status in faa.requests if kind == "data")
    faa.cancel(cancelled)                              # since the last pull of changes: one NOTAM is cancelled, as the FAA
    del faa.notams[vanished]                           # reports cancellations, and one disappears without the API ever saying so
    cancelled_at = clock()
    clock.now += 150                                   # the whole set is fetched well after that,
    m._notam_cache["KSEA"]["requested"] = True
    with caplog.at_level(logging.WARNING, logger="hangar"):
        asyncio.run(run_feed(m, clock, faa, clock() + 0.3 * H))
    assert shown_ids(m, "KSEA", clock()) == {stays}
    whole = next(r for r in m.nms.usage.rows if r["kind"] == "baseline" and r["t"] > cancelled_at)
    after = next(r for r in m.nms.usage.rows if r["kind"] == "delta" and r["t"] > whole["t"])
    # so that only a pull reaching back to before the whole fetch can still report the cancellation
    assert last_pull < cancelled_at < whole["t"] - notams.DELTA_OVERLAP_S
    assert notams.parse_time(after["what"]) <= cancelled_at
    diff = m._notam_cache["KSEA"]["diff"]
    assert diff["gone"] == 0 and diff["missed_removal"] == 1 and m._notam_cache["KSEA"]["unseen"] is None
    rows = [r["diff"] for r in m.nms.usage.rows if r["kind"] == "compare"]
    assert rows[0]["gone"] == 2 and rows[1] == {"newer": 1, "missed_removal": 1, "missed_new": 0, "missed_change": 0}
    assert m.nms.usage.summary(clock(), window_s=None)["mismatches"] == 1
    assert "had not brought {'missed_removal': 1}" in caplog.text


def test_a_change_made_in_the_minute_of_the_last_pull_is_not_counted_as_missed(monkeypatch, caplog):
    set_nms_keys(monkeypatch, environment="staging")
    clock = FakeClock(start=1_800_000_030.0)                         # half a minute past the minute: pulls fall there too
    faa = Faa(clock, {"SEA": "KSEA"})
    first = faa.file("SEA", hours=30)
    edited = faa.file("SEA", "TWY B CLSD", hours=30)
    m = simulated_manager(clock, faa, ["KSEA"])
    asyncio.run(run_feed(m, clock, faa, clock() + 0.3 * H))
    assert m._last_kind == "changes"
    last_pull = max(t for t, kind, _status in faa.requests if kind == "data")
    clock.now = last_pull + 5                                        # five seconds after the last pull, in the same minute
    added = faa.file("SEA", "TWY C CLSD", hours=30)                  # its update time, to the minute, is before the pull
    faa.change(edited, "TWY B CLSD EXC XNG")
    assert notams.parse_time(faa.notams[added]["lastUpdated"]) < last_pull
    m._notam_cache["KSEA"]["requested"] = True                       # and the whole set is fetched before the next pull
    with caplog.at_level(logging.WARNING, logger="hangar"):
        asyncio.run(run_feed(m, clock, faa, clock() + 0.2 * H))
    rows = [r["diff"] for r in m.nms.usage.rows if r["kind"] == "compare"]
    assert rows[0]["appeared"] == 1 and rows[0]["changed"] == 1
    assert rows[1] == {"newer": 2, "missed_new": 0, "missed_change": 0, "missed_removal": 0}
    assert m.nms.usage.summary(clock(), window_s=None)["mismatches"] == 0 and "had not brought" not in caplog.text
    assert shown_ids(m, "KSEA", clock()) == {first, edited, added}
    diff = m._notam_cache["KSEA"]["diff"]                            # the location's state, as the admin page shows it
    assert diff["newer"] == rows[0]["newer"] + 2 and diff["appeared"] == diff["changed"] == 0 and diff["missed_new"] == 0


def test_differences_older_than_a_pull_of_changes_can_reach_are_counted_unverified(monkeypatch):
    set_nms_keys(monkeypatch, environment="staging")
    clock = FakeClock(start=1_800_000_000.0)
    faa = Faa(clock, {"SEA": "KSEA"})
    keep, edit, gone = (faa.file("SEA", f"TWY {c} CLSD", hours=30) for c in "ABC")
    m = simulated_manager(clock, faa, ["KSEA"])
    asyncio.run(run_feed(m, clock, faa, clock() + 0.3 * H))
    entry = m._notam_cache["KSEA"]
    entry["synced"] -= 2 * H                                         # last complete two hours ago: out of a pull's reach
    before = entry["synced"] - H                                     # and changed before that, as the API dates it
    faa.change(edit, "TWY B CLSD EXC XNG")
    added = faa.file("SEA", "TWY D CLSD", hours=30)
    for key in (edit, added):
        faa.notams[key]["lastUpdated"], faa.modified[key] = minute(before), before
    faa.cancel(gone)
    asyncio.run(run_feed(m, clock, faa, clock() + 0.2 * H))
    rows = [r["diff"] for r in m.nms.usage.rows if r["kind"] == "compare"]
    assert rows == [{"same": 1, "ended": 0, "newer": 0, "unverified": 3}]   # one gone, one appeared, one changed
    assert m._notam_cache["KSEA"]["unseen"] is None and m.nms.usage.summary(clock(), window_s=None)["mismatches"] == 0
    assert shown_ids(m, "KSEA", clock()) == {keep, edit, added}


def test_a_missed_change_is_recorded_and_logged_when_the_whole_set_shows_it(monkeypatch, caplog):
    set_nms_keys(monkeypatch, environment="staging")
    clock = FakeClock(start=1_800_000_000.0)
    faa = Faa(clock, {"SEA": "KSEA"})
    first = faa.file("SEA", hours=30)
    m = simulated_manager(clock, faa, ["KSEA"])
    asyncio.run(run_feed(m, clock, faa, clock() + 0.3 * H))
    # the stand-in files a NOTAM and backdates it, so that no pull of changes ever returns it
    hidden = faa.file("SEA", "TWY Z CLSD", hours=30)
    faa.notams[hidden]["lastUpdated"] = minute(clock() - 5 * H)
    faa.modified[hidden] = clock() - 5 * H
    m._notam_cache["KSEA"]["requested"] = True
    with caplog.at_level(logging.WARNING, logger="hangar"):
        asyncio.run(run_feed(m, clock, faa, clock() + 0.3 * H))
    assert shown_ids(m, "KSEA", clock()) == {first, hidden}          # the whole set repaired it
    rows = [r["diff"] for r in m.nms.usage.rows if r["kind"] == "compare"]
    assert rows[0]["appeared"] == 1 and rows[1]["missed_new"] == 1   # unsettled, then missed: the next pull did not bring it
    assert m._notam_cache["KSEA"]["diff"]["missed_new"] == 1 and m._notam_cache["KSEA"]["diff"]["appeared"] == 0
    assert m.nms.usage.summary(clock(), window_s=None)["mismatches"] == 1
    assert "had not brought {'missed_new': 1}" in caplog.text and "TWY Z" not in caplog.text


# ---- the source the display names, test environments, non-US NOTAMs --------------------------------

def cfg_of(**n):
    return Config.model_validate({"notams": {"enabled": True, "locations": [{"ident": "KS50"}], **n}})


@pytest.mark.parametrize("source", ["faa", "sample"])
@pytest.mark.parametrize("environment", ["production", "staging", "fit"])
@pytest.mark.parametrize("rewrite", [True, False])
@pytest.mark.parametrize("description", ["Qwen 3.8 27B", ""])
def test_the_source_label_and_the_views_for_every_combination_of_settings(source, environment, rewrite, description):
    translate = {"enabled": rewrite, "base_url": "http://model.test/v1", "model": "worker-instruct", "description": description}
    cfg = cfg_of(source=source, environment=environment, translate=translate)
    views = [v for v in build_views(cfg) if v["type"] == "notams"]
    problem = live_notams_problem(cfg)
    if source == "sample":
        assert problem is None and len(views) == 1
        assert views[0]["source_label"] == notam_source_label(cfg) == "sample data" and views[0]["test_environment"] is False
    elif rewrite and description:
        assert problem is None and len(views) == 1
        assert views[0]["source_label"] == "FAA NMS Processed with Qwen 3.8 27B LLM"
        assert views[0]["test_environment"] is (environment != "production")
    else:
        # live NOTAMs are never shown under a label that would not be true
        assert views == []
        assert problem["loc"] == ["notams", "translate", "enabled" if not rewrite else "description"]


def test_a_saved_file_with_live_views_and_no_rewrite_or_a_short_interval_still_loads(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**base_cfg(), "notams": {
        "enabled": True, "source": "faa", "refresh_s": 120, "locations": [{"ident": "KS50"}],
        "translate": {"enabled": False}}}))
    loaded = load_config(str(path))
    assert loaded.source == "file" and loaded.error is None           # not replaced by a backup or by defaults
    assert loaded.config.notams.enabled and loaded.config.notams.refresh_s == 180
    assert [v["id"] for v in build_views(loaded.config)] == ["local:KSEA", "region:Puget Sound", "satellite"]   # all but NOTAMs


def test_saving_live_views_without_the_rewrite_is_refused_and_the_reason_is_in_the_status(client, monkeypatch):
    stub_faa(monkeypatch, {"KS50": [feature("1")]})
    body = notams_cfg()
    body["notams"]["translate"] = {"enabled": False}
    r = put(client, body)
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["notams", "translate", "enabled"]
    body["notams"]["translate"] = {**REWRITE, "description": ""}
    assert put(client, body).json()["detail"][0]["loc"] == ["notams", "translate", "description"]
    body["notams"]["refresh_s"] = 60
    body["notams"]["translate"] = dict(REWRITE)
    assert put(client, body).status_code == 200
    assert client.get("/api/config").json()["notams"]["refresh_s"] == 180
    assert client.get("/api/status").json()["notams"]["blocked"] is None


def test_a_test_environment_is_marked_and_its_notams_never_pass_for_production_ones(client, monkeypatch):
    stub = stub_faa(monkeypatch, {"KS50": [feature("1")]})
    assert put(client, notams_cfg(environment="staging")).status_code == 200
    assert wait_until(lambda: shown(client)["count"] == 1, 3)
    r = shown(client)
    assert r["environment"] == "staging" and r["test_environment"] is True
    assert r["source_label"] == "FAA NMS Processed with Stub 1B LLM"
    assert client.get("/api/status").json()["notams"]["test_environment"] is True
    assert wait_until(lambda: json.load(open(mg.NOTAM_STORE))["locations"].get("KS50", {}).get("environment") == "staging", 3)
    stub.by_ident["KS50"] = async_never
    assert put(client, notams_cfg(environment="production")).status_code == 200
    r = shown(client)                                                 # at once: nothing fetched from staging is shown
    assert r["environment"] == "production" and r["test_environment"] is False and r["notams"] == [] and r["pending"] is True
    # nor after a restart, when the cache on disk still holds what staging sent
    restarted = object.__new__(mg.DataManager)
    restarted.cfg, restarted._notam_cache, restarted._notam_ts = main_mod.manager.cfg, {}, None
    restarted._now, restarted._rewrites = main_mod.manager._now, {}
    restarted._load_notam_store()
    assert restarted._notam_cache["KS50"]["environment"] == "staging" and len(restarted._notam_cache["KS50"]["records"]) == 1
    assert restarted.get_notams("KS50")["notams"] == [] and restarted.get_notams("KS50")["pending"] is True


async def async_never():
    await asyncio.sleep(30)


@pytest.mark.parametrize("classification, account, foreign", [
    ("DOM", "SEA", False), ("FDC", "FDC", False), ("DOM", "SUAW", False),
    ("INTL", "KSEA", False),            # the US's own international series: classified INTL, issued by a US account
    ("MIL", "KQZC", False),             # US military
    ("INTL", "EGGNYNYX", True),         # the AFTN address of a foreign NOTAM office (the specification's example)
    ("MIL", "EGGNYNYX", True),
    ("INTL", "SNOWTAM", True),
    ("", "", False),
])
def test_a_location_with_notams_issued_abroad_is_flagged_as_not_authoritative(client, monkeypatch, classification, account,
                                                                              foreign):
    record = notams.normalize(feature("2", classification=classification, account=account, text="TWY A CLSD"))
    assert notams.is_foreign(record) is foreign
    other = feature("2", classification=classification, account=account, text="TWY A CLSD")
    stub_faa(monkeypatch, {"KS50": [feature("1"), other]})
    assert put(client, notams_cfg()).status_code == 200
    assert wait_until(lambda: shown(client)["count"] == 2, 3)
    assert shown(client)["non_us"] is foreign


def bfi(key, classification="DOM", account="BFI", text="RWY 14R/32L CLSD", start=-H, end=2 * H, **extra):
    return notams.normalize(feature(key, location="BFI", account=account, classification=classification, text=text,
                                    start=start, end=end, now=NOW, **extra))


@pytest.mark.parametrize("records, hidden", [
    ([bfi("d"), bfi("i", "INTL", "KBFI")], {"i"}),                                          # an exact repeat
    ([bfi("d", "FDC", "FDC"), bfi("i", "INTL", "KBFI")], {"i"}),                            # of an FDC NOTAM too
    ([bfi("d"), bfi("i", "INTL", "KBFI", text="RWY  14R/32L\nCLSD")], {"i"}),               # spacing aside
    ([bfi("d", end=None), bfi("i", "INTL", "KBFI", end=None)], {"i"}),                      # both permanent
    ([bfi("d"), bfi("i", "INTL", "KBFI", text="RWY 14R/32L CLSD EXC TAX")], set()),         # other text
    ([bfi("d"), bfi("i", "INTL", "KBFI", start=-H + 60)], set()),                           # another start
    ([bfi("d"), bfi("i", "INTL", "KBFI", end=3 * H)], set()),                               # another end
    ([bfi("d"), bfi("i", "INTL", "KBFI", end=None)], set()),                                # dated against permanent
    ([bfi("d"), bfi("i", "INTL", "EGGNYNYX")], set()),                                      # issued abroad
    ([bfi("d"), bfi("m", "MIL", "KBFI")], set()),                                           # military, not the series
    ([bfi("i", "INTL", "KBFI")], set()),                                                    # nothing it repeats
    ([bfi("d"), bfi("e")], set()),                                                          # two domestic: both kept
    ([bfi("i", "INTL", "KBFI"), bfi("j", "INTL", "KBFI")], set()),                          # two international: both kept
    ([bfi("d"), bfi("i", "INTL", "KBFI"), bfi("j", "INTL", "KBFI")], {"i", "j"}),
])
def test_an_international_series_notam_is_hidden_only_where_it_exactly_repeats_a_domestic_one(records, hidden):
    assert notams.repeats(records) == {f"NMS_ID_{k}" for k in hidden}
    assert notams.repeats(records, NOW) == {f"NMS_ID_{k}" for k in hidden}     # all of them in effect


def test_a_repeat_counts_only_while_the_domestic_notam_it_repeats_is_in_effect():
    copy = bfi("i", "INTL", "KBFI")
    assert notams.repeats([bfi("d", cancelationDate=iso(NOW - 60)), copy], NOW) == set()
    assert notams.repeats([bfi("d", type="C"), copy], NOW) == set()
    assert notams.repeats([bfi("d"), copy], NOW) == {"NMS_ID_i"}
    assert notams.repeats([bfi("d"), bfi("i", "INTL", "KBFI", cancelationDate=iso(NOW - 60))], NOW) == set()


def test_the_view_hides_repeats_and_shows_one_again_when_its_domestic_notam_is_cancelled(client, monkeypatch):
    text = "TWY B4 CLSD"
    domestic = feature("d", location="BFI", account="BFI", text=text)
    stub_faa(monkeypatch, {"KBFI": [
        domestic,
        feature("i", location="BFI", account="KBFI", classification="INTL", text=text, number="A1234/26"),
        feature("o", location="BFI", account="KBFI", classification="INTL", text="TWY B5 CLSD"),
        feature("f", location="BFI", account="EGGNYNYX", classification="INTL", text=text),
    ]})
    assert put(client, notams_cfg(("KBFI",))).status_code == 200
    assert wait_until(lambda: shown(client, "KBFI")["count"] == 3, 3)
    r = shown(client, "KBFI")
    assert sorted(n["id"] for n in r["notams"]) == ["NMS_ID_d", "NMS_ID_f", "NMS_ID_o"] and r["repeats_hidden"] == 1
    # the domestic NOTAM is cancelled: what it said is now shown from the international series
    domestic["properties"]["coreNOTAMData"]["notam"].update(cancelationDate=iso(time.time() - 60), lastUpdated=iso(time.time()))
    assert put(client, notams_cfg(("KBFI",))).status_code == 200      # a saved config pulls the changes at once
    assert wait_until(lambda: shown(client, "KBFI")["repeats_hidden"] == 0, 5)
    assert sorted(n["id"] for n in shown(client, "KBFI")["notams"]) == ["NMS_ID_f", "NMS_ID_i", "NMS_ID_o"]


def test_the_usage_record_is_served_to_a_settings_key_and_as_a_file(client, monkeypatch):
    stub = stub_faa(monkeypatch, {})
    for n in range(3):
        stub.usage.add({"t": 1000.0 + 180 * n, "kind": "delta", "env": "staging", "status": 200, "bytes": 2,
                        "gap": None if n == 0 else 180.0})
    assert client.get("/api/notams/usage").status_code == 401
    r = client.get("/api/notams/usage", auth=AUTH)
    body = r.json()
    assert r.status_code == 200 and len(body["rows"]) == 3 and body["all_time"]["pulls"] == 3
    assert body["all_time"]["shortest_gap_s"] == 180.0 and body["all_time"]["limit_gap_s"] == FAA_PULL_INTERVAL_S
    assert body["summary"]["pulls"] == 0                              # none of them in the last 24 hours
    assert len(client.get("/api/notams/usage", params={"limit": 2}, auth=AUTH).json()["rows"]) == 2
    r = client.get("/api/notams/usage", params={"download": "true"}, auth=AUTH)
    assert r.headers["content-disposition"].startswith('attachment; filename="nms-usage-') and r.json()["rows"] == body["rows"]
