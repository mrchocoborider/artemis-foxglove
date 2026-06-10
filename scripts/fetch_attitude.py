#!/usr/bin/env python3
"""
Fetch Artemis II AROW telemetry samples from the Wayback Machine.

NASA's AROW (Artemis Real-time Orbit Watch) live tracker polled spacecraft
state from a public Google Cloud Storage bucket every ~60 seconds during
the mission. The endpoint:

    https://storage.googleapis.com/storage/v1/b/p-2-cen1/o/October%2F1%2FOctober_105_1.txt

Every snapshot is a JSON document containing a `File` header (`Activity`
= "MIS" for live mission data, "SIM" for simulation) and a series of
`Parameter_NNNN` entries — position (feet, equatorial J2000), velocity
(ft/s), attitude quaternion (unitless), angular rates (deg/s), thruster
state, RCS state, etc.

The bucket has been locked down post-mission (returns 401), but the
Wayback Machine captured ~57 unique snapshots across April 1–14, 2026.
This script enumerates those captures, downloads each one, parses the
parameters of interest, deduplicates by the telemetry-internal timestamp
(`Parameter_2003.Time`) so multiple Wayback fetches of the same NASA
generation collapse to a single sample, and writes the result to
`data/attitude.json`.

Format reference: Ian Dees's reverse-engineered parameter table —
https://github.com/iandees/artemis-viewer/blob/main/README.md

Output schema (`data/attitude.json`):

    [
      {
        "time_utc":     "2026-04-07T23:58:21.890000+00:00",
        "x_km":  -107940.27, "y_km":  -294946.19, "z_km":  -169235.80,
        "vx_km_s":   0.366,  "vy_km_s":  0.545,  "vz_km_s":  0.243,
        "qw":  -0.029166, "qx": -0.092574, "qy": 0.741040, "qz": -0.664409,
        "rate_roll_dps":  0.000428,
        "rate_pitch_dps": -0.001031,
        "rate_yaw_dps":  -2.83e-05,
        "activity": "MIS",
        "wayback_timestamp": "20260408051746",
        "generation": "1775778900000000"
      },
      ...
    ]

Position/velocity are converted from feet → km on the way out (the same
conversion iandees applies when consuming the live feed). Quaternion and
angular rates are passed through unchanged. Samples are sorted by
`time_utc` ascending.

Usage:
    python scripts/fetch_attitude.py
    python scripts/fetch_attitude.py --start 20260401 --end 20260415
    python scripts/fetch_attitude.py --output data/attitude.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_OUTPUT = DATA_DIR / "attitude.json"

# AROW Orion telemetry path inside NASA's GCS bucket.
TELEMETRY_PATH = "October/1/October_105_1.txt"
BUCKET = "p-2-cen1"

# Wayback's CDX index lets us enumerate snapshots without scraping HTML.
# matchType=prefix catches both the bare metadata URL and the
# `?alt=media` data-URL variants iandees's worker uses.
CDX_URL = (
    "https://web.archive.org/cdx/search/cdx"
    "?url=storage.googleapis.com/storage/v1/b/{bucket}/o/{escaped}"
    "&matchType=prefix"
    "&from={start}&to={end}"
    "&output=json&filter=statuscode:200&limit={limit}"
)

# `if_` removes Wayback's banner/iframe wrapper so we get the original
# response body. Without it the JSON is corrupted by inserted scripts.
WAYBACK_FETCH = "https://web.archive.org/web/{ts}if_/{url}"

# Position/velocity conversion. Iandees uses the same constant (he calls
# it FT_TO_KM); we keep it explicit so the parsed JSON is in km/(km/s)
# matching our downstream Horizons CSV units.
FT_TO_KM = 0.0003048

# Parameter IDs we extract. Source: iandees's parseTelemetry().
PARAM_X, PARAM_Y, PARAM_Z = 2003, 2004, 2005
PARAM_VX, PARAM_VY, PARAM_VZ = 2009, 2010, 2011
PARAM_QW, PARAM_QX, PARAM_QY, PARAM_QZ = 2012, 2013, 2014, 2015
PARAM_ROLL_RATE, PARAM_PITCH_RATE, PARAM_YAW_RATE = 2101, 2102, 2103


def list_wayback_captures(start: str, end: str, limit: int) -> list[dict[str, str]]:
    """Use Wayback's CDX API to enumerate captures of the AROW telemetry
    object. Returns a list of dicts {timestamp, original} sorted by
    timestamp ascending. We deliberately keep both the bare-metadata URL
    and the ?alt=media URLs in the result; downloaders pick the right
    one based on whether the response is metadata JSON or telemetry JSON.
    """
    escaped = urllib.parse.quote(TELEMETRY_PATH, safe="")
    url = CDX_URL.format(bucket=BUCKET, escaped=escaped,
                         start=start, end=end, limit=limit)
    print(f"[attitude] enumerating Wayback CDX for {TELEMETRY_PATH}")
    with urllib.request.urlopen(url, timeout=60) as r:
        rows = json.loads(r.read())
    if not rows or rows[0][0] != "urlkey":
        raise RuntimeError("Unexpected CDX response shape")
    out = [{"timestamp": r[1], "original": r[2]} for r in rows[1:]]
    out.sort(key=lambda x: x["timestamp"])
    print(f"[attitude]   {len(out)} captures across {start}..{end}")
    return out


def parse_dayofyear_time(s: str) -> dt.datetime | None:
    """AROW's Parameter time field is `YYYY:DDD:HH:MM:SS.fff` (day-of-year).
    Convert to a timezone-aware UTC datetime. Returns None on parse
    failure — robustly skip rather than crash the whole batch on one
    weird sample.
    """
    try:
        year, doy, hms = s.split(":", 2)
        h, m, sec = hms.split(":")
        whole = dt.datetime(int(year), 1, 1, int(h), int(m), int(float(sec)),
                            tzinfo=dt.timezone.utc) + dt.timedelta(days=int(doy) - 1)
        # Preserve sub-second precision separately since strptime / our
        # truncating int(float(sec)) above drops it.
        frac = float(sec) - int(float(sec))
        return whole + dt.timedelta(microseconds=int(round(frac * 1_000_000)))
    except (ValueError, IndexError):
        return None


def parse_telemetry(payload: dict[str, Any], wayback_ts: str) -> dict[str, Any] | None:
    """Convert a raw AROW telemetry document into our compact attitude
    sample shape. Returns None if the document is metadata-only (no
    Parameter_NNNN entries — those come from Wayback captures of the
    bare GCS object metadata URL) or if any of the required Parameter
    entries are missing/Bad.
    """
    file_hdr = payload.get("File")
    if not isinstance(file_hdr, dict):
        return None
    activity = file_hdr.get("Activity")

    def get_param(num: int) -> float | None:
        p = payload.get(f"Parameter_{num}")
        if not isinstance(p, dict):
            return None
        if p.get("Status") != "Good":
            return None
        try:
            return float(p["Value"])
        except (TypeError, ValueError, KeyError):
            return None

    x_ft = get_param(PARAM_X)
    y_ft = get_param(PARAM_Y)
    z_ft = get_param(PARAM_Z)
    if x_ft is None or y_ft is None or z_ft is None:
        return None

    qw = get_param(PARAM_QW)
    qx = get_param(PARAM_QX)
    qy = get_param(PARAM_QY)
    qz = get_param(PARAM_QZ)

    # Reject samples where the quaternion isn't usable. We still keep
    # position-only samples in case attitude is desired later, but for
    # now we filter strictly so consumers can rely on every sample
    # having a valid (qw, qx, qy, qz).
    if any(v is None for v in (qw, qx, qy, qz)):
        return None
    norm = (qw * qw + qx * qx + qy * qy + qz * qz) ** 0.5
    if not (0.95 < norm < 1.05):
        return None

    p2003 = payload.get(f"Parameter_{PARAM_X}", {})
    time_str = p2003.get("Time")
    when = parse_dayofyear_time(time_str) if time_str else None
    if when is None:
        return None

    sample = {
        "time_utc": when.isoformat(),
        "x_km": x_ft * FT_TO_KM,
        "y_km": y_ft * FT_TO_KM,
        "z_km": z_ft * FT_TO_KM,
        "qw": qw, "qx": qx, "qy": qy, "qz": qz,
        "activity": activity,
        "wayback_timestamp": wayback_ts,
    }
    vx = get_param(PARAM_VX)
    vy = get_param(PARAM_VY)
    vz = get_param(PARAM_VZ)
    if vx is not None and vy is not None and vz is not None:
        sample["vx_km_s"] = vx * FT_TO_KM
        sample["vy_km_s"] = vy * FT_TO_KM
        sample["vz_km_s"] = vz * FT_TO_KM
    rr = get_param(PARAM_ROLL_RATE)
    pr = get_param(PARAM_PITCH_RATE)
    yr = get_param(PARAM_YAW_RATE)
    if rr is not None: sample["rate_roll_dps"] = rr
    if pr is not None: sample["rate_pitch_dps"] = pr
    if yr is not None: sample["rate_yaw_dps"] = yr
    return sample


def fetch_capture(timestamp: str, original: str, sleep_s: float,
                  max_retries: int = 4) -> dict[str, Any] | None:
    """Download one Wayback snapshot. Returns the parsed JSON payload, or
    None on permanent failure. Retries with exponential back-off on
    transient errors (network blips, 429 rate-limit, 503 overloaded
    backend) — Wayback rate-limits hard on bursty traffic and the
    standard recipe is "wait longer and try again".
    """
    url = WAYBACK_FETCH.format(ts=timestamp, url=original)
    delay = sleep_s
    last_err: str | None = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "artemis-foxglove/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            # 429 / 5xx — retry with back-off. 4xx other than 429 is
            # almost certainly permanent (bad URL, missing capture).
            if e.code != 429 and not (500 <= e.code < 600):
                print(f"[attitude]   {timestamp} permanent {last_err}",
                      file=sys.stderr)
                time.sleep(sleep_s)
                return None
        except Exception as e:
            last_err = str(e)
        else:
            time.sleep(sleep_s)
            try:
                return json.loads(body)
            except json.JSONDecodeError as e:
                # Wayback occasionally returns its own HTML error page
                # (e.g. "robot block" or "page cannot be displayed")
                # with a 200 status. Treat as permanent for this URL.
                snippet = body[:80].decode("utf-8", "replace")
                print(f"[attitude]   {timestamp} JSON parse error "
                      f"({snippet!r})", file=sys.stderr)
                return None
        # Back-off: 0.5 -> 1 -> 2 -> 4 seconds (or whatever sleep_s starts at)
        sleep_for = delay * (2 ** attempt)
        time.sleep(sleep_for)
    print(f"[attitude]   {timestamp} giving up after {max_retries} "
          f"attempts ({last_err})", file=sys.stderr)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="20260401",
                        help="Wayback CDX from-date (yyyymmdd). Default 20260401.")
    parser.add_argument("--end", default="20260415",
                        help="Wayback CDX to-date (yyyymmdd). Default 20260415.")
    parser.add_argument("--limit", type=int, default=5000,
                        help="Max CDX rows to enumerate. Default 5000.")
    parser.add_argument("--sleep", type=float, default=0.5,
                        help="Seconds between Wayback fetches (rate-limit "
                             "buffer). Default 0.5.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="Output JSON path. Default data/attitude.json.")
    parser.add_argument("--accept-sim", action="store_true",
                        help="Also keep samples whose File.Activity is 'SIM' "
                             "(simulation/test data). Default: only 'MIS'.")
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)

    captures = list_wayback_captures(args.start, args.end, args.limit)
    if not captures:
        print("[attitude] no Wayback captures found — exiting", file=sys.stderr)
        return 1

    samples: dict[str, dict[str, Any]] = {}  # keyed by telemetry time_utc
    n_skipped_metadata = 0
    n_skipped_sim = 0
    n_skipped_no_telem = 0
    n_skipped_dupe = 0
    n_failed_fetch = 0

    for i, cap in enumerate(captures):
        ts = cap["timestamp"]
        original = cap["original"]
        print(f"[attitude] [{i+1:3d}/{len(captures):3d}] {ts} "
              f"{original.split('/')[-1][:50]}…")
        payload = fetch_capture(ts, original, args.sleep)
        if payload is None:
            n_failed_fetch += 1
            continue

        # Skip GCS metadata captures — those have no Parameter_* entries,
        # only fields like generation/size/md5Hash/etc.
        has_params = any(k.startswith("Parameter_") for k in payload)
        if not has_params:
            n_skipped_metadata += 1
            continue

        parsed = parse_telemetry(payload, ts)
        if parsed is None:
            n_skipped_no_telem += 1
            continue

        if parsed["activity"] != "MIS" and not args.accept_sim:
            n_skipped_sim += 1
            continue

        # Dedupe by telemetry-internal timestamp: NASA only updates the
        # GCS file every ~60s, so multiple Wayback captures within that
        # window often have the same `Parameter_2003.Time` and identical
        # state. Keep the first one encountered (= earliest Wayback
        # capture of that NASA generation).
        key = parsed["time_utc"]
        if key in samples:
            n_skipped_dupe += 1
            continue
        samples[key] = parsed

    if not samples:
        print("[attitude] no usable telemetry samples extracted — exiting",
              file=sys.stderr)
        return 1

    out = sorted(samples.values(), key=lambda s: s["time_utc"])
    args.output.write_text(json.dumps(out, indent=2))
    print(f"[attitude] wrote {len(out)} unique samples → {args.output}")
    print(f"[attitude]   skipped: {n_skipped_metadata} metadata-only, "
          f"{n_skipped_no_telem} no/bad telemetry, "
          f"{n_skipped_sim} SIM (use --accept-sim to keep), "
          f"{n_skipped_dupe} duplicates by telemetry time, "
          f"{n_failed_fetch} fetch failures")
    print(f"[attitude]   coverage: {out[0]['time_utc']} → {out[-1]['time_utc']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
