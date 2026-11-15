"""The display API reference (data/display_api.md), filled in with this display's
address and the views it cycles through."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Dict, List

TEMPLATE = (Path(__file__).parent / "data" / "display_api.md").read_text(encoding="utf-8")
PLACEHOLDER = "http://<pi-ip>:8000"
VIEWS_START, VIEWS_END = "<!-- views -->", "<!-- /views -->"
FILENAME = "display-api.md"

# A scheme, a host name or address, and a port: what a browser's Host header gives.
BASE_RE = re.compile(r"https?://(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.?|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _cell(value) -> str:
    """Text for a table cell: one line, its pipes escaped."""
    return CONTROL_RE.sub(" ", str(value)).replace("|", "\\|")


def _code(value) -> str:
    """A code span for a table cell, delimited by more backticks than it holds."""
    text = _cell(value)
    run = max((len(r) for r in re.findall(r"`+", text)), default=0)
    if not run:
        return f"`{text}`"
    fence = "`" * (run + 1)
    return f"{fence} {text} {fence}"


def views_block(views: List[Dict]) -> str:
    """The views of this display, each id once, in cycle order."""
    if not views:
        return "This display has no views yet: the owner adds them on the admin page."
    counts = Counter(v["id"] for v in views)
    rows = [
        "These were the display's views when this document was made. `GET /api/views` gives the current list.",
        "",
        "| id | type | label | seconds on screen | entries in the cycle |",
        "|---|---|---|---|---|",
    ]
    seen = set()
    for v in views:
        if v["id"] in seen:
            continue
        seen.add(v["id"])
        dwell = f"{v['dwell_s']:g}" if isinstance(v.get("dwell_s"), (int, float)) else ""
        rows.append(f"| {_code(v['id'])} | {_cell(v['type'])} | {_cell(v['label'])} | {dwell} | {counts[v['id']]} |")
    return "\n".join(rows)


def render(base_url: str, views: List[Dict], template: str = TEMPLATE) -> str:
    """The reference with `base_url` for the address (kept as the placeholder when it
    is not a plain scheme, host and port) and this display's views."""
    text = template
    base = base_url.rstrip("/")
    if BASE_RE.fullmatch(base):
        text = text.replace(PLACEHOLDER, base)
    start, end = text.find(VIEWS_START), text.find(VIEWS_END)
    if 0 <= start < end:
        text = text[:start] + views_block(views) + text[end + len(VIEWS_END):]
    return text
