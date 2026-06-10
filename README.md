# Artemis II — Foxglove

A Foxglove visualization of the Artemis II mission (April 1–11, 2026).
Pulls trajectory data from JPL Horizons and photo metadata from Hank Green's
[Artemis-Timeline](https://github.com/hankmt/Artemis-Timeline) project, fuses
them into a single MCAP file on a synchronized timeline, and ships a Foxglove
layout to play it all back.

Inspired by [artemistimeline.com](https://artemistimeline.com/). All photo and
trajectory data is publicly available NASA / public-domain content.

## What you get

When you open the generated MCAP in Foxglove with the included layout:

- **3D panel** — Earth, Moon, and Orion plotted in J2000 ECI, with a polyline
trail of the spacecraft's full trajectory. Orion is positioned via a
`FrameTransform` so it moves as you scrub the timeline.
- **Image panel** — one always-on panel showing whichever photo is current.
All photos (Nikon D5, GoPro, iPhone, exterior, crew, ground) are
multiplexed onto a single `/camera/all/image` topic in chronological
order so the panel never sits empty waiting for that specific camera's
next shot. Per-image `frame_id` still names the source camera, and each
on-Orion camera continues to publish a `CameraCalibration` so the 3D
panel renders frustums per camera bolted to the spacecraft.
- **Plot panel** — distance from Earth, distance from Moon, and speed over the
full mission (in km / km/s).
- **Indicator** — current mission phase (Launch / Outbound / Lunar Flyby /
Return Coast / EDL) and the official mission elapsed time.
- **State Transitions** — a horizontal Gantt-style strip of `/orion/phase`
and `/milestones`, so the timeline reads as colored bands of "where in the
mission are we right now?".
- **Log panel** — `/events`: now one entry per photo (camera, photographer,
caption, location). Click an entry to seek the player to that photo's
capture time — handy for stepping through the album without waiting for
real-time playback.
- **Raw Messages** — live readout of the current photo's metadata.

## Pipeline

```
       JPL Horizons API                 hankmt/Artemis-Timeline                Cloudflare R2
   (target -1024, ctr 399/301)              photos.js                       artemistimeline.com/web
            │                                  │                                    │
            ▼                                  ▼                                    ▼
  fetch_horizons.py                    fetch_photos.py  ◄──── downloads & resizes photos
            │                                  │
            └────────────────┬─────────────────┘
                             ▼
                      build_mcap.py
                             │
                             ▼
                output/artemis-ii.mcap   ←── open in Foxglove with layout/artemis-ii.json
```

## Setup

Requires Python 3.10+.

```bash
cd projects/artemis-foxglove
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run the pipeline

```bash
# 1. Trajectory: Orion (-1024) relative to Earth (399) and Moon (301)
python scripts/fetch_horizons.py

# 2. Photos: metadata + downsampled JPEGs (~1280px longest edge)
python scripts/fetch_photos.py

# 3. Build the MCAP
python scripts/build_mcap.py

# Result: output/artemis-ii.mcap (open in Foxglove, then File > Import Layout)
```

### Scene scale

By default the build runs in **megameters** (1 scene unit = 1e6 m). The
trajectory then spans ~500 units instead of ~5e8 metres, which keeps WebGL
Float32 vertex/depth math well-conditioned and lets Foxglove's worldUnits-mode
line and frustum rendering behave. The default `output/artemis-ii.mcap` pairs
with `layout/artemis-ii.json`.

To build the original **mission scale** (scene in real metres), pass
`--mission-scale`:

```bash
python scripts/build_mcap.py --mission-scale
# → output/artemis-ii-mission.mcap, open with layout/artemis-ii-mission.json
```

`layout/artemis-ii-mission.json` is the hand-edited metre-scale baseline;
`layout/artemis-ii.json` (the default Mm layout) is generated from it by
`scripts/rescale_layout.py`. If you tweak the mission-scale layout, re-run that
script to regenerate the default:

```bash
python scripts/rescale_layout.py            # → layout/artemis-ii.json (÷1e6)
```

(`--scene-unit-m K` / `rescale_layout.py --factor K` produce other scales, e.g.
`1e3` for kilometers.)

The full mission is ~10 days at 1-minute trajectory sampling and ~hundreds of
photos. End-to-end build typically takes 10–30 minutes depending on your
connection (photos dominate).

## File layout

```
artemis-foxglove/
├── README.md                this file
├── requirements.txt         Python deps
├── scripts/
│   ├── fetch_horizons.py    JPL Horizons → data/horizons_earth.parquet
│   ├── fetch_photos.py      photos.js + R2 photos → data/photos_meta.json + data/web/*.jpg
│   ├── build_mcap.py        Both → output/artemis-ii.mcap (Mm scale by default)
│   └── rescale_layout.py    artemis-ii-mission.json → artemis-ii.json (÷ scene unit)
├── data/                    intermediate artifacts (gitignored)
├── output/                  generated MCAPs (gitignored)
└── layout/
    ├── artemis-ii.json          default Mm-scale layout (generated)
    └── artemis-ii-mission.json  hand-edited metre-scale baseline
```

## Notes on the data

**Times.** JPL Horizons returns UTC. Hank's `photos.js` uses EDT (UTC−4).
The build script normalizes everything to UTC nanoseconds before writing to
MCAP. Photos are also clamped to a mission window (configurable in
`build_mcap.py`) and respect Hank's `enabled` flag.

**Mission start time.** `MISSION_WINDOW_START` is pinned to **2026-04-02
02:00 UTC** — the first row of the JPL Horizons SPK. That's about 3.5 hours
post-liftoff (Horizons doesn't track the boost phase). Foxglove always opens
a file at `message_start_time`, so this is the earliest moment the 3D and
plot panels actually have data. Anything earlier (pre-launch ground photos,
the LIFTOFF / MECO events, the first few crew snaps) is filtered out so the
file doesn't open in a dead zone. The trade-off: if you want the launch
sequence in the file, drop `MISSION_WINDOW_START` back to ~22:00 UTC on
04-01 and accept that the first ~4 hours of the timeline will only have
photos to look at.

**Frame.** Trajectory is in J2000 / ICRF (NASA's "Mean of J2000" inertial
frame, effectively fixed to distant quasars). Earth-centered for the bulk of
the visualization, with Moon-centered ephemeris available as a sub-frame
during the flyby.

**Units.** Foxglove's 3D panel uses **meters** for all positions, sphere
sizes, frame translations, and camera distances. Horizons returns km; the
build script multiplies by 1000 only at the boundaries that feed the 3D /
TF channels. JSON state messages (`/orion/state`) stay in km/km/s so the
plot panel reads naturally. If you tweak the layout's camera or grid size,
remember those are in meters: Earth radius is 6,378,137 m, the Moon orbits
at ~3.84 × 10⁸ m, and Orion's apogee is ~4.3 × 10⁸ m.

The 3D panel handles this scale just fine — WebGL Float32 has ~30 m of
precision out at lunar distance, and with `near=1000, far=5e9` the depth
buffer leaves ~400 m of resolution at the far end (no z-fighting between
any of our primitives). The 500 m limit you may be remembering is the Map
panel, which uses lat/lon and is unrelated.

**3D panel: display frame + follow mode.** The included layout sets:

```
followTf:    "orion"
followMode:  "follow-position"
distance:    30,000 km from Orion
```

…which centers the spacecraft and lets Earth, the Moon, and the trajectory
trail flow past as the timeline advances. `follow-position` (instead of
`follow-pose`) keeps your camera orientation fixed as you scrub — useful
since Orion has no real attitude data yet (see SPICE note below).

The 30,000 km default is chosen so the stylized spacecraft fills a healthy
fraction of the frame (it's a ~20 Mm-wide map icon — see the URDF section
below). You will need to zoom out to see Earth + the full trajectory at the
same time. If you'd rather watch the whole mission from a fixed Earth
viewpoint, set `followTf: "earth"` and `followMode: "follow-none"`, and bump
`distance` up to ~600,000,000 m so the Moon's orbit fits in frame.

The orange line you see is the entire 9-day trajectory, logged once at
mission start as a single polyline in the `earth` frame. It's not "where
Orion has been" — it's the full pre-computed arc, so you can see where
it's headed too. The arrow at the spacecraft's current position is what
moves along it (matches the artemistimeline.com viewer).

**Attitude (SPICE CK).** Still not available as of mid-May 2026.
`naif.jpl.nasa.gov/pub/naif/` has no `ARTEMIS2/` directory and NAIF's policy
is that operational kernels are typically archived 6–9 months after data
acquisition — splashdown was only ~5 weeks ago. Realistic ETA for public
CK (attitude) kernels is **late 2026 → mid 2027**. The SPK that Horizons
uses internally for `-1024` already exists, but is not redistributed
separately. `build_mcap.py` keeps a `Quaternion(0,0,0,1)` placeholder on
the `/tf` `earth → orion` transform; the swap-in point for `spiceypy` is
inside `write_transforms_and_state`.

**Photos.** Every photo is stored as a JPEG `CompressedImage` message on a
single multiplexed topic, `/camera/all/image`, in chronological order. The
prior layout had one image topic per camera bucket (Nikon D5 / GoPro /
iPhone / crew / exterior / ground), which meant five Image panels each
sitting empty until that specific camera's first shot. The unified topic
keeps the Image panel populated continuously; per-image `frame_id` still
names the source camera (`camera_<bucket>` for on-Orion cameras, `""` for
ground), so frustum highlighting in the 3D panel still tracks which camera
took the active photo. The `/camera/<bucket>/calibration` topics remain
per-bucket because the 3D panel renders one frustum per calibration topic.
Original-resolution images remain on Cloudflare R2 and are linked via
`/photo/meta`; we embed a ~1280 px web copy to keep the MCAP playable. Pass
`--include-disabled-photos` to `build_mcap.py` if you want everything Hank's
data file exposes (including training shots).

**Camera frustums (faked intrinsics).** None of the published photo metadata
includes calibration data, so we synthesize plausible `CameraCalibration`
messages per bucket — a representative resolution and a horizontal FOV
matching each camera's optics (28° HFOV for the Nikon D5 with a 70-200mm,
65° for the iPhone main lens, 118° for the GoPro, etc.). Each on-Orion
camera also gets a static `orion → camera_<bucket>` transform with a
fictitious pose chosen so the frustums spread out around the body instead
of stacking on top of each other (see `CAMERAS` in `build_mcap.py`). Treat
the result as illustrative — "the photo was probably from somewhere over
here" — not as photogrammetry. The `ground` bucket (KSC tower / ascent
tracking shots) gets no transform and no calibration: it isn't bolted to
the spacecraft and shouldn't pretend to be. If real intrinsics or extrinsics
ever surface, drop them into `CAMERAS` and the rig will pick them up.

Note that Foxglove's 3D panel renders calibration topics as **wireframe
frustums** only — it doesn't project the corresponding image onto the
frustum's far face. The single Image panel handles pixel display; the 3D
panel just shows where each camera is pointed. (If image-into-frustum is what you
want, that'd be a feature request against the 3D panel rather than a build
script change.) Frustums are scaled to the same Earth-radius "map icon" size
as the URDF (~6-10 Mm cone length, set via per-topic `distance` and `width`
in the layout) so they read clearly at the default 30,000 km zoom without
dominating the frame.

**URDF.** Two flavors live in `models/`:

- `models/orion.urdf` (the **default**) — AROW body mesh + deployed solar
arrays. The `<mesh>` tag points at `models/orion.glb`, vendored in this
repo (originally extracted from NASA's "Track NASA's Artemis II in Real
Time" page / AROW, via
`[iandees/artemis-viewer](https://github.com/iandees/artemis-viewer)`).
Effectively the same spacecraft model NASA's own live tracker shows:
detailed CM + SM + engine bell + RCS thrusters + antennae. At build time
the script applies Ian Dees's per-SAW deployment transforms, then
base64-embeds the processed glb into the URDF text as a
`data:model/gltf-binary` URL. The MCAP is then fully self-contained —
Foxglove does not fetch anything at runtime. NASA's AROW imagery is covered
by the
[NASA Images and Media Usage Guidelines](https://www.nasa.gov/nasa-brand-center/images-and-media/);
the glb was extracted from AROW's Unity bundle and the deployment poses
were tuned by Ian Dees.
- `models/orion-primitives.urdf` (the **fallback**) — a primitives-only
stand-in (CM + adapter + SM + four solar panels). No network required; no
external dependency. Lower fidelity but always works. Build with this by
passing `--urdf models/orion-primitives.urdf` to `scripts/build_mcap.py`.

Both are scaled up so the spacecraft is legible at the layout's default
30,000 km camera distance. At true scale Orion is ~9 m tall — at default
zoom that's a fraction of a pixel. Rather than make the user manually zoom
from 30,000 km down to ~50 m every time they open the file, we draw the
spacecraft as a "map icon" sized comparably to Earth's radius. The two
URDFs encode that scale very differently:

- `models/orion.urdf` (mesh) uses `<mesh scale="7000000 …"/>`. The AROW glb
is normalized to a unit cube (native AABB ≈ 0.88 × 0.85 × 1.0 *units*,
**not** meters), so you can't transfer multipliers between the two URDFs:
the mesh scale is in glb-native units, and `7,000,000` produces a ~~7 Mm
body (about Earth's radius — so the spacecraft sits comfortably alongside
Earth without dominating it). The URDF's four primitive panel `<box>`
visuals are dimensioned in the same final URDF metres: each panel is
5 Mm radial × 2.5 Mm along the body axis × 50 km thick, attached at
the SM lateral surface (~~2.1 Mm) and extending out to ~7.1 Mm — wingspan
~14 Mm tip to tip. At build time the script reads `models/orion.glb`,
applies the tuned SAW deployment transforms (cached processed copy at
`data/cache/orion-deployed.glb`), then base64-embeds the result directly
into the URDF text. Pass `--no-deploy-panels` to skip SAW deployment
(not recommended for distributable MCAPs — the bare `orion.glb` filename
cannot be resolved from the `/orion/urdf` topic).
- `models/orion-primitives.urdf` encodes dimensions directly in metres
(e.g. `length="3700000"` means 3.7 Mm). At those values the body is
~6 Mm tall and the panels span ~13 Mm wingspan. Same convention as Hank's artemistimeline.com viewer and
Foxglove's own position arrow. (For the mesh URDF the scale is applied via
the URDF `<mesh scale="…"/>` attribute; the glb file itself is unscaled.)
The matching `CAMERAS` table in `scripts/build_mcap.py` and the per-topic
`distance`/`width` for frustum rendering in `layout/artemis-ii.json` are
all dimensioned in the same scale — change one knob, change them all. The
two visibility levers — `<mesh scale>` / `CAMERAS` offsets and the layout's
`cameraState.distance` — trade off against each other; if you want a wider
view of the trajectory, bump `distance` up rather than shrinking the URDF.

The URDF is shipped **inside the MCAP** on an `/orion/urdf` topic with
`std_msgs/msg/String` schema, and the layout's URDF layer subscribes to it
via `sourceType: "topic"`. No external file paths, no per-machine
configuration — just opens the file and the spacecraft is there. (For the
mesh URDF, "the spacecraft is there" once the glb fetch completes —
typically a second or two over a normal connection.)

Topic name is deliberately **not** `/robot_description`: the 3D panel's URDF
subsystem special-cases that exact name in `shouldSubscribe`, short-circuiting
before the custom-layer subscription path. So if you publish on
`/robot_description`, no custom URDF layer ever subscribes and the layer
silently renders nothing — you have to manually toggle the built-in
topic-style URDF instance to visible. Any other topic name takes the
custom-layer branch and Just Works. (See
`app/packages/viz/src/panels/ThreeDeeRender/renderables/urdf/Urdfs.ts:888`
if you ever want to confirm the upstream behavior.)

The layout sets `meshUpAxis: "y_up"` so the standard glTF Y-up → ROS Z-up
conversion is applied to the mesh URDF; if you replace the glb with a Z-up
asset (some COLLADA / STL files), flip that to `z_up` or add an
`rpy="1.5708 0 0"` to the visual's `<origin>` to compensate. The orange
arrow on `/orion/pose` stays as a redundant always-visible position marker.

## Navigating the timeline

The mission spans ~9 days, so 1× playback would take 9 days. Some options:

1. **Drag the timeline scrubber** — fastest way to jump anywhere.

## Inspiration & sources

- [Artemis II Photo Timeline](https://artemistimeline.com/) and its
[GitHub repo](https://github.com/hankmt/Artemis-Timeline) — Hank Green
- [JPL Horizons](https://ssd.jpl.nasa.gov/horizons/) — trajectory ephemerides
- [Track NASA's Artemis II in Real Time](https://www.nasa.gov/missions/artemis/artemis-2/track-nasas-artemis-ii-mission-in-real-time/) — AROW
- [NAIF SPICE](https://naif.jpl.nasa.gov/naif/) — for future attitude work

