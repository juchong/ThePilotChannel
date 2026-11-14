"""The model rewrite of NOTAMs: the answer check, what the model is given, the
endpoint client, and the pipeline around them. No test needs a model: the check
is exercised with the FAA's own example NOTAMs and with every wrong answer
notam_cases.py can build from the FAA's records, the client with a stub
transport and a stand-in HTTP server, the pipeline with a stub model."""
import asyncio
import collections
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from notam_cases import AUXILIARIES, RELATING_WORDS, wrong_answers
from support import AUTH, base_cfg, put, wait_until
from test_notams import feature, shown, stub_faa

from app import llm
from app import main as main_mod
from app import notam_translate as nt
from app import notams
from app.notam_decode import PROCEDURE_KEYWORDS, TABLE_TERMS, TERMS, TYPE_SEQUENCE, WORDS, body_of, classify, decode, split_body


def faa_examples():
    with open(os.path.join(os.path.dirname(__file__), "data", "faa_examples.json")) as fh:
        data = json.load(fh)
    return data["examples"] + data["aim_examples"]


def faa_samples():
    with open(os.path.join(os.path.dirname(__file__), "data", "nms_samples.json")) as fh:
        return [notams.normalize(f) for f in json.load(fh)["sample_initial_load"]]


# ---- the answer check ------------------------------------------------------------------------------

def test_the_faa_wording_of_every_faa_example_is_accepted():
    examples = faa_examples()
    assert len(examples) > 350
    refused = []
    for e in examples:
        plan = nt.Plan(e)
        refused += [(e, answer, p) for answer in (decode(e), plan.literal()) for p in [plan.problems(answer)] if p]
    assert refused == []


def test_no_wrong_answer_to_any_faa_example_is_accepted():
    examples = faa_examples()
    plans = [nt.Plan(e) for e in examples]
    built, accepted = collections.Counter(), []
    for n, plan in enumerate(plans):
        literal = plan.literal()
        for kind, detail, answer in wrong_answers(plan, plans[(n + 1) % len(plans)]):
            if answer == literal:
                continue
            built[kind] += 1
            if not plan.problems(answer):
                accepted.append((kind, detail, examples[n], answer))
    assert accepted == []
    # every kind of wrong answer was really built, in numbers
    assert built["left out"] > 2000 and built["order changed"] > 2000 and built["fact replaced"] > 4000
    assert built["unit changed"] > 1000 and built["other meaning"] > 100 and built["opposite"] > 40 and built["added"] > 200000
    assert built["mark added"] > 10000 and built["tense changed"] > 40
    assert built["left as written"] > 2000 and built["case changed"] > 400 and built["word replaced"] > 1000
    assert built["statements joined"] > 100


def test_the_faa_sample_notams_pass_as_written_and_fail_every_wrong_answer():
    a, b = faa_samples()
    for record, other in ((a, b), (b, a)):
        protect = (record["location"], record["icao_location"], record["accountability"])
        plan, foreign = nt.Plan(record["text"], protect), nt.Plan(other["text"])
        assert plan.problems(record["plain"]["text"]) == []
        wrong = [(k, d) for k, d, answer in wrong_answers(plan, foreign) if answer != plan.literal() and not plan.problems(answer)]
        assert wrong == []


def test_an_answer_may_add_grammar_words_and_use_any_wording_the_faa_gives():
    # ACT: active, activated or activity (Order JO 7340.2); LGT: light or lighting
    assert nt.check("AD AP BIRD ACT INCREASED NW SIDE", "Airport bird activity increased at the north-west side") == []
    assert nt.check("AD AP BIRD ACT INCREASED NW SIDE", "The airport bird active increased north-west side") == []
    assert nt.check("TWY B CL LGT U/S", "The taxiway B centerline lights are unserviceable") == []
    assert nt.check("TWY B CL LGT U/S", "Taxiway B centre line lighting is unserviceable") == []
    # a phrase the FAA writes in words: in those words
    assert nt.check("RWY 18 RWY END ID LGT U/S", "The runway 18 runway end identifier light is unserviceable") == []
    assert nt.check("(U) RWY 07/25 UNSAFE", "Unverified: runway 07/25 is unsafe") == []
    # a wording the FAA does not give, however reasonable, is not accepted
    assert nt.check("AD AP BIRD ACT INCREASED NW SIDE", "Airport bird action increased at the north-west side") != []
    assert nt.check("RWY 18 RWY END ID LGT U/S", "Runway 18 runway end identification light is unserviceable") != []
    assert nt.check("TWY B CL LGT U/S", "Taxiway B centerline lights are out of service") != []
    assert nt.check("(U) RWY 07/25 UNSAFE", "Runway 07/25 is unsafe") != []


def test_a_rewrite_never_leaves_as_written_what_the_literal_decode_spells_out():
    assert nt.check("SVC ATIS NOT AVBL", "The ATIS is not available") == ["'ATIS' is left as written"]
    # the keyword opens a rewrite by its FAA name only
    assert nt.check("SVC ATIS NOT AVBL", "Services: the automatic terminal information service is not available") == []
    assert nt.check("SVC ATIS NOT AVBL", "SVC automatic terminal information service is not available") != []
    assert nt.check("RWY 18 RWY END ID LGT U/S", "Runway 18 RWY END ID LGT is unserviceable") != []
    assert nt.check("NAV VOR 010-035 BEYOND 35NM SFC-2000FT UNUSABLE",
                    "The VHF omni-directional radio range 010-035 beyond 35NM, surface to 2000 feet, is unusable") != []
    assert nt.check("AD AP CLSD MON-FRI DLY SS-SR", "The airport is closed MON-FRI daily sunset to sunrise") != []
    # the NOTAM copied, in whole or with its body in capitals, is no rewrite: so for every FAA example
    for e in faa_examples():
        _unverified, _keyword, tokens = split_body(body_of(e))
        copy = " ".join(tokens)
        assert nt.Plan(e).problems(("Unverified: " if _unverified else "") + copy) != [], e
        assert nt.Plan(e).problems(body_of(e)) != [], e
    # a plain word of the FAA's prose left in capitals is the NOTAM copied; a word that may be a name keeps its capitals
    assert nt.check("RWY 07/25 UNSAFE ABANDONED VEHICLE", "Runway 07/25 is UNSAFE, abandoned vehicle") == ["'UNSAFE' is left as written"]
    assert nt.check("AIRSPACE DRUM MOA ACT 500FT AGL-4999FT",
                    "The DRUM military operations area is active, 500 feet above ground level to 4999 feet") == []
    # a procedure NOTAM keeps the contractions of its charts in the literal decode, and a rewrite may keep them too
    heading = "IAP SOMEWHERE MUNI, SOMEWHERE, WA. ILS OR LOC RWY 16, AMDT 1..."
    opening = "SOMEWHERE MUNI, SOMEWHERE, WA. Instrument landing system or localizer runway 16, amendment 1..."
    charted = [term for term in sorted(TERMS) if term not in TABLE_TERMS and re.fullmatch(r"[A-Z]{4,}", term)]
    assert len(charted) > 100
    for term in charted:
        assert nt.check(f"{heading} {term} 1200 NA.", f"{opening} {term} 1200 is not authorized.") == [], term
        assert nt.check(f"{heading} {term} 1200 NA.", f"{opening} {TERMS[term]} 1200 is not authorized.") == [], term
        assert nt.check(f"SVC {term} 1200 U/S", f"The {term} 1200 is unserviceable") == [f"{term!r} is left as written"], term


def test_a_token_of_joined_pieces_is_checked_piece_by_piece():
    notam = "NAV ILS RWY 27 ON TEST DO NOT USE RMK/1.FALSE INDICATIONS POSSIBLE 2.VMC ONLY"
    assert nt.check(notam, decode(notam)) == []
    good = ("The instrument landing system runway 27 is on test, do not use. Remark: 1. false indications possible, "
            "2. visual meteorological conditions only")
    assert nt.check(notam, good) == []
    for wrong in (good.replace("1. false", "false"), good.replace("1. false", "2. false"), good.replace("2. visual", "3. visual"),
                  good.replace("Remark: ", ""), good.replace("false", "true"), good.replace("1. false", "1.5 false"),
                  good.replace("visual meteorological conditions", "VMC")):
        assert nt.check(notam, wrong) != [], wrong
    notam = "NAV VORTAC 116.2/CH 109 COMMISSIONED"
    assert nt.check(notam, "The VOR and TACAN 116.2/channel 109 is commissioned") == []
    assert nt.check(notam, "The VOR and TACAN 116.2, channel 109, is commissioned") == []
    for wrong in ("The VOR and TACAN 116.3/channel 109 is commissioned", "The VOR and TACAN 116/channel 109 is commissioned",
                  "The VOR and TACAN 116.2/109 is commissioned", "The VOR and TACAN 116.2/channel 2/109 is commissioned"):
        assert nt.check(notam, wrong) != [], wrong
    # every FAA example whose tokens are joined by marks: the literal decode passes
    for e in faa_examples():
        assert nt.check(e, decode(e)) == [], e
    assert nt.check("AD AP ARRIVALS FOR RWY01R CLSD", "The airport arrivals for runway 01R is closed") == []
    # figures the NOTAM joins by a mark stay joined
    joined = "NAV GPS (YPG_AZ GPS 21-06)(INCLUDING WAAS) NOT AVBL"
    assert nt.check(joined, decode(joined)) == []
    for wrong in (decode(joined).replace("21-06", "21, 06"), decode(joined).replace("21-06", "21 06"), decode(joined).replace("21-06", "21/06"),
                  decode(joined).replace("21-06", "21-07")):
        assert nt.check(joined, wrong) != [], wrong
    closed = "AD MOV AREA RWY 01L/19R-CLSD"
    assert nt.check(closed, "The movement area runway 01L/19R is closed") == []
    assert nt.check(closed, "The movement area runway 01L, 19R is closed") != [] and nt.check(closed, "The movement area runway 01L/19L is closed") != []
    # a full stop that joins a word to a figure
    stuck = "OBST CRANE 148 FT AGL.430 MSL NOT LGTD"
    assert decode(stuck) == "Crane 148 feet above ground level.430 mean sea level not lighted" and nt.check(stuck, decode(stuck)) == []
    assert nt.check(stuck, "A crane, 148 feet above ground level, 430 mean sea level, is not lighted") == []
    assert nt.check(stuck, "A crane, 148 feet above ground level.431 mean sea level, is not lighted") != []
    # in a procedure NOTAM a joined token with a contraction of the charts stays whole in the literal decode, which passes
    charted = "SID SOMEWHERE MUNI, SOMEWHERE, WA. ANYWAY ONE DEPARTURE... CHANGED TO WAYPOINT WH019 REF:AIP AD 2.24"
    assert "REF:AIP" in decode(charted) and nt.check(charted, decode(charted)) == []
    assert nt.check("AD AP ARRIVALS FOR RWY01R CLSD", "The airport arrivals for runway 01L is closed") != []


def test_a_word_or_a_name_may_be_written_in_any_case_and_a_code_only_as_it_is_written():
    for notam, answer in (
        ("SVC WIND SYSTEM EAGLECREST NOT AVBL", "The wind system at Eaglecrest is not available"),      # a name of six letters or more
        ("AD AP ELK NEAR MOVEMENT AREAS", "Airport: elk near the movement areas"),                      # a dictionary word of three letters
        ("AD AP ABN NOT STD GREEN ONLY", "The airport aerodrome beacon is not standard, green only"),   # a word the lists also enter as a name
        ("APRON STAND BRAVO CLSD", "Apron: stand Bravo is closed"),                                     # a word of the phonetic alphabet
        ("RWY 07/25 UNSAFE ABANDONED VEHICLE", "Runway 07/25 is unsafe, Abandoned vehicle"),
    ):
        assert nt.check(notam, answer) == [], answer
    for notam, answer in (
        ("AIRSPACE UAS WI AN AREA DEFINED AS 13NM RADIUS OF SYR SFC-17999FT",
         "An unmanned aircraft system within an area defined as a 13 nautical mile radius of Syr, surface to 17999 feet"),
        ("SID DALLAS/FORT WORTH INTL, DALLAS, TX. PODDE THREE DEPARTURE... DO NOT EXCEED 240KT UNTIL LARRN.",
         "DALLAS/FORT WORTH INTL, DALLAS, TX. PODDE three departure... do not exceed 240 knots until Larrn."),      # a fix name
        ("SID DALLAS/FORT WORTH INTL, DALLAS, TX. PODDE THREE DEPARTURE... DO NOT EXCEED 240KT UNTIL LARRN.",
         "Dallas/Fort Worth Intl, Dallas, TX. PODDE three departure... do not exceed 240 knots until LARRN."),      # the heading's names
        ("SVC WX REP U/S", "The weather rep is unserviceable"),                                         # a contraction the sources do not settle
        ("COM ARINC CPDLC NOT AVBL", "Arinc controller-pilot data link communications are not available"),
        ("SVC VOLMET BCST U/S", "The Volmet broadcast is unserviceable"),                               # also one of six letters or more
        ("SVC SNOWTAM BCST U/S", "The snowtam broadcast is unserviceable"),
        ("TWY C CLSD", "Taxiway c is closed"),                                                         # a designator
        ("AD AP MOBILE JET A FUEL NOT AVBL", "The airport mobile jet a fuel is not available"),
        ("NAV BZB ILS RWY 22R U/S", "The Bzb instrument landing system runway 22R is unserviceable"),   # an identifier
        ("OBST TOWER 345140N0804506W (12NM N PWK) 1049FT", "A tower at 345140n0804506w (12 nautical miles north of PWK), 1049 feet"),
    ):
        assert nt.check(notam, answer) != [], answer
    # and the same letters: never another word
    assert nt.check("AD AP ELK NEAR MOVEMENT AREAS", "Airport: elks near the movement areas") != []
    assert nt.check("SVC WIND SYSTEM EAGLECREST NOT AVBL", "The wind system at Eagle Crest is not available") != []


def test_the_letter_a_and_two_letters_after_a_comma_may_be_lower_case_only_between_plain_words():
    notam = ("SECURITY ...SPECIAL NOTICE... THIS IS A RESTATEMENT OF A PREVIOUSLY ISSUED ADVISORY NOTICE. PILOTS ARE STRONGLY ADVISED TO "
             "AVOID SUCH SITES AS POWER PLANTS (NUCLEAR, HYDRO-ELECTRIC, OR COAL), DAMS.")
    good = ("Security: ...special notice... This is a restatement of a previously issued advisory notice. Pilots are strongly advised to "
            "avoid such sites as power plants (nuclear, hydro-electric, or coal), dams.")
    assert nt.check(notam, good) == [] and nt.check(notam, decode(notam)) == []
    assert nt.check(notam, good.replace("is a restatement", "is restatement")) != []
    assert nt.check(notam, good.replace(", or coal", ", coal")) != [] and nt.check(notam, good.replace(", or coal", ", and coal")) != []
    # the letter A after a word that takes a label, or before anything but a plain word, is a designator
    for text, answer in (
        ("AD AP MOBILE JET A FUEL NOT AVBL", "The airport mobile jet a fuel is not available"),
        ("AD AP ARFF INDEX A NOT AVBL", "The airport airport rescue and fire fighting index a is not available"),
        ("APRON RAMP E OF A CLSD", "The apron ramp east of a is closed"),
        ("TWY B BTN A AND C CLSD", "Taxiway B between a and C is closed"),
        ("APRON RAMP E OF A AND B CLSD", "The apron ramp east of a and B is closed"),      # one of a list of designators
        ("TWY A CLSD", "Taxiway a is closed"),
    ):
        assert nt.check(text, answer) != [], answer
    # two letters after a comma are a state code after a name, in a heading, and before anything but a plain word
    for text, answer in (
        ("IAP PORTLAND INTL, PORTLAND, OR. ILS RWY 10R NA", "PORTLAND INTL, PORTLAND, or. Instrument landing system runway 10R is not authorized"),
        ("AIRSPACE UAS WI AN AREA DEFINED AS 5NM RADIUS OF SALEM, OR SFC-400FT",
         "An unmanned aircraft system within an area defined as a 5 nautical mile radius of SALEM, or surface to 400 feet"),
        ("AIRSPACE LASER LGT WI AN AREA DEFINED AS 5NM RADIUS OF SALEM, OR LASER DEMONSTRATIONS",
         "Laser light within an area defined as a 5 nautical mile radius of SALEM, or laser demonstrations"),
        ("AIRSPACE UAS WI AN AREA DEFINED AS 5NM RADIUS OF OREGON CITY, OR SFC-400FT",
         "An unmanned aircraft system within an area defined as a 5 nautical mile radius of Oregon city, or surface to 400 feet"),
        ("ROUTE ZAB ZKC. V12 PANHANDLE (PNH) VORTAC, TX TO GAGE (GAG) VORTAC, OK MOCA 5000.",
         decode("ROUTE ZAB ZKC. V12 PANHANDLE (PNH) VORTAC, TX TO GAGE (GAG) VORTAC, OK MOCA 5000.").replace(", TX", ", tx")),
    ):
        assert nt.check(text, answer) != [], answer


def test_a_full_stop_between_two_statements_of_the_notam_is_kept():
    notam = "NAV ILS RWY 16 WITHDRAWN MAINT. AVBL WITH 30 MIN PN TO ATC"
    literal = decode(notam)
    assert "maintenance. Available" in literal and nt.check(notam, literal) == []
    assert nt.check(notam, "The instrument landing system runway 16 is withdrawn, maintenance. It is available with 30 minutes prior notice "
                           "required to air traffic control") == []
    assert nt.check(notam, literal.replace("maintenance. Available", "maintenance; available")) == []
    # two statements run into one say something else
    for joined in ("maintenance is available", "maintenance, available", "maintenance available", "maintenance and available",
                   "maintenance: available", "maintenance - available"):
        assert nt.check(notam, literal.replace("maintenance. Available", joined)) == ["the full stop after 'MAINT' is missing"], joined
    # the full stop that ends the NOTAM, and an ellipsis, separate nothing
    assert nt.check("TWY C CLSD.", "Taxiway C is closed") == []
    assert nt.check("SECURITY ...SPECIAL NOTICE... PILOTS ARE REMINDED", "Security: special notice, pilots are reminded") == []
    # every FAA example with a full stop inside: its statements joined by a comma are refused
    seen = 0
    for e in faa_examples():
        plan = nt.Plan(e)
        stops = [n for n, item in enumerate(plan.items) if item.stop]
        for n in stops:
            answer = " ".join(item.render + ("," if m == n else "." if item.stop else "") for m, item in enumerate(plan.items))
            assert plan.problems(answer) == [f"the full stop after {plan.items[n].label!r} is missing"], (e, answer)
            seen += 1
    assert seen > 15


def test_due_before_a_cause_may_be_written_due_to_and_no_other_to_is_added():
    notam = "NAV ILS RWY 16 U/S DUE MAINT"
    assert nt.check(notam, "The instrument landing system runway 16 is unserviceable due to maintenance") == []
    assert nt.check(notam, "The instrument landing system runway 16 is unserviceable due maintenance") == []
    assert nt.check(notam, "The instrument landing system runway 16 is unserviceable to due maintenance") != []
    assert nt.check(notam, "The instrument landing system runway 16 is unserviceable due to to maintenance") != []
    assert nt.check(notam, "The instrument landing system to runway 16 is unserviceable due to maintenance") != []
    # where the NOTAM writes its own TO, that is the one
    assert nt.check("NAV ILS RWY 16 U/S DUE TO MAINT", "The instrument landing system runway 16 is unserviceable due to maintenance") == []
    assert nt.check("NAV ILS RWY 16 U/S DUE TO MAINT", "The instrument landing system runway 16 is unserviceable due to to maintenance") != []
    # DUE with nothing after it gives no cause
    assert nt.check("NAV ILS RWY 16 U/S DUE", "The instrument landing system runway 16 is unserviceable due to") != []


def model_of(*replies):
    """A model that gives these replies in turn, and the conversations it was sent."""
    seen = []

    async def ask(messages):
        seen.append(messages)
        return replies[len(seen) - 1]

    return ask, seen


def test_a_refused_answer_goes_back_to_the_model_once_with_what_was_wrong_with_it():
    notam, wrong, right = "AD AP CLSD EXC SKI", "The airport is closed except for ski", "The airport is closed except ski"
    ask, seen = model_of(json.dumps({"plain": wrong}), json.dumps({"plain": right}))
    result = asyncio.run(nt.checked_rewrite(ask, notam, "AD"))
    assert result["text"] == right and [answer for answer, _problems in result["tries"]] == [wrong, right]
    assert len(seen) == 2 and seen[0] == nt.messages_for(notam, "AD") and seen[1][:len(seen[0])] == seen[0]
    assert seen[1][-2] == {"role": "assistant", "content": json.dumps({"plain": wrong})}
    assert seen[1][-1]["role"] == "user" and "not from the NOTAM: for" in seen[1][-1]["content"]
    # right at once: asked once
    ask, seen = model_of(json.dumps({"plain": right}))
    assert asyncio.run(nt.checked_rewrite(ask, notam, "AD"))["text"] == right and len(seen) == 1
    # wrong again: refused, and not asked a third time
    ask, seen = model_of(json.dumps({"plain": wrong}), json.dumps({"plain": "The airport is open"}), json.dumps({"plain": right}))
    result = asyncio.run(nt.checked_rewrite(ask, notam, "AD"))
    assert "text" not in result and result["refused"] and len(seen) == nt.ATTEMPTS == 2
    assert result["refused"] == nt.check(notam, "The airport is open")
    # a reply that is not the JSON asked for goes back too
    ask, seen = model_of("The airport is closed except ski.", json.dumps({"plain": right}))
    result = asyncio.run(nt.checked_rewrite(ask, notam, "AD"))
    assert result["text"] == right and "not the JSON" in seen[1][-1]["content"]
    # the second answer is checked like the first: no wrong answer gets through on the second try
    plan, other = nt.Plan(notam), nt.Plan("RWY 16/34 CLSD")
    for _kind, _detail, answer in list(wrong_answers(plan, other))[::50]:
        if answer != plan.literal():
            ask, seen = model_of(json.dumps({"plain": wrong}), json.dumps({"plain": answer}))
            assert "text" not in asyncio.run(nt.checked_rewrite(ask, notam, "AD")), answer


def test_plain_punctuation_between_the_elements_is_accepted():
    """Commas, semicolons, a full stop and parentheses do not change a NOTAM: every
    FAA example written out with them, its own full stops kept, is accepted."""
    refused = []
    for e in faa_examples():
        plan = nt.Plan(e)
        renders = [item.render + ("." if item.stop else "") for item in plan.items if re.search(r"[A-Za-z0-9]", item.render)]
        if len(renders) != len(plan.items):
            continue      # the NOTAM writes a free-standing mark of its own
        prefix = "Unverified: " if plan.unverified else ""
        for answer in (prefix + ", ".join(renders) + ".", prefix + "; ".join(renders),
                       prefix + " ".join(renders[:-1] + ["(" + renders[-1] + ")"])):
            if plan.problems(answer):
                refused.append((e, answer, plan.problems(answer)))
    assert refused == []


def test_punctuation_the_notam_writes_after_an_element_may_be_kept():
    text = "APRON ACFT STAND 12: 432352.88N 1320946.15E, ACFT STAND 13: 432353.85N 1320948.55E CLSD"
    assert nt.check(text, decode(text)) == []
    plan = nt.Plan(text)
    assert plan.problems("Apron aircraft stand 12: 432352.88N 1320946.15E, aircraft stand 13: 432353.85N 1320948.55E closed") == []
    assert plan.problems("Apron aircraft stand 12 432352.88N 1320946.15E aircraft stand 13 432353.85N 1320948.55E closed") == []
    # a colon the NOTAM does not write between two numbers is not accepted
    assert nt.check("APRON ACFT STAND 12 432352 CLSD", "Apron aircraft stand 12: 432352 closed") != []
    # and the NOTAM's own comma must keep its space: "1, 500" is not "1,500"
    assert nt.check("APRON ACFT STAND 1, 500FT CLSD", "Apron aircraft stand 1, 500 feet closed") == []
    assert nt.check("APRON ACFT STAND 1, 500FT CLSD", "Apron aircraft stand 1,500 feet closed") != []


def test_a_free_standing_mark_of_the_notam_is_part_of_it():
    plan = nt.Plan("AD AP CLSD DLY 1200 - 1400 MON & TUE")
    assert plan.problems("The airport is closed daily 1200 - 1400 Monday & Tuesday") == []
    assert plan.problems("The airport is closed daily 1200 1400 Monday & Tuesday") != []
    assert plan.problems("The airport is closed daily 1200 - 1400 Monday Tuesday") != []
    assert plan.problems("The airport is closed daily 1200 - 1400 Monday and Tuesday") != []
    assert nt.check("AD AP CLSD DLY 1200 - 1400 MON & TUE", decode("AD AP CLSD DLY 1200 - 1400 MON & TUE")) == []


def test_only_the_joining_words_can_be_added_out_of_the_whole_faa_vocabulary():
    """Every word of the FAA's own prose and every wording of its NOTAM tables,
    added alone to an FAA example written out in the FAA's wording, at the end and
    after its first element: the answer is accepted for the joining words only."""
    examples = faa_examples()
    plans = [nt.Plan(e) for e in examples if len(nt.Plan(e).items) > 2]
    vocabulary = sorted(set(WORDS) | {w for accept in nt.ACCEPT.values() for a in accept for w in re.findall(r"[a-z]+", a.lower())}
                        | {w.lower() for group in (AUXILIARIES, RELATING_WORDS) for phrase in group for w in phrase.split()}
                        | set("abcdefghijklmnopqrstuvwxyz"))
    assert len(vocabulary) > 3500
    accepted = set()
    for n, word in enumerate(vocabulary):
        plan = plans[n % len(plans)]
        renders = [item.render + ("." if item.stop else "") for item in plan.items]
        prefix = "Unverified: " if plan.unverified else ""
        at_end = prefix + " ".join(renders + [word])
        inside = prefix + " ".join(renders[:1] + [word] + renders[1:])
        ok_end, ok_inside = not plan.problems(at_end), not plan.problems(inside)
        assert ok_end == ok_inside, (word, at_end)
        if ok_end:
            accepted.add(word)
    assert accepted == {"a", "an", "the", "is", "are", "and", "of", "at", "with", "that", "which", "there", "it", "its", "this"}


def test_every_word_the_notam_writes_is_required_however_short():
    """TO, FOR, IN, ON, AND and the like are part of the NOTAM: an answer without
    one, or with another in its place, is refused. Counted over every FAA example."""
    short = {"TO", "FOR", "IN", "ON", "AND", "OF", "AS", "AT", "BE", "BY", "FROM", "WITH", "IS", "ARE", "OR", "NO", "NOT"}
    seen = collections.Counter()
    for e in faa_examples():
        plan = nt.Plan(e)
        renders = [item.render for item in plan.items]
        prefix = "Unverified: " if plan.unverified else ""
        for i, item in enumerate(plan.items):
            if item.label not in short or item.says:
                continue
            seen[item.label] += 1
            without = prefix + " ".join(renders[:i] + renders[i + 1:])
            assert plan.problems(without), (item.label, e, without)
    assert len(seen) >= 12 and sum(seen.values()) > 150, seen


def test_model_answers_published_with_their_errors_are_refused():
    """A NOTAM and five model answers printed in the literature (Dani et al., ACL
    Findings 2025, table 6): each adds a name and dates; three drop "all
    surfaces"; one turns work in progress into "complete"."""
    text = body_of("!SVA 12/007 SVA AD AP ALL SFC WIP SN REMOVAL 2112090022-2112100022")
    answers = [
        "Work on snow removal at Savoonga Airport is underway from December 9th, 2021, 12:22 AM to December 10th, 2021, 12:22 AM.",
        "Snow removal work is in progress at Savoonga Airport (SVA) from December 9, 2021, 00:22 UTC to December 10, 2021, 00:22 UTC.",
        "At Savoonga Airport, all surface work including snow removal is complete as of December 9, 2021, at 12:22 AM.",
        "The work to remove snow on all surfaces at Savoonga Airport (SVA) has started and is making progress as of "
        "December 9, 2021, at 12:22 AM, with the expected completion by December 10, 2021, at 12:22 AM.",
        "Snow removal work is in progress at the airport",        # the same answer without its name and dates
        "All surface work including snow removal is complete",
    ]
    plan = nt.Plan(text, protect=("SVA",))
    assert all(plan.problems(a) for a in answers)
    assert plan.problems("The airport: all surfaces work in progress, snow removal") == []


def test_every_worked_example_is_an_faa_example_of_its_type_and_passes_the_check():
    bodies = {body_of(e) for e in faa_examples()}
    wrong = []
    for kind, pairs in nt.EXAMPLES.items():
        assert len(pairs) >= 3, kind
        for example, answer in pairs:
            kind_of_example = classify(example)
            if (example not in bodies or nt.check(example, answer)
                    or (kind_of_example != kind and not (kind == "PROCEDURE" and kind_of_example in PROCEDURE_KEYWORDS))):
                wrong.append((kind, example, nt.check(example, answer)))
    assert wrong == []
    # every type the FAA examples cover with at least three NOTAMs has examples of its own
    counts = collections.Counter(classify(b) for b in bodies)
    assert {kind for kind, n in counts.items() if n >= 3 and kind not in PROCEDURE_KEYWORDS} <= set(nt.EXAMPLES)


def test_a_notam_is_shown_the_examples_of_its_own_type_last():
    for kind in TYPE_SEQUENCE:
        shown = nt.examples_for(kind)
        assert len(shown) == nt.EXAMPLES_SHOWN and len(set(shown)) == len(shown), kind
        own = kind if kind in nt.EXAMPLES else ("PROCEDURE" if kind in PROCEDURE_KEYWORDS else None)
        if own:
            assert tuple(shown[-len(nt.EXAMPLES[own]):]) == nt.EXAMPLES[own], kind
            assert all(pair not in nt.EXAMPLES[own] for pair in shown[:-len(nt.EXAMPLES[own])]), kind
    messages = nt.messages_for("COM REMOTE COM OUTLET 122.4 U/S", "COM")
    assert "NOTAM: COM CLR DELIVERY 121.7 NOW 124.125" in messages[-3]["content"]
    assert json.loads(messages[-2]["content"]) == {"plain": "Clearance delivery 121.7 is now 124.125"}


# ---- what the model is given -----------------------------------------------------------------------

def test_the_model_is_never_given_validity_times_and_only_faa_wordings():
    wordings = {w.lower() for accept in nt.ACCEPT.values() for w in accept}
    for record in faa_samples():
        message = nt.user_message(record["text"], record["type"])
        assert not re.search(r"\d{10}", message), message      # the validity group is not part of the body
    for e in faa_examples():
        message = nt.user_message(body_of(e), "OTHER")
        glossary, keep = nt.glossary(body_of(e))
        for written, says in glossary:
            if written in nt.ACCEPT:
                assert {w.lower() for w in says.split(" or ")} <= wordings, (written, says)
        plan = nt.Plan(body_of(e))
        facts = {item.label for item in plan.items if re.search(r"\d", item.label) and not item.says}
        assert facts <= set(keep), (facts - set(keep), e)
        assert message.count("NOTAM: ") == 1 and "GLOSSARY:" in message and "KEEP: " in message


def test_the_words_the_model_is_told_it_may_add_are_the_words_the_check_allows():
    rule = re.search(r"The only words you may add are: ([^.]+)\.", nt.SYSTEM_PROMPT).group(1)
    assert {w.strip() for w in rule.split(",")} == set(nt.GRAMMAR)


def test_the_conversation_is_the_rules_the_worked_examples_and_the_notam():
    messages = nt.messages_for("RWY 20 RWY END ID LGT U/S", "RWY", ("8WC", "", "STL"))
    assert messages[0] == {"role": "system", "content": nt.SYSTEM_PROMPT}
    assert len(messages) == 2 + 2 * nt.EXAMPLES_SHOWN
    assert [m["role"] for m in messages[1:-1]] == ["user", "assistant"] * nt.EXAMPLES_SHOWN
    assert all(nt.parse_reply(m["content"]) for m in messages[2:-1:2])
    assert "NOTAM: RWY 20 RWY END ID LGT U/S" in messages[-1]["content"] and messages[-1]["role"] == "user"
    assert nt.messages_for("RWY 20 RWY END ID LGT U/S", "RWY") == nt.messages_for("RWY 20 RWY END ID LGT U/S", "RWY")


@pytest.mark.parametrize(
    "reply, expected",
    [
        ('{"plain": "Runway 20 is closed"}', "Runway 20 is closed"),
        ('  {"plain": " Runway 20\\n is closed "}  ', "Runway 20 is closed"),
        ('<think>RWY means runway...</think>\n{"plain": "Runway 20 is closed"}', "Runway 20 is closed"),
        ('```json\n{"plain": "Runway 20 is closed"}\n```', "Runway 20 is closed"),
        ('Here you go: {"plain": "Runway 20 is closed"} Hope that helps.', "Runway 20 is closed"),
        ("Runway 20 is closed", None),
        ('{"text": "Runway 20 is closed"}', None),
        ('{"plain": ""}', None),
        ('{"plain": 20}', None),
        ('["Runway 20 is closed"]', None),
        ('{"plain": "Runway 20 is closed"', None),
        ("", None),
        (None, None),
    ],
)
def test_a_reply_is_read_only_when_it_is_the_json_asked_for(reply, expected):
    assert nt.parse_reply(reply) == expected


# ---- the endpoint client ---------------------------------------------------------------------------

def completion(content):
    return {"id": "x", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}


def chat_model(handler):
    return llm.ChatModel(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_the_model_is_asked_once_at_temperature_zero_for_schema_constrained_json(monkeypatch):
    monkeypatch.setenv("HANGAR_LLM_API_KEY", "sk-the-key")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=completion('{"plain": "ok"}'))

    messages = nt.messages_for("RWY 20 RWY END ID LGT U/S", "RWY")
    reply = asyncio.run(chat_model(handler).complete("https://llm.example.net/v1/", "qwen", messages, 30, nt.RESPONSE_SCHEMA))
    assert reply == '{"plain": "ok"}' and len(seen) == 1
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://llm.example.net/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-the-key"
    assert body["model"] == "qwen" and body["temperature"] == 0 and body["messages"] == messages
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["schema"] == nt.RESPONSE_SCHEMA
    assert 0 < body["max_tokens"] <= 2000


def test_an_endpoint_that_refuses_the_schema_is_asked_again_without_it_and_remembered():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format json_schema is not supported"}})
        return httpx.Response(200, json=completion('{"plain": "ok"}'))

    async def run():
        model = chat_model(handler)
        assert await model.complete("https://llm.example.net/v1", "qwen", [], 30, nt.RESPONSE_SCHEMA) == '{"plain": "ok"}'
        assert await model.complete("https://llm.example.net/v1", "qwen", [], 30, nt.RESPONSE_SCHEMA) == '{"plain": "ok"}'

    asyncio.run(run())
    assert ["response_format" in b for b in bodies] == [True, False, False]


@pytest.mark.parametrize(
    "response, message",
    [
        (httpx.Response(401, json={"error": "bad key sk-the-key"}), "rejected the API key"),
        (httpx.Response(403, json={}), "rejected the API key"),
        (httpx.Response(404, json={}), "no such path or model"),
        (httpx.Response(500, text="upstream exploded"), "HTTP 500"),
        (httpx.Response(200, text="<html>gateway</html>"), "not a chat completion"),
        (httpx.Response(200, json={"choices": []}), "not a chat completion"),
        (httpx.Response(200, json=completion("   ")), "returned no text"),
        (httpx.Response(200, json=completion(None)), "returned no text"),
    ],
)
def test_an_unusable_endpoint_is_reported_without_the_key_or_the_reply(monkeypatch, response, message):
    monkeypatch.setenv("HANGAR_LLM_API_KEY", "sk-the-key")
    with pytest.raises(llm.ModelError) as err:
        asyncio.run(chat_model(lambda request: response).complete("https://llm.example.net/v1", "qwen", [], 30))
    assert message in str(err.value) and "sk-the-key" not in str(err.value) and "exploded" not in str(err.value)


def test_a_timeout_and_a_dead_endpoint_are_model_errors():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    def dead(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(llm.ModelError, match="did not answer within 5 s"):
        asyncio.run(chat_model(slow).complete("https://llm.example.net/v1", "qwen", [], 5))
    with pytest.raises(llm.ModelError, match="unreachable"):
        asyncio.run(chat_model(dead).complete("https://llm.example.net/v1", "qwen", [], 5))


class StandIn(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint on a real socket: /v1/models and
    /v1/chat/completions, answering in the shape LiteLLM returns."""
    requests: list = []
    delay_s = 0.0

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer sk-stand-in":
            return self._send(401, {"error": {"message": "Authentication Error"}})
        self._send(200, {"object": "list", "data": [{"id": "qwen-local", "object": "model"}, {"id": "other"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        StandIn.requests.append((self.path, dict(self.headers), body))
        if self.headers.get("Authorization") != "Bearer sk-stand-in":
            return self._send(401, {"error": {"message": "Authentication Error"}})
        time.sleep(StandIn.delay_s)
        notam = re.search(r"NOTAM: (.*)", body["messages"][-1]["content"]).group(1)
        answer = nt.Plan(notam).literal()
        self._send(200, {
            "id": "chatcmpl-1", "object": "chat.completion", "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"plain": answer})}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 20, "total_tokens": 920},
        })


@pytest.fixture()
def stand_in():
    StandIn.requests, StandIn.delay_s = [], 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


def test_a_rewrite_travels_over_real_http_and_comes_back_checked(stand_in, monkeypatch):
    monkeypatch.setenv("HANGAR_LLM_API_KEY", "sk-stand-in")
    text = "RWY 16/34 CLSD TO ACFT WINGSPAN MORE THAN 70FT AND TAIL HGT MORE THAN 49FT"

    async def run():
        async with httpx.AsyncClient() as client:
            model = llm.ChatModel(client)
            assert await model.models(stand_in) == ["qwen-local", "other"]
            return await model.complete(stand_in, "qwen-local", nt.messages_for(text, "RWY"), 10, nt.RESPONSE_SCHEMA)

    answer = nt.parse_reply(asyncio.run(run()))
    assert answer and nt.check(text, answer) == []
    path, headers, body = StandIn.requests[-1]
    assert path == "/v1/chat/completions" and body["model"] == "qwen-local" and body["temperature"] == 0


def test_over_real_http_a_wrong_key_and_a_slow_model_are_errors_not_hangs(stand_in, monkeypatch):
    async def ask(timeout_s):
        async with httpx.AsyncClient() as client:
            return await llm.ChatModel(client).complete(stand_in, "qwen-local", nt.messages_for("RWY 20 CLSD", "RWY"), timeout_s)

    monkeypatch.setenv("HANGAR_LLM_API_KEY", "sk-wrong")
    with pytest.raises(llm.ModelError, match="rejected the API key"):
        asyncio.run(ask(10))
    monkeypatch.setenv("HANGAR_LLM_API_KEY", "sk-stand-in")
    StandIn.delay_s = 3.0
    t0 = time.time()
    with pytest.raises(llm.ModelError, match="did not answer within 1 s"):
        asyncio.run(ask(1))
    assert time.time() - t0 < 2.5


# ---- the pipeline ------------------------------------------------------------------------------------

def rewrite_cfg(idents=("KS50",), **translate):
    body = base_cfg()
    body["notams"] = {
        "enabled": True, "source": "faa", "lookahead_h": 24,
        "locations": [{"ident": i, "name": "", "enabled": True} for i in idents],
        "translate": {"enabled": True, "base_url": "https://llm.example.net/v1", "model": "qwen-local", "timeout_s": 30, **translate},
    }
    return body


def stub_model(monkeypatch, answer_for):
    """Stand in for the model: `answer_for(notam_text)` gives the reply text, or
    raises."""
    calls = []

    async def complete(base_url, model, messages, timeout_s, schema=None):
        asked = [m["content"] for m in messages if m["role"] == "user" and m["content"].startswith("TYPE: ")][-1]
        notam = re.search(r"NOTAM: (.*)", asked).group(1)
        calls.append((base_url, model, notam))
        result = answer_for(notam)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    monkeypatch.setattr(main_mod.manager.llm, "complete", complete)
    return calls


def plain_of(client, ident="KS50"):
    return {n["text"]: n["plain"] for n in shown(client, ident)["notams"]}


def counts(client):
    return client.get("/api/status").json()["notams"]["translate"]


def test_a_checked_rewrite_is_shown_and_an_answer_that_fails_the_check_never_is(client, monkeypatch):
    good, bad, junk = "RWY 16/34 CLSD", "TWY A CLSD", "APRON MAIN RAMP CLSD"
    stub_faa(monkeypatch, {"KS50": [feature("1", text=good), feature("2", text=bad), feature("3", text=junk)]})
    answers = {
        good: json.dumps({"plain": "Runway 16/34 is closed"}),
        bad: json.dumps({"plain": "Taxiway B is closed"}),          # a designator changed
        junk: "I think the main ramp is closed.",                    # not the JSON asked for
    }
    stub_model(monkeypatch, lambda notam: answers[notam])
    assert put(client, rewrite_cfg()).status_code == 200
    assert wait_until(lambda: counts(client)["pending"] == 0 and counts(client)["shown"] == 1, 4)
    got = plain_of(client)
    assert got[good] == {"text": "Runway 16/34 is closed", "source": "model", "model": "qwen-local"}
    assert got[bad] == {"text": "Taxiway A closed", "source": "rules"}
    assert got[junk]["source"] == "rules"
    assert (counts(client)["shown"], counts(client)["refused"], counts(client)["error"]) == (1, 2, None)


def test_the_display_sends_a_refused_answer_back_once_and_shows_the_corrected_one(client, monkeypatch):
    fixed, stubborn = "AD AP CLSD EXC SKI", "TWY K CLSD"      # neither is a NOTAM of another test: a rewrite is asked for once
    stub_faa(monkeypatch, {"KS50": [feature("1", text=fixed), feature("2", text=stubborn)]})
    asked = collections.Counter()

    def answer(notam):
        asked[notam] += 1
        if notam == fixed:
            return json.dumps({"plain": "The airport is closed except for ski" if asked[notam] == 1 else "The airport is closed except ski"})
        return json.dumps({"plain": "Taxiway L is closed"})

    stub_model(monkeypatch, answer)
    assert put(client, rewrite_cfg()).status_code == 200
    assert wait_until(lambda: counts(client)["pending"] == 0 and counts(client)["shown"] == 1, 4)
    got = plain_of(client)
    assert got[fixed] == {"text": "The airport is closed except ski", "source": "model", "model": "qwen-local"}
    assert got[stubborn] == {"text": "Taxiway K closed", "source": "rules"}
    assert asked == {fixed: 2, stubborn: 2}


def test_the_view_never_waits_for_the_model(client, monkeypatch):
    stub_faa(monkeypatch, {"KPWT": [feature("1", location="PWT", text="RWY 02/20 CLSD")]})

    async def never(notam):
        await asyncio.sleep(30)

    stub_model(monkeypatch, never)
    assert put(client, rewrite_cfg(("KPWT",))).status_code == 200
    assert wait_until(lambda: shown(client, "KPWT")["count"] == 1, 3)
    t0 = time.time()
    got = shown(client, "KPWT")["notams"][0]["plain"]
    assert time.time() - t0 < 1.0 and got == {"text": "Runway 02/20 closed", "source": "rules"}


def test_each_notam_is_sent_to_the_model_once_however_often_it_is_fetched(client, monkeypatch):
    stub_faa(monkeypatch, {"KS50": [feature("1", text="RWY 16/34 CLSD"), feature("2", text="TWY A CLSD")]})
    calls = stub_model(monkeypatch, lambda notam: json.dumps({"plain": nt.Plan(notam).literal()}))
    assert put(client, rewrite_cfg()).status_code == 200
    assert wait_until(lambda: counts(client)["shown"] == 2, 4)
    for n in range(3):      # each saved change fetches the NOTAMs again
        assert put(client, rewrite_cfg(label=None) if False else {**rewrite_cfg(), "notams": {**rewrite_cfg()["notams"], "label": f"NOTAMs {n}"}}).status_code == 200
        time.sleep(0.4)
    assert wait_until(lambda: counts(client)["pending"] == 0, 3)
    assert sorted(c[2] for c in calls) == ["RWY 16/34 CLSD", "TWY A CLSD"]
    # another model is another question
    assert put(client, rewrite_cfg(model="qwen-other")).status_code == 200
    assert wait_until(lambda: len(calls) == 4 and counts(client)["shown"] == 2, 4)
    assert {c[1] for c in calls} == {"qwen-local", "qwen-other"}


def test_a_model_outage_leaves_the_literal_text_and_is_logged_once(client, monkeypatch, caplog):
    stub_faa(monkeypatch, {"KS50": [feature("1", text="RWY 16/34 CLSD")]})
    state = {"down": True}

    def answer(notam):
        if state["down"]:
            raise llm.ModelError("the model endpoint is unreachable: ConnectError")
        return json.dumps({"plain": "Runway 16/34 is closed"})

    calls = stub_model(monkeypatch, answer)
    caplog.set_level(logging.INFO, logger="hangar")
    assert put(client, rewrite_cfg()).status_code == 200
    assert wait_until(lambda: counts(client)["error"] is not None, 4)
    for n in range(2):
        before = len(calls)
        assert put(client, {**rewrite_cfg(), "notams": {**rewrite_cfg()["notams"], "label": f"NOTAMs {n}"}}).status_code == 200
        assert wait_until(lambda: len(calls) > before, 4)
    assert plain_of(client)["RWY 16/34 CLSD"] == {"text": "Runway 16/34 closed", "source": "rules"}
    assert len([r for r in caplog.records if "NOTAM rewrite model unavailable" in r.getMessage()]) == 1
    state["down"] = False
    assert put(client, {**rewrite_cfg(), "notams": {**rewrite_cfg()["notams"], "label": "NOTAMs back"}}).status_code == 200
    assert wait_until(lambda: plain_of(client)["RWY 16/34 CLSD"]["source"] == "model", 4)
    assert counts(client)["error"] is None


def test_turning_the_rewrite_off_shows_the_literal_text_again(client, monkeypatch):
    stub_faa(monkeypatch, {"KS50": [feature("1", text="RWY 16/34 CLSD")]})
    stub_model(monkeypatch, lambda notam: json.dumps({"plain": "Runway 16/34 is closed"}))
    assert put(client, rewrite_cfg()).status_code == 200
    assert wait_until(lambda: plain_of(client).get("RWY 16/34 CLSD", {}).get("source") == "model", 4)
    assert put(client, rewrite_cfg(enabled=False)).status_code == 200
    assert wait_until(lambda: plain_of(client).get("RWY 16/34 CLSD", {}).get("source") == "rules", 3)


def test_the_model_key_is_never_returned_and_the_test_endpoint_needs_the_admin_password(client, monkeypatch):
    monkeypatch.setenv("HANGAR_LLM_API_KEY", "sk-the-key")
    stub_faa(monkeypatch, {})
    stub_model(monkeypatch, lambda notam: json.dumps({"plain": "Runway 20 runway end identifier light is unserviceable"}))

    async def models(base_url, timeout_s=15.0):
        return ["qwen-local"]

    monkeypatch.setattr(main_mod.manager.llm, "models", models)
    assert put(client, rewrite_cfg()).status_code == 200
    body = {"base_url": "https://llm.example.net/v1", "model": "qwen-local", "timeout_s": 30}
    assert client.post("/api/notams/model-test", json=body).status_code == 401
    r = client.post("/api/notams/model-test", json=body, auth=AUTH)
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["key"] is True and r.json()["models"] == ["qwen-local"]
    assert r.json()["answer"] == "Runway 20 runway end identifier light is unserviceable" and r.json()["refused"] == []
    for response in (r, client.get("/api/status"), client.get("/api/config"), client.get("/healthz")):
        assert "sk-the-key" not in response.text
    assert client.post("/api/notams/model-test", json={**body, "base_url": "ftp://llm.example.net"}, auth=AUTH).status_code == 422


def test_the_test_endpoint_reports_a_refused_answer_and_a_dead_endpoint(client, monkeypatch):
    stub_faa(monkeypatch, {})
    assert put(client, base_cfg()).status_code == 200
    body = {"base_url": "https://llm.example.net/v1", "model": "qwen-local", "timeout_s": 30}

    async def no_models(base_url, timeout_s=15.0):
        raise llm.ModelError("the model endpoint rejected the API key (HTTP 401)")

    monkeypatch.setattr(main_mod.manager.llm, "models", no_models)
    stub_model(monkeypatch, lambda notam: json.dumps({"plain": "Runway 20 lights are out until tomorrow"}))
    r = client.post("/api/notams/model-test", json=body, auth=AUTH).json()
    assert r["ok"] is False and r["answer"] is None and r["refused"] and "rejected the API key" in r["models_error"]

    def dead(notam):
        raise llm.ModelError("the model endpoint is unreachable: ConnectError")

    stub_model(monkeypatch, dead)
    r = client.post("/api/notams/model-test", json=body, auth=AUTH).json()
    assert r["ok"] is False and "unreachable" in r["error"]


@pytest.mark.parametrize(
    "translate, loc",
    [
        ({"enabled": True, "base_url": "", "model": "qwen"}, ["notams", "translate"]),
        ({"enabled": True, "base_url": "https://llm.example.net/v1", "model": ""}, ["notams", "translate"]),
        ({"enabled": False, "base_url": "ftp://llm.example.net", "model": ""}, ["notams", "translate", "base_url"]),
        ({"enabled": False, "base_url": "http://169.254.169.254/v1", "model": ""}, ["notams", "translate", "base_url"]),
        ({"enabled": False, "timeout_s": 1}, ["notams", "translate", "timeout_s"]),
        ({"enabled": False, "api_key": "sk-x"}, ["notams", "translate", "api_key"]),
    ],
)
def test_bad_model_settings_are_rejected(client, translate, loc):
    body = base_cfg()
    body["notams"] = {"enabled": False, "translate": translate}
    r = put(client, body)
    assert r.status_code == 422, r.text
    assert loc in [e["loc"][: len(loc)] for e in r.json()["detail"]], r.json()
