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

import asyncio
import collections
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

import httpx

from .config import CONFIG_PATH, NMS_HOSTS
from .notam_decode import DATA_DIR, HEADER_RE, TYPE_LABEL, TYPE_ORDER, classify, decode
from .store import STORE

TOKEN_MARGIN_S = 60       # renew the bearer token this long before it expires
REQUEST_TIMEOUT_S = 45.0  # the API answers 408 itself after 30 s; that answer is waited for
# The FAA's limits for the NMS-API: one data pull every three minutes in production
# and never more than one request a second. Every environment is paced by the
# production limit, so a test environment shows what production will see.
PULL_INTERVAL_S = 180.0
REQUEST_SPACING_S = 1.0
BACKOFF_MAX_S = 3600.0    # a 429 lengthens the wait between pulls up to this
DELTA_OVERLAP_S = 60      # a pull of changes reaches back this far past the last one
DELTA_REACH_S = 3600      # a set older than this is fetched whole instead of patched
DELTA_WINDOW_S = 24 * 3600   # the API takes a lastUpdatedDate no further back than this
WINDOW_MARGIN_S = 60      # and one this close to the window's edge is not sent
BASELINE_S = 6 * 3600     # a location's whole set is fetched again this often
USAGE_PATH = os.path.join(os.path.dirname(CONFIG_PATH) or ".", "nms-usage.jsonl")
USAGE_KEEP = 6000         # rows kept: about twelve days at one pull every three minutes
PULL_KINDS = ("baseline", "delta")
UNKNOWN_LOCATION = (400, 404)
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$")
DIGITS_RE = re.compile(r"^(\d{4}|\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$")
VALIDITY_RE = re.compile(r"(\d{10})\s*-\s*(?:(\d{10})(EST)?|(PERM))\s*$")
TIME_TOLERANCE_S = 60     # fields and message agree when within a minute
OFFSET_RE = re.compile(r"^([+-])(\d+)([mhd])$")
OFFSET_S = {"m": 60, "h": 3600, "d": 86400}


class NotConfigured(RuntimeError):
    """No FAA credentials are stored for the environment."""


class RateLimited(RuntimeError):
    """The FAA answered 429."""


class CredentialsRejected(RuntimeError):
    """The FAA refused the client ID and secret."""


class TokenRefused(RuntimeError):
    """A data pull was answered 401; the token was renewed for the next pull."""


def credentials(environment: str) -> tuple[str, str]:
    """The client ID and secret stored for this environment (empty when none are)."""
    return STORE.nms_pair(environment)


US_ACCOUNT_MAX = 4        # a US accountability is a domestic (3) or ICAO (4) identifier


def is_foreign(record: Dict) -> bool:
    """Whether a NOTAM was issued outside the US: its accountability is the AFTN
    address of a foreign NOTAM office, longer than any US account. The
    classification does not tell: the US issues its own international series."""
    return len(record.get("accountability") or "") > US_ACCOUNT_MAX


DOMESTIC_CLASSES = ("DOM", "FDC")
INTERNATIONAL_CLASS = "INTL"


def repeats(records: List[Dict], now: Optional[float] = None) -> set:
    """The ids of the US international-series NOTAMs among `records` that repeat
    a domestic or FDC NOTAM among them: the same text, start and end. With
    `now`, only NOTAMs not cancelled or ended by then count, on either side."""
    def key(r: Dict) -> tuple:
        return " ".join(r["text"].split()), r["start"], r["end"]

    live = [r for r in records if now is None or not _over(r, now)]
    domestic = {key(r) for r in live if r["classification"] in DOMESTIC_CLASSES}
    return {r["id"] for r in live
            if r["classification"] == INTERNATIONAL_CLASS and not is_foreign(r) and key(r) in domestic}


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


def affects(record: Dict, ident: str, strict: bool = False) -> bool:
    """Whether the NOTAM's affected location is `ident`. A record that states no
    location is kept when the API selected it by location, and never when it
    came from a pull of every changed NOTAM (`strict`)."""
    named = {record.get("location"), record.get("icao_location")} - {"", None}
    if not named:
        return not strict
    return bool(named & set(query_forms(ident)))


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


def _over(record: Dict, now: float) -> bool:
    return record["kind"] == "C" or record.get("cancelled") is not None or (record["end"] is not None and record["end"] <= now)


UNSETTLED = ("gone", "appeared", "changed")
MISSED = {"gone": "missed_removal", "appeared": "missed_new", "changed": "missed_change"}


def unsettled_ids(merged: List[Dict], fresh: List[Dict], now: float, synced: float) -> Dict[str, List[str]]:
    """The NOTAMs in which a location's set, kept current by pulls of changes,
    differs from the whole set fetched at `now` in a way the whole set cannot
    explain: `gone`, held as in effect and no longer listed; `appeared`, listed
    and not held; `changed`, listed otherwise than held. A NOTAM updated at or
    after the last pull (`synced`) was changed after it and is not among them;
    the API gives update times to the minute only, so an earlier one may still
    have been changed after the last pull."""
    listed = {r["id"]: r for r in fresh}
    held = {r["id"]: r for r in merged}
    out: Dict[str, List[str]] = {k: [] for k in UNSETTLED}
    for key, old in held.items():
        if key not in listed and not _over(old, now):
            out["gone"].append(key)
    for key, new in listed.items():
        if (new.get("updated") or 0) >= synced:
            continue
        old = held.get(key)
        if old is None:
            out["appeared"].append(key)
        elif (old.get("updated"), old.get("cancelled")) != (new.get("updated"), new.get("cancelled")):
            out["changed"].append(key)
    return out


def compare_sets(merged: List[Dict], fresh: List[Dict], now: float, synced: float) -> Dict[str, int]:
    """How a location's set, kept current by pulls of changes, differs from the
    whole set fetched at `now`, by count. `same`; `ended`, which the API stops
    listing; `newer`, updated at or after the last pull (`synced`); and the
    unsettled classes of unsettled_ids(). Those are settled by the next pull of
    changes, which reaches back to before the last pull: a NOTAM it reports was
    changed after the last pull, one it does not report was missed (MISSED)."""
    out = {"same": 0, "ended": 0, "newer": 0, **{k: 0 for k in UNSETTLED}}
    have = {r["id"]: r for r in merged}
    want = {r["id"]: r for r in fresh}
    for key, old in have.items():
        new = want.get(key)
        if new is None:
            out["ended" if _over(old, now) else "gone"] += 1
        elif (old.get("updated"), old.get("cancelled")) == (new.get("updated"), new.get("cancelled")):
            out["same"] += 1
        else:
            out["newer" if (new.get("updated") or 0) >= synced else "changed"] += 1
    for key, new in want.items():
        if key not in have:
            out["newer" if (new.get("updated") or 0) >= synced else "appeared"] += 1
    return out


def settle(unseen: Dict[str, List[str]], pulled: set) -> Dict[str, int]:
    """Settle unsettled NOTAMs by a pull of changes reaching back to before the
    last pull: `newer` when it carries them, otherwise missed, by class."""
    out = {"newer": 0, **{MISSED[k]: 0 for k in UNSETTLED}}
    for kind, ids in unseen.items():
        for key in ids:
            out["newer" if key in pulled else MISSED[kind]] += 1
    return out


class PullGate:
    """Paces the requests to the NMS-API: data pulls at least PULL_INTERVAL_S
    apart, any two requests at least REQUEST_SPACING_S apart. The time of the
    last pull before a restart is carried over, so a restart never shortens an
    interval. A 429 lengthens the wait, doubling up to BACKOFF_MAX_S."""

    def __init__(self, last_pull_wall: Optional[float] = None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, sleep=asyncio.sleep):
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self._next = clock() + self._carried(last_pull_wall)   # when the next pull may start
        # the last pull on this clock, so that the first interval after a restart is measured too
        self._last_pull: Optional[float] = (
            None if last_pull_wall is None else clock() - max(0.0, wall() - last_pull_wall))
        self._last_request: Optional[float] = None
        self._penalty = 0.0

    def _carried(self, last_pull_wall: Optional[float]) -> float:
        if last_pull_wall is None:
            return 0.0
        elapsed = self.wall() - last_pull_wall
        if elapsed < 0:      # the clock was set back since: wait a whole interval
            return PULL_INTERVAL_S
        return max(0.0, PULL_INTERVAL_S - elapsed)

    def wait_s(self) -> float:
        """Seconds until a data pull may start."""
        return max(0.0, self._next - self.clock())

    @property
    def limited(self) -> bool:
        return self._penalty > 0

    async def wait_turn(self) -> None:
        while (wait := self.wait_s()) > 0:
            await self.sleep(min(wait, 30.0))

    async def space(self) -> None:
        """Wait until a request of any kind may be sent, and count it."""
        if self._last_request is not None:
            while (wait := self._last_request + REQUEST_SPACING_S - self.clock()) > 0:
                await self.sleep(wait)
        self._last_request = self.clock()

    def mark_pull(self) -> Optional[float]:
        """Count a data pull that starts now. -> seconds since the previous one."""
        now = self.clock()
        if now < self._next:
            raise RuntimeError("a data pull was started before its turn")
        gap = None if self._last_pull is None else now - self._last_pull
        self._last_pull = now
        self._next = now + max(PULL_INTERVAL_S, self._penalty)
        return gap

    def rate_limited(self, retry_after: Optional[float] = None) -> float:
        """The FAA answered 429: lengthen the wait. -> seconds until the next pull."""
        self._penalty = min(BACKOFF_MAX_S, max(2 * PULL_INTERVAL_S, 2 * self._penalty))
        self._next = max(self._next, self.clock() + max(self._penalty, retry_after or 0.0))
        return self.wait_s()

    def answered(self) -> None:
        """A pull was served: the usual interval applies again, from that pull."""
        if self._penalty and self._last_pull is not None:
            self._next = self._last_pull + PULL_INTERVAL_S
        self._penalty = 0.0


class UsageLog:
    """Every request sent to the NMS-API, one JSON line each, appended as it is
    made: the record that the display stays inside the FAA's limits."""

    def __init__(self, path: Optional[str] = None, keep: int = USAGE_KEEP):
        self.path = path or USAGE_PATH
        self.keep = keep
        self.rows: Deque[Dict[str, Any]] = collections.deque(maxlen=keep)
        self._lines = 0
        self.failing = False
        try:
            with open(self.path) as fh:
                for line in fh:
                    self._lines += 1
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("t"), (int, float)):
                        self.rows.append(row)
        except OSError:
            pass

    def last_pull(self) -> Optional[float]:
        return next((r["t"] for r in reversed(self.rows) if r.get("kind") in PULL_KINDS), None)

    def add(self, row: Dict[str, Any]) -> None:
        self.rows.append(row)
        try:
            if self._lines >= 2 * self.keep:
                tmp = self.path + ".tmp"
                with open(tmp, "w") as fh:
                    fh.writelines(json.dumps(r, separators=(",", ":")) + "\n" for r in self.rows)
                os.replace(tmp, self.path)
                self._lines = len(self.rows)
            else:
                with open(self.path, "a") as fh:
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
                self._lines += 1
            self.failing = False
        except OSError:
            self.failing = True

    def summary(self, now: float, window_s: Optional[float] = 86400.0, env: Optional[str] = None) -> Dict[str, Any]:
        """Totals over the last `window_s` seconds, or over the whole record (None),
        of the requests to one environment, or to all (None)."""
        rows = [r for r in self.rows
                if (window_s is None or r["t"] >= now - window_s) and (env is None or r.get("env") == env)]
        pulls = [r for r in rows if r.get("kind") in PULL_KINDS]
        gaps = [r["gap"] for r in pulls if isinstance(r.get("gap"), (int, float))]
        return {
            "window_s": window_s,
            "environment": env,
            "first": rows[0]["t"] if rows else None,
            "last": rows[-1]["t"] if rows else None,
            "pulls": len(pulls),
            "baselines": sum(1 for r in pulls if r.get("kind") == "baseline"),
            "deltas": sum(1 for r in pulls if r.get("kind") == "delta"),
            "tokens": sum(1 for r in rows if r.get("kind") == "token"),
            "shortest_gap_s": round(min(gaps), 1) if gaps else None,
            "limit_gap_s": PULL_INTERVAL_S,
            "status_401": sum(1 for r in rows if r.get("status") == 401),
            "status_429": sum(1 for r in rows if r.get("status") == 429),
            "failed": sum(1 for r in rows if "status" in r and not 200 <= r["status"] < 300
                          and r["status"] not in UNKNOWN_LOCATION),
            "compared": sum(1 for r in rows if r.get("kind") == "compare"),
            "mismatches": sum(sum(v for k, v in (r.get("diff") or {}).items() if k.startswith("missed_")) for r in rows),
        }


class NmsSource:
    """The FAA NMS-API: a client-credentials bearer token, a location's NOTAMs
    and the NOTAMs changed since a time, both as GeoJSON. Every request goes
    through the gate and into the usage log."""

    name = "faa"

    def __init__(self, client: httpx.AsyncClient, gate: Optional[PullGate] = None, usage: Optional[UsageLog] = None,
                 environment: str = "production"):
        self.client = client
        self.usage = usage if usage is not None else UsageLog()
        self.gate = gate if gate is not None else PullGate(self.usage.last_pull())
        self.environment = environment
        self._token: Optional[str] = None
        self._token_until = 0.0
        self._sending: Optional[asyncio.Lock] = None

    def use(self, environment: str) -> None:
        if environment != self.environment:
            self.environment = environment
            self.reset_token()

    def reset_token(self) -> None:
        self._token, self._token_until = None, 0.0

    @property
    def base(self) -> str:
        return NMS_HOSTS[self.environment]

    @property
    def configured(self) -> bool:
        return all(credentials(self.environment))

    def _lock(self) -> asyncio.Lock:
        if self._sending is None:
            self._sending = asyncio.Lock()
        return self._sending

    def _log(self, kind: str, status: int, started: float, resp: Optional[httpx.Response] = None,
             env: Optional[str] = None, **more) -> None:
        """Record a request; `rid` is the id the API gave it (x-request-id)."""
        row = {
            "t": round(started, 1), "kind": kind, "env": env or self.environment, "status": status,
            "bytes": len(resp.content) if resp is not None else 0,
            "ms": int((self.gate.wall() - started) * 1000), **more,
        }
        rid = resp.headers.get("x-request-id") if resp is not None else None
        if rid:
            row["rid"] = rid[:64]
        self.usage.add(row)

    async def _ask_token(self, environment: str) -> tuple[str, float]:
        """Ask an environment for a bearer token with the pair stored for it (not a
        data pull). -> the token and its lifetime. The caller holds the lock."""
        client_id, secret = credentials(environment)
        if not (client_id and secret):
            raise NotConfigured(f"no FAA credentials are stored for the {environment} environment")
        await self.gate.space()
        started = self.gate.wall()
        try:
            resp = await self.client.post(
                f"{NMS_HOSTS[environment]}/v1/auth/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, secret),
                timeout=REQUEST_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            self._log("token", 0, started, env=environment, error=exc.__class__.__name__)
            raise RuntimeError(f"FAA NMS token request failed: {exc.__class__.__name__}") from None
        self._log("token", resp.status_code, started, resp, env=environment)
        if resp.status_code in (400, 401, 403):
            raise CredentialsRejected(f"FAA NMS rejected the credentials (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise RuntimeError(f"FAA NMS token request answered HTTP {resp.status_code}")
        try:
            body = resp.json()
            token = body.get("access_token")
        except (ValueError, AttributeError):
            token, body = None, {}
        if not token:
            raise RuntimeError("FAA NMS token response carried no access_token")
        try:
            lifetime = float(body.get("expires_in") or 0)
        except (TypeError, ValueError):
            lifetime = 0.0
        return token, lifetime

    async def _new_token(self) -> str:
        """A bearer token for the environment in use, kept until it nearly expires.
        The caller holds the lock."""
        token, lifetime = await self._ask_token(self.environment)
        self._token = token
        self._token_until = self.gate.clock() + max(0.0, lifetime - TOKEN_MARGIN_S)
        return token

    async def _bearer(self) -> str:
        if self._token and self.gate.clock() < self._token_until:
            return self._token
        return await self._new_token()

    async def check_credentials(self, environment: Optional[str] = None) -> Dict[str, Any]:
        """Whether the pair stored for an environment (the one in use when none is
        named) gets a token there. Costs no data pull; only a token for the
        environment in use is kept."""
        environment = environment or self.environment
        async with self._lock():
            try:
                if environment == self.environment:
                    await self._new_token()
                else:
                    await self._ask_token(environment)
            except (NotConfigured, RuntimeError) as exc:
                return {"ok": False, "environment": environment, "error": str(exc)}
        return {"ok": True, "environment": environment, "error": None}

    async def _pull(self, kind: str, params: Dict[str, str], since: Optional[float] = None, **what) -> httpx.Response:
        """One data pull, at its turn. A 401 renews the token for the next pull
        and a 429 lengthens the wait; neither is retried here. `since` is the
        time a pull of changes reaches back to: outside the window the API
        takes when the pull's turn has come, nothing is sent."""
        await self.gate.wait_turn()     # outside the lock: a credentials check need not wait for a turn
        async with self._lock():
            await self.gate.wait_turn()
            token = await self._bearer()
            await self.gate.space()
            if since is not None:
                now = self.gate.wall()
                if not now - DELTA_WINDOW_S + WINDOW_MARGIN_S <= since <= now:
                    raise ValueError("changes can only be asked for the last 24 hours")
            gap = self.gate.mark_pull()
            started = self.gate.wall()
            try:
                resp = await self.client.get(
                    f"{self.base}/nmsapi/v1/notams",
                    params=params,
                    headers={"Authorization": f"Bearer {token}", "nmsResponseFormat": "GEOJSON"},
                    timeout=REQUEST_TIMEOUT_S,
                )
            except httpx.HTTPError as exc:
                self._log(kind, 0, started, gap=None if gap is None else round(gap, 1), error=exc.__class__.__name__, **what)
                raise RuntimeError(f"FAA NMS request failed: {exc.__class__.__name__}") from None
            self._log(kind, resp.status_code, started, resp, gap=None if gap is None else round(gap, 1), **what)
            if resp.status_code == 429:
                try:
                    retry_after = float(resp.headers.get("retry-after") or 0)
                except ValueError:
                    retry_after = 0.0
                wait = self.gate.rate_limited(retry_after)
                raise RateLimited(f"the FAA is limiting requests (HTTP 429); next pull in {round(wait)} s")
            if resp.status_code == 401:
                self.reset_token()
                try:
                    await self._new_token()
                except CredentialsRejected:
                    raise
                except (NotConfigured, RuntimeError):
                    pass
                raise TokenRefused("FAA NMS refused the access token (HTTP 401); it was renewed for the next pull")
            if resp.status_code < 400:
                self.gate.answered()
            return resp

    @staticmethod
    def _features(resp: httpx.Response) -> List[Dict]:
        """The NOTAMs of an answer. Only an answer that states success, lists no
        error and carries its data as GeoJSON is a set of NOTAMs; anything else
        is an error, never "no NOTAMs"."""
        try:
            body = resp.json()
            status, errors, data = body.get("status"), body.get("errors"), body.get("data")
        except (ValueError, AttributeError):
            raise RuntimeError("FAA NMS answer is not the expected JSON") from None
        if errors or str(status or "").strip().lower() != "success":
            first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
            detail = " ".join(str(first.get(k) or "").strip() for k in ("code", "message")).strip()
            raise RuntimeError(f"FAA NMS reported a failure{': ' + detail[:160] if detail else ''}")
        if not isinstance(data, dict):
            raise RuntimeError("FAA NMS answer is not the expected JSON")
        features = data.get("geojson")
        if features is None and data.get("url"):
            # A link to a file stands for a set too large to be sent inline.
            raise RuntimeError("FAA NMS answered with a file link instead of NOTAMs")
        if features is None and data.get("aixm") is not None:
            raise RuntimeError("FAA NMS answered in AIXM instead of GeoJSON")
        if not isinstance(features, list) and features is not None:
            raise RuntimeError("FAA NMS answer is not the expected JSON")
        return [f for f in features or [] if isinstance(f, dict)]

    async def baseline(self, form: str, ident: str) -> Optional[List[Dict]]:
        """Every NOTAM the API files under one spelling of a location, as GeoJSON
        features; None when the API does not know the spelling."""
        resp = await self._pull("baseline", {"location": form}, what=ident, form=form)
        if resp.status_code in UNKNOWN_LOCATION:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(f"FAA NMS answered HTTP {resp.status_code} for {form}")
        return self._features(resp)

    async def changes(self, since: float) -> List[Dict]:
        """Every NOTAM changed since `since` (epoch seconds), any location."""
        stamp = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = await self._pull("delta", {"lastUpdatedDate": stamp}, since=since, what=stamp)
        if resp.status_code >= 400:
            raise RuntimeError(f"FAA NMS answered HTTP {resp.status_code} for changes since {stamp}")
        return self._features(resp)


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
                    "notamTranslation": [{"type": "LOCAL_FORMAT", "simpleText": message}],
                }},
                "geometry": None,
            })
        return out
