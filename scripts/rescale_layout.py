#!/usr/bin/env python3
"""
Derive a scene-unit-rescaled Foxglove layout from the canonical
`layout/artemis-ii.json` baseline.

`build_mcap.py --scene-unit-m K` divides every distance-flavoured value
that crosses into a 3D channel by K. For Foxglove to render the scaled
MCAP at the same visual scale it renders the metres MCAP, the matching
distance-flavoured values in the layout JSON have to be divided by the
same K — otherwise the camera starts ~120 Mm away from a scene whose
geometry now lives inside a ±500-unit box, frustum widths overshoot
their topics' scenes, etc.

This is a curated traversal: only the keys we know are world-unit
flavoured are rescaled. Everything else (per-pixel line widths,
quaternions/colors/RPY, point-size hints, panel splits, indicator
font-sizes, etc.) is passed through untouched. The produced layout is
byte-identical to a hand edit but won't drift if we re-rescale after
the baseline moves.

Usage:
    scripts/rescale_layout.py                       # 1e6, default I/O
    scripts/rescale_layout.py --factor 1e3          # km variant
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


# After the proportional rescale, force these layout keys to specific physical
# values (expressed in metres). Use this for keys whose literal scaling
# preserves the proportion but breaks visibility — typically line widths that
# were set near-invisible in the metres baseline because the metres-scale
# worldUnits rendering path was broken, but which we want readable at scaled
# builds where the path works.
#
# Each entry: (panel_id_prefix, key_path_inside_panel_cfg, value_in_metres).
_PHYSICAL_OVERRIDES_M: list[tuple[str, tuple[str, ...], float]] = [
    # /camera/all/calibration.width — Foxglove's native frustum line width
    # in scene units (worldUnits-mode three.js LineSegments2). The baseline
    # has it at 1 m, deliberately subpixel because the worldUnits path
    # silently fails at metre-orbital scale (the same precision wall that
    # made us add the /camera/marker SceneEntity workaround). At reasonable
    # scene-unit factors that path comes back to life, so force the line to
    # ~50 km of physical width so it reads at any zoom level.
    ("3D!", ("topics", "/camera/all/calibration", "width"), 50_000.0),
    # /camera/all/calibration.distance — frustum length, kept short in the
    # baseline (80 km) for the same reason. Match the /camera/marker
    # SceneEntity's CAMERA_MARKER_DISTANCE_M (8 Mm) so the two visuals
    # overlay cleanly when both render.
    ("3D!", ("topics", "/camera/all/calibration", "distance"), 8_000_000.0),
]


def _suffix_for(factor: float) -> str:
    pretty = {1e3: "km", 1e6: "Mm", 1e9: "Gm"}.get(factor)
    return pretty or f"unit{factor:g}m"


def _scale_number(v: Any, k: float) -> Any:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / k
    return v


def _scale_list(v: Any, k: float) -> Any:
    if isinstance(v, list):
        return [_scale_number(x, k) for x in v]
    return v


def rescale(layout: dict, k: float) -> dict:
    """Return a deep copy of `layout` with every distance-flavoured value
    divided by `k`. Touches:

      For each 3D panel (`configById["3D!*"]`):
        - `cameraState.distance`, `near`, `far`
        - `cameraState.targetOffset` (array of 3 metres)
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

    _apply_physical_overrides(out, k)
    return out


def _apply_physical_overrides(layout: dict, k: float) -> None:
    """Mutate `layout` in place: walk every `_PHYSICAL_OVERRIDES_M` entry
    and assign the override (converted from metres to scene units) to the
    matching key path. Silently skips entries whose key path doesn't
    exist (so the override list can be a superset of any one layout)."""
    for panel_prefix, path, metres in _PHYSICAL_OVERRIDES_M:
        for panel_id, panel_cfg in layout.get("configById", {}).items():
            if not isinstance(panel_cfg, dict):
                continue
            if not panel_id.startswith(panel_prefix):
                continue
            cur: Any = panel_cfg
            for segment in path[:-1]:
                if not isinstance(cur, dict) or segment not in cur:
                    cur = None
                    break
                cur = cur[segment]
            if isinstance(cur, dict) and path[-1] in cur:
                cur[path[-1]] = metres / k


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                   help="Baseline layout to rescale (default: layout/artemis-ii.json).")
    p.add_argument("--factor", type=float, default=1e6, metavar="K",
                   help="Divide every distance-flavoured value by K. Should "
                        "match the value passed to "
                        "`build_mcap.py --scene-unit-m`. Default 1e6.")
    p.add_argument("--output", type=Path, default=None,
                   help=("Output layout path. Defaults to "
                         "layout/artemis-ii-<suffix>.json where <suffix> is "
                         "km/Mm/Gm or unit<K>m."))
    args = p.parse_args()

    if args.factor <= 0:
        print("ERROR: --factor must be positive", file=sys.stderr)
        return 1
    if args.factor == 1.0:
        print("ERROR: --factor 1.0 would just copy the input. Refusing.",
              file=sys.stderr)
        return 1

    if args.output is None:
        args.output = ROOT / "layout" / f"artemis-ii-{_suffix_for(args.factor)}.json"

    layout = json.loads(args.input.read_text())
    rescaled = rescale(layout, args.factor)
    args.output.write_text(json.dumps(rescaled, indent=2) + "\n")
    print(f"[rescale] {args.input.name} → {args.output} (÷ {args.factor:g})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
