"""NOTAM text: the keyword that types a NOTAM, and a literal plain-language
rendering of its body.

Everything here follows the documents FAA Order 7930.2 names for NOTAM text. The
keyword is the first element after the location identifier. A contraction is
spelled out only if data/notam_terms.json lists it, and that file is generated
by backend/tools/build_notam_data.py from the AIM's table of contractions found
in NOTAMs, Order 7930.2, Order JO 7340.2, the FAA AIP and the abbreviations of
ICAO Doc 8400: each entry carries the text it comes from, and a contraction the
sources do not agree on is left as written. Designators, identifiers, numbers
and unknown words are never changed. Validity times are not rendered here; the
display shows them from the NOTAM's own fields.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

with open(os.path.join(DATA_DIR, "notam_terms.json")) as _fh:
    _TABLE = json.load(_fh)
with open(os.path.join(DATA_DIR, "notam_words.json")) as _fh:
    WORDS = frozenset(json.load(_fh))
with open(os.path.join(DATA_DIR, "english_words.json")) as _fh:
    _ENGLISH = json.load(_fh)
ENGLISH = frozenset(_ENGLISH["words"])
# What the dictionaries enter besides: words of three letters, names and the
# words of the phonetic alphabet. The decoder leaves them as written.
ENGLISH_ALSO = frozenset(_ENGLISH["also"])

UNITS: Dict[str, Tuple[str, str]] = {u: (e["singular"], e["plural"]) for u, e in _TABLE["units"].items()}
# A unit's contraction is a unit after a number (20 MIN, 250 KT). Apart from a
# number it is spelled out only where the FAA writes it so in a NOTAM of its own
# (HR: "AREA HR CHANGED").
UNITS_ALONE: Dict[str, str] = {u: e["alone"] for u, e in _TABLE["units"].items() if e.get("alone")}
TERMS: Dict[str, str] = {term: entry["text"] for term, entry in _TABLE["terms"].items() if term not in UNITS}
# For a contraction whose definition lists several forms of one word, the words
# the FAA's NOTAM documents write before and after each form.
FORMS: Dict[str, Dict[str, Tuple[frozenset, frozenset]]] = {
    term: {form: (frozenset(ctx["before"]), frozenset(ctx["after"])) for form, ctx in entry["forms"].items()}
    for term, entry in _TABLE["terms"].items() if entry.get("forms")
}
# Those the FAA's NOTAM tables list (the AIM table, Order 7930.2) or the FAA writes
# in NOTAM text of its own; the others are known only as ICAO-usage contractions
# of Order JO 7340.2.
TABLE_TERMS: Dict[str, str] = {term: text for term, text in TERMS.items()
                               if _TABLE["terms"][term].get("listed_in") or _TABLE["terms"][term].get("shown")}
PHRASES: Dict[Tuple[str, ...], str] = {tuple(p.split()): e["text"] for p, e in _TABLE["phrases"].items()}
PHRASE_MAX = max(len(p) for p in PHRASES)
# What opens a report on one runway in another state's NOTAMs ("RSC 08 ..."):
# before a runway designator it has that state's meaning, not the lists'.
RUNWAY_REPORTS: Dict[str, str] = {term: e["text"] for term, e in _TABLE["runway_reports"].items()}
DAYS: Dict[str, str] = {day: e["text"] for day, e in _TABLE["days"].items()}
COMPASS: Dict[str, str] = {letter: e["text"] for letter, e in _TABLE["compass"].items()}
SUN: Dict[str, str] = {code: e["text"] for code, e in _TABLE["sun"].items()}
# The words of the phonetic alphabet (AIM TBL 4-2-7) name letters: taxiway BRAVO.
SPELLING = frozenset(_TABLE["spelling"])
# Words the FAA's NOTAM documents write directly before the article "a".
BEFORE_ARTICLE = frozenset(_TABLE["before_article"])

# The order the display groups NOTAM keywords in. Obstructions come last: most
# are fixed objects. The labels are the FAA's (AIM keyword table).
TYPE_SEQUENCE = ("AD", "RWY", "TWY", "APRON", "AIRSPACE", "SECURITY", "SPECIAL", "NAV", "COM", "SVC", "IAP", "ODP",
                 "SID", "STAR", "DVA", "VFP", "ROUTE", "CHART", "DATA", "OTHER", "OBST")
TYPE_LABEL: Dict[str, str] = {**_TABLE["keywords"], "OTHER": "Other"}
TYPES: Tuple[Tuple[str, str], ...] = tuple((code, TYPE_LABEL[code]) for code in TYPE_SEQUENCE)
TYPE_ORDER: Dict[str, int] = {code: i for i, code in enumerate(TYPE_SEQUENCE)}
KEYWORDS = frozenset(TYPE_SEQUENCE) - {"OTHER"}
# A surface designator follows these keywords, so they stay in the rendering.
SURFACE_KEYWORDS = frozenset({"RWY", "TWY", "APRON"})
SURFACE_WORDS = ("RWY", "TWY", "TXL")
SURFACE_PLURALS = frozenset(word + "S" for word in SURFACE_WORDS)

# International NOTAM Q code, second and third letters (the subject), mapped to
# a keyword per FAA Order 7930.2 appendix B. Used only when the text carries no
# keyword.
QCODE_SUBJECT = {
    "FA": "AD", "FH": "AD", "FP": "AD", "LB": "AD", "LU": "AD", "LW": "AD", "MA": "AD",
    "LX": "TWY", "LY": "TWY", "MG": "TWY", "MX": "TWY", "MY": "TWY",
    "MK": "APRON", "MN": "APRON", "MP": "APRON",
    "LG": "SVC",
    "OB": "OBST", "OL": "OBST",
    "PA": "STAR", "PD": "SID", "PI": "IAP", "PM": "IAP", "PO": "IAP", "PU": "IAP",
}
QCODE_GROUP = {
    "A": "AIRSPACE", "R": "AIRSPACE", "W": "AIRSPACE",
    "C": "COM",
    "F": "SVC", "S": "SVC",
    "G": "NAV", "I": "NAV", "N": "NAV",
    "L": "RWY", "M": "RWY",
}

# "!ACC number LOC ": accountability, NOTAM number, affected location. The number
# holds a digit or a slash; a message printed without one has "!ACC LOC ".
HEADER_RE = re.compile(r"^!\s*(?P<acc>[A-Z0-9]{3,4})\s+(?:(?P<number>\S*[\d/]\S*)\s+)?(?P<loc>[A-Z0-9]{3,4})\s+")
# FDC NOTAMs for a procedure open with "AIRPORT NAME, CITY, ST." (Order 7930.2 chapter 7).
PROCEDURE_KEYWORDS = frozenset({"IAP", "ODP", "SID", "STAR", "DVA", "VFP", "SPECIAL", "CHART", "DATA"})
# NOTAMs that name fixes: a five-letter group there may be a fix name.
ROUTE_KEYWORDS = PROCEDURE_KEYWORDS | {"ROUTE"}
VALIDITY_RE = re.compile(r"\s+\d{10}\s*-\s*(?:\d{10}\s*(?:EST)?|PERM)\s*$")
UNVERIFIED = "(U)"

RWY_DESIG_RE = re.compile(r"^\d{1,2}[LRC]?(?:/\d{1,2}[LRC]?)*$")
TWY_DESIG_RE = re.compile(r"^[A-Z]{1,2}\d{0,2}(?:-[A-Z]{1,2}\d{0,2})?$")
NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+|\d+/\d+|\d+-\d+/\d+)"
NUMBER_RE = re.compile(rf"^{NUMBER}$")
UNIT_CODES = tuple(sorted(UNITS, key=lambda unit: (-len(unit), unit)))
UNIT_RE = re.compile(rf"^({NUMBER})({'|'.join(UNIT_CODES)})$")
LEVEL = r"(?:SFC|GND|UNL|\d+(?:FT|M)|FL\d+)"
LEVEL_RANGE_RE = re.compile(rf"^({LEVEL})-({LEVEL})$")
DAY = r"(?:MON|TUE|WED|THU|FRI|SAT|SUN)"
DAY_RE = re.compile(rf"^({DAY})(?:-({DAY}))?$")
SUN_RE = re.compile(r"^(SR|SS|\d{4})-(SR|SS|\d{4})$")
DISTANCE_UNITS = ("FT", "NM", "M", "KM")
DISTANCE_RE = re.compile(rf"^{NUMBER}(?:{'|'.join(DISTANCE_UNITS)})$")
DIMENSION_RE = re.compile(rf"^{NUMBER}(?:FT|IN|M|CM|NM|KM)$")
HEIGHT_RE = re.compile(r"^\d+(?:FT|M)$")
# The level that ends a range begun by a height and its datum ("500FT AGL-4999FT").
DATUM_RANGE_RE = re.compile(rf"^([A-Z]{{2,4}})-({LEVEL})$")
AIRWAY_RE = re.compile(r"^[A-Z]{1,2}\d{1,4}$")
GLUED_SURFACE_RE = re.compile(r"^(RWY)(\d{1,2}[LRC]?)$")
# A mark, or a run of marks, that joins two pieces of one token ("RMK/FALSE", "1.PSN", "APN)-CLSD").
JOIN_RE = re.compile(r"[/,.:()-]+")
ADDRESS_RE = re.compile(r"WWW\.|HTTPS?(?![A-Z])|@|[A-Z][A-Z0-9_-]*\.(?:COM|GOV|ORG|NET|AERO|MIL|HTML?|PDF|ASPX?)(?![A-Z])")
# A single compass letter is a direction only next to one of these.
COMPASS_NEXT = frozenset({"OF", "SIDE", "END", "HALF", "PORTION", "EDGE"})
# The words that join two designators of one list ("TWY A AND B").
JOINS = frozenset({"AND", "OR", "&"})
LEAD = "("
TRAIL = ").,:;"


def body_of(text: str) -> str:
    """The NOTAM's body: without a leading "!ACC number LOC" header and without
    the trailing validity group."""
    body = " ".join((text or "").split())
    body = HEADER_RE.sub("", body)
    return VALIDITY_RE.sub("", body).strip()


def _leading_keyword(body: str) -> Optional[str]:
    words = body.split()
    if words and words[0] == UNVERIFIED:
        words = words[1:]
    # An FDC NOTAM for an area leads with "<state>..<KEYWORD>".
    pieces: List[str] = []
    for word in words[:2]:
        pieces.extend(p for p in word.split("..") if p)
    for i, piece in enumerate(pieces[:3]):
        candidate = piece.rstrip(TRAIL)
        if candidate in KEYWORDS and (i == 0 or re.fullmatch(r"[A-Z]{2}", pieces[i - 1])):
            return candidate
    return None


def classify(text: str, raw: str = "", qcode: str = "") -> str:
    """The NOTAM's keyword: from its text, else from the full message, else from
    the Q code's subject, else OTHER."""
    for candidate in (text, raw):
        keyword = _leading_keyword(body_of(candidate or ""))
        if keyword:
            return keyword
    q = (qcode or "").upper()
    if len(q) >= 3 and q[0] == "Q":
        subject = q[1:3]
        return QCODE_SUBJECT.get(subject) or QCODE_GROUP.get(subject[0], "OTHER")
    return "OTHER"


def split_token(token: str) -> Tuple[str, str, str]:
    """-> (leading punctuation, core, trailing punctuation). An ellipsis before a
    word ("...SPECIAL NOTICE...") is leading punctuation; a single point may
    begin a number (".5NM")."""
    start = 0
    while start < len(token) and (token[start] in LEAD or token.startswith("..", start)
                                  or (start and token[start] == "." and token[start - 1] == ".")):
        start += 1
    end = len(token)
    while end > start and token[end - 1] in TRAIL:
        end -= 1
    return token[:start], token[start:end], token[end:]


def split_body(text: str) -> Tuple[bool, Optional[str], List[str]]:
    """-> (unverified, keyword, tokens): whether the body carries the (U) marker,
    the category keyword it leads with (None for RWY, TWY and APRON, whose
    keyword is part of the subject), and the remaining tokens."""
    body = body_of(text)
    unverified = body.startswith(UNVERIFIED + " ")
    if unverified:
        body = body[len(UNVERIFIED) + 1:]
    tokens = body.split()
    keyword = None
    if tokens and ".." not in tokens[0] and tokens[0] in KEYWORDS - SURFACE_KEYWORDS and len(tokens) > 1:
        keyword, tokens = tokens[0], tokens[1:]
    return unverified, keyword, tokens


def heading_length(tokens: List[str]) -> int:
    """Tokens of the "AIRPORT NAME, CITY, ST." heading a procedure NOTAM opens
    with: up to the two-letter state code that follows a comma and ends with a
    full stop. 0 when there is none."""
    for i, token in enumerate(tokens[:12]):
        if i and re.fullmatch(r"[A-Z]{2}\.+", token) and tokens[i - 1].endswith(","):
            return i + 1
    return 0


# Ground stations a NOTAM names by their identifier ("AML VORTAC"), the
# facilities a NAV NOTAM may name the same way ("NAV BZB ILS RWY 22R"), and the
# compass points of a position reference ("(12NM N PWK)").
STATIONS = frozenset("VOR VORTAC VOR/DME VORDME DVOR NDB NDB/DME TACAN VOT".split())
NAVAIDS = STATIONS | frozenset("ILS LOC GP DME LDA SDF MLS GLS OM MM IM".split())
COMPASS_POINTS = frozenset("N S E W NE NW SE SW NNE ENE ESE SSE SSW WSW WNW NNW".split())
# The two-letter codes of the U.S. states, the District of Columbia and the
# territories, as a NOTAM writes them after a place name and a comma ("DALLAS, TX").
STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD "
    "TN TX UT VT VA WA WV WI WY DC AS GU MP PR VI".split())
IDENTIFIER_RE = re.compile(r"[A-Z0-9]{2,5}$")
NM_DISTANCE_RE = re.compile(r"\d+(?:\.\d+)?NM$")
RADIAL_RE = re.compile(r"R-\d{3}$")


def terms_for(keyword: Optional[str]) -> Dict[str, str]:
    """The contractions spelled out in a NOTAM of this keyword. A procedure NOTAM
    uses the contractions of the instrument procedure charts (Order 7930.2 para
    3-3-1), so there only those a NOTAM table lists, or the FAA writes in a NOTAM
    of its own, are spelled out."""
    return TABLE_TERMS if keyword in PROCEDURE_KEYWORDS else TERMS


def location_positions(tokens: List[str], keyword: Optional[str] = None) -> set:
    """Indexes of tokens that stand where a NOTAM names a location by its
    identifier, whatever the letters spell: the centre of a radius ("RADIUS OF
    SAT"), the end of a position reference ("(5NM N SAT)"), an identifier alone
    in parentheses ("WACO (ACT) VORTAC"), the identifier before a ground station
    or a radial ("AML VORTAC", "CVE R-046", "NAV BZB ILS"), and the points of an
    area ("DEFINED AS MCN TO SUG TO IRQ")."""
    parts = [split_token(t) for t in tokens]
    cores = [core for _lead, core, _trail in parts]
    area = next((i + 2 for i in range(len(cores) - 1) if cores[i] == "DEFINED" and cores[i + 1] == "AS"), None)
    found = set()
    for i, (lead, core, trail) in enumerate(parts):
        if not IDENTIFIER_RE.match(core) or NUMBER_RE.match(core) or UNIT_RE.match(core):
            continue
        before = cores[i - 1] if i else ""
        after = cores[i + 1] if i + 1 < len(cores) else ""
        if i >= 2 and before == "OF" and cores[i - 2] == "RADIUS":
            found.add(i)
        elif trail.startswith(")") and (
            (i >= 2 and before in COMPASS_POINTS and tokens[i - 2].startswith("(") and NM_DISTANCE_RE.match(cores[i - 2]))
            or (i >= 3 and before == "OF" and cores[i - 2] in COMPASS_POINTS and tokens[i - 3].startswith("(")
                and NM_DISTANCE_RE.match(cores[i - 3]))
        ):
            found.add(i)
        elif lead == "(" and trail.startswith(")") and core != "U":
            found.add(i)
        elif core not in NAVAIDS and (
            after in STATIONS or RADIAL_RE.match(after) or (keyword == "NAV" and i == 0 and after in NAVAIDS)
        ):
            found.add(i)
        elif area is not None and i >= area and core != "TO" and (before == "TO" or after == "TO"):
            found.add(i)
    return found


def token_roles(tokens: List[str], protected: Iterable[str] = (), keyword: Optional[str] = None) -> List[str]:
    """What each token is: "designator" (a runway or taxiway designator following
    RWY, TWY or TXL; a further one after a comma, or after AND, OR or &),
    "surface" (RWY, TWY or TXL itself), "report" (what opens a report on the
    runway whose designator follows), "protected" (one of the NOTAM's own identifiers, a name in a
    procedure NOTAM's heading, or a token standing where a location is named by
    its identifier), "state" (a state code after a comma), else "text"."""
    protected = set(protected)
    located = location_positions(tokens, keyword)
    roles: List[str] = []
    surface: Optional[str] = None   # keyword whose designators follow
    listed: Optional[str] = None    # keyword of the designator just read
    heading = heading_length(tokens) if keyword in PROCEDURE_KEYWORDS else 0
    for i, token in enumerate(tokens):
        _lead, core, trail = split_token(token)
        if i < heading:
            roles.append("protected")
            continue
        if surface:
            if is_designator(core, surface):
                roles.append("designator")
                listed = surface
                if not trail.endswith(","):
                    surface = None
                continue
            surface = None
        if listed and core in JOINS and i + 1 < len(tokens):
            following = split_token(tokens[i + 1])[1]
            if is_designator(following, listed) and not is_word(following):
                surface = listed
        listed = None
        state_code = core in STATE_CODES and i > 0 and tokens[i - 1].endswith(",")
        if not core or core in protected or (i in located and not is_word(core)):
            roles.append("protected")
        elif state_code:
            roles.append("state")
        elif core in SURFACE_WORDS or core in SURFACE_PLURALS:
            surface = "RWY" if core.startswith("RWY") else "TWY"
            roles.append("surface")
        elif core in RUNWAY_REPORTS and i + 1 < len(tokens) and RWY_DESIG_RE.match(split_token(tokens[i + 1])[1]):
            surface = "RWY"
            roles.append("report")
        else:
            roles.append("text")
    return roles


def is_designator(core: str, surface: str) -> bool:
    """A runway designator after RWY, a taxiway designator (or several joined
    by slashes) after TWY or TXL."""
    if surface == "RWY":
        return bool(RWY_DESIG_RE.match(core))
    return core != "ALL" and all(TWY_DESIG_RE.match(part) for part in core.split("/"))


def after_distance(cores: List[str], i: int) -> bool:
    """Whether the token at i follows a distance ("2.5NM", "6 NM")."""
    return i >= 1 and bool(DISTANCE_RE.match(cores[i - 1])
                           or (i >= 2 and cores[i - 1] in DISTANCE_UNITS and NUMBER_RE.match(cores[i - 2])))


def is_compass(core: str, nxt: str, follows_distance: bool = False) -> bool:
    """A single compass letter read as a direction: only next to a distance or
    before a word such as SIDE or OF."""
    return core in COMPASS and bool(nxt in COMPASS_NEXT or DISTANCE_RE.match(nxt) or follows_distance)


def is_article(core: str, nxt: str) -> bool:
    """The letter A before a distance ("WI A 45NM RADIUS") is the article."""
    return core == "A" and bool(DISTANCE_RE.match(nxt))


def may_be_article(core: str, prev: str, nxt: str) -> bool:
    """The letter A after a word the FAA writes before the article and before a
    plain word ("IS A RESTATEMENT OF A PREVIOUSLY ISSUED") may be the article:
    only the sentence tells. After a word that takes a label it is the label
    (JET A, INDEX A)."""
    before = (TERMS.get(prev) or prev).lower().split(" ")[-1]
    return core == "A" and before in BEFORE_ARTICLE and nxt not in JOINS and len(nxt) >= 3 and (is_word(nxt) or is_english(nxt))


def is_by(core: str, prev: str, nxt: str, before: str = "") -> bool:
    """X between two dimensions (10000FT X 150FT, 4IN DEPTH X 12IN WIDTH)."""
    return core == "X" and bool(DIMENSION_RE.match(nxt) and (DIMENSION_RE.match(prev) or DIMENSION_RE.match(before)))


def phrase_at(cores: List[str], roles: List[str], i: int) -> Optional[Tuple[int, str]]:
    """-> (number of tokens, rendering) of the FAA-defined phrase starting at i."""
    for n in range(min(PHRASE_MAX, len(cores) - i), 1, -1):
        if tuple(cores[i:i + n]) in PHRASES and all(r in ("text", "surface") for r in roles[i:i + n]):
            return n, PHRASES[tuple(cores[i:i + n])]
    return None


def datum_range(core: str, prev: str) -> Optional[Tuple[str, str]]:
    """-> (datum, level) of a token that ends a level range after a height:
    "AGL-4999FT" in "500FT AGL-4999FT"."""
    m = DATUM_RANGE_RE.match(core)
    return (m.group(1), m.group(2)) if m and m.group(1) in TERMS and HEIGHT_RE.match(prev) else None


def unit_text(core: str) -> Optional[str]:
    m = UNIT_RE.match(core)
    if not m:
        return None
    singular, plural = UNITS[m.group(2)]
    one = m.group(1) == "1" or re.fullmatch(r"\d+/\d+", m.group(1))   # 1 inch, 1/2 inch
    return f"{m.group(1)} {singular if one else plural}"


def level_text(core: str) -> str:
    return TERMS.get(core) or unit_text(core) or core


def is_word(core: str) -> bool:
    """A token that is plain English spelled in capitals: a word of the FAA's own
    prose (data/notam_words.json; of two or three letters only what its NOTAM
    documents write as a word, and no contraction a list also defines)."""
    return core.isalpha() and core.lower() in WORDS


def is_english(core: str) -> bool:
    """A token of four letters or more that the dictionaries enter as an ordinary
    word and not also as a name (data/english_words.json); no contraction of
    any list and no word of the phonetic alphabet is among them."""
    return core.isalpha() and core.lower() in ENGLISH


def is_fix_name(core: str) -> bool:
    """A five-letter group that is no contraction and no word."""
    return len(core) == 5 and core.isalpha() and core not in TERMS and not is_word(core) and not is_english(core)


def is_plain(core: str, keyword: Optional[str] = None) -> bool:
    """Whether a token read by itself is plain English. A word of the phonetic
    alphabet names a letter and stays as written. A five-letter word of the
    dictionaries alone may be a fix name in a NOTAM on a procedure or a route,
    and stays as written there."""
    if core in SPELLING:
        return False
    return is_word(core) or (is_english(core) and not (len(core) == 5 and keyword in ROUTE_KEYWORDS))


def reads_as_word(cores: List[str], roles: List[str], i: int, keyword: Optional[str]) -> bool:
    """Whether the token at i is plain English where it stands: a five-letter
    word of the dictionaries alone stays as written beside a route element
    (DCT, an airway, a five-letter name), where it is a fix name."""
    core = cores[i]
    if not is_plain(core, keyword):
        return False
    if is_word(core) or len(core) != 5:
        return True
    return not any(0 <= j < len(cores) and roles[j] == "text"
                   and (cores[j] == "DCT" or AIRWAY_RE.match(cores[j]) or is_fix_name(cores[j])) for j in (i - 1, i + 1))


def plural_text(core: str, terms: Dict[str, str]) -> Optional[str]:
    """RWY, TWY or TXL written with a plural S, as an FDC NOTAM of the FAA's
    writes it ("RWYS 17C/R, 18L/R"). Other groups ending in S are contractions
    or names of their own (MSAS)."""
    return terms[core[:-1]] + "s" if core in SURFACE_PLURALS and core[:-1] in terms else None


def piece_text(piece: str, terms: Dict[str, str], keyword: Optional[str]) -> Optional[Tuple[str, str]]:
    """-> (kind, reading) of one piece of a joined token, or None when nothing
    settles it. Kinds: "term", "unit", "glued" (RWY19L), "word", and "as
    written" for a number or a runway designator."""
    if piece in terms:
        return "term", terms[piece]
    if NUMBER_RE.match(piece) or RWY_DESIG_RE.match(piece):
        return "as written", piece
    if UNIT_RE.match(piece):
        return "unit", unit_text(piece)
    m = GLUED_SURFACE_RE.match(piece)
    if m:
        return "glued", f"{terms[m.group(1)]} {m.group(2)}"
    if is_plain(piece, keyword):
        return "word", piece.lower()
    return None


def joined_pieces(core: str, terms: Dict[str, str], keyword: Optional[str] = None) -> Optional[List[Tuple[str, str, str]]]:
    """A token of several pieces joined by marks, every piece of which reads by
    itself: [(as written, kind, reading)], marks included with the kind "mark".
    None when a piece is not settled, when nothing in it is spelled out, and for
    an address. A hyphen between a letter and a figure joins a designation
    (LDA-2, W-181), and a point, comma or colon between two figures is part of
    a number: neither is a place to part the token. Words joined by hyphens that the
    dictionaries enter as one word read as written, in lower case."""
    if ADDRESS_RE.search(core) or not JOIN_RE.search(core):
        return None

    def parts(rest: str) -> Optional[List[Tuple[str, str, str]]]:
        whole = piece_text(rest, terms, keyword)
        if whole:
            return [(rest, *whole)]
        for m in reversed(list(JOIN_RE.finditer(rest))):      # the longest first piece that reads by itself
            left, right = rest[:m.start()], rest[m.end():]
            if not left or not right or (m.group(0) == "-" and left[-1].isalpha() and right[0].isdigit()) \
                    or (m.group(0) in ".,:" and left[-1].isdigit() and right[0].isdigit()):      # inside a number
                continue
            first = piece_text(left, terms, keyword)
            if not first:
                continue
            following = parts(right)
            if following:
                return [(left, *first), (m.group(0), "mark", m.group(0))] + following
        return None

    found = parts(core)
    if found and any(kind not in ("mark", "as written") for _written, kind, _reading in found):
        merged: List[Tuple[str, str, str]] = []
        for piece in found:      # figures joined by a mark are one fact ("21-06", "01L/19R"): they stay joined
            if piece[1] == "as written" and len(merged) >= 2 and merged[-1][1] == "mark" and merged[-2][1] == "as written":
                whole = merged[-2][0] + merged[-1][0] + piece[0]
                merged[-2:] = [(whole, "as written", whole)]
            else:
                merged.append(piece)
        return merged
    if re.fullmatch(r"[A-Z]+(?:-[A-Z]+)+", core) and is_plain(core.replace("-", ""), keyword):
        return [(core, "word", core.lower())]
    return None


def _singular(word: str) -> str:
    word = re.sub(r"[^a-z]", "", word.lower())
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def form_of(term: str, default: str, before: str, after: str) -> str:
    """The form of a several-form contraction that the word after it, else the
    word before it, calls for in the FAA's own writing; else the usual form."""
    forms = FORMS.get(term)
    if not forms:
        return default
    for position, word in ((1, _singular(after)), (0, _singular(before))):
        fitting = [form for form, words in forms.items() if word and word in words[position]]
        if len(fitting) == 1:
            return fitting[0]
        if default in fitting:
            return default
    return default


def decode(text: str, protect: Iterable[str] = ()) -> str:
    """A literal plain-language rendering of a NOTAM body. `protect` names
    identifiers (the NOTAM's own location and accountability) that are never
    read as contractions or words."""
    unverified, keyword, tokens = split_body(text)
    prefix = "Unverified: " if unverified else ""
    roles = token_roles(tokens, {p.upper() for p in protect if p}, keyword)
    cores = [split_token(t)[1] for t in tokens]
    terms = terms_for(keyword)

    out: List[str] = []
    skip = 0
    for i, token in enumerate(tokens):
        if skip:
            skip -= 1
            continue
        lead, core, trail = split_token(token)
        nxt = cores[i + 1] if i + 1 < len(cores) else ""
        prev = cores[i - 1] if i else ""

        if roles[i] in ("designator", "protected", "state"):
            out.append(token)
            continue
        if roles[i] == "report":
            out.append(lead + RUNWAY_REPORTS[core] + trail)
            continue
        phrase = phrase_at(cores, roles, i)
        if phrase:
            count, rendering = phrase
            out.append(lead + rendering + split_token(tokens[i + count - 1])[2])
            skip = count - 1
            continue

        rendered: Optional[str] = None
        m = LEVEL_RANGE_RE.match(core)
        if m:
            rendered = f"{level_text(m.group(1))} to {level_text(m.group(2))}"
        datum = datum_range(core, prev)
        if datum:
            rendered = f"{TERMS[datum[0]]} to {level_text(datum[1])}"
        if rendered is None:
            rendered = unit_text(core)
        if rendered is None:
            m = DAY_RE.match(core)
            if m:
                rendered = DAYS[m.group(1)] + (f" through {DAYS[m.group(2)]}" if m.group(2) else "")
            elif "/" in core and all(part in DAYS for part in core.split("/")):
                rendered = "/".join(DAYS[part] for part in core.split("/"))
        if rendered is None:
            m = SUN_RE.match(core)
            if m and (m.group(1) in SUN or m.group(2) in SUN):
                rendered = f"{SUN.get(m.group(1), m.group(1))} to {SUN.get(m.group(2), m.group(2))}"
        if rendered is None and core in UNITS and NUMBER_RE.match(prev):
            singular, plural = UNITS[core]
            rendered = singular if prev == "1" or re.fullmatch(r"\d+/\d+", prev) else plural
        if rendered is None and core in UNITS_ALONE and not re.search(r"\d", prev):
            rendered = UNITS_ALONE[core]
        if rendered is None and is_compass(core, nxt, after_distance(cores, i)):
            rendered = COMPASS[core]
        if rendered is None and is_by(core, prev, nxt, cores[i - 2] if i >= 2 else ""):
            rendered = "by"
        if rendered is None and is_article(core, nxt):
            rendered = "a"
        if rendered is None and GLUED_SURFACE_RE.match(core):
            rendered = piece_text(core, terms, keyword)[1]
        if rendered is None and core in terms:
            follows = terms.get(nxt, nxt.lower() if is_word(nxt) else "").split(" ")[0]
            rendered = form_of(core, terms[core], out[-1].split(" ")[-1] if out else "", follows)
        if rendered is None:
            rendered = plural_text(core, terms)
        if rendered is None:
            pieces = joined_pieces(core, terms, keyword)
            if pieces:
                rendered = "".join(reading for _written, _kind, reading in pieces)
        if rendered is None and reads_as_word(cores, roles, i, keyword):
            rendered = core.lower()
        out.append(lead + (rendered if rendered is not None else core) + trail)

    # Sentence case: the first word, and a word that follows a full stop.
    for i, word in enumerate(out):
        after_stop = i > 0 and out[i - 1].endswith(".") and not out[i - 1].endswith("..")
        if (i == 0 or after_stop) and word[:1].islower():
            out[i] = word[0].upper() + word[1:]
    return prefix + " ".join(out)
