#!/usr/bin/env python3
"""Run the NOTAM decoder over a file of real NOTAM texts and report every way it
could have changed their meaning. The texts are not part of the repository:
give a file with one NOTAM per line, or a CSV whose first column is the text
(";" separated, as the public DEEL-AI/NOTAM dataset is).

  python3 backend/tools/check_notam_text.py FILE [--show N] [--answers [--jobs N]]

Checks, each over every text:
  - the decoder does not raise;
  - the numbers in the rendering are exactly the numbers in the NOTAM;
  - every runway and taxiway designator survives as written;
  - every word of the rendering is the NOTAM's own or comes from the FAA table;
  - the check applied to a model's answer accepts the literal rendering (a
    faithful answer is never refused).
It also counts each substitution made, so the commonest can be read for sense.

With --answers it also builds, for every text, every wrong answer described in
backend/tests/notam_cases.py (an element left out, two exchanged, a fact or a
unit replaced, another documented meaning, the opposite condition, something
added) and reports any that the model-answer check accepts. That is hundreds of
answers per text: --jobs N shares the work between N processes, and progress
goes to standard error.
Exit status 1 when any check fails.
"""
from __future__ import annotations

import collections
import csv
import multiprocessing
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
from app.notam_decode import (COMPASS, DAYS, FORMS, PHRASES, RUNWAY_REPORTS, SUN, SURFACE_PLURALS, TERMS, UNITS, body_of,  # noqa: E402
                              decode, split_body, split_token, token_roles)
from app.notam_translate import Plan, check  # noqa: E402

NUMBER = re.compile(r"\d+(?:\.\d+)?|\.\d+")
DESIGNATOR = re.compile(r"(?:RWY (\d{1,2}[LRC]?(?:/\d{1,2}[LRC]?)*)|TWY ([A-Z]{1,2}\d{0,2}))(?=[ ,]|$)")


def texts_of(path: str) -> list:
    with open(path, newline="") as fh:
        if path.endswith(".csv"):
            rows = list(csv.reader(fh, delimiter=";"))
            return [" ".join(";".join(r[:-1]).split()) for r in rows[1:] if len(r) >= 2]
        return [" ".join(line.split()) for line in fh if line.strip()]


def allowed_words() -> set:
    out = {"through", "to", "by", "unverified"}
    forms = [form for forms in FORMS.values() for form in forms]
    for rendering in (list(TERMS.values()) + forms + list(PHRASES.values()) + list(DAYS.values()) + list(COMPASS.values())
                      + list(SUN.values()) + list(RUNWAY_REPORTS.values())):
        out.update(re.findall(r"[a-z]+", rendering.lower()))
    for forms in UNITS.values():
        for form in forms:
            out.update(form.split())
    out.update(TERMS[surface[:-1]] + "s" for surface in SURFACE_PLURALS)      # RWYS, TWYS
    return out


_TEXTS: list = []
_PLANS: list = []


def wrong_answers_for(n: int) -> tuple:
    """-> (wrong answers built by kind, failures) for text n."""
    from notam_cases import wrong_answers

    plan = _PLANS[n]
    foreign = next((p for p in _PLANS[n + 1:] + _PLANS[:n] if p is not None and p is not plan), None)
    built, found = collections.Counter(), []
    if plan is None or foreign is None:
        return built, found
    literal = plan.literal()
    if plan.problems(literal):
        found.append(("the item-by-item rendering is refused", (_TEXTS[n], f"{plan.problems(literal)} <= {literal}")))
    for kind, detail, answer in wrong_answers(plan, foreign):
        if answer == literal:
            continue
        built[kind] += 1
        if not plan.problems(answer):
            found.append((f"wrong answer accepted ({kind})", (_TEXTS[n], f"{detail} => {answer}")))
    return built, found


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    show = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 5
    texts = texts_of(sys.argv[1])
    allowed = allowed_words()
    failures = collections.defaultdict(list)
    substitutions = collections.Counter()
    changed = 0
    for text in texts:
        try:
            out = decode(text)
        except Exception as exc:  # noqa: BLE001
            failures["decoder raised"].append((text, repr(exc)))
            continue
        body = body_of(text)
        changed += out.lower() != " ".join(split_body(text)[2]).lower()
        if sorted(NUMBER.findall(body)) != sorted(NUMBER.findall(out)):
            failures["number added, dropped or changed"].append((text, out))
        for d in (a or b for a, b in DESIGNATOR.findall(body)):
            if not re.search(rf"(?<![A-Za-z0-9]){re.escape(d)}(?![A-Za-z0-9])", out):
                failures["designator not kept"].append((text, f"{d!r} missing from: {out}"))
        source = set(re.findall(r"[a-z]+", body.lower()))
        strangers = [w for w in re.findall(r"[A-Za-z]+", out) if w.lower() not in source and w.lower() not in allowed]
        if strangers:
            failures["word from neither the NOTAM nor the FAA table"].append((text, strangers))
        refused = check(text, out)
        if refused:
            failures["the literal decode is refused by the model-answer check"].append((text, f"{refused} <= {out}"))
        _u, keyword, tokens = split_body(text)
        roles = token_roles(tokens, (), keyword)
        for token, role in zip(tokens, roles):
            core = split_token(token)[1]
            if role == "text" and core in TERMS:
                substitutions[f"{core} -> {TERMS[core]}"] += 1
    built = collections.Counter()
    if "--answers" in sys.argv:
        jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else 1
        _TEXTS.extend(texts)
        for text in texts:
            try:
                _PLANS.append(Plan(text))
            except Exception as exc:  # noqa: BLE001
                failures["the answer check raised"].append((text, repr(exc)))
                _PLANS.append(None)
        with multiprocessing.get_context("fork").Pool(jobs) as pool:      # the workers inherit the prepared texts
            for done, (counts, found) in enumerate(pool.imap(wrong_answers_for, range(len(texts)), chunksize=4), 1):
                built.update(counts)
                for name, case in found:
                    failures[name].append(case)
                if done % 100 == 0 or done == len(texts):
                    print(f"  wrong answers: {done} / {len(texts)} texts, {sum(built.values())} built, "
                          f"{sum(len(v) for k, v in failures.items() if k.startswith('wrong answer'))} accepted",
                          file=sys.stderr, flush=True)
    print(f"{len(texts)} NOTAM texts; {changed} changed by the decoder")
    if built:
        print(f"{sum(built.values())} wrong answers built: " + ", ".join(f"{k} {v}" for k, v in built.most_common()))
    names = ["decoder raised", "number added, dropped or changed", "designator not kept",
             "word from neither the NOTAM nor the FAA table", "the literal decode is refused by the model-answer check"]
    names += sorted(k for k in failures if k not in names)
    for name in names:
        cases = failures.get(name, [])
        print(f"  {name}: {len(cases)}")
        for text, detail in cases[:show]:
            print(f"      {text[:150]}\n        => {str(detail)[:170]}")
    print("commonest substitutions:")
    for item, n in substitutions.most_common(60):
        print(f"  {n:5d}  {item}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
