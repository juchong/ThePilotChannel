"""NOTAMs by affected location, from the FAA NOTAM Management Service API (NMS-API).

A NOTAM belongs to a monitored location when the location it affects is that
location. The office accountable for it (the issuer) is never used to select
NOTAMs: a restriction at one airport is routinely issued under another's
accountability. The API is asked by `location`, and what it returns is checked
against the location again here.

Times are kept as epoch seconds (UTC) exactly as the API states them. A NOTAM is
shown while it is in effect, or when it takes effect within the look-ahead.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from .notam_decode import DATA_DIR, HEADER_RE, TYPE_LABEL, TYPE_ORDER, classify, decode

NMS_BASE = (os.environ.get("HANGAR_NMS_BASE") or "https://api-nms.aim.faa.gov").rstrip("/")
TOKEN_MARGIN_S = 60       # renew the bearer token this long before it expires
REQUEST_TIMEOUT_S = 20.0
UNKNOWN_LOCATION = (400, 404)
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$")
DIGITS_RE = re.compile(r"^(\d{4}|\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$")
VALIDITY_RE = re.compile(r"(\d{10})\s*-\s*(?:(\d{10})(EST)?|(PERM))\s*$")
TIME_TOLERANCE_S = 60     # fields and message agree when within a minute
OFFSET_RE = re.compile(r"^([+-])(\d+)([mhd])$")
OFFSET_S = {"m": 60, "h": 3600, "d": 86400}


class NotConfigured(RuntimeError):
    """The FAA credentials are not set."""


def credentials() -> tuple[str, str]:
    return os.environ.get("HANGAR_NMS_CLIENT_ID", ""), os.environ.get("HANGAR_NMS_CLIENT_SECRET", "")


def query_forms(ident: str) -> List[str]:
    """The spellings a location is asked for and matched under. The API takes the
    domestic or the ICAO form; a U.S. facility without an ICAO code is filed under
    its three-character identifier, which a display configured for METARs spells
    with a leading K."""
    ident = ident.upper()
    forms = [ident]
    if len(ident) == 4 and ident[0] == "K":
        forms.append(ident[1:])
    elif len(ident) == 3:
        forms.append("K" + ident)
    return forms


def parse_time(value) -> Optional[int]:
    """A date-time as epoch seconds (UTC); None when absent, "PERM" or unreadable.
    The API gives ISO 8601 (2025-08-21T02:34:00.000Z) or twelve digits
    (YYYYMMDDHHMM); a NOTAM's own validity group has ten (YYMMDDHHMM). A trailing
    EST (estimated) is not part of the time."""
    if not isinstance(value, str):
        return None
    v = value.strip().upper()
    if v.endswith("EST"):
        v = v[:-3].strip()
    if not v or v == "PERM":
        return None
    m = DIGITS_RE.match(v)
    if m:
        year = int(m.group(1))
        try:
            dt = datetime(year + 2000 if year < 100 else year, *(int(g) for g in m.groups()[1:]), tzinfo=timezone.utc)
        except ValueError:
            return None
        return int(dt.timestamp())
    if not ISO_RE.match(v):   # a date without a time is not a time
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def stated_validity(message: str) -> Optional[Dict]:
    """The validity a NOTAM message itself ends with (YYMMDDHHMM-YYMMDDHHMM,
    with EST or PERM), as {start, end, estimated, permanent}; None without one."""
    m = VALIDITY_RE.search((message or "").upper())
    if not m:
        return None
    return {
        "start": parse_time(m.group(1)),
        "end": parse_time(m.group(2)) if m.group(2) else None,
        "estimated": bool(m.group(3)),
        "permanent": bool(m.group(4)),
    }


def _truthy(value) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _message(translations, kind: str, *keys: str) -> str:
    for entry in translations or []:
        if isinstance(entry, dict) and str(entry.get("type", "")).upper() == kind:
            for key in keys:
                if isinstance(entry.get(key), str) and entry[key].strip():
                    return entry[key].strip()
    return ""


def normalize(feature: Dict) -> Optional[Dict]:
    """One NMS GeoJSON feature as the record the display uses, or None when it
    carries no NOTAM."""
    core = ((feature or {}).get("properties") or {}).get("coreNOTAMData") or {}
    notam = core.get("notam")
    if not isinstance(notam, dict):
        return None
    translations = core.get("notamTranslation")
    text = " ".join(str(notam.get("text") or "").split())
    domestic = _message(translations, "LOCAL_FORMAT", "domestic_message", "simpleText", "formattedText")
    icao = _message(translations, "ICAO", "icao_message", "formattedText", "simpleText")
    raw = domestic or icao or text
    if not (text or raw):
        return None
    # The message's own header (!ACC number LOC) names the NOTAM as pilots know
    # it; the API's number field may carry the sequence number alone.
    header = HEADER_RE.match(domestic.upper())
    number = (header.group("number") if header else None) or str(notam.get("number") or "").strip()
    accountability = str(notam.get("accountId") or (header.group("acc") if header else "")).strip().upper()
    location = str(notam.get("location") or (header.group("loc") if header else "")).strip().upper()
    icao_location = str(notam.get("icaoLocation") or "").strip().upper()

    # Times come from the API's fields. Where a field is absent or unreadable the
    # validity the message itself states is used; where both are readable and
    # disagree, the fields are kept and the record says so.
    start = parse_time(notam.get("effectiveStart"))
    end_field = str(notam.get("effectiveEnd") or "").strip().upper()
    end = parse_time(end_field)
    permanent = end_field == "PERM"
    estimated = _truthy(notam.get("estimated")) or end_field.endswith("EST")
    stated = stated_validity(domestic)
    differ = False
    if stated:
        if start is None:
            start = stated["start"]
        elif stated["start"] is not None and abs(start - stated["start"]) >= TIME_TOLERANCE_S:
            differ = True
        if end is None and not permanent:
            end, permanent, estimated = stated["end"], stated["permanent"], estimated or stated["estimated"]
        elif end is not None and (stated["permanent"] or (stated["end"] is not None and abs(end - stated["end"]) >= TIME_TOLERANCE_S)):
            differ = True
        elif permanent and not stated["permanent"]:
            differ = True
    keyword = classify(text, domestic, str(notam.get("selectionCode") or ""))
    schedule = str(notam.get("schedule") or "").split("~")[-1].strip()
    return {
        "id": str(notam.get("id") or f"{accountability}-{number}-{notam.get('year') or ''}"),
        "number": number,
        "accountability": accountability,
        "location": location,
        "icao_location": icao_location,
        "classification": str(notam.get("classification") or "").strip().upper(),
        "kind": str(notam.get("type") or "").strip().upper(),   # N new, R replacement, C cancellation
        "type": keyword,
        "type_label": TYPE_LABEL[keyword],
        "issued": parse_time(notam.get("issued")),
        "updated": parse_time(notam.get("lastUpdated")),
        "start": start,
        "end": end,
        "permanent": permanent,
        "end_estimated": end is not None and estimated,
        "times_differ": differ,
        "cancelled": parse_time(notam.get("cancelationDate")),
        "schedule": schedule,
        "text": text,
        "raw": raw,
        "plain": {
            "text": decode(text or raw, protect=(location, icao_location, accountability)),
            "source": "rules",
        },
    }


def affects(record: Dict, ident: str) -> bool:
    """Whether the NOTAM's affected location is `ident`. A record that states no
    location is kept: the API selected it by location."""
    named = {record.get("location"), record.get("icao_location")} - {"", None}
    return not named or bool(named & set(query_forms(ident)))


def select(records: List[Dict], now: float, lookahead_h: float) -> List[Dict]:
    """The NOTAMs to show at `now`: in effect, or taking effect within the
    look-ahead; cancelled and expired ones dropped. Grouped by type in display
    order; within a type the ones in effect come first, most recently issued
    first, then the upcoming ones, soonest first."""
    horizon = now + lookahead_h * 3600
    out = []
    for r in records:
        if r["kind"] == "C" or (r["cancelled"] is not None and r["cancelled"] <= now):
            continue
        if r["end"] is not None and r["end"] <= now:
            continue
        start = r["start"]
        if start is not None and start > horizon:
            continue
        upcoming = start is not None and start > now
        out.append({**r, "status": "upcoming" if upcoming else "active"})

    def key(r):
        if r["status"] == "active":
            return (TYPE_ORDER[r["type"]], 0, -(r["issued"] or 0))
        return (TYPE_ORDER[r["type"]], 1, r["start"])

    return sorted(out, key=key)


class NmsSource:
    """The FAA NMS-API: client-credentials bearer token, NOTAMs by location as GeoJSON."""

    name = "faa"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._token: Optional[str] = None
        self._token_until = 0.0

    @property
    def configured(self) -> bool:
        return all(credentials())

    async def _bearer(self, force: bool = False) -> str:
        if self._token and not force and time.monotonic() < self._token_until:
            return self._token
        client_id, secret = credentials()
        if not (client_id and secret):
            raise NotConfigured("FAA NMS credentials are not set (HANGAR_NMS_CLIENT_ID and HANGAR_NMS_CLIENT_SECRET in .env)")
        resp = await self.client.post(
            f"{NMS_BASE}/v1/auth/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, secret),
            timeout=REQUEST_TIMEOUT_S,
        )
        if resp.status_code in (401, 403):
            raise RuntimeError(f"FAA NMS rejected the credentials (HTTP {resp.status_code})")
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise RuntimeError("FAA NMS token response carried no access_token")
        try:
            lifetime = float(body.get("expires_in") or 0)
        except (TypeError, ValueError):
            lifetime = 0.0
        self._token = token
        self._token_until = time.monotonic() + max(0.0, lifetime - TOKEN_MARGIN_S)
        return token

    async def _get(self, form: str) -> httpx.Response:
        async def once(force: bool) -> httpx.Response:
            return await self.client.get(
                f"{NMS_BASE}/nmsapi/v1/notams",
                params={"location": form},
                headers={"Authorization": f"Bearer {await self._bearer(force)}", "nmsResponseFormat": "GEOJSON"},
                timeout=REQUEST_TIMEOUT_S,
            )

        resp = await once(False)
        if resp.status_code == 401:   # the token expired early or was revoked
            resp = await once(True)
        return resp

    async def fetch(self, ident: str) -> List[Dict]:
        """Every NOTAM the API files under the location, as GeoJSON features, asked
        for under each spelling and merged by NOTAM id. A spelling the API does not
        know is skipped unless it does not know any of them."""
        features: Dict[str, Dict] = {}
        unknown: List[str] = []
        forms = query_forms(ident)
        for form in forms:
            resp = await self._get(form)
            if resp.status_code in UNKNOWN_LOCATION:
                unknown.append(form)
                continue
            resp.raise_for_status()
            body = resp.json() or {}
            for n, feature in enumerate((body.get("data") or {}).get("geojson") or []):
                notam = (((feature or {}).get("properties") or {}).get("coreNOTAMData") or {}).get("notam") or {}
                features.setdefault(str(notam.get("id") or f"{form}:{n}"), feature)
        if len(unknown) == len(forms):
            raise RuntimeError(f"FAA NMS does not know location {' or '.join(forms)}")
        return list(features.values())


class SampleSource:
    """Invented NOTAMs for previewing the view (data/notam_sample.json), in the
    shape the FAA API returns, with times relative to the request."""

    name = "sample"
    configured = True

    def __init__(self):
        with open(os.path.join(DATA_DIR, "notam_sample.json")) as fh:
            self._entries = json.load(fh)["notams"]

    @staticmethod
    def _at(now: float, offset) -> Optional[datetime]:
        m = OFFSET_RE.match(str(offset or ""))
        if not m:
            return None
        seconds = int(m.group(2)) * OFFSET_S[m.group(3)]
        at = now + seconds if m.group(1) == "+" else now - seconds
        return datetime.fromtimestamp(at - at % 60, tz=timezone.utc)

    async def fetch(self, ident: str) -> List[Dict]:
        now = time.time()
        forms = query_forms(ident)
        location = min(forms, key=len)
        iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")      # noqa: E731
        group = lambda dt: dt.strftime("%y%m%d%H%M")                # noqa: E731
        out = []
        for n, e in enumerate(self._entries):
            text = e["text"].replace("{LOC}", location)
            issued, updated = self._at(now, e["issued"]), self._at(now, e.get("updated") or e["issued"])
            start, end = self._at(now, e["start"]), self._at(now, e.get("end"))
            estimated = bool(e.get("estimated")) and end is not None
            validity = f"{group(start)}-{group(end) + ('EST' if estimated else '') if end else 'PERM'}"
            notam = {
                "id": f"SAMPLE_{location}_{n:02d}",
                "number": e["number"],
                "year": issued.strftime("%Y"),
                "type": "N",
                "issued": iso(issued),
                "lastUpdated": iso(updated),
                "location": location,
                "icaoLocation": max(forms, key=len),
                "effectiveStart": iso(start),
                "effectiveEnd": iso(end) if end else "PERM",
                "estimated": "true" if estimated else "false",
                "text": text,
                "classification": e.get("classification", "DOM"),
                "accountId": e["accountId"],
                "schedule": e.get("schedule") or "",
            }
            message = f"!{e['accountId']} {e['number']} {location} {text} {validity}"
            out.append({
                "type": "Feature",
                "properties": {"coreNOTAMData": {
                    "notam": notam,
                    "notamTranslation": [{"type": "LOCAL_FORMAT", "domestic_message": message}],
                }},
                "geometry": None,
            })
        return out
