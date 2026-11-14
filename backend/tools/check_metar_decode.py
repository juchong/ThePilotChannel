#!/usr/bin/env python3
"""Check the raw-METAR decoder (backend/app/metar_decode.py) against
aviationweather.gov's own decode of the same reports, and list what it leaves unread.
The reports are not part of the repository; run this in the app image, which has the
backend's dependencies:

  docker run --rm -v "$PWD/backend:/b:ro" -v DIR:/d -w /b hangar-display:latest \\
      python tools/check_metar_decode.py --fetch /d/awc.jsonl      # download a set
  ... python tools/check_metar_decode.py /d/awc.jsonl [--gateway /d/cycles] [--show N]

--fetch OUT [HOURS] writes aviationweather.gov's decoded JSON, one record per line,
for every station in its bulk file of current METARs (at most 400 records come back
per request, so HOURS above 1 gives a sample of the older reports, not all of them).
--gateway DIR adds the report texts of the NWS gateway's hourly files
(https://tgftp.nws.noaa.gov/data/observations/metar/cycles/00Z.TXT to 23Z.TXT, saved
in DIR) to the unread-group audit; they carry no decode to compare with.

For every record the decoder's wind, visibility, altimeter, temperature, dew point,
cloud layers, observation time and flight category are compared with the record's.
A difference counts as explained only when it falls in one of the classes below, each
a case where the report's own text settles the matter:
  - a wind, visibility, cloud or tenths-of-a-degree group that is not in code form,
    which the decoder leaves out;
  - a visibility with NDV, a vertical visibility with a slashed suffix, a wind whose
    gust is slashed out, or a temperature whose dew point is slashed out, which the
    record omits and the code form defines;
  - cloud layers beyond the fourth, layers with base 000, and layers beside a
    vertical visibility, which the record drops;
  - a flight category the record gives although a cloud group without amount or
    height leaves the ceiling open, or withholds although the reported elements
    settle it, or takes from layers or a visibility it read differently.
Every body group that fed no field is counted by kind; a group of no known kind is
listed.

A record without a flight category lacks the sky condition or the visibility. The
category the display gives it (weather.normalize_metar) is compared with the one the
report's text settles: it is the same, or withheld because the report carries a
visibility or a cloud height the record lacks, or a visibility with NDV for which the
record takes another group.

Exit status 1 when a difference is unexplained or a report raises.

--fixture OUT [N] writes backend/tests/data/metar_reports.json from the given files:
going through the records in order of station and text, a record is taken while one
of the report forms in FORMS, its flight category, or the element a record without
a category carries, has been seen fewer than N times (default 3). Each case holds the report, the record's fields, and the fields
the test compares: all but those where the record falls in a class above.
"""
from __future__ import annotations

import collections
import csv
import glob
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.metar_decode import UNLIMITED_FT, _CLOUD_UNREADABLE, _TENTHS, _WIND, decode, observed_at  # noqa: E402
from app.weather import category_in_range, normalize_metar  # noqa: E402

USER_AGENT = "the-pilot-channel/0.2 (+https://github.com/juchong/ThePilotChannel)"
BULK_CSV = "https://aviationweather.gov/data/cache/metars.cache.csv.gz"
API = "https://aviationweather.gov/api/data/metar"
FIELDS = ["wdir", "wspd", "wgst", "visib", "altim", "temp", "dewp", "clouds", "obsTime", "fltCat"]

_WX = r"(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)"
# Body groups the decoded fields do not come from, by kind (FM 15 and FMH-1 group names).
UNREAD_KINDS = [
    ("report modifier", r"AUTO|COR|RTD|CC[A-Z]"),
    ("variable wind direction", r"\d{3}V\d{3}"),
    ("runway visual range", r"R\d{2}[LRC]?/[MP]?\d{4}(?:V[MP]?\d{4})?(?:FT)?(?:/?[UDN])?|R\d{2}[LRC]?/{2,}\S*|R/{4,}|RVRNO"),
    ("present or recent weather", rf"(?:RE)?[+-]?(?:VC)?(?:MI|PR|BC|DR|BL|SH|TS|FZ)*{_WX}*|NSW|RE//"),
    ("element not observed", r"/{2,}|M|A/{4}|Q/{4}|/{5}(?:KT|MPS)|/{4}SM|/+(?:CB|TCU)"),
    ("second or directional visibility", r"\d{4}(?:[NSEW]{1,2}|NDV|//)?"),
    ("second altimeter group", r"[AQ]\d{4}"),
    ("runway state", r"R?\d{2}[LRC]?/(?:\d{6}|[\d/]{6}|CLRD\d{2}|CLRD//|SNOCLO)|R/SNOCLO|SNOCLO|\d{8}"),
    ("sea state", r"W(?:\d{2}|//)/(?:[SH]\d{1,3}|[SH]/{1,3}|/{2,3})"),
    ("wind shear", r"WS|ALL|RWY|RWY\d{2}[LRC]?|R\d{2}[LRC]?"),
    ("colour state", r"(?:BLACK)?(?:BLU|WHT|GRN|YLO[12]?|AMB|RED)\+?"),
]


_B = r"(?<!\S)"
_E = r"(?!\S)"
# Report forms the test fixture must cover, as patterns over the body of a report.
FORMS = {
    "calm wind": rf"{_B}00000(?:KT|MPS){_E}",
    "variable wind": rf"{_B}VRB\d{{2}}",
    "gust": r"\dG\d{2,3}(?:KT|MPS)",
    "wind in MPS": rf"\dMPS{_E}",
    "wind of 9 MPS or more": rf"\d{{3}}(?:09|[1-9]\d)(?:G\d+)?MPS{_E}",
    "direction 000 with a speed": rf"{_B}000(?:0[1-9]|[1-9]\d)(?:G\d+)?KT",
    "direction 360": rf"{_B}360\d{{2}}",
    "three-digit wind speed": rf"{_B}\d{{6}}(?:G\d+)?KT",
    "wind slashed out": rf"{_B}/////(?:KT|MPS)",
    "wind with E prefix": rf"{_B}E\d{{5}}KT",
    "wind group not in code form": rf"{_B}(?!\d{{5,6}}(?:G\d{{2,3}})?KT)(?!VRB)(?!E\d)(?!/)\S*\d\S*KT{_E}",
    "visibility in metres": rf"(?:KT|MPS|V\d{{3}}) (?!9999)\d{{4}}{_E}",
    "visibility 9999": rf"{_B}9999{_E}",
    "visibility 0000": rf"{_B}0000{_E}",
    "CAVOK": rf"{_B}CAVOK{_E}",
    "visibility with NDV": r"\d{4}NDV",
    "second or directional visibility": rf"{_B}\d{{4}} \d{{4}}[NSEW]{{0,2}}{_E}",
    "whole statute miles": rf"{_B}[1-9]SM{_E}",
    "10SM": rf"{_B}10SM{_E}",
    "more than 10SM": rf"{_B}(?:1[1-9]|[2-9]\d)SM{_E}",
    "three-digit statute miles": rf"{_B}\d{{3}}SM{_E}",
    "gust slashed out": rf"\dG//(?:KT|MPS){_E}",
    "fraction of a statute mile": rf"(?:KT|V\d{{3}}) \d/\d{{1,2}}SM",
    "whole and fraction statute miles": rf"{_B}\d \d/\dSM",
    "less than a statute-mile value": rf"{_B}M\d/\dSM",
    "visibility in KM": rf"{_B}(?!10KM)\d{{1,2}}KM{_E}",
    "visibility of 10KM": rf"{_B}10KM{_E}",
    "eighths or sixteenths of a statute mile": rf"\d/(?:8|16)SM",
    "visibility slashed out": rf"{_B}////(?:SM)?{_E}",
    "FEW layer": rf"{_B}FEW\d{{3}}",
    "SCT layer": rf"{_B}SCT\d{{3}}",
    "BKN layer": rf"{_B}BKN\d{{3}}",
    "OVC layer": rf"{_B}OVC\d{{3}}",
    "vertical visibility": rf"{_B}VV\d{{3}}",
    "vertical visibility slashed": rf"{_B}VV///",
    "cloud base 000": rf"{_B}(?:FEW|SCT|BKN|OVC|VV)000",
    "cloud height slashed": rf"{_B}(?:FEW|SCT|BKN|OVC)///",
    "cloud amount slashed": rf"{_B}///\d{{3}}",
    "cloud type suffix": rf"{_B}(?:FEW|SCT|BKN|OVC)\d{{3}}(?:CB|TCU)",
    "cloud type slashed": rf"{_B}(?:FEW|SCT|BKN|OVC)\d{{3}}///",
    "convective cloud without amount or height": rf"{_B}/+(?:CB|TCU){_E}",
    "sky slashed out": rf"{_B}/{{6}}(?:///)?{_E}",
    "five or more layers": rf"(?:{_B}(?:FEW|SCT|BKN|OVC)\d{{3}}\S*\s+){{5}}",
    "SKC": rf"{_B}SKC{_E}",
    "CLR": rf"{_B}CLR{_E}",
    "NSC": rf"{_B}NSC{_E}",
    "NCD": rf"{_B}NCD{_E}",
    "negative temperature": rf"{_B}M\d{{2}}/",
    "negative dew point": rf"\d{{2}}/M\d{{2}}{_E}",
    "dew point missing": rf"{_B}M?\d{{2}}/{_E}",
    "dew point slashed out": rf"{_B}M?\d{{2}}///{_E}",
    "temperature slashed out": rf"(?:\d|/|[A-Z]) /////{_E}",
    "altimeter in inches": rf"{_B}A\d{{4}}{_E}",
    "altimeter in hPa": rf"{_B}Q\d{{4}}{_E}",
    "hPa group before the inches group": rf"{_B}Q\d{{4}} A\d{{4}}{_E}",
    "inches group before the hPa group": rf"{_B}A\d{{4}} Q\d{{4}}{_E}",
    "altimeter slashed out": rf"{_B}[AQ]////{_E}",
    "AUTO": rf"{_B}AUTO{_E}",
    "COR": rf"{_B}(?:COR|CC[A-Z]){_E}",
    "SPECI": r"^SPECI ",
    "variable wind direction": rf"{_B}\d{{3}}V\d{{3}}{_E}",
    "runway visual range": rf"{_B}R\d{{2}}[LRC]?/",
    "present weather": rf"{_B}[+-]?(?:VC)?(?:MI|PR|BC|DR|BL|SH|TS|FZ)*{_WX}+{_E}",
}
# Forms that lie outside the body or need more than a pattern.
WHOLE_REPORT_FORMS = {
    "trend with forecast groups": r" (?:TEMPO|BECMG)(?: \S+)* (?:\d{4}|[A-Z]{3}\d{3}|VV///)",
    "NOSIG": r" NOSIG",
    "time 2400Z": r"^(?:METAR |SPECI )?\S{4} \d{2}2400Z",
    "tenths group": r" RMK .*(?<!\S)T[01]\d{3}[01]\d{3}",
    "tenths group below zero": r" RMK .*(?<!\S)T1\d{3}",
    "tenths group without dew point": r" RMK .*(?<!\S)T[01]\d{3}(?!\d)",
    "trailing punctuation": r"[;=]$",
}


def forms_of(raw: str, rec: dict) -> list:
    body = " ".join(body_of(raw))
    found = [name for name, pattern in FORMS.items() if re.search(pattern, body)]
    found += [name for name, pattern in WHOLE_REPORT_FORMS.items() if re.search(pattern, raw)]
    tenths = re.search(r" RMK .*(?<!\S)T([01])(\d{3})", raw)
    whole = re.search(rf"{_B}(M?)(\d{{2}})/", body)
    if tenths and whole:
        off = abs((-1 if tenths.group(1) == "1" else 1) * int(tenths.group(2)) / 10 - (-1 if whole.group(1) else 1) * int(whole.group(2)))
        if off > 1.0:
            found.append("tenths group more than a degree off the body")
        elif off > 0.5:
            found.append("tenths group over half a degree off the body")
    both = re.search(rf"{_B}A(\d{{4}}) Q(\d{{4}}){_E}|{_B}Q(\d{{4}}) A(\d{{4}}){_E}", body)
    if both:
        inches, hpa = (both.group(1), both.group(2)) if both.group(1) else (both.group(4), both.group(3))
        if abs(int(inches) / 100 * 25.4 * 4 / 3 - int(hpa)) > 0.3:
            found.append("altimeter groups a few tenths of a hPa apart")
    found.append(f"category {rec.get('fltCat')}")
    if not rec.get("fltCat"):
        shown = normalize_metar(rec, 0)
        if rec.get("clouds") is None and shown["visibility_sm"] is not None:
            found.append("no sky condition, visibility " + ("below 1 SM" if shown["visibility_sm"] < 1 else "of 1 SM or more"))
        if rec.get("clouds") is not None and shown["visibility_sm"] is None:
            low = shown["ceiling_ft"] is not None and shown["ceiling_ft"] < 500
            found.append("no visibility, " + ("ceiling below 500 ft" if low else "no ceiling below 500 ft"))
    return found


def partial_record(rec: dict, dec: dict, settled, raw: str):
    """How the category the display gives a record without one stands to the category
    the report settles; None when nothing in the report explains a difference."""
    shown = normalize_metar(rec, 0)["category"]
    if shown == settled:
        return "the category the report settles" if shown else "none, as the report settles none"
    if shown is None and rec.get("visib") is None and dec.get("visib") is not None:
        return "none: the report carries a visibility the record lacks"
    if shown is None and rec.get("visib") != dec.get("visib") and any(re.fullmatch(r"\d{4}NDV", t) for t in body_of(raw)):
        return "none: the record takes another group for a visibility with NDV"
    if shown is None and rec.get("clouds") is None and tuple(dec["ceiling_range"]) != (0, UNLIMITED_FT):
        return "none: the report carries a cloud height the record lacks"
    return None


def fetch(out: str, hours: str) -> None:
    def get(url: str) -> bytes:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=90) as r:
            return r.read()

    rows = csv.DictReader(io.StringIO(gzip.decompress(get(BULK_CSV)).decode("utf-8", "replace")))
    ids = sorted({row["station_id"] for row in rows if row.get("station_id")})
    total = 0
    with open(out, "w") as fh:
        for i in range(0, len(ids), 250):
            query = {"ids": ",".join(ids[i:i + 250]), "format": "json"}
            if hours:
                query["hours"] = hours
            body = get(API + "?" + urllib.parse.urlencode(query))
            for rec in json.loads(body) if body.strip() else []:
                fh.write(json.dumps(rec) + "\n")
                total += 1
            print(f"\r{total} records", end="", file=sys.stderr)
            time.sleep(1.5)
    print(file=sys.stderr)


def body_of(raw: str) -> list:
    out = []
    for tok in raw.split():
        if tok in ("RMK", "NOSIG", "BECMG", "TEMPO", "INTER"):
            break
        out.append(tok.rstrip(";="))
    return out


def same(field: str, a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= 0.005
    return a == b


def explain(field: str, awc, ours, raw: str, dec: dict, rec: dict):
    """The class a difference belongs to, or None when nothing in the report explains it."""
    body = body_of(raw)
    ndv = any(re.fullmatch(r"\d{4}NDV", t) for t in body)
    if field in ("wdir", "wspd", "wgst") and ours is None and "wspd" not in dec:
        return "wind group not in code form, or with a speed no wind reaches: left out"
    if field in ("wdir", "wspd") and awc is None and any(re.fullmatch(r"(?:\d{3}|VRB)\d{2,3}G//(?:KT|MPS)", t) for t in body):
        return "wind with the gust slashed out: direction and speed read"
    if field == "visib":
        if ndv and ours is not None:
            return "visibility with NDV: read"
        if ours is None and "visib" not in dec:
            return "visibility group not in code form: left out"
    if field in ("temp", "dewp") and awc is None and any(re.fullmatch(r"M?\d{2}///", t) for t in body):
        return "temperature with the dew point slashed out: read"
    if field in ("temp", "dewp") and " RMK " in raw:
        remarks = [t.rstrip(";=") for t in raw.split(" RMK ", 1)[1].split()]
        if any(re.match(r"T[01]\d{3}", t) and not _TENTHS.match(t) for t in remarks):
            return "tenths group not in code form: the body's whole degrees stand"
    if field == "clouds" and ours is None and any(_CLOUD_UNREADABLE.match(t) for t in body):
        return "cloud group not in code form: left out, the ceiling left open"
    if field == "clouds" and ours is not None:
        kept = [layer for layer in ours if layer[1] != 0 or layer[0] == "OVX"]
        if awc is None and any(re.fullmatch(r"VV\d{3}///", t) for t in body):
            return "vertical visibility with a slashed suffix: read"
        if awc == [layer for layer in ours if layer[0] == "OVX"] != ours:
            return "cloud layers beside a vertical visibility: kept"
        if awc == ours[:4]:
            return "more than four cloud layers: all kept"
        if awc == kept[:4]:
            return "cloud layer with base 000: kept"
    if field == "fltCat":
        open_ceiling = dec["ceiling_range"][0] != dec["ceiling_range"][1]
        open_vis = dec["visibility_range"][0] == 0.0 and dec["visibility_range"][1] > 90
        if ours is None and awc is not None and (open_ceiling or open_vis):
            return "category given by the record although the report leaves the ceiling or visibility open: withheld"
        if ndv and awc != ours and rec.get("visib") != dec.get("visib"):
            return "category from a visibility with NDV, which the record does not read"
        layers = [(c["cover"], c["base"]) for c in dec.get("clouds") or []]
        if awc is not None and ours is not None and [(c.get("cover"), c.get("base")) for c in rec.get("clouds") or []] != layers:
            return "category from the layers the record drops: the report's lowest ceiling decides"
        if awc is None and ours is not None and (rec.get("visib") is None or rec.get("clouds") is None):
            return "category the record withholds for a missing element although the report settles it: given"
    return None


def kind_of(tok: str) -> str:
    for name, pattern in UNREAD_KINDS:
        if re.fullmatch(pattern, tok):
            return name
    return ""


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--fetch"]:
        fetch(args[1], args[2] if len(args) > 2 else "")
        return 0
    show = int(args[args.index("--show") + 1]) if "--show" in args else 3
    gateway = args[args.index("--gateway") + 1] if "--gateway" in args else None
    fixture = args[args.index("--fixture") + 1] if "--fixture" in args else None
    depth = 3
    if fixture and args.index("--fixture") + 2 < len(args) and args[args.index("--fixture") + 2].isdigit():
        depth = int(args.pop(args.index("--fixture") + 2))
    files = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] not in ("--show", "--gateway", "--fixture"))]

    tally = {f: collections.Counter() for f in FIELDS}
    partial = collections.Counter()
    classes = collections.defaultdict(list)
    unexplained = []
    unread = collections.Counter()
    unknown = collections.Counter()
    unknown_example = {}
    failures = []
    not_decoded = []
    seen = set()

    def audit_unread(raw: str):
        left: list = []
        try:
            dec = decode(raw, left)
        except Exception as exc:  # noqa: BLE001
            failures.append((repr(exc), raw))
            return None
        for tok in left:
            kind = kind_of(tok)
            unread[kind or "of no known kind"] += 1
            if not kind:
                shape = re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", tok))
                unknown[shape] += 1
                unknown_example.setdefault(shape, (tok, raw))
        return dec

    records = {}
    for path in files:
        for line in open(path):
            rec = json.loads(line)
            if rec.get("rawOb"):
                records.setdefault((rec["icaoId"], rec["rawOb"]), rec)
    form_count = collections.Counter()
    cases = []
    for key in sorted(records):
        rec = records[key]
        raw = rec["rawOb"]
        seen.add((rec["icaoId"], " ".join(raw.split()[1:]) if raw.split()[0] in ("METAR", "SPECI") else raw))
        differing = set()
        dec = audit_unread(raw)
        if dec is None:
            not_decoded.append(raw)
            continue
        ours = dict(dec)
        ours["obsTime"] = observed_at(dec["time"], rec["obsTime"])
        ours["fltCat"] = category_in_range(dec["ceiling_range"], dec["visibility_range"])
        for f in FIELDS:
            a, b = rec.get(f), ours.get(f)
            if f == "clouds":
                a = [(c.get("cover"), c.get("base")) for c in a] if a is not None else None
                b = [(c.get("cover"), c.get("base")) for c in b] if b is not None else None
            if a is None and b is None:
                tally[f]["neither"] += 1
            elif a is not None and b is not None and same(f, a, b):
                tally[f]["equal"] += 1
            else:
                tally[f]["differ"] += 1
                differing.add(f)
                why = explain(f, a, b, raw, dec, rec)
                (classes[why] if why else unexplained).append((f, a, b, raw))
        if not rec.get("fltCat"):
            standing = partial_record(rec, dec, ours["fltCat"], raw)
            partial[standing or "unexplained"] += 1
            if not standing:
                unexplained.append(("category shown", normalize_metar(rec, 0)["category"], ours["fltCat"], raw))
        forms = forms_of(raw, rec)
        if any(form_count[name] < depth for name in forms):
            form_count.update(forms)
            cases.append({
                "raw": raw,
                "record": {f: rec[f] for f in FIELDS if rec.get(f) is not None},
                "compare": [f for f in FIELDS if f not in differing],
            })

    gateway_reports = 0
    if gateway:
        for path in sorted(glob.glob(os.path.join(gateway, "*.TXT"))):
            for line in open(path, errors="replace"):
                line = line.strip()
                m = re.match(r"^([A-Z][A-Z0-9]{3}) \d{6}Z", line)
                if not m or (m.group(1), line) in seen:
                    continue
                seen.add((m.group(1), line))
                gateway_reports += 1
                audit_unread(line)

    if fixture:
        with open(fixture, "w") as fh:
            fh.write("[\n" + ",\n".join(json.dumps(c, separators=(",", ":")) for c in cases) + "\n]\n")
        missing = [name for name in list(FORMS) + list(WHOLE_REPORT_FORMS) if not form_count[name]]
        print(f"{len(cases)} cases written to {fixture}; forms with no report in these files: {missing or 'none'}")
    compared = sum(tally["wdir"].values())
    print(f"{compared} reports compared with aviationweather.gov's decode, {len(not_decoded)} not decoded" + (f"; {gateway_reports} gateway reports audited for unread groups" if gateway else ""))
    for raw in not_decoded[:show]:
        print("   not decoded:", raw[:150])
    print(f"{'field':8} {'equal':>7} {'neither':>8} {'differ':>7}")
    for f in FIELDS:
        print(f"{f:8} {tally[f]['equal']:7} {tally[f]['neither']:8} {tally[f]['differ']:7}")
    print("explained differences, by class:")
    for why, items in sorted(classes.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(items):5}  {why}")
        for f, a, b, raw in items[:show]:
            print(f"           {f}: record {a!r}, decoder {b!r} | {raw[:140]}")
    print(f"records without a flight category: {sum(partial.values())}; the display gives")
    for standing, n in partial.most_common():
        print(f"  {n:5}  {standing}")
    print(f"unexplained differences: {len(unexplained)}")
    for f, a, b, raw in unexplained[: max(show, 40)]:
        print(f"   {f}: record {a!r}, decoder {b!r} | {raw[:170]}")
    print("body groups that fed no field, by kind:")
    for kind, n in unread.most_common():
        print(f"  {n:7}  {kind}")
    if unknown:
        print(f"groups of no known kind, by shape ({len(unknown)} shapes):")
        for shape, n in unknown.most_common(max(show, 60)):
            tok, raw = unknown_example[shape]
            print(f"  {n:6}  {tok:16} in: {raw[:120]}")
    for err, raw in failures[:show]:
        print("RAISED:", err, "|", raw[:150])
    return 1 if unexplained or failures else 0


if __name__ == "__main__":
    sys.exit(main())
