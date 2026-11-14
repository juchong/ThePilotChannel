#!/usr/bin/env python3
"""Regenerate the reference data the NOTAM decoder ships with. Nothing in the
output is chosen by hand: every entry is computed from the documents below by
the rules below, and carries the text it came from.

Sources:
  AIM       Aeronautical Information Manual, chapter 5 section 1: the NOTAM keyword
            table and the table "Contractions Commonly Found in NOTAMs"
  7930.2    FAA Order 7930.2 (NOTAMs): TBL 3-3-1 NOTAM phrases, TBL 5-5-1 types of
            radar, TBL 6-1-1 types of activity, the contractions table of appendix A
            and appendix C (U.S. contractions that are not ICAO's)
  7340.2    FAA Order JO 7340.2 (Contractions), chapter 2 section 1: the decode
            table, each definition marked with its usage (ICAO, GEN, ATC, NWS, METAR)
  FAA AIP   Aeronautical Information Publication of the United States, GEN 2.2:
            abbreviations used in AIS publications
  Doc 8400  ICAO Abbreviations and Codes, as the AIPs of the states in
            DOC8400_PAGES list its abbreviations in their GEN 2.2
  STATE_PAGES  a state's own document on a kind of NOTAM its lists do not cover

Written to backend/app/data/:
  faa_contractions.json   the 7340.2 decode table: contraction -> [[usage, definition]]
  notam_terms.json        the contractions the decoder may spell out, with sources
  notam_words.json        lower-case words of the FAA's own prose and definitions
  english_words.json      "words": further English words, those of four letters or
                          more that two published word lists (SCOWL's American and
                          British lists, as Debian packages them) enter in lower
                          case and not also as a name, that no source lists as a
                          contraction and that are no word of the phonetic
                          alphabet (AIM TBL 4-2-7) and no Roman numeral;
                          "also": the lists' other entries of three letters or
                          more that no source lists as a contraction (words of
                          three letters, names, the words of the phonetic
                          alphabet), which a rewrite may write in any case
and to backend/tests/data/: faa_examples.json, the example NOTAMs the FAA prints,
and notam_sources.json, the other lists as read, for the tests.

Rules for notam_terms.json:
  1. The NOTAM vocabulary is the one Order 7930.2 para 3-3-1 names: "Contractions
     and abbreviations designated for ICAO usage as specified in FAA Order JO
     7340.2 ... and ICAO Document 8400 ... must be used in the NOTAM system". A
     contraction belongs to it when the AIM table or a table of Order 7930.2
     lists it, Order 7340.2 marks it for ICAO usage, or Doc 8400 lists it. Beside
     those: an abbreviation the FAA's NOTAM documents write out in words and
     print in NOTAM text of their own ("Aeronautical Study Number (ASN)"), and one
     of the FAA AIP's list for which Order 7340.2 gives the same meaning, if it
     has four letters or more or the FAA prints it in a NOTAM (REIL, BYD). A
     row the Order marks for general, ATC or weather usage is recorded beside the
     entry and is no source; a row marked as a telegraphy signal is no text.
  2. Doc 8400 is read from state AIPs, which mark what differs from it. They
     carry misprints and national wordings, so a wording is taken only where two
     publications print it letter for letter: two AIPs, or one and Order 7340.2
     under any usage.
  3. The meaning is the first of: the AIM table, Order 7930.2, the ICAO rows of
     Order 7340.2, Doc 8400, the written-out words, the FAA AIP. Where the Order's
     ICAO row does not spell the contraction out and Doc 8400's does, Doc 8400's
     is used. A definition may list forms of one word ("light or lighting"); a
     row that is a sentence is no second meaning beside a row that is a phrase.
     Two NOTAM tables that differ are settled by the meaning another document
     also gives (PRN).
  4. The rendering is the form of the first source that gives exactly one. When
     that source lists several forms of one word ("receive or receiver"), it is
     the form the FAA's NOTAM documents write most often, or the past-tense form
     when the contraction itself ends in D (Order 7930.2 para 3-3-1: a contraction
     decodes to singular and plural, past and present alike); "forms" then
     records the words those documents write before and after each form, so that
     the decoder can take the form its neighbour calls for ("receiver
     checkpoint", "movement areas"); a contraction written in the past tense
     keeps that form. The rendering is written as it reads inside a sentence:
     lower case, keeping names and words the FAA writes in capitals.
  5. The contraction is left as written, and recorded under "undecoded" with the
     reason, when
       - it is itself an English word of the FAA's prose or of its own
         definition; of two or three letters, a word the NOTAM documents write
         in running text. If it is a contraction of the AIM table or of an ICAO
         list besides (IF, PER, MAP), it is marked "either" and kept out of
         notam_words.json: only the sentence tells which it is;
       - it has two letters, Order 7930.2 does not list it and no example NOTAM
         of the FAA writes it as text, and either no FAA document names it for
         NOTAM or ICAO usage or some publication gives it another meaning (a
         designator, a state, a weather code);
       - only the AIM table gives the meaning, no ICAO list agrees with it and the
         FAA shows it in no NOTAM text of its own;
       - its definition is a sentence, a description, an entry with a number or
         a Q-code signal, not words to read in the contraction's place, or does
         not hold the contraction's letters (a two-letter code of the ICAO lists
         need not: HJ, sunrise to sunset);
       - its definition lists different things, not forms of one word (REP:
         report, or reporting point), unless the contraction is the initials of
         the first (WEF: with effect from);
       - two sources give it different meanings, the FAA's NOTAM documents
         write it out in other words than the list, or Doc 8400 alone gives the
         meaning while Order 7340.2 has another under a general or ATC usage,
         which U.S. text may use (OTS: organized track system, or out of
         service).
     A contraction that holds a figure (H24) is not in the table: the numbers of
     a rendering are the NOTAM's. A contraction the FAA writes in NOTAM text of
     its own is marked "shown". The "accept" list holds every form the sources
     list for the meaning, except the -ing form of a verb they also give in the
     past tense ("closing" beside "closed"): Order 7930.2 para 3-3-1 decodes a
     contraction into the past and the present tense, and "is closing" is
     neither. Those forms are recorded under "progressive".
  6. A leading X abbreviates "cross" (Order 7340.2, ICAO usage: X = Cross), and
     X is not written alone for it. The Order's own XNG and XBAR follow from rule
     1; in addition, X before the rest of each word of the FAA's prose that
     begins with "cross" is spelled out (XWIND crosswind).
  7. Phrases are those of the Order's tables and those the NOTAM documents give
     in words beside their NOTAM wording ("Runway end identifier light (RWY END ID
     LGT)"). Days, compass points and units come from the same documents and
     record where; the metric units are those of the ICAO lists. "runway_reports"
     holds what a state's document writes out and shows before a runway
     designator (Canada's RSC).

Standard library only:  python3 backend/tools/build_notam_data.py [--cache DIR]
(--cache keeps the fetched pages in DIR and reads them from there on later runs)
"""
from __future__ import annotations

import html
import io
import json
import os
import re
import sys
import tarfile
import urllib.request

BASE = "https://www.faa.gov/air_traffic/publications/atpubs"
DECODE_URL = f"{BASE}/cnt_html/chap2_section_1.html"
AIM_URL = f"{BASE}/aim_html/chap5_section_1.html"
NOTAM_ORDER = f"{BASE}/notam_html"
# State AIPs list the abbreviations of ICAO Doc 8400 in GEN 2.2 and mark with an
# asterisk every entry that differs from it or is not in it. Each address
# carries the edition of that AIP.
DOC8400_PAGES = (
    ("AIP Ireland GEN 2.2", "https://www.airnav.ie/AIRAC_MAY_2026/2026-05-14-AIRAC/html/eAIP/EI-GEN-2.2-en-IE.html"),
    ("AIP Republic of Korea GEN 2.2", "https://aim.koca.go.kr/eaipPub/Package/2025-01-09/html/eAIP/KR-GEN-2.2-en-GB.html"),
    ("AIP Portugal GEN 2.2", "https://ais.nav.pt/wp-content/uploads/AIS_Files/eVFR_Current/eVFR_Online/eAIP/html/eAIP/LP-GEN-2.2-en-PT.html"),
)
# Documents of other states that write out what opens a runway report in their NOTAMs.
STATE_PAGES = (
    ("Transport Canada AC 300-019", "https://tc.canada.ca/en/aviation/reference-centre/advisory-circulars/advisory-circular-ac-no-300-019"),
)
FAA_AIP_URL = f"{BASE}/aip_html/part1_gen_section_2.2.html"
SPELLING_URL = f"{BASE}/aim_html/chap4_section_2.html"
# English, as two published word lists: the American and the British list of
# SCOWL as Debian packages them (wamerican, wbritish). Each address carries the
# version of the package.
ENGLISH_PAGES = (
    ("wamerican 2020.12.07-4", "https://deb.debian.org/debian/pool/main/s/scowl/wamerican_2020.12.07-4_all.deb",
     "./usr/share/dict/american-english"),
    ("wbritish 2020.12.07-4", "https://deb.debian.org/debian/pool/main/s/scowl/wbritish_2020.12.07-4_all.deb",
     "./usr/share/dict/british-english"),
)
FAA_AIP = "FAA AIP GEN 2.2"
# a row that is a telegraphy signal, not text
SIGNAL = re.compile(r"\([^()]*\b(?:procedure signal|used to precede)\b[^()]*\)")
WRITTEN_OUT = "the FAA's NOTAM documents, written out"
MINOR = frozenset("and of the for to in on or a an at".split())
ORDER_PAGES = (
    [f"chap1_section_{n}" for n in (1, 2, 3, 4)]
    + ["chap2_section_1"]
    + [f"chap3_section_{n}" for n in (1, 2, 3)]
    + [f"chap4_section_{n}" for n in (1, 2, 3)]
    + [f"chap5_section_{n}" for n in (1, 2, 3, 4, 5)]
    + ["chap6_section_1", "chap7_section_1", "chap7_section_2", "appendix_a", "appendix_c"]
)
UA = "the-pilot-channel/0.2 (+https://github.com/juchong/ThePilotChannel)"
OUT = os.path.join(os.path.dirname(__file__), "..", "app", "data")
TESTS = os.path.join(os.path.dirname(__file__), "..", "tests", "data")
KEYWORDS = ("RWY TWY APRON AD OBST NAV COM SVC AIRSPACE ODP SID STAR CHART DATA DVA IAP VFP ROUTE "
            "SPECIAL SECURITY").split()
STEM = 4                 # letters two forms of one word must share
CROSS_USAGE = "ICAO"     # the usage under which Order 7340.2 defines X as "Cross"
UNITS = (                # the unit's word is the FAA's; number agreement (1 foot, 2 feet) is English grammar
    ("FT", "foot", "feet"), ("NM", "nautical mile", "nautical miles"), ("SM", "statute mile", "statute miles"),
    ("MIN", "minute", "minutes"), ("HR", "hour", "hours"), ("LB", "pound", "pounds"), ("KT", "knot", "knots"),
    ("DEG", "degree", "degrees"), ("IN", "inch", "inches"), ("SEC", "second", "seconds"),
    ("M", "meter", "meters"), ("KM", "kilometer", "kilometers"), ("CM", "centimeter", "centimeters"),
    ("KG", "kilogram", "kilograms"), ("MHZ", "megahertz", "megahertz"), ("KHZ", "kilohertz", "kilohertz"),
)
SPELLING = (("centre", "center"), ("metre", "meter"), ("defence", "defense"), ("litre", "liter"), ("programme", "program"),
            ("isation", "ization"))      # British and American spellings of one word


def american(text_: str) -> str:
    text_ = text_.lower()
    for british, us in SPELLING:
        text_ = text_.replace(british, us)
    return text_


def fetch(url: str) -> str:
    """The page at url; with --cache DIR, kept there and read from there next time."""
    cache = sys.argv[sys.argv.index("--cache") + 1] if "--cache" in sys.argv else None
    path = os.path.join(cache, re.sub(r"[^A-Za-z0-9.]+", "_", url)) if cache else None
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    if path:
        os.makedirs(cache, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(page)
    return page


def fetch_bytes(url: str) -> bytes:
    """The file at url; cached like a page."""
    cache = sys.argv[sys.argv.index("--cache") + 1] if "--cache" in sys.argv else None
    path = os.path.join(cache, re.sub(r"[^A-Za-z0-9.]+", "_", url)) if cache else None
    if path and os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if path:
        os.makedirs(cache, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
    return data


def package_file(package: bytes, name: str) -> str:
    """The text of one file of a Debian package: an ar archive whose data.tar
    member holds the files."""
    if not package.startswith(b"!<arch>\n"):
        raise ValueError("not a Debian package")
    at = 8
    while at + 60 <= len(package):
        member = package[at:at + 16].decode().strip().rstrip("/")
        size = int(package[at + 48:at + 58].decode().strip())
        if member.startswith("data.tar"):
            with tarfile.open(fileobj=io.BytesIO(package[at + 60:at + 60 + size])) as tar:
                return tar.extractfile(name).read().decode("utf-8")
        at += 60 + size + size % 2
    raise ValueError("the package holds no data.tar")


def english_of(lists: list) -> tuple:
    """-> (words, names): what the word lists enter in lower case, and what they
    enter with a capital (a name), both in lower case. Entries with an
    apostrophe or an accent are not NOTAM text."""
    words, names = set(), set()
    for entries in lists:
        for entry in entries.split("\n"):
            if entry.isascii() and entry.isalpha():
                (words if entry.islower() else names).add(entry.lower())
    return words, names


# Alfa and Juliett are ICAO's spellings of two English words; a NOTAM may write either.
ENGLISH_SPELLING = {"ALFA": "ALPHA", "JULIETT": "JULIET"}
ROMAN = re.compile(r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})")


def spelling_alphabet(page: str) -> dict:
    """The words of the ICAO phonetic alphabet (AIM TBL 4-2-7): {WORD: letter},
    with the English spelling of the two words ICAO spells its own way."""
    out = {}
    for tb in tables(page):
        for row in tb:
            if len(row) >= 3 and re.fullmatch(r"[A-Z]", row[0]) and re.fullmatch(r"[A-Z][a-z]+", row[2]) and row[2][0] == row[0]:
                out[row[2].upper()] = row[0]
                if row[2].upper() in ENGLISH_SPELLING:
                    out[ENGLISH_SPELLING[row[2].upper()]] = row[0]
    return out


def text(fragment: str) -> str:
    """Tags removed, entities decoded, soft hyphens dropped, whitespace collapsed."""
    plain = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    plain = plain.replace("\xad", "").replace("‐", "-").replace("‑", "-")
    return re.sub(r"\s+", " ", plain).strip()


def main_text(page: str) -> str:
    m = re.search(r"<main.*?</main>", page, flags=re.S)
    return text(m.group(0) if m else page)


def tables(page: str) -> list:
    out = []
    for tb in re.findall(r"<table.*?</table>", page, flags=re.S):
        rows = [[text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, flags=re.S)]
                for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, flags=re.S)]
        out.append([r for r in rows if any(r)])
    return out


def alternatives(definition: str) -> list:
    """The alternatives a definition lists, as written: "Close or closed or
    closing" -> [Close, closed, closing]. Remarks in parentheses and suffix
    markers ("Mile/s", "Light(s)", "simultaneous/ly") are not part of one. A
    slash separates alternatives only between whole words without spaces
    ("Receive/Receiver")."""
    d = re.sub(r"\(s\)|/(?:s|es|ly)\b", "", definition)
    while True:
        stripped = re.sub(r"\([^()]*\)", " ", d)
        if stripped == d:
            break
        d = stripped
    d = re.sub(r"\([^()]*$", " ", d)      # a remark the page leaves unclosed
    out: list = []
    for part in re.split(r",?\s+or\s+|;", d):
        listed = [x.strip() for x in part.split(",") if x.strip()]
        if len(listed) > 1 and not all(same_word(listed[0], x) for x in listed[1:]):
            listed = [part]      # one meaning that names several things ("aerodromes, air routes and ground aids")
        for item in listed:
            item = " ".join(item.split()).strip(" .,\u2026")
            pieces = item.split("/") if re.fullmatch(r"[A-Za-z]{3,}(?:/[A-Za-z]{3,})+", item) else [item]
            for piece in pieces:
                if len(piece) >= 2 and piece.lower() != "or" and piece.lower() not in [o.lower() for o in out]:
                    out.append(piece)
    return out


def progressive(form: str, forms: list) -> bool:
    """Whether a form is the -ing form of a verb that another form gives in the
    past tense: "closing" beside "closed", not "lighting" beside "light"."""
    f = form.lower()
    if " " in f or not f.endswith("ing") or len(f) < 6:
        return False
    stem = f[:-3]
    return any(o.lower().endswith("ed") and o.lower().startswith(stem) for o in forms)


def usable(definition: str) -> bool:
    """A definition that gives a meaning (not a bare instruction in parentheses,
    and not a cell the published page has lost)."""
    return bool(alternatives(definition))


def same_word(a: str, b: str, stem: int = STEM) -> bool:
    """Two forms of one word or phrase. Single words: equal, or sharing a stem of
    STEM letters (close, closed, closing). Phrases: the same words in the same
    order, where two words are the same when they share that stem (operating,
    operations), with or without the words of MINOR, British or American
    spelling, hyphens and plurals; or one phrase the other with words added
    ("approach control office", "filed flight plan")."""
    a, b = (re.sub(r"(?i)\s+(to|of|for|at|from)$", "", s.strip()) for s in (a, b))      # "cleared to" is "cleared"
    a, b = (re.sub(r"(?i)\b(the|a|an)\s+", "", s) for s in (a, b))
    x, y = (re.sub(r"[^a-z]", "", american(s)) for s in (a, b))
    if not x or not y:
        return False
    if x == y:
        return True
    single = not re.search(r"[ -]", a.strip()) and not re.search(r"[ -]", b.strip())
    if single:
        return min(len(x), len(y)) >= stem and x[:stem] == y[:stem]

    def same(v: str, w: str) -> bool:
        v, w = sorted((v, w), key=len)
        return v == w or singular(v) == singular(w) or (len(v) >= stem and w.startswith(v)) \
            or (len(v) >= stem + 2 and v[:stem + 2] == w[:stem + 2])

    for joined in ("", " "):      # "co-ordination" is one word, "direction-finding" two
        p, q = ([w for w in re.findall(r"[a-z]+", american(s).replace("-", joined)) if w not in MINOR] for s in (a, b))
        short, long_ = sorted((p, q), key=len)
        if not short:
            continue
        if len(short) == len(long_):
            if all(same(v, w) for v, w in zip(short, long_)) or (len(short) > 1 and sorted(short) == sorted(long_)):
                return True      # "locator outer" and "outer locator"
        elif len(short) == 1:
            if short[0] == long_[0] or singular(short[0]) == singular(long_[0]):
                return True      # "approach" and "approach control", not "report" and "reporting point"
        else:
            rest = iter(long_)
            if all(any(same(word, other) for other in rest) for word in short):
                return True      # the longer phrase only adds words
    return False


def agrees(choice: str, alts: list) -> bool:
    """A source agrees with a rendering when it lists it (also as one side of a
    slash) or lists only other forms of the same word."""
    listed = set()
    for a in alts:
        listed.add(a.lower())
        listed.update(part.strip().lower() for part in a.split("/"))
    return choice.lower() in listed or all(same_word(choice, a) for a in alts)


def alike(first: str, second: str) -> bool:
    """Whether two definitions give the same meaning: a form one lists is a form
    of what the other lists."""
    return any(agrees(a, alternatives(second)) for a in alternatives(first)) \
        or any(agrees(a, alternatives(first)) for a in alternatives(second))


def expands(term: str, rendering: str, lettered: bool = True) -> bool:
    """Whether the rendering spells the contraction out: a phrase to read in its
    place, not a sentence, a description or a code table entry, holding the
    contraction's letters in order (TWR: aerodrome control ToWeR; RNAV: aRea
    NAVigation). An X the phrase does not hold marks an abbreviation (XNG
    crossing, WX weather), a Z may stand for an S (ADZ advise), and a contraction
    of four letters or more may respell one letter (SKED schedule). A Q-code
    group (QNH, QFE) is a signal read as it stands. Without `lettered` the
    letters are not looked at."""
    words = re.findall(r"[A-Za-z]+", rendering)
    letters = re.sub(r"[^a-z]", "", term.lower())
    if not words or len(words) > 12 or re.search(r"\d|[?\u2026]|\.\.\.|, .*\bof$", rendering.strip()) \
            or re.match(r"(?i)(a|an|the|this|means|indication|do you|will you|shall i|what is|we|i)\b", rendering.strip()) \
            or re.fullmatch(r"q[a-z]{2}", letters):
        return False
    spelled = "".join(words).lower().replace("centre", "center")
    if "z" not in spelled:
        letters = letters.replace("z", "s")
    letters = letters if "x" in spelled else letters.replace("x", "")
    row = [0] * (len(spelled) + 1)      # longest common subsequence of the letters and the phrase
    for ch in letters:
        new = [0]
        for j, c in enumerate(spelled, 1):
            new.append(row[j - 1] + 1 if c == ch else max(row[j], new[-1]))
        row = new
    return bool(letters) and (not lettered or row[-1] >= len(letters) - (1 if len(letters) >= 4 else 0))


CALENDAR = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february",
            "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"}


def cased(choice: str, faa_defs: list) -> str:
    """The rendering as it reads inside a sentence. Order 7340.2 writes common
    terms in lower case and names with capitals (Flight Service Station), so its
    spelling is used where it has the same words; a rendering it does not have
    is lower-cased, keeping words written in capitals (VOR). A lone capital on
    the first word is 7340.2 starting its line, not a name; days and months
    keep theirs."""
    plain = lambda t: re.sub(r"[^a-z]", "", t.lower())      # noqa: E731
    for _usage, d in faa_defs:
        for a in alternatives(d):
            if plain(a) == plain(choice):
                choice = a
                break
        else:
            continue
        break
    else:
        choice = " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in choice.split())
    words = choice.split()
    if words and words[0][:1].isupper() and not words[0].isupper() and not any(w[:1].isupper() for w in words[1:]):
        words[0] = words[0].lower()
    out = " ".join(words)
    return out.capitalize() if out in CALENDAR else out


def decode_table(page: str) -> tuple:
    title = re.search(r"<title>(.*?)</title>", page, flags=re.S)
    terms: dict = {}
    for row in (r for tb in tables(page) for r in tb):
        if len(row) != 3 or row[0] == "Contraction" or not row[0]:
            continue
        entry = [row[2], row[1]]
        bucket = terms.setdefault(row[0].upper(), [])
        if entry not in bucket:
            bucket.append(entry)
    return (text(title.group(1)) if title else ""), terms


def aim_tables(page: str) -> tuple:
    """-> (keywords {KEYWORD: definition}, examples [message], contractions {TERM: [definition]})."""
    keyword_rows, contraction_rows = tables(page)[:2]
    keywords, examples = {}, []
    for left, right in (r for r in keyword_rows if len(r) == 2):
        m = re.match(r"^([A-Z]+(?: [A-Z]+)?(?: \(GPS\))?) Example$", left)
        if not m:
            continue
        definition, _, example = right.partition(" !")
        if m.group(1) in KEYWORDS:
            keywords[m.group(1)] = definition.strip()
        if example:
            example = re.sub(r"\s*\(Note\*.*?\)", "", example)
            examples.append("!" + re.sub(r"\s*YYMMDDHHMM\s*-\s*(?:YYMMDDHHMM|PERM)(?:\s*EST)?\s*$", "", example).strip())
    contractions: dict = {}
    for row in contraction_rows:
        if len(row) == 2 and row[0] and row[1]:
            contractions.setdefault(row[0].upper(), []).append(row[1])
    return keywords, examples, contractions


def order_terms(pages: dict) -> tuple:
    """What Order 7930.2 defines in its own tables and appendix C:
    ({TERM: (definition, where)}, {PHRASE: (definition, where)})."""
    terms: dict = {}
    phrases: dict = {}
    for name, where in (("chap3_section_3", "JO 7930.2 TBL 3-3-1"), ("chap5_section_5", "JO 7930.2 TBL 5-5-1"),
                        ("chap6_section_1", "JO 7930.2 TBL 6-1-1")):
        for tb in tables(pages[name]):
            for cell in (c for row in tb for c in row):
                m = re.match(r"^([A-Z][A-Z/ ]{1,24}?) \(([^()]+)\)?$", cell)
                if m:
                    name_, meaning = m.group(1).strip(), m.group(2).strip()
                    (phrases if " " in name_ else terms).setdefault(name_, (meaning, where))
                elif where == "JO 7930.2 TBL 5-5-1" and re.fullmatch(r"[A-Z]{2,}(?: [A-Z]{2,})+(?: No contraction)?", cell):
                    # a type of radar the table writes out because it has no contraction ("PRECISION RWY MNT")
                    phrases.setdefault(cell.replace(" No contraction", ""), (None, where))
    for tb in tables(pages["appendix_a"]):
        for row in tb:
            if len(row) == 2 and re.fullmatch(r"[A-Z]{3,6}", row[0]):
                terms.setdefault(row[0], (row[1], "JO 7930.2 appendix A"))
    listing = main_text(pages["appendix_c"])
    marks = list(re.finditer(r"(?<= )([A-Z]{2,7}(?:/ ?[A-Z]{2,4})?) - ", listing))
    for n, m in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(listing)
        terms.setdefault(m.group(1).replace(" ", ""), (listing[m.end():end].strip(), "JO 7930.2 appendix C"))
    return terms, phrases


def aip_list(page: str) -> dict:
    """One state's list of ICAO Doc 8400 abbreviations: {TERM: [definition]}. An
    entry the AIP marks with an asterisk differs from Doc 8400 or is not in it,
    and is left out."""
    notes = text(page)
    if "8400" not in notes or "asterisk" not in notes:
        return {}
    out: dict = {}
    for row in (r for tb in tables(page) for r in tb):
        if len(row) != 2 or not row[0] or not row[1] or "*" in row[0] or row[1].startswith("*"):
            continue
        term = re.sub(r"\s*[-\u2013]\s*", "-", row[0].rstrip("\u2020\u2021 ").strip())
        definition = re.sub(r"^\((?:to be )?pronounced[^)]*\)\s*", "", row[1])
        definition = re.sub(r"\s*(?:\u2026|\.{2,})+\s*$", "", definition)      # "After…": what follows is not part of it
        if re.fullmatch(r"[A-Z][A-Z0-9/-]*", term) and definition and definition not in out.setdefault(term, []):
            out[term].append(definition)
    return out


def letters(form: str) -> str:
    return re.sub(r"[^a-z]", "", form.lower())


def doc8400_terms(lists: list, faa: dict) -> dict:
    """What ICAO Doc 8400 says, as far as two publications give it letter for
    letter: {TERM: [(definition, source)]}. The publications are the state AIPs
    (name, {TERM: [definition]}) and Order 7340.2 under any usage. A form only
    one of them prints is left out (a misprint, a national wording), and so is a
    meaning that any of them marks as a telegraphy signal. A further meaning
    that one publication alone gives follows as a row marked "only": it is no
    source, but it is on record."""
    out: dict = {}
    for term in sorted({t for _name, entries in lists for t in entries}):
        groups: list = []      # one per meaning: [(publication, definition)]
        for name, entries in lists:
            for definition in entries.get(term, []):
                if not usable(definition):
                    continue
                group = next((g for g in groups if alike(definition, g[0][1])), None)
                if group is None:
                    groups.append([(name, definition)])
                else:
                    group.append((name, definition))
        for _usage, definition in faa.get(term, []):
            for group in groups:
                if usable(definition) and alike(definition, group[0][1]):
                    group.append(("JO 7340.2", definition))
        for group in groups:
            if any(SIGNAL.search(d) for _name, d in group):
                continue
            forms, seen = [], {}
            for name, definition in group:
                for form in alternatives(definition):
                    seen.setdefault(letters(form), set()).add(name)
                    if name != "JO 7340.2" and letters(form) not in [letters(f) for f in forms]:
                        forms.append(form)      # the Order's other usages confirm a form; they add none
            agreed = [f for f in forms if len(seen[letters(f)]) > 1]
            if agreed:
                names = [name for name, _e in lists if any(name in seen[letters(f)] for f in agreed)]
                out.setdefault(term, []).insert(0, (" or ".join(agreed), f"ICAO Doc 8400 ({', '.join(names)})"))
            else:      # one publication only: no source for the meaning, but a second meaning on record
                out.setdefault(term, []).append((group[0][1], f"ICAO Doc 8400 ({group[0][0]} only)"))
    return {term: rows for term, rows in out.items() if not rows[0][1].endswith(" only)")}


def faa_aip_list(page: str) -> dict:
    """The FAA's own list of abbreviations used in AIS publications (AIP GEN
    2.2): {TERM: definition}."""
    out = {}
    for row in (r for tb in tables(page) for r in tb):
        if len(row) == 2 and row[1] and re.fullmatch(r"[A-Za-z][A-Za-z/-]+", row[0]):
            out.setdefault(row[0].upper(), row[1])
    return out


def faa_aip_terms(listed: dict, faa: dict) -> dict:
    """The entries of the FAA AIP's list for which Order 7340.2 gives the same
    meaning under some usage."""
    return {term: definition for term, definition in listed.items()
            if usable(definition) and any(usable(d) and alike(definition, d) for _usage, d in faa.get(term, []))}


def written_out(prose: str) -> dict:
    """Abbreviations the text writes out before giving them in parentheses, where
    the abbreviation is the initial letters of the words ("Antenna Structure
    Registration (ASR)"): {ABBR: [words]}."""
    out: dict = {}
    for m in re.finditer(r"((?:[A-Za-z][A-Za-z/-]*,? ){1,9})\(\s*([A-Z]{2,7})\s*\)", prose):
        words = re.findall(r"[A-Za-z]+", m.group(1))
        for n in range(len(words)):
            tail = words[n:]
            major = [w for w in tail if w.lower() not in MINOR]
            if tail[0].lower() in MINOR or not major:
                continue
            if m.group(2) in ("".join(w[0] for w in major).upper(), "".join(w[0] for w in tail).upper()):
                phrase = " ".join(tail)
                if phrase.isupper():      # NOTAM text, not prose
                    break
                if not any(same_word(phrase, known) for known in out.setdefault(m.group(2), [])):
                    out[m.group(2)].append(phrase)
                break
    return out


def side_by_side(prose: str) -> dict:
    """Phrases the text gives in words and, right after, in NOTAM wording in
    parentheses ("Runway end identifier light (RWY END ID LGT)"), where each
    word is the NOTAM token itself or spells it out: {PHRASE: words}."""
    out: dict = {}
    pattern = r"((?:[A-Za-z][A-Za-z'/-]*,? ){2,10})\.? ?\(\s*((?:[A-Z][A-Z/]*)(?: [A-Z][A-Z/]*){1,7})\s*\)"
    for m in re.finditer(pattern, prose):
        tokens = m.group(2).split()
        said = re.findall(r"[A-Za-z][A-Za-z'/-]*", m.group(1))[-len(tokens):]
        if len(said) == len(tokens) and not " ".join(said).isupper() \
                and all(word.upper() == token or expands(token, word) for word, token in zip(said, tokens)) \
                and any(word.upper() != token for word, token in zip(said, tokens)):
            out.setdefault(m.group(2), " ".join(said))
    return out


def runway_reports(name_: str, page: str) -> dict:
    """What opens a report on one runway in a state's NOTAMs: the abbreviations
    the state's document writes out and its examples open with before a runway
    designator ("RSC 32"): {ABBR: (words, document)}."""
    body = text(page)
    return {abbr: (phrases[0], name_) for abbr, phrases in written_out(body).items()
            if len(phrases) == 1 and re.search(rf"Example:? {abbr} \d{{2}}[LRC]?(?![A-Za-z0-9])", body)}


def short_words_of(prose: str) -> set:
    """Two- and three-letter English words of running text: lower case and
    standing alone (not joined to a figure, a hyphen, a slash or an apostrophe,
    and not the label of a paragraph, "ad."), with the singular of a plural
    ("arcs")."""
    prose = re.sub(r"\S*(?:https?://|www\.|@)\S*|\S+\.(?:gov|com|org|net|aero|html?|pdf)\b\S*", " ", prose)
    prose = re.sub(r"\((?:s|es|ies)\)", "", prose)
    found = set(re.findall(r"(?<![A-Za-z0-9'\u2019/_=(-])([a-z]{2,4})(?![A-Za-z0-9'\u2019/_)-]|\.(?: [A-Z(]|$))", prose))
    return {w for w in found if len(w) <= 3} | {w[:-1] for w in found if len(w) == 4 and w[-1] == "s" and w[-2] not in "aeious"}


def before_article_of(prose: str) -> set:
    """The words the FAA's NOTAM documents write directly before the article "a"
    ("within a", "is a", "issue a")."""
    return set(re.findall(r"(?<![A-Za-z0-9'\u2019-])([a-z]{2,}) a (?=[a-z])", prose))


def opposites_of(pages: dict) -> list:
    """Pairs of opposite conditions Order 7930.2 TBL 3-3-1 gives as alternatives
    ("OPN or CLSD", "AVBL or NOT AVBL")."""
    pairs = []
    for tb in tables(pages["chap3_section_3"]):
        for cell in (c for row in tb for c in row):
            m = re.match(r"^((?:NOT )?[A-Z/]+) or ((?:NOT )?[A-Z/]+)$", cell)
            if m and [m.group(1), m.group(2)] not in pairs:
                pairs.append([m.group(1), m.group(2)])
    return pairs


def examples_of(pages: dict) -> list:
    """Example NOTAM bodies printed in Order 7930.2 (each starts with a keyword)."""
    body = " ".join(main_text(pages[p]) for p in sorted(pages))
    kw = "|".join(KEYWORDS)
    found = re.findall(rf"(?:…|\.\.\.)\s*((?:\(U\) )?(?:{kw})\b[^…]{{3,600}}?)\s*(?:…|\.\.\.(?!\.))", body)
    out: list = []
    for e in found:
        e = re.sub(r"\s+Y+MMDD\w*\s*-\s*Y*\w*\s*$", "", e).strip()
        e = re.sub(r"\(\s+", "(", e)
        if re.search(r"[A-Z]/ [A-Z]| /[A-Z]", e):
            e = re.sub(r"\s+/|/\s+", "/", e)
        if re.search(r"NOTE-|EXAMPLE|YYMMDD|\(city|\(airport\)|\(Include|\(description|pilots are|REASON:|XXX", e):
            continue
        if re.search(r"[a-z]{3,}", e) or len(e.split()) < 2 or e in out:
            continue
        out.append(e)
    return out


def words_of(prose: str) -> set:
    """Lower-case words of running text; addresses and file names are not text."""
    prose = re.sub(r"\S*(?:https?://|www\.|@)\S*|\S+\.(?:gov|com|org|net|aero|html?|pdf)\b\S*", " ", prose)
    return set(re.findall(r"(?<![A-Za-z])[a-z]{2,}(?![A-Za-z])", prose))


def notam_text_of(pages: dict) -> list:
    """Every piece of NOTAM text Order 7930.2 prints: the example bodies between
    ellipses and the whole FDC messages, placeholders and all."""
    body = " ".join(main_text(pages[p]) for p in sorted(pages))
    return (re.findall(r"(?:\u2026|\.\.\.)\s*([A-Z(][^\u2026]{3,700}?)\s*(?:\u2026|\.\.\.(?!\.))", body)
            + re.findall(r"!FDC [^!]{10,900}", body))


def used_in(texts: list) -> set:
    """Capitalised tokens of the NOTAM text the FAA prints."""
    used = set()
    for text_ in texts:
        tokens = text_.split()
        for n, tok in enumerate(tokens):
            core = tok.strip("().,:;\u201c\u201d\u2026\"'")
            if not re.fullmatch(r"[A-Z][A-Z/-]*", core):
                continue
            if len(core) == 2 and n and tokens[n - 1].endswith(","):
                continue      # a state code after a comma ("DALLAS, TX.") is not a use of the contraction
            used.add(core)
            used.update(part for piece in core.split("/") for part in piece.split("-"))
    return used


def written_as_text(examples: list) -> set:
    """Capitalised tokens the FAA's example NOTAMs use as text: not a designator
    after RWY, TWY, TXL or APRON, and nothing after a comma (a further
    designator, a state code)."""
    out = set()
    for example in examples:
        tokens = example.split()
        for n, tok in enumerate(tokens):
            core = tok.strip("().,:;\"'/")
            before = tokens[n - 1] if n else ""
            if not before.endswith(",") and before.strip("(") not in ("RWY", "TWY", "TXL", "APRON"):
                out.update(piece for piece in core.split("/") if re.fullmatch(r"[A-Z]+", piece))
    return out


def initials(term: str, rendering: str) -> bool:
    """Whether the contraction is the initial letters of the rendering's words
    (WEF: With Effect From), which settles it whatever else the definition adds."""
    return "".join(w[0] for w in re.findall(r"[A-Za-z]+", rendering)).upper() == re.sub(r"[^A-Z]", "", term)


def singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def neighbours(form: str, prose: str) -> tuple:
    """(words before, words after) the form in the FAA's NOTAM documents, each in
    the singular: "movement" is followed by "area", "receiver" by "checkpoint"."""
    f = re.escape(form.lower())
    before = {singular(w) for w in re.findall(rf"(?<![a-z])([a-z]{{3,}}) {f}s?(?![a-z])", prose)}
    after = {singular(w) for w in re.findall(rf"(?<![a-z]){f}s? ([a-z]{{3,}})(?![a-z])", prose)}
    return sorted(before), sorted(after)


def most_used(forms: list, prose: str) -> str:
    """Of the forms a definition lists, the one the FAA's NOTAM documents write
    most often; the first listed when none is more frequent."""
    counts = [len(re.findall(rf"(?<![a-z]){re.escape(f.lower())}(?![a-z])", prose)) for f in forms]
    return forms[counts.index(max(counts))]


FIRST_FORMS: dict = {}      # contraction -> the form its definition lists first


def build_terms(aim: dict, order: dict, faa: dict, doc: dict, faa_aip: dict, written: dict, words: set, short: set,
                used: set, shown: set, prose: str) -> tuple:
    decoded, undecoded = {}, {}
    name = re.compile(r"[A-Z][A-Z/]*")      # one that holds a figure (H24) stays as written: the line's numbers are the NOTAM's
    icao = {t: [d for u, d in defs if u == "ICAO" and usable(d) and not SIGNAL.search(d)] for t, defs in faa.items()}
    icao = {t: defs for t, defs in icao.items() if defs}
    every = {t: [d for _usage, d in defs if usable(d)] for t, defs in faa.items()}      # whatever the usage
    said = written      # every abbreviation the NOTAM documents write out
    written = {t: defs for t, defs in written.items() if t in used}
    own_table = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN", "SR", "SS"} | {unit for unit, _s, _p in UNITS}
    for term in sorted(set(aim) | set(order) | set(icao) | set(doc) | set(faa_aip) | set(written)):
        if len(term) < 2 or term in own_table or not name.fullmatch(term):   # days, sunrise and sunset, and units have tables of their own
            continue
        tables = [("AIM contractions table", d) for d in aim.get(term, []) if usable(d)]
        if term in order and usable(order[term][0]):
            tables.append((order[term][1], order[term][0]))
        listed_in = (["AIM contractions table"] if term in aim else []) + ([order[term][1]] if term in order else [])
        # Order 7340.2 says what is of ICAO usage; Doc 8400 itself says it where the Order does not
        of_order = [("JO 7340.2 (ICAO usage)", d) for d in icao.get(term, [])]
        of_doc = [(where, d) for d, where in doc.get(term, [])]
        spelled = [[(w, d) for w, d in rows if expands(term, alternatives(d)[0])] for rows in (of_order, of_doc)]
        lists = spelled[0] or spelled[1] or of_order or of_doc      # a row that is a sentence is no second meaning
        elsewhere = (every.get(term, []) + [d for d, _where in doc.get(term, [])] + said.get(term, [])
                     + ([faa_aip[term]] if term in faa_aip else []))
        differs = []
        if len(tables) > 1 and not all(alike(tables[0][1], d) for _w, d in tables[1:]):
            # the NOTAM tables differ: the meaning another document also gives
            backed = [(w, d) for w, d in tables if any(alike(d, other) for other in elsewhere)]
            if backed and all(alike(backed[0][1], d) for _w, d in backed[1:]):
                differs = [[w, d] for w, d in tables if not alike(backed[0][1], d)]
                tables = [(w, d) for w, d in tables if alike(backed[0][1], d)]
        if tables:
            sources = tables
        elif lists:
            sources = lists
        elif term in written:
            sources = [(WRITTEN_OUT, d) for d in written[term]]
        elif term in faa_aip and (len(re.sub(r"[^A-Z]", "", term)) >= 4 or term in used):
            sources = [(FAA_AIP, faa_aip[term])]      # a short group only where the FAA prints it in NOTAM text (Order 7340.2 para 1-2-3)
        else:
            continue
        cited = {d for _w, d in sources}
        general = [[u, d] for u, d in faa.get(term, []) if d not in cited]
        record = {"faa": [[where, d] for where, d in sources]}
        if listed_in:
            record["listed_in"] = listed_in     # the FAA NOTAM tables that list the contraction
        if term in used:
            record["shown"] = True              # the FAA writes it in NOTAM text of its own
        if general:
            record["jo_7340_2"] = general
        if differs:
            record["differs"] = differs
        listed = [alternatives(d) for _where, d in sources]
        top = next((n for n, alts in enumerate(listed) if len(alts) == 1), 0)
        choice = listed[top][0]
        FIRST_FORMS[term] = cased(choice, faa.get(term, []))
        if len(listed[top]) > 1:
            choice = most_used([f for f in listed[top] if not progressive(f, listed[top])] or listed[top], prose)
            if term.endswith("D"):      # the past-tense form; where the source prints none, not the -ing form
                plain_forms = [a for a in listed[top] if not a.lower().endswith("ing")]
                choice = next((a for a in listed[top] if a.lower().endswith("ed")), choice if choice in plain_forms or not plain_forms
                              else most_used(plain_forms, prose))
        of_icao = [d for d in icao.get(term, []) + [d for d, _where in doc.get(term, [])]]
        designated = any(agrees(choice, alternatives(d)) for d in of_icao)
        same = bool(re.search(rf"(?i)(?<![a-z]){re.escape(term)}(?![a-z])", choice))
        plain = term.lower() in short if len(term) <= 3 else (term.lower() in words or term.lower() + "s" in words)
        other = next((d for d in elsewhere if not alike(choice, d)), None)
        of_faa = bool(listed_in or icao.get(term))      # an FAA document names it for NOTAM or ICAO usage
        code = len(term) == 2 and term not in order and term not in shown
        problem = None
        if code and not of_faa:
            problem = "two letters, and no FAA document names it for NOTAM or ICAO usage"
        elif code and other:
            problem = "two letters, and another meaning is on record: it could be either"
        elif listed_in and term not in order and term not in used and not designated:
            problem = "only the AIM table gives this meaning, and the FAA shows it in no NOTAM of its own"
        elif not expands(term, choice, lettered=not (code and sources is lists)):
            problem = "the definition describes the contraction; it does not spell it out"
        elif not all(same_word(choice, a, 3) for a in listed[top]) and not initials(term, choice):      # move, moving, movement
            problem = f"{sources[top][0]} lists different meanings"
        else:
            for (where, _d), alts in zip(sources, listed):
                if not agrees(choice, alts):
                    problem = f"{where} gives a different meaning"
                    break
            else:
                apart = next((d for d in written.get(term, []) if not alike(choice, d)), None)
                if not tables and apart:
                    problem = f"the FAA's NOTAM documents write it out as \"{apart}\""
                elif not tables and not lists and other:
                    problem = "Order 7340.2 gives it another meaning"
                elif sources is lists and not of_order and any(
                        u in ("GEN", "ATC") and usable(d) and not alike(sources[top][1], d) for u, d in faa.get(term, [])):
                    problem = "Order 7340.2 gives it another meaning"      # U.S. text may use that one (OTS: out of service)
        if same or plain:
            if not problem and not same and (sources is lists or "AIM contractions table" in listed_in):
                record["either"] = True      # a plain word and a contraction of the lists: only the sentence tells
            problem = "the contraction is also an English word in the FAA's prose"
        if problem:
            undecoded[term] = {**record, "reason": problem}
        else:
            # every wording the sources give for this meaning: the forms their definitions list
            forms = [a for alts in listed for a in alts] + [a for d in of_icao for a in alternatives(d)]
            accept, ongoing = [], []
            for form in [choice] + forms:
                form = cased(form, faa.get(term, []))
                chosen = form == cased(choice, faa.get(term, []))      # the rendering itself is a wording of its source
                if (chosen or same_word(choice, form) or agrees(form, listed[top])) \
                        and form.lower() not in [a.lower() for a in accept + ongoing]:
                    (ongoing if progressive(form, [choice] + forms) and not chosen else accept).append(form)
            decoded[term] = {"text": cased(choice, faa.get(term, [])), "accept": accept, **record}
            if ongoing:
                decoded[term]["progressive"] = ongoing
            past = term.endswith("D") and choice.lower().endswith("ed")      # written in the past tense: it stays there
            if len(accept) > 1 and not past:      # several forms: which one a neighbouring word calls for
                forms = {f: neighbours(f, prose) for f in accept}
                if any(before or after for before, after in forms.values()):
                    decoded[term]["forms"] = {f: {"before": before, "after": after} for f, (before, after) in forms.items()}
    return decoded, undecoded


def cross_terms(faa: dict, words: set) -> dict:
    """X before the rest of each word of the FAA's prose that begins with
    "cross" (rule 5), where Order 7340.2 has no contraction of that spelling."""
    if not any(u == CROSS_USAGE and d.strip().lower() == "cross" for u, d in faa.get("X", [])):
        return {}
    prefix = [f"JO 7340.2 ({CROSS_USAGE} usage), X", next(d for u, d in faa["X"] if u == CROSS_USAGE)]
    out = {}
    for word in sorted(words):
        rest = word[len("cross"):]
        if word.startswith("cross") and len(rest) >= 3 and "X" + rest.upper() not in faa:
            out["X" + rest.upper()] = {"text": word, "accept": [word], "faa": [prefix], "prefix": "X"}
    return out


def build_phrases(order_phrases: dict, order_text: str, first_forms: dict, sources: dict, known_words: set,
                  said: dict) -> dict:
    """Phrases the FAA defines as a whole: those of the 7930.2 tables, those its
    NOTAM documents give in words beside their NOTAM wording, and NOT MNT, which
    the order prescribes for a facility that is not monitored (the passage is
    quoted from the order as the source)."""
    out = {}
    m = re.search(r"[^.]*\bNOT MNT\b[^.]*\.[^.]{0,200}?\bnot monitored\b[^.]*\.", order_text)
    if m:
        out["NOT MNT"] = {"text": "not monitored", "faa": [["JO 7930.2 chapter 5", " ".join(m.group(0).split())]]}
    for phrase, (meaning, where) in order_phrases.items():
        if meaning is None:      # a name written out word by word: each word in the first form its definition lists
            words = [first_forms.get(w) or (w.lower() if w.lower() in known_words else None) for w in phrase.split()]
            if all(words):
                out[phrase] = {"text": " ".join(words), "built": True,
                               "faa": [[where, phrase]] + [sources[w] for w in phrase.split() if w in sources]}
        elif expands(phrase.replace(" ", ""), meaning):
            out[phrase] = {"text": meaning.lower(), "faa": [[where, meaning]]}
    for phrase, words in said.items():      # the documents give the phrase in words beside its NOTAM wording
        out.setdefault(phrase, {"text": words.lower(), "faa": [[WRITTEN_OUT, words]]})
    return out


def build_small_tables(aim: dict, order: dict, faa: dict, doc: dict, short: set, examples: list) -> dict:
    """Days, compass points and units, each with the text it is from. A unit an
    example NOTAM of the FAA writes apart from a number ("AREA HR CHANGED"), and
    that is no English word, also carries the plural it reads as there."""
    def meaning(term):
        if any(usable(d) for d in aim.get(term, [])):
            d = next(d for d in aim[term] if usable(d))
            return alternatives(d)[0], "AIM contractions table", d
        if term in order and usable(order[term][0]):
            return alternatives(order[term][0])[0], order[term][1], order[term][0]
        for usage in ("ICAO", "GEN"):
            for u, d in faa.get(term, []):
                if u == usage and usable(d):
                    return alternatives(d)[0], f"JO 7340.2 ({usage} usage)", d
        for d, where in doc.get(term, []):
            return alternatives(d)[0], where, d
        raise KeyError(term)

    days, compass, units, sun = {}, {}, {}, {}
    for code in ("SR", "SS"):
        # Order 7930.2 para 3-3-2: a daily schedule may use Sunrise-Sunset (SR-SS)
        word = next(a for d in aim[code] for a in alternatives(d))
        sun[code] = {"text": word.lower(), "faa": [["AIM contractions table", aim[code][0]]]}
    for day in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"):
        # Order 7930.2 para 3-3-1: abbreviated days of the week in a schedule
        word = next(a for d in aim[day] for a in alternatives(d) if a.lower().endswith("day"))
        days[day] = {"text": word.capitalize(), "faa": [["AIM contractions table", aim[day][0]]]}
    for letter in ("N", "S", "E", "W"):
        word, where, d = meaning(letter)
        compass[letter] = {"text": word.lower(), "faa": [[where, d]]}
    for unit, singular_, plural in UNITS:
        word, where, d = meaning(unit)
        if not (same_word(word, singular_) or same_word(word, plural)):
            raise ValueError(f"unit {unit}: the source says {word!r}, the table says {singular_!r}")
        units[unit] = {"singular": singular_, "plural": plural, "faa": [[where, d]]}
        for example in examples:
            tokens = example.split()
            if unit.lower() not in short and any(tok.strip("().,:;") == unit and not re.search(r"\d", tokens[n - 1] if n else "")
                                                 for n, tok in enumerate(tokens)):
                units[unit]["alone"] = plural
    return {"days": days, "compass": compass, "units": units, "sun": sun}


def write(directory: str, name: str, data, indent=None) -> None:
    path = os.path.normpath(os.path.join(directory, name))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, separators=None if indent is not None else (",", ":"), sort_keys=True, indent=indent,
                  ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {path} ({os.path.getsize(path)} bytes)")


def main() -> int:
    title, faa = decode_table(fetch(DECODE_URL))
    if len(faa) < 2000:
        print(f"decode table looks incomplete ({len(faa)} contractions); nothing written", file=sys.stderr)
        return 1
    pages = {p: fetch(f"{NOTAM_ORDER}/{p}.html") for p in ORDER_PAGES}
    aim_page = fetch(AIM_URL)
    keywords, aim_examples, aim = aim_tables(aim_page)
    order, order_phrases = order_terms(pages)
    if len(aim) < 150 or len(keywords) < 15:
        print(f"AIM tables look incomplete ({len(aim)} contractions, {len(keywords)} keywords); nothing written", file=sys.stderr)
        return 1
    lists = [(name_, aip_list(fetch(url))) for name_, url in DOC8400_PAGES]
    if min(len(entries) for _name, entries in lists) < 250:
        print(f"the lists of ICAO Doc 8400 abbreviations look incomplete ({[len(e) for _n, e in lists]}); nothing written",
              file=sys.stderr)
        return 1
    aip_listed = faa_aip_list(fetch(FAA_AIP_URL))
    faa_aip = faa_aip_terms(aip_listed, faa)
    reports = {}
    for name_, url in STATE_PAGES:
        reports.update(runway_reports(name_, fetch(url)))

    english, names = english_of([package_file(fetch_bytes(url), member) for _name, url, member in ENGLISH_PAGES])
    spelling = spelling_alphabet(fetch(SPELLING_URL))
    if len(english) < 50000 or len(set(spelling.values())) != 26:
        print(f"the English word lists or the phonetic alphabet look incomplete ({len(english)} words, {len(spelling)} letters); "
              "nothing written", file=sys.stderr)
        return 1

    words = set()
    for defs in faa.values():
        for _usage, definition in defs:
            words |= words_of(definition)
    for page in pages.values():
        words |= words_of(main_text(page))
    words |= words_of(main_text(aim_page))

    doc = doc8400_terms(lists, faa)
    examples = examples_of(pages)
    used = used_in(notam_text_of(pages) + aim_examples)
    notam_prose = " ".join(main_text(page) for page in list(pages.values()) + [aim_page])
    short = short_words_of(notam_prose)
    written, said = written_out(notam_prose), side_by_side(notam_prose)
    decoded, undecoded = build_terms(aim, order, faa, doc, faa_aip, written, words, short, used,
                                     written_as_text(examples + aim_examples), notam_prose.lower())
    for term, entry in cross_terms(faa, words).items():
        if term not in decoded:
            undecoded.pop(term, None)
            decoded[term] = entry
    either = {term.lower() for term, entry in undecoded.items() if entry.get("either")}
    write(OUT, "faa_contractions.json", {"source": title, "url": DECODE_URL, "terms": faa})
    faa_words = {w for w in words if len(w) >= 4} | (short - either)
    write(OUT, "notam_words.json", sorted(faa_words))
    listed = set(faa) | set(aim) | set(aip_listed) | set(decoded) | {term for _name, entries in lists for term in entries}
    plain = {w for w in english if len(w) >= 4 and w not in names and w not in faa_words
             and w.upper() not in listed and w.upper() not in spelling and not ROMAN.fullmatch(w)}
    write(OUT, "english_words.json", {
        "words": sorted(plain),
        "also": sorted(w for w in (english | names) - plain - faa_words
                       if len(w) >= 3 and w.upper() not in listed and not ROMAN.fullmatch(w)),
    })
    write(OUT, "notam_terms.json", {
        "source": "Generated by backend/tools/build_notam_data.py from the AIM (chapter 5 section 1), FAA Order "
                  "7930.2, FAA Order JO 7340.2, the FAA AIP (GEN 2.2) and the ICAO Doc 8400 abbreviations that state AIPs list. "
                  "Do not edit: change the rules in the tool and regenerate.",
        "keywords": {**keywords, "DVA": "Diverse Vector Area"},
        "terms": decoded,
        "undecoded": undecoded,
        "phrases": build_phrases(order_phrases, " ".join(main_text(pages[p]) for p in sorted(pages)),
                                 {t: FIRST_FORMS.get(t, e["text"]) for t, e in decoded.items()},
                                 {t: e["faa"][0] for t, e in decoded.items()}, words, said),
        "runway_reports": {abbr: {"text": words.lower(), "faa": [[where, words]]} for abbr, (words, where) in sorted(reports.items())},
        "opposites": opposites_of(pages),
        "before_article": sorted(before_article_of(notam_prose)),
        "spelling": {word: {"letter": letter, "faa": [["AIM TBL 4-2-7", next(w for w, l in spelling.items() if l == letter).capitalize()]]}
                     for word, letter in sorted(spelling.items())},
        **build_small_tables(aim, order, faa, doc, short, examples + aim_examples),
    }, indent=1)
    write(TESTS, "faa_examples.json", {
        "source": "Example NOTAMs printed by the FAA: bodies from Order 7930.2 (chapters 3 to 7, appendix A) and "
                  "whole messages from the AIM keyword table (chapter 5 section 1). U.S. Government works.",
        "examples": examples,
        "aim_examples": aim_examples,
    }, indent=0)
    write(TESTS, "notam_sources.json", {
        "source": "What the generator read beside the FAA tables, for the tests to check its rules against: the "
                  "abbreviations each state AIP lists without the mark for a difference from ICAO Doc 8400, the FAA "
                  "AIP's list, and what the FAA's NOTAM documents write out, print as NOTAM text or write as words.",
        "doc8400": {name_: entries for name_, entries in lists},
        "faa_aip": aip_listed,
        "written_out": written,
        "side_by_side": said,
        "runway_reports": {abbr: list(entry) for abbr, entry in sorted(reports.items())},
        "notam_text": sorted(used),
        "short_words": sorted(short),
        "english": {"lists": [[name_, url] for name_, url, _member in ENGLISH_PAGES],
                    "words": len(english),
                    "also_names": sorted(w for w in english & names if len(w) >= 4),
                    "also_contractions": sorted(w for w in english if len(w) >= 4 and w.upper() in listed)},
    }, indent=0)
    print(f"{len(decoded)} contractions spelled out, {len(undecoded)} left as written, "
          f"{len(keywords)} keywords, {len(aim_examples)} AIM examples")
    return 0


if __name__ == "__main__":
    sys.exit(main())
