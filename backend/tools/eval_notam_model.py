#!/usr/bin/env python3
"""Measure a model on NOTAM rewriting: send it NOTAMs exactly as the display
would (a refused answer goes back once, with what was wrong with it), check
every answer with the display's own check, and report what it accepted and
what it refused.

Run it inside the app image, with the endpoint's key in the environment:

  docker run --rm --env-file .env -v "$PWD/backend:/work:ro" -w /work hangar-display:latest \\
      python tools/eval_notam_model.py --base-url https://llm.example.net/v1 --model NAME

To use the NOTAMs the display holds for its monitored locations, share the
running app's network and name it:

  docker run --rm --network container:hangar-display --env-file .env -v "$PWD/backend:/work:ro" -w /work \\
      hangar-display:latest python tools/eval_notam_model.py --display http://localhost:8000 \\
      --base-url https://llm.example.net/v1 --model NAME

Options:
  --file FILE     NOTAM texts to use (one per line, or a ";" separated CSV whose
                  first column is the text); default: the FAA's example NOTAMs
  --display URL   use the NOTAMs a running display shows for its locations
  --limit N       use the first N texts only
  --timeout S     seconds allowed per answer (default 60)
  --show N        accepted answers to print beside the literal decode (default 25)
  --out FILE      also write every result as JSON lines, as each one arrives
  --jobs N        NOTAMs to have with the model at a time (default 1)

Nothing is saved to the display. An endpoint that fails is tried again after a
pause; exit status 1 when it cannot be used.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import httpx  # noqa: E402

from app.llm import ChatModel, ModelError  # noqa: E402
from app.notam_decode import body_of, classify, decode  # noqa: E402
from app.notam_translate import RESPONSE_SCHEMA, checked_rewrite  # noqa: E402

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "tests", "data", "faa_examples.json")
PATIENCE = 4      # tries per NOTAM when the endpoint fails
PAUSE_S = 20


def texts_of(path: str | None) -> list:
    if not path:
        with open(EXAMPLES) as fh:
            data = json.load(fh)
        return data["examples"] + data["aim_examples"]
    with open(path, newline="") as fh:
        if path.endswith(".csv"):
            return [" ".join(";".join(r[:-1]).split()) for r in list(csv.reader(fh, delimiter=";"))[1:] if len(r) >= 2]
        return [" ".join(line.split()) for line in fh if line.strip()]


def notams_of(args) -> list:
    """-> [(text, type, its own identifiers)] to send."""
    if args.display:
        base = args.display.rstrip("/")
        out = []
        for view in httpx.get(base + "/api/views", timeout=20).json()["views"]:
            if view.get("type") != "notams":
                continue
            reply = httpx.get(base + "/api/notams", params={"location": view["location"]}, timeout=20).json()
            for n in reply["notams"]:
                out.append((n["text"] or n["raw"], n["type"], (n["location"], n["icao_location"], n["accountability"])))
        return out
    bodies = [body_of(t) for t in texts_of(args.file)]
    return [(b, classify(b), ()) for b in bodies if b]


def kind_of(problem: str) -> str:
    for key in ("is missing", "out of the NOTAM's order", "left as written", "not from the NOTAM", "a mark the NOTAM does not write",
                "not the JSON", "unverified", "far longer", "empty"):
        if key in problem:
            return key
    return "other"


async def run(args) -> int:
    notams = notams_of(args)
    notams = notams[: args.limit] if args.limit else notams
    if not notams:
        print("no NOTAMs to send")
        return 1
    kinds, seconds = collections.Counter(), []
    done: dict = {}
    stopped: list = []
    out = open(args.out, "w") if args.out else None
    async with httpx.AsyncClient(follow_redirects=True) as client:
        model = ChatModel(client)
        try:
            print("models offered:", ", ".join(await model.models(args.base_url)) or "(none listed)")
        except ModelError as exc:
            print("model list:", exc)
        waiting = iter(enumerate(notams, 1))

        async def worker() -> None:
            for n, (text, type_code, protect) in waiting:
                if stopped:
                    return
                t0 = time.monotonic()
                for attempt in range(1, PATIENCE + 1):      # an endpoint that fails is given time to come back
                    try:
                        result = await checked_rewrite(
                            lambda messages: model.complete(args.base_url, args.model, messages, args.timeout, RESPONSE_SCHEMA),
                            text, type_code, protect)
                        break
                    except ModelError as exc:
                        if attempt == PATIENCE or stopped:
                            stopped.append(f"stopped at NOTAM {n} of {len(notams)}: {exc}")
                            return
                        print(f"  NOTAM {n}: {exc}; trying again in {PAUSE_S} s", flush=True)
                        await asyncio.sleep(PAUSE_S)
                        t0 = time.monotonic()
                seconds.append(time.monotonic() - t0)
                done[n] = {"notam": text, "type": type_code, "answer": result["tries"][-1][0], "refused": result.get("refused", []),
                           "tries": result["tries"], "literal": decode(text, protect)}
                if out:
                    out.write(json.dumps(done[n]) + "\n")
                    out.flush()
                if len(done) % 25 == 0:
                    print(f"  {len(done)} / {len(notams)}", flush=True)

        await asyncio.gather(*(worker() for _ in range(max(1, args.jobs))))
    if out:
        out.close()
    if stopped:
        print("\n" + stopped[0])
        if not done:
            return 1
    results = [done[n] for n in sorted(done)]
    for r in results:
        for p in r["refused"]:
            kinds[kind_of(p)] += 1
    accepted = [r for r in results if not r["refused"]]
    refused = [r for r in results if r["refused"]]
    print(f"\n{len(results)} NOTAMs asked; {len(accepted)} answers accepted ({100 * len(accepted) / max(1, len(results)):.1f}%), "
          f"{len(refused)} refused")
    print("accepted at the first answer:", sum(1 for r in accepted if len(r["tries"]) == 1),
          "; after the model was told what was wrong:", sum(1 for r in accepted if len(r["tries"]) > 1))
    if seconds:
        print(f"seconds per answer: median {statistics.median(seconds):.1f}, slowest {max(seconds):.1f}")
    print("reasons for refusal:", dict(kinds.most_common()))
    asked, passed = collections.Counter(r["type"] for r in results), collections.Counter(r["type"] for r in accepted)
    print("accepted by type:", ", ".join(f"{kind} {passed[kind]}/{n}" for kind, n in asked.most_common()))
    same = sum(1 for r in accepted if r["answer"].lower() == r["literal"].lower())
    print(f"accepted answers identical to the literal decode: {same}")
    print("\nEVERY REFUSED ANSWER")
    for r in refused:
        print(f"  NOTAM  : {r['notam']}\n  ANSWER : {r['answer']}\n  REFUSED: {'; '.join(r['refused'])}\n")
    print(f"ACCEPTED ANSWERS (first {args.show}) BESIDE THE LITERAL DECODE")
    for r in accepted[: args.show]:
        print(f"  NOTAM  : {r['notam']}\n  LITERAL: {r['literal']}\n  MODEL  : {r['answer']}\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--file")
    parser.add_argument("--display")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--show", type=int, default=25)
    parser.add_argument("--out")
    parser.add_argument("--jobs", type=int, default=1)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
