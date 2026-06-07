"""NOAA STAR GOES sector animation frames.

The public viewer builds its loop client-side from timestamped still frames on
the STAR CDN. We fetch and parse the CDN directory listing on the server (no
browser CORS) and return the most recent N frame URLs for the frontend to
preload and animate.
"""
from __future__ import annotations

import re
from typing import Dict, List

import httpx

CDN = "https://cdn.star.nesdis.noaa.gov"
SAT_DIR = {"G16": "GOES16", "G18": "GOES18", "G19": "GOES19"}


def _fmt_time(ts: str) -> str:
    """YYYYDDDHHMM (year + day-of-year + HHMM, UTC) -> 'HH:MMZ'."""
    if len(ts) != 11:
        return ts
    return f"{ts[7:9]}:{ts[9:11]}Z"


class SatelliteSource:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def frames(self, sat: str, sector: str, band: str, size: str, count: int) -> Dict:
        sat_dir = SAT_DIR.get(sat.upper(), "GOES18")
        base = f"{CDN}/{sat_dir}/ABI/SECTOR/{sector}/{band}/"
        resp = await self.client.get(base, timeout=10.0)
        resp.raise_for_status()
        # filenames look like: 20261580701_GOES18-ABI-pnw-GEOCOLOR-1200x1200.jpg
        pat = re.compile(rf"(\d{{11}})_{re.escape(sat_dir)}-ABI-{re.escape(sector)}-{re.escape(band)}-{re.escape(size)}\.jpg")
        stamps = sorted(set(pat.findall(resp.text)))
        last = stamps[-count:] if count > 0 else stamps
        frames: List[Dict] = [
            {
                "url": f"{base}{ts}_{sat_dir}-ABI-{sector}-{band}-{size}.jpg",
                "ts": ts,
                "time": _fmt_time(ts),
            }
            for ts in last
        ]
        return {"frames": frames, "count": len(frames), "base": base}
