"""Wrong answers to a NOTAM, built from facts: every way an answer could differ
from the NOTAM that the FAA's own records or the documented failures of language
models on NOTAMs make possible. Each must be refused by the answer check.

The starting point is the NOTAM written out item by item in the FAA's wording
(`Plan.literal()`, which the check accepts). From it, for one NOTAM:

  left out        each element removed in turn (models are documented to drop
                  qualifiers such as "all surfaces")
  left as written each element the literal decode spells out, written as the
                  NOTAM has it instead (a rewrite that only copies the NOTAM)
  case changed    each number, designator, identifier and code with letters, in
                  lower case and with a capital initial
  word replaced   each plain word replaced by another word of the same NOTAM and
                  by words of a different real NOTAM
  statements joined  each full stop between two statements of the NOTAM left out,
                  or replaced by a comma, "and", "is" or a dash
  order changed   each pair of neighbouring elements exchanged (a condition
                  attached to the wrong subject)
  fact replaced   each number, designator and identifier replaced by another one
                  from the same NOTAM and by ones from a different real NOTAM
  unit changed    each unit replaced by every other unit the FAA table has
  other meaning   each contraction given every other meaning Order JO 7340.2
                  records for it (AP: anomalous propagation; LDA: localizer type
                  directional aid)
  opposite        each condition replaced by the opposite Order 7930.2 TBL 3-3-1
                  pairs it with (OPN or CLSD, AVBL or NOT AVBL)
  tense changed   each condition in the -ing form the FAA table lists beside it
                  (closing for CLSD): Order 7930.2 para 3-3-1 decodes a
                  contraction into the past and the present tense only
  added           each wording of the FAA table that is not in the NOTAM, each
                  fact of a different real NOTAM, the kinds of text models are
                  documented to add (times, names, status, advice), and every
                  word that changes when or whether a statement holds: the
                  auxiliary verbs of English other than "is" and "are", and
                  the words of time, span, direction, state, negation, limit
                  and choice; and each joining word in capitals, where it is an
                  identifier or a designator (locator AN, taxiway A)
  mark added      each number given a sign, a decimal point, a comparison, an
                  approximation or a unit mark, and each pair of neighbouring
                  numbers joined into a range, a fraction, a time or one number
"""
import json
import os
import re

from app.notam_decode import DATA_DIR, DAYS, TERMS, UNITS
from app.notam_translate import GRAMMAR, Plan

with open(os.path.join(DATA_DIR, "notam_terms.json")) as _fh:
    TABLE = json.load(_fh)

OPPOSITE = {}
for _a, _b in TABLE["opposites"]:
    OPPOSITE[_a], OPPOSITE[_b] = _b, _a

# Text language models are documented to add to a NOTAM (Dani et al., ACL Findings
# 2025, table 6: times, airport names, status wording), and words of the NOTAM
# phrase table that reverse or limit a statement.
ADDED_TEXT = (
    "not", "no", "only", "all", "except", "until further notice", "unusable", "unsafe", "now", "near",
    "is underway", "has started", "is complete", "expected completion", "Savoonga Airport", "use caution",
    "pilots should", "0500Z", "12:22 AM", "December 9, 2021", "UTC", "local time", "tomorrow", "daily",
)
# The auxiliary verbs of English, a closed class, in every form except "is" and
# "are": each states a tense, a duty, a permission or a possibility.
AUXILIARIES = (
    "be", "am", "was", "were", "been", "being", "have", "has", "had", "having", "do", "does", "did", "will",
    "would", "shall", "should", "can", "could", "may", "might", "must", "ought", "need", "dare", "used",
    "is to be", "are to be", "has to be", "have to be", "has been", "have been", "will be", "is being",
)
# Words that state a time, a span, a direction, a state, a negation, a limit or a choice.
RELATING_WORDS = (
    "to", "from", "by", "for", "in", "on", "as", "off", "out", "until", "till", "since", "before", "after",
    "during", "through", "throughout", "between", "within", "beyond", "above", "below", "over", "under", "up",
    "down", "per", "plus", "minus", "less", "more", "than", "or", "nor", "but", "if", "unless", "when", "while",
    "then", "also", "both", "each", "every", "any", "some", "none", "never", "always", "again", "still", "yet",
    "soon", "already", "once", "into", "onto", "toward", "via", "about", "around", "near", "without", "due",
)


# Marks that change how a number reads: before it (sign, decimal point,
# comparison, approximation, count, currency, ratio), after it (percent, feet and
# inches or minutes and seconds, degrees, "or more", a multiplier), and between two
# numbers (range, fraction, time, decimal, thousands, product, sum).
MARKS_BEFORE = (".", "-", "\u2013", "\u2212", "+", "<", ">", "\u2264", "\u2265", "=", "~", "\u2248", "\u00b1", "#", "$", "/", "*", "\u00d7", "^")
MARKS_AFTER = ("%", "'", '"', "\u2032", "\u2033", "\u00b0", "+", "K", "M", "x", "th")
MARKS_BETWEEN = ("-", " - ", "\u2013", " \u2013 ", "/", " / ", ":", " : ", ".", ",", "x", " x ", "\u00d7", "+", " + ", "=", "~")


def render_words(expression: str) -> str:
    """A table expression such as "NOT AVBL" in the FAA's wording."""
    return " ".join(TERMS.get(w, w.lower()) for w in expression.split())


def is_fact(item) -> bool:
    """A number, designator, identifier or code: written one way only."""
    return not item.says and not item.word


def other_meanings(label: str):
    """Meanings Order JO 7340.2 records for a contraction that are not wordings
    the FAA's NOTAM tables give it."""
    entry = TABLE["terms"].get(label)
    if not entry:
        return []
    accepted = {re.sub(r"[^a-z]", "", a.lower()) for a in entry["accept"]}
    out = []
    for _usage, definition in entry.get("jo_7340_2", []):
        plain = re.sub(r"\([^()]*\)", " ", definition)
        for alternative in re.split(r"\s+or\s+|,\s*(?:or\s+)?|;", plain):
            alternative = " ".join(alternative.split()).strip(" .")
            key = re.sub(r"[^a-z]", "", alternative.lower())
            if len(alternative) >= 3 and key not in accepted and not any(key.startswith(a) or a.startswith(key) for a in accepted):
                out.append(alternative)
    return out


def wrong_answers(plan: Plan, foreign: Plan):
    """Yield (kind, detail, answer) for every wrong answer to `plan`'s NOTAM.
    `foreign` is a different NOTAM, the source of facts that do not belong."""
    items = plan.items
    renders = [it.render + ("." if it.stop else "") for it in items]      # with the full stops that end the NOTAM's statements
    prefix = "Unverified: " if plan.unverified else ""

    def text(parts):
        return prefix + " ".join(p for p in parts if p)

    def swap(i, new):
        return text(renders[:i] + [new] + renders[i + 1:])

    def bare(value):
        return re.sub(r"[^A-Za-z0-9]", "", value)

    own_facts = {it.render for it in items if is_fact(it)}
    foreign_facts = [it.render for it in foreign.items if is_fact(it) and it.render not in own_facts and bare(it.render)]
    own_numbers = set(re.findall(r"\d+(?:\.\d+)?", " ".join(renders)))
    foreign_numbers = [n for n in dict.fromkeys(re.findall(r"\d+(?:\.\d+)?", foreign.literal())) if n not in own_numbers]

    own_words = {it.render for it in items if it.word and not it.says}
    foreign_words = [it.render for it in foreign.items if it.word and not it.says and it.render.lower() not in {w.lower() for w in own_words}]

    for i, it in enumerate(items):
        yield "left out", it.label, text(renders[:i] + renders[i + 1:])
        if it.spell and it.render != it.label:
            yield "left as written", it.label, swap(i, it.label + ("." if it.stop else ""))
        if it.stop:
            for joined in ("", ",", " and", " is", " -"):
                yield "statements joined", f"{it.label}{joined or ' '}{items[i + 1].label}", swap(i, it.render + joined)
        if is_fact(it) and re.search(r"[A-Z]", it.render):
            for other in dict.fromkeys((it.render.lower(), it.render.capitalize())):
                if other != it.render:
                    yield "case changed", f"{it.label} -> {other}", swap(i, other)
        if it.word and not it.says:
            for other in sorted(w for w in own_words if w.lower() != it.render.lower())[:3] + foreign_words[:3]:
                yield "word replaced", f"{it.label} -> {other}", swap(i, other)
        if i + 1 < len(items) and renders[i].lower() != renders[i + 1].lower():
            yield "order changed", f"{it.label} / {items[i + 1].label}", text(renders[:i] + [renders[i + 1], renders[i]] + renders[i + 2:])
        if is_fact(it):
            for other in sorted(own_facts - {it.render}) + foreign_facts[:6]:
                if bare(other) != bare(it.render):      # the same fact with other punctuation is not another fact
                    yield "fact replaced", f"{it.label} -> {other}", swap(i, other)
        elif re.search(r"\d", it.render):
            for number in dict.fromkeys(re.findall(r"\d+(?:\.\d+)?", it.render)):
                for other in sorted(own_numbers - {number})[:4] + foreign_numbers[:4]:
                    yield "fact replaced", f"{number} -> {other} in {it.label}", swap(i, re.sub(rf"(?<![\d.]){re.escape(number)}(?![\d.])", other, it.render, count=1))
        for unit, (singular, plural) in UNITS.items():
            m = re.fullmatch(rf"(.*\d)\s*(?:{re.escape(singular)}|{re.escape(plural)})", it.render)
            if m:
                for other, (_s, other_plural) in UNITS.items():
                    if other != unit:
                        yield "unit changed", f"{it.label}: {plural} -> {other_plural}", swap(i, f"{m.group(1)} {other_plural}")
        for meaning in other_meanings(it.label):
            yield "other meaning", f"{it.label} = {meaning}", swap(i, meaning)
        if it.label in OPPOSITE:
            yield "opposite", f"{it.label} -> {OPPOSITE[it.label]}", swap(i, render_words(OPPOSITE[it.label]))
        for form in TABLE["terms"].get(it.label, {}).get("progressive", []):
            yield "tense changed", f"{it.label} = {form}", swap(i, form)
        if re.fullmatch(r"\d+(?:\.\d+)?", it.render):
            for mark in MARKS_BEFORE:
                yield "mark added", f"{mark}{it.label}", swap(i, mark + it.render)
            for mark in MARKS_AFTER:
                yield "mark added", f"{it.label}{mark}", swap(i, it.render + mark)
            if i + 1 < len(items) and re.fullmatch(r"\d+(?:\.\d+)?", renders[i + 1]):
                for mark in MARKS_BETWEEN:
                    if mark == it.joint:
                        continue      # the mark the NOTAM itself writes between these two numbers ("21-06")
                    yield "mark added", f"{it.label}{mark}{items[i + 1].label}", text(renders[:i] + [it.render + mark + renders[i + 1]] + renders[i + 2:])
        elif re.match(r"\d", it.render):
            for mark in MARKS_BEFORE:
                yield "mark added", f"{mark}{it.label}", swap(i, mark + it.render)

    own_wordings = {a.lower() for it in items for a in ([it.render] + TABLE["terms"].get(it.label, {}).get("accept", []))}
    # an FAA wording that is itself a joining word ("at", "with") may be added; nothing else may
    wordings = [t for t in TERMS.values() if not all(w.lower() in GRAMMAR for w in t.split())]
    # a joining word in capitals is an identifier or a designator (locator AN, taxiway A)
    capitals = [w.upper() for w in sorted(GRAMMAR)]
    additions = [t for t in dict.fromkeys(wordings + list(DAYS.values()) + list(ADDED_TEXT) + list(AUXILIARIES) + list(RELATING_WORDS)
                                          + capitals + foreign_facts[:8] + foreign_numbers[:4])
                 if t.lower() not in own_wordings and t not in own_facts]
    whole = renders
    # inside the answer: after its first word, so that the addition is never the answer's own first word
    inside = next((k + 1 for k, r in enumerate(whole) if bare(r)), len(whole))
    def completes(k, addition):
        """Whether the addition after element k only writes out another wording its sources list,
        for that element ("approach control" + "service" is "approach control service") or for
        the next ("to" + "exercise" is "to exercise")."""
        def wordings(n):
            return {a.lower() for a in TABLE["terms"].get(items[n].label, {}).get("accept", [])} if 0 <= n < len(items) else set()
        if addition == "to" and 0 <= k < len(items) and items[k].label == "DUE":
            return True      # DUE before a cause may be written "due to"
        return f"{whole[k]} {addition}".lower() in wordings(k) or \
            (k + 1 < len(whole) and f"{addition} {whole[k + 1]}".lower() in wordings(k + 1))

    for addition in additions:
        if not completes(len(whole) - 1, addition):
            yield "added", addition, text(whole + [addition])
        if len(whole) > inside + 1 and not completes(inside - 1, addition):
            yield "added", addition, text(whole[:inside] + [addition] + whole[inside:])
