#!/usr/bin/env python3
"""
Fetch the crew activity schedule embedded in Hank Green's
hankmt/Artemis-Timeline `index.html`.

The schedule is a JS array literal of the form:

    const SCHEDULE = [
      {s:edt("2026-04-01 12:50:00"), e:edt("2026-04-01 14:30:00"),
       a:"suit-ops", l:"Suit up"},
      ...
    ];

`edt()` is a JS helper that constructs a Date from an EDT (UTC-4)
timestamp; we evaluate the same conversion in Python (`datetime` +
`timedelta(hours=-4)`) so the output is canonical UTC.

Output:
    data/schedule.json   list of {start_utc, end_utc, activity, label}

Re-runnable. NASA's Artemis II Overview Timeline PDF is the upstream
source — Hank transcribed it into `index.html`, so this script picks
up future revisions without us having to re-read the PDF.

Usage:
    python scripts/fetch_schedule.py
    python scripts/fetch_schedule.py --source path/to/index.html  # offline
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

INDEX_URL = "https://raw.githubusercontent.com/hankmt/Artemis-Timeline/main/index.html"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EDT = timezone(timedelta(hours=-4))


def _extract_schedule_block(text: str) -> str:
    """Return the contents (between the outer brackets, inclusive) of the
    `const SCHEDULE = [...]` declaration. Uses a balanced-bracket walker
    so embedded `[` / `]` inside string literals don't trip us up."""
    m = re.search(r"\bconst\s+SCHEDULE\s*=\s*\[", text)
    if not m:
        raise ValueError("Couldn't locate `const SCHEDULE = [` in source")
    start = m.end() - 1  # the `[`
    depth = 0
    in_str = False
    quote: str | None = None
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in ('"', "'"):
            in_str = True
            quote = c
            i += 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise ValueError("Unbalanced brackets in SCHEDULE block")


def parse_schedule_js(block: str) -> list[dict[str, Any]]:
    """Convert the JS array literal into a list of dicts with UTC ISO
    timestamps. We don't run a real JS engine — the entries follow a
    rigid shape, so a small set of regex substitutions plus
    json.loads() is enough.

    Steps:
      1. Drop // line comments.
      2. Replace each `edt("YYYY-MM-DD HH:MM:SS")` with the equivalent
         UTC ISO string in quotes.
      3. Quote the JS-style bare keys (`s:`, `e:`, `a:`, `l:`).
      4. Strip trailing commas.
      5. JSON-parse the now-conformant array.
    """
    text = re.sub(r"//[^\n]*\n", "\n", block)

    def _edt_to_utc(m: re.Match) -> str:
        raw = m.group(1)
        dt_local = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EDT)
        return '"' + dt_local.astimezone(timezone.utc).isoformat() + '"'

    text = re.sub(r'edt\("([^"]+)"\)', _edt_to_utc, text)
    text = re.sub(r"([{,]\s*)([a-zA-Z_]\w*)\s*:", r'\1"\2":', text)
    text = re.sub(r",(\s*[\]}])", r"\1", text)

    arr = json.loads(text)
    out: list[dict[str, Any]] = []
    for entry in arr:
        if not isinstance(entry, dict):
            continue
        try:
            out.append({
                "start_utc": entry["s"],
                "end_utc": entry["e"],
                "activity": entry["a"],
                "label": entry["l"],
            })
        except KeyError as e:
            print(f"[schedule]   skipping entry missing key {e}: {entry}",
                  file=sys.stderr)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=None,
                        help="Local index.html (skips network fetch)")
    parser.add_argument("--output", type=Path,
                        default=DATA_DIR / "schedule.json",
                        help="Where to write the parsed schedule")
    args = parser.parse_args()

    if args.source is not None:
        print(f"[schedule] reading {args.source}")
        text = args.source.read_text()
    else:
        print(f"[schedule] fetching {INDEX_URL}")
        sess = requests.Session()
        sess.headers["User-Agent"] = "artemis-foxglove/0.1 (https://artemistimeline.com)"
        r = sess.get(INDEX_URL, timeout=30)
        r.raise_for_status()
        text = r.text

    block = _extract_schedule_block(text)
    schedule = parse_schedule_js(block)
    print(f"[schedule] parsed {len(schedule)} entries")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(schedule, indent=2))
    print(f"[schedule] wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
