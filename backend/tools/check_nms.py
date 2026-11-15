#!/usr/bin/env python3
"""Ask one of the FAA's NMS-API test environments the questions the NOTAM feed
(backend/app/notams.py, manager.py) depends on, and report the answers as JSON.
It reads the FAA keys from the secrets store, never talks to production, and
sends at most one request a second, the limit the FAA sets for its test
environments. Run it in the app image, which has the backend's dependencies:

  docker run --rm -v "$PWD/backend:/b:ro" -v "$PWD/data:/data:ro" -v "$HOME/nms:/out" -w /b \\
      hangar-display:latest python tools/check_nms.py KS50 KSEA ZSE [--environment staging] \\
      [--out /out/report.json] [--as-given] [--reach SECONDS ...] [--save /out/answers]

For each location, under each spelling the feed asks it by: the HTTP status, the
status and errors the answer states, whether the feed's client reads it, the
number of NOTAMs and how many of them the feed keeps for the location, the
classifications, NOTAM types and keywords seen, how the start and end of
validity, the issue time and the update time are written, how a permanent NOTAM
and a schedule are written, whether NOTAMs not yet in effect, ended and
cancelled NOTAMs are listed, and the members a NOTAM carries. Then, whether both
spellings of a location return the same NOTAMs.

For the pull of changes (`lastUpdatedDate`), reaching back 5 minutes, 1 hour,
6 hours and to just inside the 24 hours the API takes: the same, with the
answer's size and time, whether it is NOTAMs or a link to a file, the oldest
update among them, how many affect each location, and whether it carries every
NOTAM of the locations that was updated inside its reach. A reach is not
widened after an answer that was refused, slow or large.

The token answer's member names and its lifetime are reported; no key, token or
NOTAM text is printed. `--as-given` asks each location under the spelling given
only, `--reach` picks the reaches of the pulls of changes (0 for none), and
`--save DIR` keeps every answer as received in DIR: those hold NOTAM texts and
belong outside the repository.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, ".")
from app import notams  # noqa: E402
from app.config import NMS_HOSTS  # noqa: E402

REACH_S = (300, 3600, 6 * 3600, notams.DELTA_WINDOW_S - 600)
SPACING_S = 1.1
SLOW_S = 20.0             # an answer that took longer, or was larger than LARGE_BYTES,
LARGE_BYTES = 50_000_000  # ends the widening of the reach
LOCATION_RE = re.compile(r"^[A-Za-z0-9]{3,4}$")   # what the API takes as a location


def shape(value) -> str:
    """How a value is written: digits as 9, letters as A, the rest as it is."""
    if value is None:
        return "null"
    if not isinstance(value, str):
        return type(value).__name__
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", value))[:40]


class Probe:
    def __init__(self, environment: str):
        self.environment = environment
        self.base = NMS_HOSTS[environment]
        self.client = httpx.AsyncClient(timeout=60.0)
        self.token = None
        self.last = 0.0
        self.requests = 0
        self.save_dir = None

    async def _send(self, method: str, url: str, **kw) -> httpx.Response:
        wait = self.last + SPACING_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self.last = time.monotonic()
        self.requests += 1
        return await self.client.request(method, url, **kw)

    async def get_token(self) -> dict:
        client_id, secret = notams.credentials(self.environment)
        if not (client_id and secret):
            raise SystemExit(f"no FAA credentials are stored for the {self.environment} environment")
        resp = await self._send("POST", f"{self.base}/v1/auth/token", data={"grant_type": "client_credentials"},
                                auth=(client_id, secret))
        out = {"status": resp.status_code}
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 200 and body.get("access_token"):
            self.token = body["access_token"]
            out.update(members=sorted(body), expires_in=body.get("expires_in"), token_type=body.get("token_type"))
        else:
            out["answer_members"] = sorted(body) if isinstance(body, dict) else shape(resp.text)
        return out

    async def pull(self, params: dict) -> tuple[httpx.Response, float]:
        started = time.monotonic()
        resp = await self._send("GET", f"{self.base}/nmsapi/v1/notams", params=params,
                                headers={"Authorization": f"Bearer {self.token}", "nmsResponseFormat": "GEOJSON"})
        if self.save_dir:
            name = "-".join(f"{k}_{re.sub(r'[^A-Za-z0-9]', '', v)}" for k, v in params.items())
            fd = os.open(os.path.join(self.save_dir, f"{name}.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(resp.content)
        return resp, time.monotonic() - started


def describe(resp: httpx.Response, seconds: float, now: float, ident: str = "", records: list | None = None) -> dict:
    """What an answer holds. `ident` is the location it was asked for, if any;
    the NOTAMs read from it are added to `records`."""
    out = {"status": resp.status_code, "bytes": len(resp.content), "bytes_sent": resp.num_bytes_downloaded,
           "seconds": round(seconds, 2), "request_id": bool(resp.headers.get("x-request-id")),
           "headers": {k: v for k, v in resp.headers.items()
                       if k.lower().startswith(("x-rate", "retry", "ratelimit", "link", "location", "content-type",
                                                "content-encoding"))}}
    try:
        body = resp.json()
    except ValueError:
        out["answer"] = "not JSON"
        return out
    out["members"] = sorted(body) if isinstance(body, dict) else type(body).__name__
    if not isinstance(body, dict):
        return out
    out["answer_status"] = body.get("status")
    errors = body.get("errors")
    if errors:
        out["errors"] = [{k: str(e.get(k))[:200] for k in ("code", "message")} for e in errors if isinstance(e, dict)][:5]
    if resp.status_code >= 400:
        out["message"] = str(body.get("message") or "")[:200]   # the API's own words for a refusal
        return out
    try:
        notams.NmsSource._features(resp)
        out["client_reads"] = "ok"
    except RuntimeError as exc:
        out["client_reads"] = str(exc)
    data = body.get("data")
    if not isinstance(data, dict):
        return out
    out["data_members"] = sorted(data)
    if data.get("geojson") is None:
        out["link_instead_of_notams"] = bool(data.get("url"))
        return out
    features = [f for f in data["geojson"] if isinstance(f, dict)]
    out["notams"] = len(features)
    seen = collections.defaultdict(collections.Counter)
    members = collections.Counter()
    upcoming = ended = cancelled = permanent = unreadable = oldest = differ = nameless = kept = 0
    for feature in features:
        core = ((feature.get("properties") or {}).get("coreNOTAMData") or {})
        notam = core.get("notam") or {}
        members.update(list(notam))
        for key in ("classification", "type", "estimated"):
            seen[key][str(notam.get(key))] += 1
        for key in ("id", "issued", "lastUpdated", "effectiveStart", "effectiveEnd", "cancelationDate", "schedule", "number",
                    "location", "icaoLocation", "accountId"):
            if key in notam:
                seen[key + " written as"][shape(notam.get(key))] += 1
        translations = [t for t in core.get("notamTranslation") or [] if isinstance(t, dict)]
        seen["translations"][",".join(sorted(str(t.get("type")) for t in translations))] += 1
        for t in translations:
            seen["translation members"][f"{t.get('type')}: {','.join(sorted(k for k in t if k != 'type'))}"] += 1
        record = notams.normalize(feature)
        if record is None:
            unreadable += 1
            continue
        seen["keyword"][record["type"]] += 1
        upcoming += record["start"] is not None and record["start"] > now
        cancelled += record["cancelled"] is not None
        ended += record["end"] is not None and record["end"] <= now
        differ += record["times_differ"]
        nameless += not (record["location"] or record["icao_location"])
        kept += bool(ident) and notams.affects(record, ident)
        if records is not None:
            records.append(record)
        permanent += record["permanent"]
        if record["updated"]:
            oldest = min(oldest or record["updated"], record["updated"])
    out.update(
        notam_members=dict(members.most_common()),
        seen={k: dict(v.most_common(8)) for k, v in seen.items()},
        not_yet_in_effect=upcoming, ended=ended, cancelled=cancelled, permanent=permanent, not_read=unreadable,
        times_differ_from_message=differ, name_no_location=nameless, **({"kept_for_location": kept} if ident else {}),
        oldest_update=datetime.fromtimestamp(oldest, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if oldest else None,
    )
    return out


async def run(args) -> dict:
    probe = Probe(args.environment)
    probe.save_dir = args.save
    report = {"environment": args.environment, "host": probe.base, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    report["token"] = await probe.get_token()
    if probe.token is None:
        return report
    report["locations"], report["spellings"] = {}, {}
    held = {}                                   # NOTAM id -> record, of every location asked
    for ident in args.locations:
        under = {}
        for form in [ident.upper()] if args.as_given else notams.query_forms(ident):
            records = []
            resp, seconds = await probe.pull({"location": form})
            report["locations"][f"{ident} asked as {form}"] = describe(resp, seconds, time.time(), ident, records)
            under[form] = {r["id"] for r in records if notams.affects(r, ident)}
            held.update({r["id"]: r for r in records if notams.affects(r, ident)})
        sets = list(under.values())
        report["spellings"][ident] = {
            "kept": {form: len(ids) for form, ids in under.items()},
            "same_under_every_spelling": all(ids == sets[0] for ids in sets),
            "only_under": {form: len(ids - set().union(*(o for f, o in under.items() if f != form))) for form, ids in under.items()},
        }
    report["changes"] = {}
    stop = None
    for reach in [r for r in (args.reach or REACH_S) if r > 0]:
        if stop:
            report["changes"][f"last {reach} s"] = {"not_asked": stop}
            continue
        since = time.time() - reach
        stamp = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        records = []
        resp, seconds = await probe.pull({"lastUpdatedDate": stamp})
        entry = describe(resp, seconds, time.time(), records=records)
        entry["asked_since"] = stamp
        changed = {r["id"] for r in records}
        due = [r for r in held.values() if r["updated"] and r["updated"] > since]
        entry["location_notams_updated_in_reach"] = len(due)
        entry["of_those_carried"] = sum(1 for r in due if r["id"] in changed)
        entry["affecting"] = {ident: sum(1 for r in records if notams.affects(r, ident, strict=True)) for ident in args.locations}
        report["changes"][f"last {reach} s"] = entry
        if resp.status_code != 200 or seconds > SLOW_S or len(resp.content) > LARGE_BYTES:
            stop = f"the answer for the last {reach} s was refused, slow or large"
    report["requests"] = probe.requests
    await probe.client.aclose()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("locations", nargs="+", help="location identifiers, as monitored (KS50, KSEA, ZSE)")
    parser.add_argument("--environment", default="staging", choices=[e for e in NMS_HOSTS if e != "production"])
    parser.add_argument("--out", help="also write the report to this file")
    parser.add_argument("--as-given", action="store_true", help="ask each location under the spelling given only")
    parser.add_argument("--reach", type=int, action="append", metavar="SECONDS",
                        help=f"how far back a pull of changes reaches (repeatable; 0 for none; default {REACH_S})")
    parser.add_argument("--save", metavar="DIR", help="keep every answer as received in DIR (NOTAM texts: outside the repository)")
    args = parser.parse_args()
    if any(r > notams.DELTA_WINDOW_S - 600 for r in args.reach or []):
        parser.error("the API takes changes of the last 24 hours only")
    if args.save:
        os.makedirs(args.save, mode=0o700, exist_ok=True)
    for ident in args.locations:
        if not LOCATION_RE.match(ident):
            parser.error(f"{ident}: a location is 3 or 4 letters or digits")
    report = asyncio.run(run(args))
    text = json.dumps(report, indent=1)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
    return 0 if report.get("token", {}).get("status") == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
