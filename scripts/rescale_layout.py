#!/usr/bin/env python3
"""
Derive the metre-scale "mission" Foxglove layout from the canonical megameter
layout, `layout/artemis-ii.json`.

`layout/artemis-ii.json` is the hand-edited source of truth, authored at the
**megameter** scene scale (1 scene unit = 1e6 m) that the default `build_mcap.py`
build uses. The `build_mcap.py --mission-scale` build instead expresses the
scene in real **metres** (1 unit = 1 m), so every distance-flavoured value in
the layout has to be multiplied by 1e6 to render at the same visual scale.
This script does exactly that, writing `layout/artemis-ii-mission.json`.

Workflow: edit `artemis-ii.json` (the Mm default), then re-run this script to
regenerate the matching mission-scale layout.

It's a curated traversal: only the keys we know are world-unit flavoured are
scaled. Everything else (per-pixel line widths, quaternions/colors/RPY,
point-size hints, panel splits, image-panel cameraState, etc.) is passed
through untouched.

Usage:
    scripts/rescale_layout.py                       # artemis-ii.json → artemis-ii-mission.json
    scripts/rescale_layout.py --factor 1e6          # explicit (metres per source unit)
    scripts/rescale_layout.py --input … --output …  # custom paths
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "layout" / "artemis-ii.json"
DEFAULT_OUTPUT = ROOT / "layout" / "artemis-ii-mission.json"
# Metres per source (megameter) scene unit. Multiplying the Mm layout by this
# converts it to the metre-scale mission layout.
DEFAULT_FACTOR = 1e6


def _scale_number(v: Any, k: float) -> Any:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v * k
    return v


def _scale_list(v: Any, k: float) -> Any:
    if isinstance(v, list):
        return [_scale_number(x, k) for x in v]
    return v


def rescale(layout: dict, k: float) -> dict:
    """Return a deep copy of `layout` with every distance-flavoured value
    multiplied by `k`. Touches:

      For each 3D panel (`configById["3D!*"]`):
        - `cameraState.distance`, `near`, `far`
        - `cameraState.targetOffset` (array of 3)
        - `scene.transforms.axisSize`
        - `topics[*].distance`, `topics[*].width`
        - `topics[*].arrowScale` (array of 3)
        - layers of type `foxglove.Grid`: `size`

    Image-panel `cameraState` (`distance`, `near`, `far`) is in the
    image's own NDC-ish space, NOT scene units — left alone.
    """
    out = copy.deepcopy(layout)

    for panel_id, panel_cfg in out.get("configById", {}).items():
        if not isinstance(panel_cfg, dict):
            continue
        if not panel_id.startswith("3D!"):
            continue

        cs = panel_cfg.get("cameraState", {})
        for key in ("distance", "near", "far"):
            if key in cs:
                cs[key] = _scale_number(cs[key], k)
        if "targetOffset" in cs:
            cs["targetOffset"] = _scale_list(cs["targetOffset"], k)

        transforms = panel_cfg.get("scene", {}).get("transforms", {})
        if "axisSize" in transforms:
            transforms["axisSize"] = _scale_number(transforms["axisSize"], k)

        for topic_cfg in panel_cfg.get("topics", {}).values():
            if not isinstance(topic_cfg, dict):
                continue
            for key in ("distance", "width"):
                if key in topic_cfg:
                    topic_cfg[key] = _scale_number(topic_cfg[key], k)
            if "arrowScale" in topic_cfg:
                topic_cfg["arrowScale"] = _scale_list(topic_cfg["arrowScale"], k)

        for layer_cfg in panel_cfg.get("layers", {}).values():
            if not isinstance(layer_cfg, dict):
                continue
            if layer_cfg.get("layerId") == "foxglove.Grid" and "size" in layer_cfg:
                layer_cfg["size"] = _scale_number(layer_cfg["size"], k)

    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                   help="Megameter source layout (default: layout/artemis-ii.json).")
    p.add_argument("--factor", type=float, default=DEFAULT_FACTOR, metavar="K",
                   help="Multiply every distance-flavoured value by K (metres "
                        "per source scene unit). Default 1e6 (Mm → metres).")
    p.add_argument("--output", type=Path, default=None,
                   help="Output layout path. Defaults to "
                        "layout/artemis-ii-mission.json.")
    args = p.parse_args()

    if args.factor <= 0:
        print("ERROR: --factor must be positive", file=sys.stderr)
        return 1
    if args.factor == 1.0:
        print("ERROR: --factor 1.0 would just copy the input. Refusing.",
              file=sys.stderr)
        return 1

    if args.output is None:
        args.output = DEFAULT_OUTPUT

    layout = json.loads(args.input.read_text())
    rescaled = rescale(layout, args.factor)
    args.output.write_text(json.dumps(rescaled, indent=2) + "\n")
    print(f"[rescale] {args.input.name} → {args.output} (× {args.factor:g})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
