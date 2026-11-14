"""A model's plain-language rewrite of a NOTAM body: what the model is given, and
the check that decides whether its answer may be shown.

The check does not judge style. It verifies, token by token, that the answer is
the NOTAM and nothing else:

  - every element of the NOTAM is in the answer, in the NOTAM's order: numbers,
    designators, identifiers and codes exactly as written; a contraction in a
    wording its sources document for it (data/notam_terms.json), and never left
    as written where the literal decode spells it out; a plain word in its own
    spelling;
  - every other word of the answer is one of the joining words in GRAMMAR:
    articles, "is" and "are", "and", three prepositions and six pronouns;
  - every other character is plain punctuation, placed where it cannot change
    how a number reads: no symbol, no sign or decimal point before a number, no
    mark joining two numbers;
  - a full stop the NOTAM writes between two statements is kept (as a full stop
    or a semicolon), so that two statements are never run into one.

So a model can supply articles and joining words and choose between the FAA's
own word forms. It cannot add, drop, reorder or reword anything the NOTAM
writes: such an answer is rejected, and the display keeps the literal decode.
The check does not judge whether a joining word relates two elements as the
NOTAM means. Validity times are never part of the answer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Pattern, Tuple

from .notam_decode import (COMPASS, DAY_RE, DAYS, GLUED_SURFACE_RE, LEVEL_RANGE_RE, NUMBER_RE, PHRASES, PROCEDURE_KEYWORDS,
                           ENGLISH_ALSO, RUNWAY_REPORTS, SUN, SUN_RE, TERMS, TYPE_LABEL, UNIT_RE, UNITS, UNITS_ALONE, _TABLE, after_distance,
                           datum_range, decode, is_article, is_by, is_compass, is_english, is_word, joined_pieces, may_be_article,
                           phrase_at, plural_text, reads_as_word, split_body, split_token, terms_for, token_roles)

PROMPT_VERSION = 3

# Wordings the FAA documents for each contraction the decoder spells out.
ACCEPT: Dict[str, List[str]] = {term: entry["accept"] for term, entry in _TABLE["terms"].items() if term in TERMS}
# The only words a rewrite may add. None is a verb form other than "is" and "are"
# (no tense, no duty), and none can state a time, a span, a direction, a state, a
# negation, a limit or a choice: "to", "from", "by", "for", "in", "on" and "as"
# are accepted only where the NOTAM writes them, and "to" after its DUE.
GRAMMAR = frozenset("a an the is are and of at with that which there it its this".split())
RANGE_WORDS = r"(?:to|through|until|-)"
MAX_LENGTH_FACTOR = 3          # an answer much longer than the NOTAM is not a rewrite of it

# Punctuation an answer may add. Any other character that is not a letter, a digit
# or a space (~ < > = + % # and the like) must be written in the NOTAM.
PLAIN_MARKS = frozenset(".,;:()'\"\u2018\u2019\u201c\u201d-\u2013\u2014/")
DASHES = frozenset("-\u2013\u2014")
QUOTES = frozenset("'\"\u2018\u2019\u201c\u201d")
SENTENCE_MARKS = frozenset(".,;:()'\"")

B_LEFT = r"(?<![A-Za-z0-9])(?<!\d\.)"
B_RIGHT = r"(?![A-Za-z0-9])(?!\.\d)"
B_LEFT_UNIT = r"(?<![A-Za-z0-9.])"
B_LEFT_JOINED = r"(?<![A-Za-z0-9])"
B_RIGHT_JOINED = r"(?![A-Za-z0-9])"


@dataclass
class Item:
    """One element of the NOTAM that the answer must contain."""
    label: str                                   # as written in the NOTAM
    patterns: List[Pattern] = field(default_factory=list)
    says: str = ""                               # what the glossary tells the model
    render: str = ""                             # its literal rendering (the first FAA wording, or as written)
    trail: str = ""                              # punctuation the NOTAM writes directly after it
    word: bool = False                           # plain English: written in any case
    joint: str = ""                              # the mark that joins it to the next piece of its token
    spell: bool = False                          # the literal decode spells it out: a rewrite may not leave it as written
    stop: bool = False                           # the NOTAM ends a statement after it with a full stop

    def __post_init__(self):
        if not self.render:
            self.render = self.label


def _exact(text: str) -> Pattern:
    return re.compile(B_LEFT + re.escape(text) + B_RIGHT)


def _wording(words: str, plural: bool = True) -> Pattern:
    """A wording as a phrase: case, hyphens and spacing free; the last word may
    carry a plural ending (Order 7930.2 para 3-3-1: singular means plural too)."""
    parts = [re.escape(w) for w in re.split(r"[\s-]+", words.strip()) if w]
    ending = r"(?:e?s)?" if plural else ""
    return re.compile(B_LEFT + r"[\s-]*".join(parts) + ending + B_RIGHT, re.IGNORECASE)


def _unit_patterns(number: str, unit: str) -> List[Pattern]:
    singular, plural = UNITS[unit]
    forms = "|".join(sorted({unit, unit.lower(), *(re.sub(r"\s+", r"[\\s-]+", f) for f in (singular, plural))}, key=len, reverse=True))
    return [re.compile(rf"{B_LEFT_UNIT}{re.escape(number)}[\s-]*(?:{forms}){B_RIGHT}", re.IGNORECASE)]


def _level(core: str) -> str:
    """A regex fragment for one end of a level range."""
    m = UNIT_RE.match(core)
    if m:
        return _unit_patterns(m.group(1), m.group(2))[0].pattern
    if core in TERMS:
        return "(?:" + "|".join([_wording(a).pattern for a in ACCEPT[core]] + [_exact(core).pattern]) + ")"
    return _exact(core).pattern


UNSETTLED = frozenset(_TABLE["undecoded"])


def is_free_case(core: str) -> bool:
    """A group of letters the decoder leaves as written whose letter case states
    nothing: an entry of the dictionaries (a word of three letters, a name, a
    word of the phonetic alphabet) or a name of six letters or more. A
    contraction a source lists, and a shorter group that may be an identifier or
    a fix name, keep their capitals."""
    return core.isalpha() and core not in UNSETTLED and (core.lower() in ENGLISH_ALSO or len(core) >= 6)


def _term(core: str, spell: bool = True) -> Item:
    return Item(core, [_exact(core)] + [_wording(a) for a in ACCEPT[core]], says=" or ".join(ACCEPT[core]), render=TERMS[core],
                spell=spell)


def _word(core: str, cause: bool = False) -> Item:
    """A plain word, in any case. DUE before the cause it gives may be written "due to"."""
    written = r"[\s-]*".join(re.escape(part) for part in core.split("-")) + (r"(?:\s+to)?" if cause else "")
    return Item(core, [re.compile(B_LEFT + written + B_RIGHT, re.IGNORECASE)], render=core.lower(), word=True, spell=is_word(core))


def _inside(item: Item, first: bool, last: bool) -> None:
    """An item that is one piece of a token of several: the mark that joins it
    to its neighbour may be a full stop beside a figure ("1.PSN", "WIP.40MIN")."""
    def relaxed(pattern: Pattern) -> Pattern:
        source = pattern.pattern
        if not first:
            for edge in (B_LEFT, B_LEFT_UNIT):
                if source.startswith(edge):
                    source = B_LEFT_JOINED + source[len(edge):]
                    break
        if not last and source.endswith(B_RIGHT):
            source = source[:-len(B_RIGHT)] + B_RIGHT_JOINED
        return re.compile(source, pattern.flags)

    item.patterns = [relaxed(pattern) for pattern in item.patterns]


def _quantity(core: str) -> Item:
    m = UNIT_RE.match(core)
    said = decode(core)
    return Item(core, [_exact(core)] + _unit_patterns(m.group(1), m.group(2)), says=said, render=said, spell=True)


def items_of(text: str, protect: Iterable[str] = ()) -> Tuple[bool, Optional[str], List[Item]]:
    """-> (unverified, keyword, items): the elements of a NOTAM body, in order,
    each with the ways an answer may write it."""
    unverified, keyword, tokens = split_body(text)
    roles = token_roles(tokens, {p.upper() for p in protect if p}, keyword)
    parts = [split_token(t) for t in tokens]
    cores = [core for _lead, core, _trail in parts]
    terms = terms_for(keyword)      # what the literal decode spells out in a NOTAM of this keyword
    items: List[Item] = []
    skip = 0

    def add(item: Item, last: Optional[int] = None) -> None:
        """Append an element; the punctuation the NOTAM writes after its last token is its own."""
        item.trail = parts[i if last is None else last][2]
        items.append(item)

    for i, token in enumerate(tokens):
        if skip:
            skip -= 1
            continue
        core = cores[i]
        nxt = cores[i + 1] if i + 1 < len(cores) else ""
        prev = cores[i - 1] if i else ""
        if not re.search(r"[A-Za-z0-9]", core):
            if core and not set(core) <= SENTENCE_MARKS:      # a free-standing - / & + between two elements
                add(Item(core, [re.compile(re.escape(core))]))
            continue
        if roles[i] == "state" and is_word(core) and (is_word(prev) or is_english(prev) or prev in TERMS or "-" in prev) \
                and (is_word(nxt) or is_english(nxt)):
            # two letters after a comma, between plain words: a state code, or the word ("HYDRO-ELECTRIC, OR COAL")
            add(Item(core, [re.compile(B_LEFT + re.escape(core) + B_RIGHT, re.IGNORECASE)], word=True))
            continue
        if roles[i] in ("designator", "protected", "state"):
            add(Item(core, [_exact(core)]))
            continue
        if roles[i] == "report":
            said = RUNWAY_REPORTS[core]
            add(Item(core, [_exact(core), _wording(said, plural=False)], says=said, render=said, spell=True))
            continue
        phrase = phrase_at(cores, roles, i)
        if phrase:
            count, rendering = phrase
            written = " ".join(cores[i:i + count])
            add(Item(written, [_wording(rendering, plural=False), _wording(written, plural=False)], says=rendering, render=rendering, spell=True),
                last=i + count - 1)
            skip = count - 1
            continue
        m = LEVEL_RANGE_RE.match(core)
        if m:
            low, high = _level(m.group(1)), _level(m.group(2))
            said = decode(core)
            said = said[0].lower() + said[1:]
            add(Item(core, [_exact(core), re.compile(rf"{low}\s*{RANGE_WORDS}\s*(?:the\s+)?{high}", re.IGNORECASE)],
                              says=said, render=said, spell=True))
            continue
        datum = datum_range(core, prev)
        if datum:
            said = f"{TERMS[datum[0]]} to {decode(datum[1])[0].lower() + decode(datum[1])[1:]}"
            wordings = "|".join([_wording(a).pattern for a in ACCEPT[datum[0]]] + [_exact(datum[0]).pattern])
            add(Item(core, [_exact(core), re.compile(rf"(?:{wordings})\s*,?\s*{RANGE_WORDS}\s*(?:the\s+)?{_level(datum[1])}", re.IGNORECASE)],
                     says=said, render=said, spell=True))
            continue
        if UNIT_RE.match(core):
            add(_quantity(core))
            continue
        if core in UNITS and NUMBER_RE.match(prev):
            singular, plural = UNITS[core]
            one = prev == "1" or re.fullmatch(r"\d+/\d+", prev)
            add(Item(core, [_exact(core), _wording(singular, plural=False), _wording(plural, plural=False)],
                              says=f"{singular} or {plural}", render=singular if one else plural, spell=True))
            continue
        if core in UNITS_ALONE and not re.search(r"\d", prev):
            add(Item(core, [_exact(core), _wording(UNITS_ALONE[core], plural=False)], says=UNITS_ALONE[core],
                     render=UNITS_ALONE[core], spell=True))
            continue
        if "/" in core and all(part in DAYS for part in core.split("/")):
            first = len(items)
            for part in core.split("/"):
                add(Item(part, [re.compile(B_LEFT + "(?:" + part + "|" + DAYS[part] + ")" + B_RIGHT, re.IGNORECASE)],
                         says=DAYS[part], render=DAYS[part], spell=True))
            for item in items[first:-1]:
                item.trail = ""
            continue
        m = DAY_RE.match(core)
        if m:
            first = "(?:" + m.group(1) + "|" + DAYS[m.group(1)] + ")"
            if m.group(2):
                second = "(?:" + m.group(2) + "|" + DAYS[m.group(2)] + ")"
                add(Item(core, [_exact(core), re.compile(rf"{B_LEFT}{first}\s*{RANGE_WORDS}\s*{second}{B_RIGHT}", re.IGNORECASE)],
                                  says=f"{DAYS[m.group(1)]} through {DAYS[m.group(2)]}",
                                  render=f"{DAYS[m.group(1)]} through {DAYS[m.group(2)]}", spell=True))
            else:
                add(Item(core, [re.compile(B_LEFT + first + B_RIGHT, re.IGNORECASE)], says=DAYS[m.group(1)],
                                  render=DAYS[m.group(1)], spell=True))
            continue
        m = SUN_RE.match(core)
        if m and (m.group(1) in SUN or m.group(2) in SUN):
            ends = ["(?:" + g + "|" + SUN[g] + ")" if g in SUN else re.escape(g) for g in m.groups()]
            said = f"{SUN.get(m.group(1), m.group(1))} to {SUN.get(m.group(2), m.group(2))}"
            add(Item(core, [_exact(core), re.compile(rf"{B_LEFT}{ends[0]}\s*{RANGE_WORDS}\s*{ends[1]}{B_RIGHT}", re.IGNORECASE)],
                              says=said, render=said, spell=True))
            continue
        if is_compass(core, nxt, after_distance(cores, i)):
            add(Item(core, [_exact(core), _wording(COMPASS[core], plural=False)], says=COMPASS[core], render=COMPASS[core], spell=True))
            continue
        if is_by(core, prev, nxt, cores[i - 2] if i >= 2 else ""):
            add(Item(core, [_exact(core), _wording("by", plural=False), re.compile(r"(?<![A-Za-z0-9])x(?![A-Za-z0-9])")], says="by", render="by"))
            continue
        if is_article(core, nxt):
            add(Item(core, [re.compile(B_LEFT + "a" + B_RIGHT, re.IGNORECASE)], says="a", render="a", word=True))
            continue
        if may_be_article(core, prev, nxt):
            add(Item(core, [re.compile(B_LEFT + "a" + B_RIGHT, re.IGNORECASE)], word=True))
            continue
        if core in TERMS:
            add(_term(core, core in terms))
            continue
        first = len(items)
        plural = plural_text(core, TERMS)
        if plural:
            add(Item(core, [_exact(core), _wording(plural, plural=False)], says=plural, render=plural, spell=True))
            continue
        pieces = [(core, *GLUED_SURFACE_RE.match(core).groups())] if GLUED_SURFACE_RE.match(core) else None
        if pieces:
            add(_term(pieces[0][1], pieces[0][1] in terms))
            add(Item(pieces[0][2], [_exact(pieces[0][2])]))
            items[first].trail = ""
            continue
        pieces = joined_pieces(core, TERMS, keyword)
        if pieces:
            for written, kind, _reading in pieces:
                if kind == "mark":
                    items[-1].joint = written
                elif kind == "term":
                    add(_term(written, written in terms))
                elif kind == "unit":
                    add(_quantity(written))
                elif kind == "word":
                    add(_word(written))
                elif kind == "glued":
                    surface, designator = GLUED_SURFACE_RE.match(written).groups()
                    add(_term(surface, surface in terms))
                    add(Item(designator, [_exact(designator)]))
                elif kind == "as written":
                    add(Item(written, [_exact(written)]))
            read = joined_pieces(core, terms, keyword) is not None      # whether the literal decode reads the token piece by piece
            for n, item in enumerate(items[first:], first):
                _inside(item, n == first, n == len(items) - 1)
                item.spell = item.spell and read
            for item in items[first:-1]:
                item.trail = ""
            continue
        if reads_as_word(cores, roles, i, keyword):
            add(_word(core, cause=core == "DUE" and nxt != "TO" and bool(nxt)))
            continue
        if roles[i] == "text" and is_free_case(core):
            add(Item(core, [re.compile(B_LEFT + re.escape(core) + B_RIGHT, re.IGNORECASE)], word=True))
            continue
        # anything else is a fact, an identifier, a name or a contraction no FAA
        # table settles: it stays exactly as written
        for piece in re.findall(r"[A-Za-z0-9][A-Za-z0-9.'-]*", core) if not re.search(r"\d", core) and "/" in core else [core]:
            add(Item(piece, [_exact(piece)]))
        for item in items[first:-1]:
            item.trail = ""
    for item in items[:-1]:
        item.stop = item.trail.startswith(".") and not item.trail.startswith("..")
    return unverified, keyword, items


class Plan:
    """The elements of one NOTAM body, prepared once, against which any number of
    answers can be checked."""

    def __init__(self, text: str, protect: Iterable[str] = ()):
        self.unverified, self.keyword, self.items = items_of(text, protect)
        self.source = " ".join(split_body(text)[2])
        self.opening = [_wording(TYPE_LABEL[self.keyword])] if self.keyword else []

    def literal(self) -> str:
        """The NOTAM written out item by item in the first FAA wording of each,
        with the full stops that end its statements."""
        return ("Unverified: " if self.unverified else "") + " ".join(item.render + ("." if item.stop else "") for item in self.items)

    def problems(self, answer: str) -> List[str]:
        """What is wrong with an answer; empty when it may be shown."""
        problems: List[str] = []
        answer = " ".join((answer or "").split())
        if not answer:
            return ["the answer is empty"]
        if len(answer) > MAX_LENGTH_FACTOR * len(self.source) + 200:
            problems.append("the answer is far longer than the NOTAM")

        covered: List[Tuple[int, int]] = []
        ended: Optional[Tuple[Item, int]] = None      # the element a full stop of the NOTAM follows, and where it ends in the answer
        at = 0
        if self.unverified:
            m = UNVERIFIED_RE.search(answer)
            if m:
                covered.append(m.span())
                at = m.end()
            else:
                problems.append("the NOTAM is marked (U), unverified, and the answer does not say so")
        for pattern in self.opening:      # the category may open the answer, by its FAA name
            m = pattern.search(answer, at)
            if m and not re.search(r"[A-Za-z0-9]", answer[at:m.start()]):
                covered.append(m.span())
                at = m.end()
                break
        for item in self.items:
            best = None      # the first place the item is written; of two ways of writing it there, the longer
            for pattern in item.patterns:
                m = pattern.search(answer, at)
                if m and (best is None or (m.start(), -m.end()) < (best.start(), -best.end())):
                    best = m
            if best is None:
                elsewhere = any(p.search(answer) for p in item.patterns)
                problems.append(f"{item.label!r} is out of the NOTAM's order" if elsewhere else f"{item.label!r} is missing")
                ended = None
                continue
            # a full stop of the NOTAM separates two statements: the answer keeps them apart
            if ended and not re.search(r"[.;]", answer[ended[1]:best.start()]):
                problems.append(f"the full stop after {ended[0].label!r} is missing")
            ended = (item, best.end()) if item.stop else None
            at = best.end()
            if item.spell and item.render != item.label and best.group(0) == item.label:
                problems.append(f"{item.label!r} is left as written")
            # punctuation the NOTAM writes after this element belongs to it, followed by a space as there
            kept = len(item.trail) if item.trail and answer.startswith(item.trail, at) and not answer[at + len(item.trail):][:1].strip() else 0
            if item.joint and answer.startswith(item.joint, at):      # the mark that joins two pieces of one token, kept as written
                kept = len(item.joint)
            covered.append((best.start(), at + kept))

        def is_covered(start: int, end: int) -> bool:
            return any(a <= start and end <= b for a, b in covered)

        strangers = []
        opening = True       # no word of the answer proper has been seen yet
        for m in TOKEN_RE.finditer(answer):
            first, opening = opening, opening and bool(self.unverified) and UNVERIFIED_RE.fullmatch(m.group(0)) is not None
            if is_covered(*m.span()) or _joins(m.group(0), first):
                continue
            # a token joined by punctuation may be covered piece by piece
            pieces = list(PIECE_RE.finditer(m.group(0)))
            if all(is_covered(m.start() + p.start(), m.start() + p.end()) or _joins(p.group(0), False) for p in pieces):
                continue
            strangers.append(m.group(0))
        if strangers:
            problems.append("not from the NOTAM: " + ", ".join(dict.fromkeys(strangers)))
        marks = [answer[max(0, m.start() - 8):m.end() + 8].strip() for m in MARK_RE.finditer(answer)
                 if not is_covered(*m.span()) and _alters(answer, m.start())]
        if marks:
            problems.append("a mark the NOTAM does not write there: " + ", ".join(repr(x) for x in dict.fromkeys(marks)))
        return problems


def _joins(token: str, first: bool) -> bool:
    """Whether a token of the answer is a joining word. Written in capitals it is
    an identifier or a designator (locator AN, taxiway A), never a joining word.
    A sentence may begin "The" or "An"; "A" is the article only as the answer's
    first word."""
    if token in GRAMMAR:
        return True
    if token.lower() not in GRAMMAR or token != token.capitalize():
        return False
    return len(token) > 1 or first


def _alters(answer: str, i: int) -> bool:
    """Whether the mark at answer[i], which no element of the NOTAM accounts for,
    is a symbol or could change how a number beside it reads."""
    mark = answer[i]
    if mark not in PLAIN_MARKS:
        return True
    before, after = answer[i - 1:i], answer[i + 1:i + 2]
    left, right = answer[:i].rstrip()[-1:], answer[i + 1:].lstrip()[:1]
    if mark in DASHES:
        return right.isdigit()                          # a minus sign, or a range made of two numbers
    if mark in QUOTES:
        return before.isdigit()                         # feet, inches, minutes, seconds
    if mark in "()":
        return False
    if after.isdigit():
        return True                                     # .5  ,500  /4  :30
    return mark in "/:" and left.isdigit() and right.isdigit()      # 1 / 4, 12 : 30


MARK_RE = re.compile(r"[^A-Za-z0-9\s]")
UNVERIFIED_RE = re.compile(r"(?<![A-Za-z])unverified(?![A-Za-z])", re.IGNORECASE)
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.'/-]*[A-Za-z0-9]|[A-Za-z0-9]")
PIECE_RE = re.compile(r"\d+(?:\.\d+)?|[A-Za-z0-9]+")


def check(text: str, answer: str, protect: Iterable[str] = ()) -> List[str]:
    """What is wrong with a model's rewrite of a NOTAM body; empty when it may be
    shown. `text` is the NOTAM's text field, `protect` its own identifiers."""
    return Plan(text, protect).problems(answer)


# ---- what the model is given ---------------------------------------------------------------------

SYSTEM_PROMPT = """You rewrite one FAA NOTAM as plain English for pilots reading a display in a hangar. \
Accuracy is the only goal: an awkward sentence is acceptable, a changed fact is not. Your answer is checked \
by a program and discarded if it breaks any rule below.

You are given the NOTAM text, its type, a GLOSSARY and a KEEP list.

Rules
1. Say only what the NOTAM says. Do not add, infer, explain, summarize, warn or advise.
2. Leave nothing out, not even a short word such as TO, FOR, IN, ON or AND, and keep everything in the \
NOTAM's own order. Where the NOTAM ends a statement with a full stop, end it there too: never run two of its \
statements into one.
3. Every item on the KEEP list is a number, designator, identifier, name or code. Copy each one exactly as \
written, in capitals, and do not expand, translate, convert or round it. Every other word of the NOTAM is an \
English word spelled in capitals: write it in lower case with the same spelling, unless it is part of a name \
(of a place, a facility, an airspace, a fix or a procedure), which stays in capitals.
4. The GLOSSARY gives the FAA's meaning of each contraction in this NOTAM. Write each one out in one of the \
wordings it gives: do not leave it as a contraction, and never use a meaning that is not in the GLOSSARY. A contraction written in \
the singular also means the plural, and one written in the past tense also means the present (FAA Order 7930.2).
5. A word that follows RWY, TWY or APRON is a designator: TWY C is taxiway C.
6. Do not state when the NOTAM is in effect. The display shows its dates and times separately. Days and times \
that are part of the text are on the KEEP list and stay as written.
7. The only words you may add are: a, an, the, is, are, and, of, at, with, that, which, there, it, its, this. \
Any other word must be written in the NOTAM or given by the GLOSSARY. In particular never add a word of time, \
tense, duty, direction or range: write "is closed", never "will be", "has been" or "is to be" closed, and \
never join two items with "to", "from", "for", "by", "in" or "on" unless the NOTAM writes that word there. \
The one exception: DUE before a cause may be written "due to".
8. Punctuation: commas, periods, colons, semicolons and parentheses only. Never write a symbol (~ < > = + % # &), \
and never put a dash, a slash, a point or a quote mark next to a number unless the NOTAM writes it there.
9. Write one line. Start with a capital letter.

Answer with JSON only, in this form: {"plain": "your rewrite"}"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"plain": {"type": "string"}},
    "required": ["plain"],
    "additionalProperties": False,
}

# Worked examples for the model, by NOTAM type: example NOTAMs from FAA Order
# 7930.2 with rewrites that add joining words and plain punctuation only. A test
# checks each against the FAA examples and against check(). The procedure types
# (IAP, ODP, SID, STAR and the like) share one set.
EXAMPLES: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "RWY": (
        ("RWY 16/34 CLSD TO ACFT WINGSPAN MORE THAN 70FT AND TAIL HGT MORE THAN 49FT",
         "Runway 16/34 is closed to aircraft with a wingspan of more than 70 feet and a tail height of more than 49 feet"),
        ("RWY 16 FICON 3/3/3 100 PCT COMPACTED SN 12IN SNOWBANKS",
         "Runway 16 field condition 3/3/3, 100 percent compacted snow, 12 inches snowbanks"),
        ("RWY 13 VASI BEYOND 5DEG LEFT OF RCL UNUSABLE",
         "The runway 13 visual approach slope indicator beyond 5 degrees left of the runway center line is unusable"),
    ),
    "TWY": (
        ("TWY C CLSD", "Taxiway C is closed"),
        ("TWY A3, A4, A5, TWY A BTN TWY A2 AND TWY A3 CLSD",
         "Taxiway A3, A4, A5, and taxiway A between taxiway A2 and taxiway A3 are closed"),
        ("TWY U7 HLDG PSN SIGN FOR RWY 01L/19R LGT U/S",
         "The taxiway U7 holding position sign for runway 01L/19R light is unserviceable"),
    ),
    "APRON": (
        ("APRON NORTH APN E 50FT CLSD", "Apron: the north apron east 50 feet is closed"),
        ("APRON MAIN RAMP 6FT SN PILES ADJ E SIDE", "Apron: the main ramp, 6 feet snow piles adjacent the east side"),
        ("APRON AIR CARGO APN FICON 1IN WET SN", "Apron: the air cargo apron field condition is 1 inch wet snow"),
    ),
    "AD": (
        ("AD AP CLSD EXC PPR MON-FRI 0330-1430",
         "The airport is closed except with prior permission required, Monday through Friday 0330-1430"),
        ("AD AP NOT ATTENDED MAIN TERMINAL CLSD", "The airport is not attended and the main terminal is closed"),
        ("AD AP CARIBOU NEAR MOV AREAS S OF TERMINAL DLY SS-SR",
         "Airport: caribou near the movement areas south of the terminal, daily sunset to sunrise"),
    ),
    "OBST": (
        ("OBST CRANE (ASN 20YY-ACE-5-NRA) 345140N0804506W (1.44NM SW N52) 580FT (195FT AGL) NOT LGTD",
         "A crane (aeronautical study number 20YY-ACE-5-NRA) at 345140N0804506W (1.44 nautical miles south-west of N52), 580 feet "
         "(195 feet above ground level), is not lighted"),
        ("OBST TOWER LGT (ASR UNKNOWN) 420651.07N0875406.27W (12NM N PWK) 1049FT (330FT AGL) U/S",
         "The tower light (ASR unknown) at 420651.07N0875406.27W (12 nautical miles north of PWK), 1049 feet "
         "(330 feet above ground level), is unserviceable"),
        ("OBST MOORED BALLOON WI AN AREA DEFINED AS 1NM RADIUS OF SJT 2430FT (510FT AGL) FLAGGED",
         "A moored balloon within an area defined as a 1 nautical mile radius of SJT, 2430 feet "
         "(510 feet above ground level), is flagged"),
    ),
    "NAV": (
        ("NAV ILS RWY 33 LOC UNUSABLE BEYOND 12NM",
         "The instrument landing system runway 33 localizer is unusable beyond 12 nautical miles"),
        ("NAV DME 010-035 BEYOND 30NM UNUSABLE", "The distance measuring equipment 010-035 beyond 30 nautical miles is unusable"),
        ("NAV ILS RWY 35 GP UNUSABLE SFC-2900FT",
         "The instrument landing system runway 35 glide path is unusable, surface to 2900 feet"),
    ),
    "COM": (
        ("COM REMOTE COM OUTLET 122.6 U/S", "The remote communication outlet 122.6 is unserviceable"),
        ("COM CTAF 122.8 COMMISSIONED", "The common traffic advisory frequency 122.8 is commissioned"),
        ("COM CLR DELIVERY 121.7 NOW 124.125", "Clearance delivery 121.7 is now 124.125"),
    ),
    "SVC": (
        ("SVC ATIS NOT AVBL", "The automatic terminal information service is not available"),
        ("SVC PCL FREQ CHANGED TO 122.8", "The pilot controlled lighting frequency is changed to 122.8"),
        ("SVC PCL ALL UNUSABLE BEYOND 2NM", "Pilot controlled lighting: all is unusable beyond 2 nautical miles"),
    ),
    "AIRSPACE": (
        ("AIRSPACE UAS WI AN AREA DEFINED AS 13NM RADIUS OF SYR SFC-17999FT",
         "An unmanned aircraft system within an area defined as a 13 nautical mile radius of SYR, surface to 17999 feet"),
        ("AIRSPACE PJE WI AN AREA DEFINED AS 5NM RADIUS OF BVT080015",
         "Parachute jumping activities within an area defined as a 5 nautical mile radius of BVT080015"),
        ("AIRSPACE CRYPT NORTH MOA AND REVEILLE MOA ACT 5000FT-16000FT",
         "The CRYPT north military operations area and the REVEILLE military operations area are active, 5000 feet to 16000 feet"),
    ),
    "PROCEDURE": (
        ("SPECIAL JUNEAU INTERNATIONAL, JUNEAU, AK. LDA-2 RWY 8 AMDT 9 PROCEDURE TURN NA.",
         "JUNEAU INTERNATIONAL, JUNEAU, AK. LDA-2 runway 8 amendment 9: the procedure turn is not authorized."),
        ("VFP JOHN F KENNEDY INTL, NEW YORK, NY. PARKWAY VISUAL RWY 13L/R, ORIG...WEATHER MINIMUMS 3000 FOOT "
         "CEILING AND 3 MILES VISIBILITY.",
         "JOHN F KENNEDY INTL, NEW YORK, NY. PARKWAY visual runway 13L/R, original...weather minimums: 3000 foot "
         "ceiling and 3 miles visibility."),
        ("IAP DALLAS LOVE FIELD, DALLAS, TX. ILS OR LOC RWY 31R, AMDT 5... CHART NOTE: SIMULTANEOUS APPROACH "
         "AUTHORIZED WITH RWY 31L. MISSED APPROACH: CLIMB TO 1000 THEN CLIMBING RIGHT TURN TO 5000 ON HEADING 330 AND "
         "CVE R-046 TO FINGR INT/CVE 36.4 DME AND HOLD. CHART LOC RWY 31L. THIS IS ILS OR LOC RWY 31R, AMDT 5A.",
         "DALLAS LOVE FIELD, DALLAS, TX. Instrument landing system or localizer runway 31R, amendment 5... Chart "
         "note: simultaneous approach is authorized with runway 31L. Missed approach: climb to 1000 then climbing "
         "right turn to 5000 on heading 330 and CVE R-046 to FINGR INT/CVE 36.4 distance measuring equipment and hold. Chart localizer "
         "runway 31L. This is instrument landing system or localizer runway 31R, amendment 5A."),
    ),
}
EXAMPLES_SHOWN = 8       # per NOTAM: one of each other type first, then those of its own type


def examples_for(type_code: str) -> List[Tuple[str, str]]:
    """The worked examples shown with a NOTAM of this type: those of its own type
    last, next to the NOTAM, preceded by the first example of each other type."""
    own = type_code if type_code in EXAMPLES else ("PROCEDURE" if type_code in PROCEDURE_KEYWORDS else "")
    mine = list(EXAMPLES.get(own, ()))
    others = [pairs[0] for kind, pairs in EXAMPLES.items() if kind != own]
    return others[:max(0, EXAMPLES_SHOWN - len(mine))] + mine


def glossary(text: str, protect: Iterable[str] = ()) -> Tuple[List[Tuple[str, str]], List[str]]:
    """-> (glossary [(as written, the FAA's wordings)], keep [as written]) for a NOTAM body."""
    _unverified, _keyword, items = items_of(text, protect)
    meanings: List[Tuple[str, str]] = []
    keep: List[str] = []
    for item in items:
        if item.says:
            if (item.label, item.says) not in meanings:
                meanings.append((item.label, item.says))
        elif not item.word and item.label not in keep:
            keep.append(item.label)
    return meanings, keep


def user_message(text: str, type_code: str, protect: Iterable[str] = ()) -> str:
    unverified, _keyword, _items = items_of(text, protect)
    meanings, keep = glossary(text, protect)
    body = " ".join(text.split())
    lines = [f"TYPE: {TYPE_LABEL.get(type_code, 'Other')}", f"NOTAM: {body}", "GLOSSARY:"]
    lines += [f"  {written} = {says}" for written, says in meanings] or ["  (none)"]
    if unverified:
        lines.append("  (U) = unverified: begin your rewrite with the word Unverified")
    lines.append("KEEP: " + (" | ".join(keep) if keep else "(none)"))
    return "\n".join(lines)


@lru_cache(maxsize=None)
def _opening_messages(type_code: str) -> Tuple[Tuple[str, str], ...]:
    """(role, content) of the rules and the worked examples shown with a NOTAM of this type."""
    from .notam_decode import classify   # local: avoids importing at module load for the examples' types

    out = [("system", SYSTEM_PROMPT)]
    for example, answer in examples_for(type_code):
        out.append(("user", user_message(example, classify(example))))
        out.append(("assistant", json.dumps({"plain": answer})))
    return tuple(out)


def messages_for(text: str, type_code: str, protect: Iterable[str] = ()) -> List[Dict[str, str]]:
    """The chat messages for one NOTAM: the rules, the worked examples for its
    type as earlier turns, then the NOTAM with its own glossary."""
    out = [{"role": role, "content": content} for role, content in _opening_messages(type_code)]
    out.append({"role": "user", "content": user_message(text, type_code, protect)})
    return out


ATTEMPTS = 2      # a refused answer goes back to the model once, with what the check found


def retry_messages(messages: List[Dict[str, str]], answer: str, problems: List[str]) -> List[Dict[str, str]]:
    """The conversation after a refused answer: that answer, and what was wrong with it."""
    found = "\n".join(f"- {problem}" for problem in problems)
    return messages + [
        {"role": "assistant", "content": answer},
        {"role": "user", "content": f"That rewrite was discarded:\n{found}\nRewrite the same NOTAM again. Keep every item in the "
                                    "NOTAM's own order, leave nothing out, and add no word other than those rule 7 lists. "
                                    "Answer with JSON only."},
    ]


async def checked_rewrite(ask: Callable[[List[Dict[str, str]]], Awaitable[str]], text: str, type_code: str,
                          protect: Iterable[str] = ()) -> Dict:
    """Ask a model (ask: messages -> its reply) to rewrite one NOTAM and check the
    answer. -> {"text": the rewrite} or {"refused": what was wrong with the last
    answer}, each with "tries": every (answer, problems) in turn."""
    protect = tuple(protect)
    plan = Plan(text, protect)
    messages = messages_for(text, type_code, protect)
    tries: List[Tuple[Optional[str], List[str]]] = []
    for _attempt in range(ATTEMPTS):
        reply = await ask(messages)
        answer = parse_reply(reply)
        problems = ["the reply is not the JSON asked for"] if answer is None else plan.problems(answer)
        tries.append((answer, problems))
        if not problems:
            return {"text": answer, "tries": tries}
        messages = retry_messages(messages, json.dumps({"plain": answer}) if answer is not None else reply[:500], problems)
    return {"refused": tries[-1][1], "tries": tries}


def parse_reply(content: str) -> Optional[str]:
    """The rewrite in a model's reply, or None when the reply is not the JSON
    object asked for. Reasoning blocks and code fences around it are ignored."""
    if not isinstance(content, str):
        return None
    content = re.sub(r"<think>.*?</think>", " ", content, flags=re.S | re.I)
    content = re.sub(r"^\s*```(?:json)?|```\s*$", "", content.strip(), flags=re.I).strip()
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start:end + 1])
    except ValueError:
        return None
    plain = data.get("plain") if isinstance(data, dict) else None
    if not isinstance(plain, str) or not plain.strip():
        return None
    return " ".join(plain.split())
