"""Raw METAR or SPECI text to the decoded fields aviationweather.gov's JSON carries.

A station aviationweather.gov does not serve is read from the NWS gateway as report
text (nws_gateway.py). This module turns that text into the record `weather.normalize_metar`
takes, so a station reads the same from either source. The report layout is FMH-1
chapter 12 (U.S.) and WMO-No. 306 code form FM 15 (international); units and
conventions are aviationweather.gov's: wind in knots, visibility in statute miles
("10+" for 10SM, "6+" for 9999, 10KM and CAVOK), altimeter in hPa, cloud bases in feet.

Where a report gives a cloud layer without its height, or an element is missing, the
record carries the range the ceiling or the visibility can lie in, so the flight
category is given only when every value in the range leads to the same one.
"""
from __future__ import annotations

import calendar
import re
import time
from typing import Dict, List, Optional, Tuple

KT_PER_MPS = 3600 / 1852
SM_PER_M = 0.0006214             # aviationweather.gov's factor for metric visibility
HPA_PER_INHG = 25.4 * 4 / 3      # aviationweather.gov's: inches to mm of mercury, then 4/3 hPa per mm
TENTHS_TOLERANCE_C = 1.0
MAX_WIND_KT = 250                # no surface wind reaches this: a higher figure is a mistyped group

MAX_VISIB_SM = 99                # the most aviationweather.gov's visib holds
UNLIMITED_FT = 99999    # no ceiling
UNLIMITED_SM = 99.0     # no upper bound on visibility

# Groups that end the body of a report: remarks, and the trend forecast, whose
# wind, visibility and cloud groups describe the next two hours, not the observation.
_END_OF_BODY = {"RMK", "NOSIG", "BECMG", "TEMPO", "INTER"}
_CLEAR = {"SKC", "CLR", "NSC", "NCD"}
_CEILING = {"BKN", "OVC", "VV"}

_STATION = re.compile(r"^[A-Z][A-Z0-9]{3}$")
_TIME = re.compile(r"^(\d{2})(\d{2})(\d{2})Z$")
_WIND = re.compile(r"^E?(\d{3}|VRB|///)(\d{2,3}|//)(?:G(\d{2,3}|//))?(KT|MPS)$")
_VIS_M = re.compile(r"^(\d{4})(?:NDV)?$")
_VIS_SM = re.compile(r"^(M)?(?:(\d{1,3})|(\d{1,2})/(\d{1,2}))SM$")
_VIS_KM = re.compile(r"^(\d{1,2})KM$")
_WHOLE = re.compile(r"^\d{1,2}$")
_CLOUD = re.compile(r"^(FEW|SCT|BKN|OVC|VV|///)(\d{3}|///)(?:CB|TCU|///)?$")
_CLOUD_UNREADABLE = re.compile(r"^(?:(BKN|OVC|VV)\S*|/+(?:CB|TCU))$")  # a ceiling cover or a convective cloud, the rest not in code form
_TEMP = re.compile(r"^(M?\d{2}|//|XX)/(M?\d{2}|//|XX)?$")
_ALTIM = re.compile(r"^([AQ])(\d{4})$")
_TENTHS = re.compile(r"^T([01])(\d{3})(?:([01])(\d{3})|/{4})?$")


def _half_up(value: float, places: int) -> float:
    scale = 10 ** places
    return int(value * scale + 0.5) / scale


def _number(value: float):
    return int(value) if value == int(value) else value


def _speed(text: Optional[str], unit: str) -> Optional[int]:
    if not text or text == "//":
        return None
    v = float(text)
    if unit == "MPS":
        v *= KT_PER_MPS
    return int(v + 0.5)


def _celsius(text: Optional[str]) -> Optional[int]:
    if not text or text in ("//", "XX"):
        return None
    return -int(text[1:]) if text.startswith("M") else int(text)


def _tenths(sign: str, digits: str) -> float:
    v = int(digits) / 10
    return _number(-v if sign == "1" and v else v)


def _precise(whole: Optional[int], tenths: float):
    """The remark's value in tenths when it agrees with the body's whole degrees,
    else the body's value; the tenths alone when the body gives none."""
    if whole is None or abs(tenths - whole) <= TENTHS_TOLERANCE_C:
        return tenths
    return whole


def _visibility(tokens: List[str], i: int) -> Tuple[Optional[object], Tuple[float, float], int]:
    """The visibility group at tokens[i] as (value, range in statute miles, tokens
    used). The value is a number of statute miles or the string aviationweather.gov
    gives for the top of a scale; tokens used is 0 when this is no visibility group."""
    tok = tokens[i]
    if tok == "CAVOK":
        return "6+", (6.0, UNLIMITED_SM), 1
    m = _VIS_M.match(tok)
    if m:
        metres = int(m.group(1))
        if metres >= 9999:
            return "6+", (6.0, UNLIMITED_SM), 1
        miles = metres * SM_PER_M
        return _number(_half_up(miles, 2)), (miles, miles), 1
    m = _VIS_KM.match(tok)
    if m:
        if int(m.group(1)) == 10:
            return "6+", (6.0, UNLIMITED_SM), 1
        miles = int(m.group(1)) * 1000 * SM_PER_M
        return _number(_half_up(miles, 2)), (miles, miles), 1
    whole = 0
    used = 1
    if _WHOLE.match(tok) and i + 1 < len(tokens):
        frac = _VIS_SM.match(tokens[i + 1])
        if frac and frac.group(3) and not frac.group(1):
            whole, tok, used = int(tok), tokens[i + 1], 2
    m = _VIS_SM.match(tok)
    if not m:
        return None, (0.0, UNLIMITED_SM), 0
    if m.group(2) is not None:
        miles = float(m.group(2))
    else:
        if int(m.group(4)) == 0:
            return None, (0.0, UNLIMITED_SM), 0
        miles = whole + int(m.group(3)) / int(m.group(4))
    if miles == 10:
        return "10+", (10.0, UNLIMITED_SM), used
    return _number(_half_up(min(miles, MAX_VISIB_SM), 2)), (miles, miles), used


def _ceiling_range(layers: List[Tuple[Optional[str], Optional[int]]], sky_reported: bool, cavok: bool) -> Tuple[int, int]:
    """The lowest and highest the ceiling can be, in feet, from the cloud groups in
    report order as (cover, base); either part is None when the report slashes it out.
    Layers are reported lowest first, so a broken or overcast layer without a height
    is no lower than the layer before it. A group with neither amount nor height may
    be a ceiling at any height, except beside CAVOK, which rules a ceiling out.
    UNLIMITED_FT stands for no ceiling."""
    if not sky_reported:
        return 0, UNLIMITED_FT
    low = high = UNLIMITED_FT
    floor = 0
    for cover, base in layers:
        if cavok and (cover is None or base is None):
            continue
        if cover in _CEILING:
            low = min(low, base if base is not None else 0 if cover == "VV" else floor)
            if base is not None:
                high = min(high, base)
        elif cover is None:  # amount missing: the layer may or may not be a ceiling
            low = min(low, base if base is not None else 0)
        if base is not None:
            floor = max(floor, base)
    return low, high


def decode(report: str, unread: Optional[List[str]] = None) -> Optional[Dict]:
    """The decoded record for one report, or None when the text is not a METAR
    (no station identifier and time at its head, or a NIL report).

    Fields the report does not carry are absent, as in aviationweather.gov's JSON:
    wdir, wspd, wgst, visib, altim, temp, dewp, clouds. Beside them the record has
    `time` (day, hour, minute; `observed_at` turns it into an epoch time),
    `ceiling_range` (feet) and `visibility_range` (statute miles). `unread`, when
    given, collects the body groups that fed no field."""
    tokens = [t for t in (t.rstrip(";=") for t in report.split()) if t]
    while tokens and tokens[0] in ("METAR", "SPECI", "COR"):
        tokens.pop(0)
    if len(tokens) < 2 or not _STATION.match(tokens[0]):
        return None
    when = _TIME.match(tokens[1])
    if not when:
        return None
    day, hour, minute = (int(g) for g in when.groups())
    if not (1 <= day <= 31 and minute < 60 and (hour < 24 or (hour, minute) == (24, 0))):
        return None
    if "NIL" in tokens[2:4]:
        return None

    rec: Dict = {"icaoId": tokens[0], "rawOb": " ".join(report.split()), "time": (day, hour, minute)}
    layers: List[Tuple[Optional[str], Optional[int]]] = []
    vis_range = (0.0, UNLIMITED_SM)
    altim: Dict[str, int] = {}
    sky_reported = clouds_reported = wind_seen = temp_seen = False
    body_temp = body_dewp = None
    i = 2
    while i < len(tokens) and tokens[i] not in _END_OF_BODY:
        tok = tokens[i]
        step = 1
        # the groups come in a fixed order: nothing after the temperature or the
        # altimeter is visibility or sky condition
        sky_open = not temp_seen and not altim
        wind = _WIND.match(tok)
        cloud = _CLOUD.match(tok) if sky_open else None
        if wind and not wind_seen:
            wind_seen = True
            direction, speed, gust, unit = wind.groups()
            spd = _speed(speed, unit)
            gst = _speed(gust, unit)
            if spd is not None and direction != "///" and max(spd, gst or 0) <= MAX_WIND_KT:
                rec["wspd"] = spd
                if direction == "VRB" or (direction == "000" and spd):
                    rec["wdir"] = "VRB"
                else:
                    rec["wdir"] = int(direction)
                if gst is not None:
                    rec["wgst"] = gst
        elif "visib" not in rec and not sky_reported and sky_open and _visibility(tokens, i)[2]:
            rec["visib"], vis_range, step = _visibility(tokens, i)
            sky_reported = clouds_reported = tok == "CAVOK"
        elif cloud:
            cover, base = cloud.group(1), cloud.group(2)
            layers.append((None if cover == "///" else cover, None if base == "///" else int(base) * 100))
            sky_reported = True
            clouds_reported = clouds_reported or cover == "VV" or "///" not in (cover, base)
        elif sky_open and _CLOUD_UNREADABLE.match(tok):
            layers.append((_CLOUD_UNREADABLE.match(tok).group(1), None))
            sky_reported = True
            if unread is not None:
                unread.append(tok)
        elif sky_open and tok in _CLEAR:
            sky_reported = clouds_reported = True
        elif _TEMP.match(tok) and not temp_seen:
            body_temp, body_dewp = (_celsius(g) for g in _TEMP.match(tok).groups())
            temp_seen = body_temp is not None or body_dewp is not None
        elif _ALTIM.match(tok) and tok[0] not in altim:
            altim[tok[0]] = int(tok[1:])
        elif unread is not None:
            unread.append(tok)
        i += step

    tenths = None
    if "RMK" in tokens:
        tenths = next((m for m in map(_TENTHS.match, tokens[tokens.index("RMK") + 1:]) if m), None)
    if body_temp is not None:
        rec["temp"] = body_temp
    if body_dewp is not None:
        rec["dewp"] = body_dewp
    if tenths:
        rec["temp"] = _precise(body_temp, _tenths(tenths.group(1), tenths.group(2)))
        if tenths.group(3):
            rec["dewp"] = _precise(body_dewp, _tenths(tenths.group(3), tenths.group(4)))

    if "Q" in altim:  # a report with both gives the same pressure twice; hPa needs no conversion
        rec["altim"] = altim["Q"]
    elif "A" in altim:
        rec["altim"] = _number(_half_up(altim["A"] / 100 * HPA_PER_INHG, 1))
    if clouds_reported:
        rec["clouds"] = [
            {"cover": "OVX" if cover == "VV" else cover, "base": base}
            for cover, base in layers
            if cover is not None and base is not None
        ]
    rec["ceiling_range"] = _ceiling_range(layers, sky_reported, "CAVOK" in tokens[2:i])
    rec["visibility_range"] = vis_range
    return rec


def observed_at(time_group: Tuple[int, int, int], anchor: float) -> Optional[float]:
    """The epoch time of a report's day-hour-minute group: the occurrence of that
    day and time nearest to `anchor` (epoch seconds), looking one month either way."""
    day, hour, minute = time_group
    a = time.gmtime(anchor)
    best = None
    for shift in (-1, 0, 1):
        month = a.tm_mon + shift
        year = a.tm_year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        if day > calendar.monthrange(year, month)[1]:
            continue
        t = calendar.timegm((year, month, day, hour, minute, 0))
        if best is None or abs(t - anchor) < abs(best - anchor):
            best = t
    return float(best) if best is not None else None
