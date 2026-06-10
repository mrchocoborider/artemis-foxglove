# Artemis II — Foxglove

A Foxglove visualization of the Artemis II mission (April 1–11, 2026).
Pulls trajectory data from JPL Horizons and photo metadata from Hank Green's
[Artemis-Timeline](https://github.com/hankmt/Artemis-Timeline) project, fuses
them into a single MCAP file on a synchronized timeline, and ships a Foxglove
layout to play it all back.

Inspired by [artemistimeline.com](https://artemistimeline.com/). All photo and
trajectory data is publicly available NASA / public-domain content.

## What you get

When you open the generated MCAP with the included layout, you get a **tabbed
workspace** (Visuals / Data) with a custom **Photo Stepper** panel docked
underneath.

**Visuals tab**

- **3D panel** — Earth, Moon, and Orion in J2000 ECI. The spacecraft is the
AROW mesh (a URDF parented to the `orion` frame via a `/tf` `earth → orion`
transform), so it moves as you scrub the timeline. An orange polyline
(`/orion/trail`) shows the full mission arc, a star field
(`/scene/stars`) backdrops the scene, and a single camera frustum marks
where the current on-Orion photo was taken.
- **Image panel** — the current photo. Every photo (Nikon D5, GoPro, iPhone,
exterior, crew, ground) is multiplexed onto one `/camera/all/image` topic in
chronological order, so the panel never sits empty waiting for a specific
camera's next shot.

**Data tab**

- **Plot panel** — distance from Earth and distance from Moon over the full
mission (km), read from `/orion/state`.
- **State Transitions** — a Gantt-style strip of crew activity
(`/orion/activity.label`): sleep / exercise / science / piloting / suit-ops /
observation / reentry-prep bands, transcribed from the NASA Artemis II
timeline.
- **Indicator** — the latest mission milestone (`/milestones.name`), held
until the next one fires: Pre-launch → LIFTOFF → MECO + STAGE SEP → TLI →
Lunar flyby → Apogee → Entry interface → Splashdown.

**Photo Stepper** (custom extension in `extension/`) — step through the photos
one at a time or run a slideshow; each step seeks the player so the 3D panel,
plots, and image all snap to that photo's capture time. Forward/back (`→`/`j`,
`←`/`k`), play/pause (`Space`), first/last (`Home`/`End`), an adjustable
slideshow interval, a per-camera filter, and a metadata readout (filename,
camera, caption, full-res URL, UTC, "N of M"). It subscribes to `/photo/meta`.

The build also writes an `/events` Log topic (one entry per photo); it isn't in
the default layout, but you can add a Log panel bound to it to click-to-seek
through the album.

## Pipeline

```
   JPL Horizons API            hankmt/Artemis-Timeline                 Cloudflare R2
 (target -1024,             photos.js + activity timeline           artemistimeline.com/web
  ctr 399/301)                    │            │                            │
       │                          ▼            ▼                            ▼
       ▼                   fetch_photos.py  fetch_schedule.py   ◄── downloads & resizes
 fetch_horizons.py                │            │                    photos
       │                          │            │
       └───────────────┬──────────┴────────────┘
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

# 3. Crew activity timeline (drives the State Transitions strip)
python scripts/fetch_schedule.py

# 4. Build the MCAP
python scripts/build_mcap.py

# Result: output/artemis-ii.mcap (open in Foxglove, then File > Import Layout)
```

`scripts/fetch_attitude.py` also exists — it scrapes Orion attitude quaternions
from archived AROW telemetry into `data/attitude.json` — but the build does not
consume it yet (the `earth → orion` transform still logs an identity
quaternion; see the Attitude note below).

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
│   ├── fetch_horizons.py    JPL Horizons → data/horizons_{earth,moon}.csv
│   ├── fetch_photos.py      photos.js + R2 photos → data/photos_meta.json + data/web/*.jpg
│   ├── fetch_schedule.py    activity timeline → data/schedule.json
│   ├── fetch_attitude.py    archived AROW telemetry → data/attitude.json (not yet wired in)
│   ├── build_mcap.py        all of the above → output/artemis-ii.mcap (Mm scale by default)
│   └── rescale_layout.py    artemis-ii-mission.json → artemis-ii.json (÷ scene unit)
├── models/
│   ├── orion.urdf               AROW glb mesh URDF (default)
│   ├── orion.glb                vendored AROW mesh
│   └── orion-primitives.urdf    primitives-only fallback URDF
├── extension/               Photo Stepper Foxglove panel (TypeScript)
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

**Mission window.** The build keeps everything between `MISSION_WINDOW_START`
(**2026-04-01 17:00 UTC**, ~5.5 h before the 22:35:25 UTC liftoff) and
`MISSION_WINDOW_END` (**2026-04-11 22:00 UTC**, ~22 h after the 00:07:27 UTC
splashdown). The window is deliberately wider than the Horizons ephemeris so it
captures Hank's pre-launch and recovery photos (suit-up, walkout, liftoff, and
the post-splashdown hours), none of which are in the trajectory file.

The file opens at `MISSION_WINDOW_START`, but JPL Horizons only starts tracking
~3.5 h post-liftoff (its first row is ~2026-04-02 02:00 UTC; it doesn't model
the boost phase). To keep the 3D panel from opening empty, `write_pre_horizons_anchor()`
parks Orion at the first Horizons position with a one-shot `earth → orion`
transform, so pre-launch on-Orion photos have somewhere to project and the
timeline transitions smoothly into real ephemeris once Horizons takes over.
This is a stylization — Orion is actually still on the pad during that window.

**Frame.** Trajectory is in J2000 / ICRF (NASA's "Mean of J2000" inertial
frame, effectively fixed to distant quasars). `earth` is the root frame;
`moon` and `orion` are child frames positioned from Horizons via `/tf`
(`earth → moon`, `earth → orion`), and a single `camera` frame hangs off
`orion` for the photo frustum. The trajectory, plots, and state are all
Earth-relative.

**Units.** Internally the build authors every 3D quantity (positions, sphere
sizes, frame translations, camera distances) in **meters** — Horizons returns
km, so it multiplies by 1000 at the boundaries that feed the 3D / TF channels.
It then divides everything that crosses into a 3D channel by the scene unit (see
[Scene scale](#scene-scale)); at the **megameter** default that means the scene
is expressed in units of 1e6 m. JSON state messages (`/orion/state`) are *not*
scaled — they stay in km / km/s so the plot panel reads naturally.

So the values you see in the default `layout/artemis-ii.json` are in megameters:
Earth's radius is ~6.378 units, the Moon orbits at ~384 units, Orion's apogee is
~413 units, and the 3D panel's `near`/`far` are `0.001` / `5000`. (In the
metre-scale `artemis-ii-mission.json` baseline the same values are ×1e6 — Earth
radius 6,378,137 m, `near` 1000, `far` 5e9.) The megameter default keeps WebGL's
Float32 vertex/depth math well-conditioned across the whole ±500-unit trajectory;
the metre baseline is where that math (and Foxglove's worldUnits line/frustum
rendering) starts to break down at orbital distance.

**3D panel: display frame + follow mode.** The included layout sets:

```
followTf:    "orion"
followMode:  "follow-position"
distance:    ~120 units (~120,000 km from Orion, in the default Mm layout)
```

…which centers the spacecraft and lets Earth, the Moon, and the trajectory
trail flow past as the timeline advances. `follow-position` (instead of
`follow-pose`) keeps your camera orientation fixed as you scrub — useful
since Orion has no real attitude data yet (see Attitude note below).

That distance is chosen so the stylized spacecraft reads at a comfortable size
(it's a ~20 Mm-wide map icon — see the URDF section below) while still showing a
good chunk of the trajectory. Zoom out to see Earth + the full arc at once. If
you'd rather watch the whole mission from a fixed Earth viewpoint, set
`followTf: "earth"`, `followMode: "follow-none"`, and bump `distance` up to
~600 units (~600 Mm) so the Moon's orbit fits in frame.

The orange line is the entire ~9-day trajectory, logged once at mission start as
a single polyline in the `earth` frame. It's not "where Orion has been" — it's
the full pre-computed arc, so you can see where it's headed too. The position
that tracks along it is the URDF spacecraft model (parented to the `orion`
frame). A redundant orange position arrow is published on `/orion/pose` but
ships hidden in the default layout; toggle it on if you want the
artemistimeline.com-style marker.

**Attitude.** As of June 2026 the build logs no real attitude: the `/tf`
`earth → orion` transform carries a `Quaternion(0,0,0,1)` placeholder
(`IDENTITY_QUAT`), which is why the layout uses `follow-position` rather than
`follow-pose`. The swap-in point is inside `write_transforms_and_state`.

There are two ways to get real attitude, neither wired in yet:

- **SPICE CK kernels** — still not on NAIF. `naif.jpl.nasa.gov/pub/naif/` has
no `ARTEMIS2/` directory, and NAIF typically archives operational kernels
6–9 months after acquisition (splashdown was ~2 months ago). Realistic ETA
for public CK (attitude) kernels is **late 2026 → mid 2027**. The SPK Horizons
uses internally for `-1024` already exists but isn't redistributed separately.
- **Archived AROW telemetry** — `scripts/fetch_attitude.py` scrapes Orion
attitude quaternions (plus position and rates) from AROW's live-telemetry
endpoint as captured by the Wayback Machine, writing `data/attitude.json`.
This is already runnable; what's missing is the `build_mcap.py` glue to
interpolate those quaternions onto the trajectory timeline and log them
instead of `IDENTITY_QUAT`.

**Photos.** Every photo is a JPEG `CompressedImage` on a single multiplexed
topic, `/camera/all/image`, in chronological order, so the Image panel stays
populated continuously instead of waiting on any one camera's next shot.
On-Orion photos are stamped with the shared `camera` `frame_id`; ground photos
(KSC tower / ascent tracking) are frameless (`""`) since they aren't bolted to
the spacecraft. There's one shared frustum on `/camera/all/calibration`,
re-emitted at every photo timestamp so the 3D panel has a fresh calibration
wherever you seek. (A handful of NASA ground JPEGs are saved landscape but are
actually portrait; the build re-encodes those upright — see `_PHOTO_ROTATIONS_CCW`.)
Original-resolution images stay on Cloudflare R2 and are linked via
`/photo/meta`; we embed a ~1280 px web copy to keep the MCAP playable. Of the
~540 entries in Hank's data file, ~480 land in the file (disabled training shots
are skipped); pass `--include-disabled-photos` to `build_mcap.py` to keep
everything.

**Camera frustum (faked intrinsics).** None of the published photo metadata
includes calibration data, and the real mounting poses of Hank's cameras (a mix
of crew DSLRs, phones, GoPros, and exterior payloads) aren't published. Rather
than fake five different rigs, the build emits **one shared frustum**: a single
`CameraCalibration` on `/camera/all/calibration` (a generic 4032×3024 sensor at
a 60° horizontal FOV — `CAMERA_HFOV_DEG`) attached to a single `camera` frame.
That frame is parked ~5 Mm (`CAMERA_RADIUS_M`) out from Orion's origin and
pointed back inward so the cone projects into open space instead of being buried
in the URDF body. Treat it as illustrative — "the photo was taken from Orion" —
not as photogrammetry. Ground photos get no frame and no calibration.

Because the image and calibration topics share the `/camera/all/` prefix (and
the layout points `/camera/all/image`'s `cameraInfoTopic` at the calibration
with `planarProjectionFactor: 1`), Foxglove's 3D panel can project the current
image onto the frustum's far plane; the dedicated Image panel shows it full-size
either way. A second, plain-lines fallback frustum is drawn on `/camera/marker`
(a `SceneEntity` of `LinePrimitive`s with screen-pixel line width) because
Foxglove's native worldUnits frustum rendering produces degenerate geometry at
orbital scene coordinates — the same Float32 wall the megameter scene scale
exists to dodge. The frustum is ~8 Mm long (`CAMERA_MARKER_DISTANCE_M`), sized
comparably to the URDF "map icon" so it reads at the default zoom.

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
~120,000 km camera distance. At true scale Orion is ~9 m tall — at default
zoom that's a fraction of a pixel. Rather than make the user manually zoom
from ~120,000 km down to ~50 m every time they open the file, we draw the
spacecraft as a "map icon" sized comparably to Earth's radius. The two
URDFs encode that scale very differently (both authored in metres, then
divided by the scene unit at build — see below):

- `models/orion.urdf` (mesh) is a single `<mesh>` visual using
`<mesh scale="7000000 …"/>`. The AROW glb is normalized to a unit cube (native
AABB ≈ 0.88 × 0.85 × 1.0 *units*, **not** meters), so the scale is in glb-native
units: `7,000,000` produces a ~7 Mm body (about Earth's radius — so the
spacecraft sits comfortably alongside Earth without dominating it). The solar
arrays aren't URDF primitives here; they're the four deployed SAWs baked into
the glb. At build time the script reads `models/orion.glb`, applies the tuned
per-SAW deployment transforms (`_AROW_PANEL_TRANSFORMS`, cached processed copy
at `data/cache/orion-deployed.glb`), then base64-embeds the result directly into
the URDF text. Pass `--no-deploy-panels` to leave the arrays folded against the
hull (not recommended for distributable MCAPs — the bare `orion.glb` filename
can't be resolved from the `/orion/urdf` topic).
- `models/orion-primitives.urdf` encodes dimensions directly in metres
(e.g. `length="3700000"` means 3.7 Mm). At those values the body is
~6 Mm tall and the panels span ~13 Mm wingspan. (For the mesh URDF the scale
is applied via the URDF `<mesh scale="…"/>` attribute; the glb file itself is
unscaled.)

**Scene-unit rescale.** Both URDFs are authored in metres, but
`_rescale_urdf_xml()` divides every distance-flavoured attribute (`<mesh
scale>`, `<origin xyz>`, `<box size>`) by the scene unit before embedding, so
the URDF matches the rest of the scene. In the default megameter build the
mesh `scale="7000000"` becomes `7` and the primitive body becomes ~6 units
(~6 Mm). The camera-rig constants in `scripts/build_mcap.py` (`CAMERA_RADIUS_M`,
`CAMERA_MARKER_DISTANCE_M`) and the per-topic `distance`/`width` in the layout
live in the same metres-then-rescaled scale — change one knob, change them all.
The two visibility levers — the URDF/camera sizes and the layout's
`cameraState.distance` — trade off against each other; for a wider view of the
trajectory, bump `distance` up rather than shrinking the URDF.

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
`rpy="1.5708 0 0"` to the visual's `<origin>` to compensate. (The `/orion/pose`
arrow is a redundant position marker, but ships hidden in the default layout.)

## Navigating the timeline

The mission spans ~9 days, so 1× playback would take 9 days. Options, fastest
first:

1. **Photo Stepper** — jump photo-to-photo (or auto-advance as a slideshow).
Each step seeks the player, so all panels follow. This is the intended way to
tour the album. See the keyboard shortcuts under "What you get".
2. **Drag the timeline scrubber** — jump anywhere instantly.
3. **Play** — the layout ships a default playback speed of **60×** (`playbackConfig.speed`),
so a real-time run still finishes in a few hours rather than nine days. Bump it
higher in the playback controls.
4. **Add a Log panel** bound to `/events` for click-to-seek on the headline
milestones / per-photo entries.

## Inspiration & sources

- [Artemis II Photo Timeline](https://artemistimeline.com/) and its
[GitHub repo](https://github.com/hankmt/Artemis-Timeline) — Hank Green
- [JPL Horizons](https://ssd.jpl.nasa.gov/horizons/) — trajectory ephemerides
- [Track NASA's Artemis II in Real Time](https://www.nasa.gov/missions/artemis/artemis-2/track-nasas-artemis-ii-mission-in-real-time/) — AROW
- [NAIF SPICE](https://naif.jpl.nasa.gov/naif/) — for future attitude work

