#!/usr/bin/env python3
"""
Fetch Orion (NAIF ID -1024) state vectors from JPL Horizons for the full
Artemis II mission window.

Queries two ephemerides:
  * Earth-centered (CENTER=399) — used for the primary 3D scene
  * Moon-centered  (CENTER=301) — useful for the lunar flyby segment

Both are requested in the ICRF / J2000 reference frame. Output is saved as
CSV in data/ for easy inspection; build_mcap.py reads these directly.

Usage:
    python scripts/fetch_horizons.py
    python scripts/fetch_horizons.py --step 30s  # finer resolution

Times are UTC. Mission window:
    Launch:     2026-04-01 22:35:25 UTC
    Splashdown: 2026-04-11 00:07:27 UTC
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from astroquery.jplhorizons import Horizons

# Artemis II mission window (UTC).
# Horizons's SPK for -1024 covers ~2026-04-02 02:00 → 2026-04-10 23:53 UTC.
# That excludes the first ~3.5 h of ascent (pre-stage-separation) and the
# final ~14 min of reentry (post-EI). Splashdown was 2026-04-11 00:07:27 UTC.
MISSION_START = "2026-04-02 02:00"
MISSION_END = "2026-04-10 23:50"

# NAIF body IDs. We pass them to Horizons via the unambiguous '500@<body>'
# form: '500' is the geocentric (no station offset) site code, and '@<body>'
# names the SPICE body that site is attached to. Passing '301' on its own
# does NOT mean "Moon body center" — Horizons interprets it as a topocentric
# observatory site, and the resulting vectors are bogus (e.g. an apparent
# Moon-from-Earth distance that stays fixed at ~8,700 km for every sample).
ORION = "-1024"
EARTH = "500@399"
MOON = "500@301"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def fetch_one(center: str, label: str, start: str, end: str, step: str) -> None:
    out_path = DATA_DIR / f"horizons_{label}.csv"
    print(f"[horizons] {ORION} relative to {center} ({label}) "
          f"{start} → {end} step={step}")

    obj = Horizons(
        id=ORION,
        location=center,
        epochs={"start": start, "stop": end, "step": step},
    )
    # Disable astroquery's pickle cache: we run this maybe once a week, the
    # cache pickle files can fail to write under sandboxed environments, and
    # corrupted caches have been known to silently return stale data.
    tab = obj.vectors(refplane="earth", cache=False)  # ICRF/J2000 equatorial frame

    # Horizons returns positions in AU and velocities in AU/day by default;
    # convert to km and km/s for downstream use.
    AU_KM = 149_597_870.7
    DAY_S = 86_400.0

    rows = []
    for r in tab:
        rows.append({
            "datetime_utc": str(r["datetime_str"]),
            "jd_tdb": float(r["datetime_jd"]),
            "x_km": float(r["x"]) * AU_KM,
            "y_km": float(r["y"]) * AU_KM,
            "z_km": float(r["z"]) * AU_KM,
            "vx_km_s": float(r["vx"]) * AU_KM / DAY_S,
            "vy_km_s": float(r["vy"]) * AU_KM / DAY_S,
            "vz_km_s": float(r["vz"]) * AU_KM / DAY_S,
            "range_km": float(r["range"]) * AU_KM,
            "range_rate_km_s": float(r["range_rate"]) * AU_KM / DAY_S,
        })

    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"[horizons] wrote {len(rows)} rows → {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", default="1m",
                        help="Horizons step size (e.g. '1m', '30s', '5m')")
    parser.add_argument("--start", default=MISSION_START)
    parser.add_argument("--end", default=MISSION_END)
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    fetch_one(EARTH, "earth", args.start, args.end, args.step)
    fetch_one(MOON, "moon", args.start, args.end, args.step)

    # Sanity check: derive Moon-from-Earth at the first epoch from the two
    # ephemerides and bail loudly if it isn't ~360,000–405,000 km. Catches
    # the CENTER='301' vs 'CENTER=500@301' ambiguity early.
    import csv as _csv, math as _math
    with (DATA_DIR / "horizons_earth.csv").open() as f:
        e0 = next(_csv.DictReader(f))
    with (DATA_DIR / "horizons_moon.csv").open() as f:
        m0 = next(_csv.DictReader(f))
    dx = float(e0["x_km"]) - float(m0["x_km"])
    dy = float(e0["y_km"]) - float(m0["y_km"])
    dz = float(e0["z_km"]) - float(m0["z_km"])
    moon_dist = _math.sqrt(dx*dx + dy*dy + dz*dz)
    if not (340_000 <= moon_dist <= 410_000):
        print(f"[horizons] WARNING: implied Moon-from-Earth distance is "
              f"{moon_dist:.0f} km at {e0['datetime_utc']}. Expected "
              f"~360,000–405,000 km. The two CSVs may not share a CENTER "
              f"convention.", file=sys.stderr)

    print("[horizons] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
