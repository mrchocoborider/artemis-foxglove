#!/usr/bin/env python3
"""
Build the Artemis II MCAP from the artifacts produced by fetch_horizons.py and
fetch_photos.py.

Channels written:
    /tf                              FrameTransform   earth → orion / moon, orion → camera
    /orion/pose                      PoseInFrame      single pose, parented to earth
    /orion/trail                     SceneUpdate      polyline trail of the full mission
    /scene                           SceneUpdate      Earth + Moon spheres
    /orion/state                     json             distance_earth_km, distance_moon_km, speed_km_s
    /orion/activity                  json             crew activity label (single string-state per interval)
    /milestones                      json             current mission milestone (LIFTOFF, TLI, …)
    /events                          foxglove.Log     one entry per photo (camera, caption, photographer)
    /orion/urdf                      std_msgs/String  the Orion URDF, embedded for the URDF layer
    /camera/all/image                CompressedImage  unified photo stream (all cameras, chronological)
    /camera/all/calibration          CameraCalibration single synthesized frustum that all images attribute to
    /camera/marker                   SceneUpdate      manual fallback frustum drawn as line primitives
    /photo/meta                      json             metadata of currently-displayed photo

Frame hierarchy:
    earth (root, J2000 ECI)
    ├── moon
    └── orion

Units: Foxglove's 3D primitives (positions, sphere/cube sizes, camera distance)
are in **meters** by convention. Trajectory data from Horizons arrives in km;
we multiply by 1000 (KM_TO_M) at log time. JSON state messages stay in km for
human-readable plots — those don't go through the 3D panel.

Photos: we honor the `enabled` flag from Hank's photos.js (his admin UI uses it
to hide training shots / duplicates). One entry in particular — a 2025-01-30
Victor Glover training photo — would otherwise pull message_start_time back by
14 months, leaving the 3D panel empty when Foxglove opens the file at t0.
We also bound everything to a configurable mission window for safety.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import math
import random
import re
import struct
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import foxglove
from foxglove import Channel, Schema
from foxglove.channels import (
    CameraCalibrationChannel,
    CompressedImageChannel,
    FrameTransformChannel,
    LogChannel,
    PointCloudChannel,
    PoseInFrameChannel,
    SceneUpdateChannel,
)
from foxglove.messages import (
    CameraCalibration,
    Color,
    CompressedImage,
    FrameTransform,
    LinePrimitive,
    LinePrimitiveLineType,
    Log,
    LogLevel,
    ModelPrimitive,
    PackedElementField,
    PackedElementFieldNumericType,
    Point3,
    PointCloud,
    Pose,
    PoseInFrame,
    Quaternion,
    SceneEntity,
    SceneUpdate,
    SpherePrimitive,
    Timestamp,
    Vector3,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WEB = DATA / "web"
MODELS = ROOT / "models"

# All distances logged into 3D scene/frame channels are in METERS (Foxglove's
# convention for SceneEntity sizes, FrameTransform translations, camera
# near/far/distance, etc.). Horizons gives us km, so we multiply by KM_TO_M
# every time data crosses into the 3D channels.
KM_TO_M = 1000.0
EARTH_RADIUS_KM = 6378.137
MOON_RADIUS_KM = 1737.4
EARTH_RADIUS_M = EARTH_RADIUS_KM * KM_TO_M
MOON_RADIUS_M = MOON_RADIUS_KM * KM_TO_M

# Optional scene-unit downscale. SCENE_UNIT_M is the number of metres one 3D
# scene unit represents; default 1.0 keeps the scene in metres (status quo).
# Pass `--scene-unit-m 1e6` to work in megameters: the trajectory spans ~500
# units instead of 5e8 metres, which keeps Float32 vertex/depth math well-
# conditioned and lets Foxglove's worldUnits-mode line/frustum rendering
# behave (the workaround in CAMERA_MARKER_LINE_WIDTH_PX exists precisely
# because that path breaks at metre-scale orbital coordinates). Set in
# main() from the CLI; every coordinate that crosses into a 3D channel is
# routed through `_to_units` so the conversion is centralised.
#
# Plot/state values (`/orion/state.distance_earth_km`, etc.) are *not*
# scaled — those stay in real km for human-readable axes.
SCENE_UNIT_M: float = 1.0


def _to_units(meters: float) -> float:
    """Convert a value in metres to scene units."""
    return meters / SCENE_UNIT_M


def _scaled_default_output(scene_unit_m: float) -> Path:
    """Pick an output filename that reflects the scene scale, so a 1.0
    build and a megameter build can sit side-by-side without clobbering
    each other."""
    if scene_unit_m == 1.0:
        return ROOT / "output" / "artemis-ii.mcap"
    pretty = {1e3: "km", 1e6: "Mm", 1e9: "Gm"}.get(scene_unit_m)
    suffix = pretty or f"unit{scene_unit_m:g}m"
    return ROOT / "output" / f"artemis-ii-{suffix}.mcap"

# Mission window (UTC). Anything outside this window is discarded.
#
# Wider-than-Horizons by design: Hank's photo set covers crew suit-up,
# walkout, liftoff, and ~9 h of recovery — none of which are in the
# Horizons trajectory file (which runs from ~3.5 h post-liftoff through
# splashdown). To keep those 110+ photos in the file, we open the window
# T-5h30m before liftoff and close it ~21h54m after splashdown.
#
# Pre-Horizons gap (T-5h30m → first Horizons row at 2026-04-02 02:00):
# `write_pre_horizons_anchor()` emits a one-shot earth→orion TF using
# the first Horizons sample's position so Foxglove's 3D panel isn't
# empty during the pre-launch window. This is a stylization — orion is
# actually on the pad during this period, not in space — but it gives
# pre-launch on-Orion photos somewhere to project, and the timeline
# transitions smoothly into real ephemeris data once Horizons takes
# over. See the helper docstring for details.
#
# Post-Horizons gap (last Horizons row → MISSION_WINDOW_END): handled
# implicitly by Foxglove's sample-and-hold on the last TF + state
# values; orion sits at entry interface for the recovery hours.
MISSION_WINDOW_START = datetime(2026, 4, 1, 17, 0, 0, tzinfo=timezone.utc)
MISSION_WINDOW_END = datetime(2026, 4, 11, 22, 0, 0, tzinfo=timezone.utc)
LIFTOFF_UTC = datetime(2026, 4, 1, 22, 35, 25, tzinfo=timezone.utc)
SPLASHDOWN_UTC = datetime(2026, 4, 11, 0, 7, 27, tzinfo=timezone.utc)


# ─────────────────────── camera frustum ──────────────────────────
# We don't have real intrinsics or mounting poses for any of Hank's cameras
# — Hank's set is a mix of crew DSLRs, phones, GoPros, and exterior payloads
# whose actual placements aren't published. A single shared frustum (one
# camera frame, one CameraCalibration message) does the job:
#
#   - It marks "the photo currently on screen was taken from Orion" without
#     pretending to be metric.
#   - One frustum is far easier to see than five — earlier per-bucket
#     frustums were buried inside the URDF body or off-screen at large
#     orion-relative offsets, so the user could never spot them.
#   - The image and calibration topics share the `/camera/all/` prefix,
#     which lets Foxglove's 3D panel project the active image onto the
#     frustum's far plane (Images.ts uses `getTopicMatchPrefix` to pair
#     them).
#
# The frustum sits at the orion frame origin and points along orion +Z
# (URDF "up"). Frustum size is tuned in layout/artemis-ii.json (the
# `/camera/all/calibration` topic settings) and is in world meters.
#
# Foxglove camera convention (from foxglove.CameraCalibration):
#   camera-+X = image right, camera-+Y = image down, camera-+Z = into scene


def _intrinsics_from_fov(width: int, height: int, hfov_deg: float) -> list[float]:
    """Row-major 3x3 K for a pinhole camera with the given horizontal FOV."""
    fx = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    fy = fx
    cx, cy = width / 2.0, height / 2.0
    return [fx, 0.0, cx,  0.0, fy, cy,  0.0, 0.0, 1.0]


# Single shared camera frame for all on-Orion photos. The frame is parked
# CAMERA_RADIUS_M out from orion's origin along a direction in the URDF
# horizontal (X-Y) plane, and pointed back along that direction so the
# frustum projects outward into empty space — earlier setups put the
# apex at the orion origin where the URDF body would occlude most of the
# wireframe.
#
# Rotation composition (applied in world-frame, outermost first):
#   3. CAMERA_YAW_DEG around orion +Z — sweeps the whole rig (mounting
#      position + optical axis) around orion's "up" axis. 0° = +X side,
#      -90° = -Y side (90° CW as viewed from above), +90° = +Y side
#      (90° CCW). Flip the sign if it comes out the wrong way in your
#      viewport.
#   2. +90° around orion +Y — maps camera +Z onto orion +X so the
#      optical axis points outward (modulated by step 3).
#   1. CAMERA_ROLL_DEG around camera +Z — rolls the image plane around
#      the optical axis. -90° → 90° CCW in viewport.
CAMERA_FRAME = "camera"
CAMERA_RADIUS_M = 5_000_000.0
CAMERA_ROLL_DEG = -90.0
CAMERA_YAW_DEG = -90.0


def _quat_mul(a: tuple[float, float, float, float],
              b: tuple[float, float, float, float],
              ) -> tuple[float, float, float, float]:
    """Hamilton-product quaternion multiplication on (x,y,z,w) tuples;
    q = a*b applies b first then a in the world frame (= a then b in
    body-axis intrinsic order)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw*bx + ax*bw + ay*bz - az*by,
        aw*by - ax*bz + ay*bw + az*bx,
        aw*bz + ax*by - ay*bx + az*bw,
        aw*bw - ax*bx - ay*by - az*bz,
    )


_q_yaw_to_x = (0.0, math.sin(math.pi/4), 0.0, math.cos(math.pi/4))
_q_roll = (
    0.0, 0.0,
    math.sin(math.radians(CAMERA_ROLL_DEG) / 2),
    math.cos(math.radians(CAMERA_ROLL_DEG) / 2),
)
_q_orion_yaw = (
    0.0, 0.0,
    math.sin(math.radians(CAMERA_YAW_DEG) / 2),
    math.cos(math.radians(CAMERA_YAW_DEG) / 2),
)
# inner = roll then yaw-to-+X (existing behaviour);
# outer composition adds the orion-frame yaw on top so the entire
# camera frame revolves around orion +Z.
_q_inner = _quat_mul(_q_yaw_to_x, _q_roll)
_qx, _qy, _qz, _qw = _quat_mul(_q_orion_yaw, _q_inner)
CAMERA_ROTATION = Quaternion(x=_qx, y=_qy, z=_qz, w=_qw)

# Mount point in orion's frame: starts at (radius, 0, 0) and rotates
# with CAMERA_YAW_DEG around orion +Z so the apex tracks the optical
# axis. For yaw=0 → +X side; -90° → -Y side; +90° → +Y side.
_yaw_rad = math.radians(CAMERA_YAW_DEG)
CAMERA_TRANSLATION_M = (
    CAMERA_RADIUS_M * math.cos(_yaw_rad),
    CAMERA_RADIUS_M * math.sin(_yaw_rad),
    0.0,
)
CAMERA_W, CAMERA_H = 4032, 3024   # generic 4:3 stand-in
CAMERA_HFOV_DEG = 60.0            # medium-wide; pleasant frustum cone

# Frustum wireframe on /camera/marker — a SceneEntity LinePrimitive
# pyramid. We use *scale-invariant* line thickness (pixel-space) because
# three.js LineSegments2's worldUnits expansion path uses Float32
# position buffers and produces degenerate geometry at our 8 Mm scene
# scale (Cameras.ts's CameraCalibration frustum hits the same wall —
# that's why its yellow wireframe was never visible).
CAMERA_MARKER_DISTANCE_M = 8_000_000.0  # frustum length in scene meters
CAMERA_MARKER_LINE_WIDTH_PX = 1.0       # screen-pixel line thickness

# Per-photo CCW rotation overrides (degrees). A handful of NASA KSC
# ground photos were saved as 1280×853 landscape JPEGs even though
# their content is portrait — no EXIF Orientation tag is present, so
# the only way to display them upright is to re-encode the pixels.
# Anything not listed here is logged as-is. Values must be 90/180/270.
_PHOTO_ROTATIONS_CCW: dict[str, int] = {
    "KSC-20260401-PH-KLS01_0013.jpg": 90,
    "KSC-20260401-PH-KLS01_0040.jpg": 90,
    "KSC-20260401-PH-KLS01_0101.jpg": 90,
    "KSC-20260401-PH-KLS01_0106.jpg": 90,
    "KSC-20260401-PH-KLS01_0207.jpg": 90,
}


def _rotate_jpeg_ccw(data: bytes, degrees: int) -> bytes:
    """Re-encode ``data`` as a JPEG rotated CCW by ``degrees`` (must be a
    multiple of 90). PIL's ``rotate(angle)`` is mathematically CCW, so
    no sign flip is needed. Quality 90 keeps file size reasonable while
    preserving Hank's web-resolution photos."""
    if degrees % 90 != 0:
        raise ValueError(f"rotation must be multiple of 90, got {degrees}")
    if degrees % 360 == 0:
        return data
    from io import BytesIO
    from PIL import Image
    img = Image.open(BytesIO(data))
    rotated = img.rotate(degrees, expand=True)
    if rotated.mode != "RGB":
        rotated = rotated.convert("RGB")
    out = BytesIO()
    rotated.save(out, format="JPEG", quality=90, optimize=True)
    return out.getvalue()


# Mission milestones — see EVENTS below. The Indicator panel binds to
# `/milestones.name` (string-state, sample-and-hold) so the active
# milestone is always visible.
#
# Higher-resolution timeline annotation comes from `data/schedule.json`
# (NASA Artemis II Overview Timeline PDF, transcribed by Hank Green
# in hankmt/Artemis-Timeline). Run `scripts/fetch_schedule.py` once
# to populate it; `write_activity()` emits one /orion/activity message
# per interval boundary which renders as a single-line StateTransitions
# strip with sleep / piloting / science / suit-ops / etc. bands.

# Headline events for the /events Log topic. Foxglove's Log panel renders
# these as a clickable, scrollable list — much faster way to navigate the
# 10-day mission than waiting on real-time playback. Times for TLI / lunar
# closest approach / apogee come from the Horizons ephemeris itself; ascent
# milestones are nominal Artemis II planning values.
EVENTS: list[tuple[str, str, str, str]] = [
    # (iso_utc, level, name, message)
    ("2026-04-01T22:35:25Z", "info",  "LIFTOFF",          "SLS lifts off Pad 39B."),
    ("2026-04-01T22:43:30Z", "info",  "MECO + STAGE SEP", "Core stage main engine cutoff and separation."),
    ("2026-04-03T00:00:00Z", "info",  "TLI",              "Trans-lunar injection at perigee (~7,700 km)."),
    ("2026-04-06T23:02:00Z", "info",  "Lunar flyby",      "Closest approach to the Moon (~8,282 km from Moon center)."),
    ("2026-04-06T23:06:00Z", "info",  "Apogee",           "Free-return apogee (~413,140 km from Earth)."),
    ("2026-04-10T23:53:00Z", "info",  "Entry interface",  "EI: Orion enters the atmosphere at ~11 km/s."),
    ("2026-04-11T00:07:27Z", "info",  "Splashdown",       "Pacific splashdown off San Diego."),
]


# ─────────────────────────── helpers ───────────────────────────


def parse_horizons_dt(s: str) -> datetime:
    """Horizons strings look like 'A.D. 2026-Apr-01 22:00:00.0000'."""
    s = s.replace("A.D. ", "").strip()
    return datetime.strptime(s.split(".")[0], "%Y-%b-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def parse_edt(s: str) -> datetime:
    """Hank's photos.js times are EDT (UTC-4). Returns UTC datetime.

    Accepts both 'YYYY-MM-DD HH:MM:SS' and the slightly looser 'YYYY-MM-DD
    HH:MM' that a couple of entries use. Optional 'T' separator and trailing
    fractional seconds are tolerated.
    """
    s = s.strip().replace("T", " ")
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})(?::(\d{2}))?", s)
    if not m:
        raise ValueError(f"Unparseable EDT time: {s}")
    date_part = m.group(1)
    hm = m.group(2)
    sec = m.group(3) or "00"
    dt = datetime.strptime(f"{date_part} {hm}:{sec}", "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone(timedelta(hours=-4))).astimezone(timezone.utc)


def ts_from(dt: datetime) -> Timestamp:
    epoch = dt.timestamp()
    return Timestamp(sec=int(epoch), nsec=int((epoch - int(epoch)) * 1e9))


def ns_from(dt: datetime) -> int:
    return int(dt.timestamp() * 1e9)


def met_str(dt: datetime) -> str:
    delta = dt - LIFTOFF_UTC
    sign = "-" if delta.total_seconds() < 0 else ""
    secs = abs(int(delta.total_seconds()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{d:02d}d {h:02d}:{m:02d}:{s:02d}"


def in_window(dt: datetime) -> bool:
    return MISSION_WINDOW_START <= dt <= MISSION_WINDOW_END


# ─────────────────────────── JSON schemas ───────────────────────────
# Explicit JSON schemas so Foxglove's Plot/Indicator/RawMessages panels
# can introspect fields. Without these, the SDK registers a generic empty
# schema and "/orion/state.distance_earth_km" can't autocomplete.

STATE_SCHEMA: dict[str, Any] = {
    "title": "OrionState",
    "type": "object",
    "properties": {
        "distance_earth_km":  {"type": "number", "description": "Distance from Earth center (km)"},
        "distance_moon_km":   {"type": ["number", "null"], "description": "Distance from Moon center (km)"},
        "altitude_earth_km":  {"type": "number", "description": "Altitude above Earth surface (km)"},
        "altitude_moon_km":   {"type": ["number", "null"], "description": "Altitude above Moon surface (km)"},
        "speed_km_s":         {"type": "number", "description": "Inertial speed (km/s)"},
        "speed_kph":          {"type": "number", "description": "Inertial speed (km/h)"},
    },
}

ACTIVITY_SCHEMA: dict[str, Any] = {
    "title": "CrewActivity",
    "type": "object",
    "properties": {
        "activity": {"type": "string",
                     "description": "Activity category (sleep, exercise, "
                                    "science, piloting, suit-ops, observation, "
                                    "deep-obs, reentry-prep, free)"},
        "label":    {"type": "string",
                     "description": "Human-readable label for the current "
                                    "interval (e.g. 'Sleep (4 hrs)', "
                                    "'TLI burn')"},
    },
}

PHOTO_META_SCHEMA: dict[str, Any] = {
    "title": "PhotoMeta",
    "type": "object",
    "properties": {
        "filename":    {"type": "string"},
        "camera":      {"type": "string"},
        "topic":       {"type": "string", "description": "Camera bucket (nikon_d5, gopro, …)"},
        "description": {"type": "string"},
        "media_url":   {"type": "string", "format": "uri"},
        "photographer":{"type": "string"},
        "location":    {"type": "string"},
        "settings":    {"type": "string"},
    },
}

# /milestones — string state for the Indicator panel. Each EVENTS entry
# emits a message; between them, sample-and-hold leaves the most recent
# milestone displayed. A synthetic "Pre-launch" message at scene start
# gives the Indicator a value from t0.
MILESTONE_SCHEMA: dict[str, Any] = {
    "title": "MissionMilestone",
    "type": "object",
    "properties": {
        "name": {"type": "string",
                 "description": "Latest mission milestone passed"},
    },
}


# ─────────────────────────── loaders ───────────────────────────


def load_horizons(path: Path) -> list[dict[str, Any]]:
    out = []
    with path.open() as f:
        for row in csv.DictReader(f):
            dt = parse_horizons_dt(row["datetime_utc"])
            if not in_window(dt):
                continue
            out.append({
                "dt": dt,
                "x": float(row["x_km"]),
                "y": float(row["y_km"]),
                "z": float(row["z_km"]),
                "vx": float(row["vx_km_s"]),
                "vy": float(row["vy_km_s"]),
                "vz": float(row["vz_km_s"]),
                "range_km": float(row["range_km"]),
                "range_rate_km_s": float(row["range_rate_km_s"]),
            })
    return out


def load_photos() -> list[dict[str, Any]]:
    meta_path = DATA / "photos_meta.json"
    return json.loads(meta_path.read_text())


def load_schedule() -> list[dict[str, Any]]:
    """Crew activity intervals from `data/schedule.json` (produced by
    `scripts/fetch_schedule.py`). Each entry has `start_utc`, `end_utc`,
    `activity`, `label`. Returns [] if the file is missing — the build
    proceeds without a /orion/activity strip in that case."""
    sched_path = DATA / "schedule.json"
    if not sched_path.exists():
        return []
    return json.loads(sched_path.read_text())


# ─────────────────────────── writers ───────────────────────────

IDENTITY_QUAT = Quaternion(x=0.0, y=0.0, z=0.0, w=1.0)


def write_static_scene(scene_chan: SceneUpdateChannel, first_dt: datetime) -> None:
    """Earth + Moon in the scene panel, logged once at mission start as
    static entities. Each body is a textured glTF sphere when its
    texture downloads successfully; if the network is unavailable we
    fall back to the original flat-color SpherePrimitive so offline
    builds still produce a sane scene.

    The textured GLB path uses a unit-radius sphere baked into the GLB
    and scales it via `ModelPrimitive.scale` so it works at any
    --scene-unit-m factor. Earth and Moon both go through the same
    `_planet_sphere_glb` helper (different texture URLs); both inherit
    the layout's `meshUpAxis: y_up` rotation to land their polar axes
    on scene-frame +Z.
    """
    earth_r = _to_units(EARTH_RADIUS_M)
    moon_r = _to_units(MOON_RADIUS_M)

    earth_glb = _planet_sphere_glb("earth", EARTH_TEXTURE_URL, "earth_atmos_2048.jpg")
    moon_glb = _planet_sphere_glb("moon", MOON_TEXTURE_URL, "moon_1024.jpg")

    def _planet_entity(eid: str, frame: str, glb: bytes | None,
                       radius: float, fallback_color: Color) -> SceneEntity:
        kwargs: dict[str, Any] = dict(
            id=eid, frame_id=frame, timestamp=ts_from(first_dt),
            frame_locked=True,
        )
        if glb is not None:
            kwargs["models"] = [ModelPrimitive(
                pose=Pose(position=Vector3(x=0, y=0, z=0), orientation=IDENTITY_QUAT),
                scale=Vector3(x=radius, y=radius, z=radius),
                color=Color(r=1.0, g=1.0, b=1.0, a=1.0),
                override_color=False,  # let the GLB's PBR material/texture win
                data=glb,
                media_type="model/gltf-binary",
            )]
        else:
            kwargs["spheres"] = [SpherePrimitive(
                pose=Pose(position=Vector3(x=0, y=0, z=0), orientation=IDENTITY_QUAT),
                size=Vector3(x=2*radius, y=2*radius, z=2*radius),
                color=fallback_color,
            )]
        return SceneEntity(**kwargs)

    earth_entity = _planet_entity(
        "earth", "earth", earth_glb, earth_r,
        Color(r=0.2, g=0.4, b=0.8, a=0.95),
    )
    moon_entity = _planet_entity(
        "moon", "moon", moon_glb, moon_r,
        Color(r=0.78, g=0.78, b=0.78, a=1.0),
    )
    scene_chan.log(SceneUpdate(entities=[earth_entity, moon_entity]),
                   log_time=ns_from(first_dt))


def write_starfield(chan: PointCloudChannel,
                    scene_start_dt: datetime,
                    n_stars: int = 6000,
                    radius_m: float = 3_000_000_000.0,
                    seed: int = 20260401) -> None:
    """A static random starfield in the earth (J2000 inertial-ish) frame:
    `n_stars` points uniformly distributed on a sphere of physical radius
    `radius_m`, with a power-law-distributed intensity field so most
    stars are dim and a handful stand out.

    Earth frame keeps the stars stationary while orion sweeps through the
    scene — they don't parallax with the spacecraft. Logged once at
    `scene_start_dt`, frame-locked, and converted from physical metres to
    scene units via `_to_units` so it works at any --scene-unit-m factor.

    Default `radius_m` of 3 Gm sits inside the layout's `cameraState.far`
    (5 Gm at metre scale) but well outside the trajectory (~500 Mm), so
    the starfield reads as "infinitely far" without being clipped.

    Determinism: a fixed seed (mission liftoff date as YYYYMMDD) makes
    the constellation byte-identical between rebuilds.
    """
    rng = random.Random(seed)
    point_stride = 16  # 4 × float32: x, y, z, intensity
    buf = bytearray(point_stride * n_stars)
    for i in range(n_stars):
        # Uniform on the unit sphere: z uniform in [-1, 1], θ uniform in
        # [0, 2π). The (1 - z²) factor is what makes it uniform-by-area
        # rather than clustered at the poles.
        z = rng.uniform(-1.0, 1.0)
        theta = rng.uniform(0.0, 2.0 * math.pi)
        rxy = math.sqrt(1.0 - z * z)
        x = rxy * math.cos(theta) * radius_m
        y = rxy * math.sin(theta) * radius_m
        zw = z * radius_m
        # Power-law brightness: u**4 biases the distribution heavily
        # toward dim values, so a few stars look bright against a sea of
        # faint specks.
        intensity = rng.random() ** 4
        struct.pack_into(
            "<ffff", buf, i * point_stride,
            _to_units(x), _to_units(y), _to_units(zw), intensity,
        )

    fields = [
        PackedElementField(name="x", offset=0,
                           type=PackedElementFieldNumericType.Float32),
        PackedElementField(name="y", offset=4,
                           type=PackedElementFieldNumericType.Float32),
        PackedElementField(name="z", offset=8,
                           type=PackedElementFieldNumericType.Float32),
        PackedElementField(name="intensity", offset=12,
                           type=PackedElementFieldNumericType.Float32),
    ]
    chan.log(PointCloud(
        timestamp=ts_from(scene_start_dt),
        frame_id="earth",
        pose=Pose(position=Vector3(x=0, y=0, z=0), orientation=IDENTITY_QUAT),
        point_stride=point_stride,
        fields=fields,
        data=bytes(buf),
    ), log_time=ns_from(scene_start_dt))


def write_trajectory_trail(scene_chan: SceneUpdateChannel,
                           earth_rows: list[dict[str, Any]],
                           scene_start_dt: datetime) -> None:
    """One big polyline of Orion's path through the mission, in the earth frame.
    Points converted km → scene units. Logged at `scene_start_dt` so the trail
    is visible from the moment the MCAP opens, even when the earliest
    Horizons row is hours later."""
    points = [
        Point3(
            x=_to_units(r["x"] * KM_TO_M),
            y=_to_units(r["y"] * KM_TO_M),
            z=_to_units(r["z"] * KM_TO_M),
        )
        for r in earth_rows
    ]
    trail = SceneEntity(
        id="orion_trail",
        frame_id="earth",
        timestamp=ts_from(scene_start_dt),
        frame_locked=True,
        lines=[LinePrimitive(
            type=LinePrimitiveLineType.LineStrip,
            pose=Pose(position=Vector3(x=0, y=0, z=0), orientation=IDENTITY_QUAT),
            thickness=1.5,
            scale_invariant=True,
            points=points,
            # Lower alpha so Orion's arrow + the Earth/Moon spheres read more
            # clearly against the trail. Whole 9-day trajectory is shown at once
            # (matching artemistimeline.com); the spacecraft moves along it.
            color=Color(r=1.0, g=0.55, b=0.0, a=0.45),
        )],
    )
    scene_chan.log(SceneUpdate(entities=[trail]), log_time=ns_from(scene_start_dt))


def write_transforms_and_state(tf: FrameTransformChannel,
                               pose_chan: PoseInFrameChannel,
                               state_chan: Channel,
                               earth_rows: list[dict[str, Any]],
                               moon_rows: list[dict[str, Any]]) -> None:
    """One TF + state sample per Horizons row."""
    # Index moon rows by datetime for fast lookup; assume both files share epochs.
    moon_ix = {r["dt"]: r for r in moon_rows}

    for r in earth_rows:
        log_ns = ns_from(r["dt"])
        ts = ts_from(r["dt"])
        x_u = _to_units(r["x"] * KM_TO_M)
        y_u = _to_units(r["y"] * KM_TO_M)
        z_u = _to_units(r["z"] * KM_TO_M)

        # earth -> orion (scene units)
        tf.log(FrameTransform(
            timestamp=ts,
            parent_frame_id="earth",
            child_frame_id="orion",
            translation=Vector3(x=x_u, y=y_u, z=z_u),
            rotation=IDENTITY_QUAT,  # no attitude yet — placeholder until CKs ship
        ), log_time=log_ns)

        # earth -> moon (only if we have a sample at this epoch).
        # We have orion_in_earth and orion_in_moon, so
        # moon_in_earth = orion_in_earth - orion_in_moon.
        mr = moon_ix.get(r["dt"])
        if mr:
            mx_u = _to_units((r["x"] - mr["x"]) * KM_TO_M)
            my_u = _to_units((r["y"] - mr["y"]) * KM_TO_M)
            mz_u = _to_units((r["z"] - mr["z"]) * KM_TO_M)
            tf.log(FrameTransform(
                timestamp=ts,
                parent_frame_id="earth",
                child_frame_id="moon",
                translation=Vector3(x=mx_u, y=my_u, z=mz_u),
                rotation=IDENTITY_QUAT,
            ), log_time=log_ns)
            d_moon = math.sqrt(mr["x"]**2 + mr["y"]**2 + mr["z"]**2)
        else:
            d_moon = float("nan")

        # Pose (some panels prefer a Pose topic over a TF). Scene units.
        pose_chan.log(PoseInFrame(
            timestamp=ts,
            frame_id="earth",
            pose=Pose(
                position=Vector3(x=x_u, y=y_u, z=z_u),
                orientation=IDENTITY_QUAT,
            ),
        ), log_time=log_ns)

        # Derived metrics
        d_earth = math.sqrt(r["x"]**2 + r["y"]**2 + r["z"]**2)
        speed = math.sqrt(r["vx"]**2 + r["vy"]**2 + r["vz"]**2)
        state_chan.log({
            "distance_earth_km": d_earth,
            "distance_moon_km": d_moon,
            "altitude_earth_km": d_earth - EARTH_RADIUS_KM,
            "altitude_moon_km": d_moon - MOON_RADIUS_KM if math.isfinite(d_moon) else None,
            "speed_km_s": speed,
            "speed_kph": speed * 3600,
        }, log_time=log_ns)


def write_pre_horizons_anchor(tf: FrameTransformChannel,
                              pose_chan: PoseInFrameChannel,
                              state_chan: Channel,
                              first_earth_row: dict[str, Any],
                              first_moon_row: dict[str, Any] | None,
                              scene_start_dt: datetime) -> None:
    """Emit one-shot earth→orion + earth→moon TFs / pose / state at
    `scene_start_dt`, using the first real Horizons sample's positions
    as placeholders. Foxglove's TF tree sample-and-holds, so both frames
    sit at these synthetic positions from the moment the MCAP opens
    until the real Horizons stream starts ~3+ hours later, when the next
    TFs take over.

    Without the earth→moon placeholder, anything rendered in the `moon`
    frame (e.g. the moon sphere on /scene) can't be resolved to the
    panel's followTf (`orion`), and Foxglove surfaces it as
    "Missing transform from frame <moon> to frame <orion>".

    Stylization disclosure: orion is on the launchpad during the
    pre-Horizons window, not in space at the first Horizons sample's
    position. We make this trade because:

      • Without it, the 3D panel is empty for the pre-launch window
        (URDF, frustum, scene anchor all have nowhere to render until
        the first real TF arrives).
      • Ascent kinematics aren't in our data (no Horizons rows for
        T-0 through T+3h28m, no SPICE CK).
      • The pre-launch crew/nikon photos benefit from rendering on the
        camera frustum even if the underlying spacecraft pose is wrong.

    The /orion/state placeholder message keeps the Plot panel populated
    for the same window. Milestone / activity priming happens in
    write_milestones() and write_activity().
    """
    log_ns = ns_from(scene_start_dt)
    ts = ts_from(scene_start_dt)
    x_u = _to_units(first_earth_row["x"] * KM_TO_M)
    y_u = _to_units(first_earth_row["y"] * KM_TO_M)
    z_u = _to_units(first_earth_row["z"] * KM_TO_M)

    tf.log(FrameTransform(
        timestamp=ts,
        parent_frame_id="earth",
        child_frame_id="orion",
        translation=Vector3(x=x_u, y=y_u, z=z_u),
        rotation=IDENTITY_QUAT,
    ), log_time=log_ns)

    # earth → moon placeholder. Horizons gives us orion-in-earth
    # (`first_earth_row`) and orion-in-moon (`first_moon_row`); from those
    # the moon's position in the earth frame is the difference of the two
    # vectors — same arithmetic the per-sample loop in
    # write_transforms_and_state uses.
    d_moon: float | None = None
    if first_moon_row is not None:
        mx_u = _to_units((first_earth_row["x"] - first_moon_row["x"]) * KM_TO_M)
        my_u = _to_units((first_earth_row["y"] - first_moon_row["y"]) * KM_TO_M)
        mz_u = _to_units((first_earth_row["z"] - first_moon_row["z"]) * KM_TO_M)
        tf.log(FrameTransform(
            timestamp=ts,
            parent_frame_id="earth",
            child_frame_id="moon",
            translation=Vector3(x=mx_u, y=my_u, z=mz_u),
            rotation=IDENTITY_QUAT,
        ), log_time=log_ns)
        d_moon = math.sqrt(first_moon_row["x"]**2 + first_moon_row["y"]**2
                           + first_moon_row["z"]**2)

    pose_chan.log(PoseInFrame(
        timestamp=ts,
        frame_id="earth",
        pose=Pose(
            position=Vector3(x=x_u, y=y_u, z=z_u),
            orientation=IDENTITY_QUAT,
        ),
    ), log_time=log_ns)

    d_earth = math.sqrt(first_earth_row["x"]**2 + first_earth_row["y"]**2
                        + first_earth_row["z"]**2)
    speed = math.sqrt(first_earth_row["vx"]**2 + first_earth_row["vy"]**2
                      + first_earth_row["vz"]**2)
    state_chan.log({
        "distance_earth_km": d_earth,
        "distance_moon_km": d_moon,
        "altitude_earth_km": d_earth - EARTH_RADIUS_KM,
        "altitude_moon_km": (d_moon - MOON_RADIUS_KM) if d_moon is not None else None,
        "speed_km_s": speed,
        "speed_kph": speed * 3600,
    }, log_time=log_ns)


def write_camera_rig(tf: FrameTransformChannel,
                     marker_chan: SceneUpdateChannel,
                     first_dt: datetime) -> CameraCalibrationChannel:
    """Static `orion → camera` transform + a one-shot CameraCalibration on
    `/camera/all/calibration` + a SceneEntity-based fallback frustum on
    `/camera/marker`. Logged at first_dt so all three are up the instant
    the file opens. Returns the calibration channel so write_photos can
    re-emit calibration alongside each image timestamp (gives the 3D panel
    a fresh CameraCalibration at any seek position).

    The marker is a redundant pyramid drawn from line primitives: identical
    geometry to the calibration frustum, but rendered through the regular
    SceneUpdate pipeline (LineList primitives) so it doesn't depend on the
    panel's CameraCalibration plumbing — useful as a sanity check when the
    calibration frustum doesn't appear.
    """
    log_ns = ns_from(first_dt)
    ts = ts_from(first_dt)

    tx, ty, tz = (_to_units(c) for c in CAMERA_TRANSLATION_M)
    tf.log(FrameTransform(
        timestamp=ts,
        parent_frame_id="orion",
        child_frame_id=CAMERA_FRAME,
        translation=Vector3(x=tx, y=ty, z=tz),
        rotation=CAMERA_ROTATION,
    ), log_time=log_ns)

    chan = CameraCalibrationChannel(topic="/camera/all/calibration")
    K = _intrinsics_from_fov(CAMERA_W, CAMERA_H, CAMERA_HFOV_DEG)
    # P = [K | 0] for an unrectified pinhole camera.
    P = K[:3] + [0.0] + K[3:6] + [0.0] + K[6:9] + [0.0]
    chan.log(CameraCalibration(
        timestamp=ts,
        frame_id=CAMERA_FRAME,
        width=CAMERA_W,
        height=CAMERA_H,
        distortion_model="plumb_bob",
        D=[0.0, 0.0, 0.0, 0.0, 0.0],
        K=K,
        R=[1.0, 0.0, 0.0,  0.0, 1.0, 0.0,  0.0, 0.0, 1.0],
        P=P,
    ), log_time=log_ns)

    marker_chan.log(_build_camera_marker_update(first_dt), log_time=log_ns)
    return chan


def _build_camera_marker_update(when: datetime) -> SceneUpdate:
    """A magenta wireframe pyramid in the `camera` frame mirroring the
    CameraCalibration frustum (apex at camera origin, far rectangle at
    z=CAMERA_MARKER_DISTANCE_M derived from CAMERA_HFOV_DEG and the
    image aspect). Lines are screen-pixel thickness so they render
    reliably at multi-Mm scene coordinates."""
    d = _to_units(CAMERA_MARKER_DISTANCE_M)
    half_w = d * math.tan(math.radians(CAMERA_HFOV_DEG / 2))
    aspect = CAMERA_W / CAMERA_H
    half_h = half_w / aspect
    apex = Point3(x=0.0, y=0.0, z=0.0)
    tl = Point3(x=-half_w, y=-half_h, z=d)
    tr = Point3(x= half_w, y=-half_h, z=d)
    bl = Point3(x=-half_w, y= half_h, z=d)
    br = Point3(x= half_w, y= half_h, z=d)
    segments: list[Point3] = [
        apex, tl,  apex, tr,  apex, bl,  apex, br,
        tl, tr,  tr, br,  br, bl,  bl, tl,
    ]
    pyramid = LinePrimitive(
        type=LinePrimitiveLineType.LineList,
        pose=Pose(position=Vector3(x=0, y=0, z=0), orientation=IDENTITY_QUAT),
        thickness=CAMERA_MARKER_LINE_WIDTH_PX,
        scale_invariant=True,  # screen-pixel thickness — avoids the
                                # worldUnits LineSegments2 path that
                                # silently kills the wireframe at
                                # multi-Mm scene coordinates.
        points=segments,
        color=Color(r=1.0, g=0.0, b=1.0, a=1.0),
    )
    entity = SceneEntity(
        id="camera_marker",
        frame_id=CAMERA_FRAME,
        timestamp=ts_from(when),
        frame_locked=True,
        lines=[pyramid],
    )
    return SceneUpdate(entities=[entity])


_PHOTO_LOG_NS_KEY = "_log_ns"


def assign_unique_photo_timestamps(photos: list[dict[str, Any]],
                                   include_disabled: bool) -> int:
    """Mutate `photos` in place, attaching a unique nanosecond log_time
    to each eligible image entry under the `_log_ns` key.

    `time_edt` has 1-second resolution, so multiple photos commonly
    collide on the same nanosecond. Foxglove's seek/scrub addresses
    messages by log_time, and per-message UIs (PhotoStepperPanel,
    Image panel, Log panel click-to-seek) need each photo at a unique
    log_time to be individually selectable.

    We disambiguate deterministically by sorting (source_ns, filename)
    and bumping each duplicate by 1 ns. The 1-ns offset is invisible
    in the timeline UI but makes every photo addressable.

    Returns the number of photos whose timestamps were bumped (purely
    for diagnostic logging in main()).

    Idempotent: callers (write_photos, write_photo_log) can rely on
    `_log_ns` being present without re-running the assignment.
    """
    eligible: list[tuple[int, dict[str, Any]]] = []
    for p in photos:
        if not p.get("is_image"):
            continue
        if not include_disabled and not p.get("raw", {}).get("enabled", True):
            continue
        if not (WEB / p["filename"]).exists():
            continue
        try:
            dt_utc = parse_edt(p["time_edt"])
        except Exception:
            continue
        if not in_window(dt_utc):
            continue
        eligible.append((ns_from(dt_utc), p))

    eligible.sort(key=lambda item: (item[0], item[1]["filename"]))
    used: set[int] = set()
    bumped = 0
    for src_ns, p in eligible:
        log_ns = src_ns
        while log_ns in used:
            log_ns += 1
        if log_ns != src_ns:
            bumped += 1
        used.add(log_ns)
        p[_PHOTO_LOG_NS_KEY] = log_ns
    return bumped


def write_photos(photos: list[dict[str, Any]],
                 meta_chan: Channel,
                 cal_chan: CameraCalibrationChannel,
                 marker_chan: SceneUpdateChannel,
                 include_disabled: bool) -> dict[str, int]:
    """All CompressedImage messages on a single /camera/all/image topic, all
    attributed to the single `camera` frame (so they project onto the shared
    `/camera/all/calibration` frustum in the 3D panel).

    `ground` photos keep frame_id="" — those are at KSC, not on the
    spacecraft; the Image panel still shows them, but the 3D panel won't
    try to mount them on the orion-attached frustum.

    The shared CameraCalibration is re-emitted at every image timestamp so
    the frustum has a fresh calibration available wherever the user seeks
    (Foxglove sample-and-holds, but a per-image emit guarantees correctness
    even after large jumps in the timeline).

    Each photo's log_time comes from `assign_unique_photo_timestamps` so
    that ties from the 1-second-resolution `time_edt` field don't make
    photos un-seekable individually. Caller must invoke that helper first.
    """
    counts: dict[str, int] = {}
    skipped_disabled = 0
    skipped_window = 0
    skipped_missing = 0

    image_chan = CompressedImageChannel(topic="/camera/all/image")
    counts["/camera/all/image"] = 0
    by_bucket: dict[str, int] = {}

    # Pre-compute the calibration kwargs once so we can re-emit at every
    # image timestamp without round-tripping through the read-only message.
    K = _intrinsics_from_fov(CAMERA_W, CAMERA_H, CAMERA_HFOV_DEG)
    P = K[:3] + [0.0] + K[3:6] + [0.0] + K[6:9] + [0.0]
    cal_kwargs: dict[str, Any] = dict(
        frame_id=CAMERA_FRAME,
        width=CAMERA_W, height=CAMERA_H,
        distortion_model="plumb_bob",
        D=[0.0, 0.0, 0.0, 0.0, 0.0], K=K,
        R=[1.0, 0.0, 0.0,  0.0, 1.0, 0.0,  0.0, 0.0, 1.0],
        P=P,
    )

    # Walk in the same sorted order assign_unique_photo_timestamps used,
    # so messages on each channel are written monotonically by log_time.
    eligible = [p for p in photos if _PHOTO_LOG_NS_KEY in p]
    eligible.sort(key=lambda p: p[_PHOTO_LOG_NS_KEY])

    # Diagnostic counters for filtered-out photos. We replicate the same
    # filter checks the assigner used so the printed totals are accurate
    # even though we don't process those photos here.
    for p in photos:
        if not p.get("is_image"):
            continue
        if _PHOTO_LOG_NS_KEY in p:
            continue
        if not include_disabled and not p.get("raw", {}).get("enabled", True):
            skipped_disabled += 1
            continue
        if not (WEB / p["filename"]).exists():
            skipped_missing += 1
            continue
        try:
            dt_utc = parse_edt(p["time_edt"])
        except Exception:
            continue
        if not in_window(dt_utc):
            skipped_window += 1

    for p in eligible:
        log_ns = p[_PHOTO_LOG_NS_KEY]
        ts = Timestamp(sec=log_ns // 1_000_000_000,
                       nsec=log_ns % 1_000_000_000)
        try:
            dt_utc = parse_edt(p["time_edt"])
        except Exception:
            continue
        bucket = p["topic"]
        path = WEB / p["filename"]
        with path.open("rb") as f:
            data = f.read()

        rotation_ccw = _PHOTO_ROTATIONS_CCW.get(p["filename"], 0)
        if rotation_ccw:
            data = _rotate_jpeg_ccw(data, rotation_ccw)

        # On-Orion photos go on the shared `camera` frame; ground photos
        # stay frame-less (they're not bolted to the spacecraft).
        cam_frame_id = "" if bucket == "ground" else CAMERA_FRAME

        image_chan.log(CompressedImage(
            timestamp=ts,
            frame_id=cam_frame_id,
            data=data,
            format="jpeg",
        ), log_time=log_ns)

        # Refresh the shared calibration + the diagnostic marker at the
        # same timestamp so the frustum and the magenta/cyan reference
        # spheres are rendered with current state even after large seeks
        # past the static one-shot logged at mission start.
        if cam_frame_id:
            cal_chan.log(
                CameraCalibration(timestamp=ts, **cal_kwargs),
                log_time=log_ns,
            )
            marker_chan.log(
                _build_camera_marker_update(dt_utc),
                log_time=log_ns,
            )

        raw = p.get("raw", {})
        meta_chan.log({
            "filename": p["filename"],
            "camera": p.get("camera", "") or "",
            "topic": bucket,
            "description": p.get("description", "") or "",
            "media_url": p.get("media_url", "") or "",
            "photographer": raw.get("photographer", "") or "",
            "location": raw.get("location", "") or "",
            "settings": raw.get("settings", "") or "",
        }, log_time=log_ns)
        counts["/camera/all/image"] += 1
        by_bucket[bucket] = by_bucket.get(bucket, 0) + 1

    print(f"[build]   photos: skipped {skipped_disabled} disabled, "
          f"{skipped_window} out-of-window, {skipped_missing} missing-file")
    if by_bucket:
        breakdown = ", ".join(f"{b}={n}" for b, n in sorted(by_bucket.items()))
        print(f"[build]   photos by camera: {breakdown}")
    return counts


def write_photo_log(events_chan: LogChannel,
                    photos: list[dict[str, Any]],
                    include_disabled: bool) -> int:
    """One foxglove.Log entry per photo on /events.

    Foxglove's Log panel renders a clickable list of these messages — clicking
    one seeks the player to the photo's timestamp, which lets the user scrub
    through the 366-photo album without waiting for real-time playback. We
    keep mission events / phase boundaries on a separate /milestones channel
    that's better visualized as a State Transitions strip (see write_milestones)
    rather than buried in a chronological log.

    Uses the same `_log_ns` field assigned by `assign_unique_photo_timestamps`
    so each Log entry sits at the same unique timestamp as its matching
    /photo/meta and /camera/all/image messages — a click in the Log panel
    seeks to exactly the right photo.
    """
    n = 0
    eligible = [p for p in photos if _PHOTO_LOG_NS_KEY in p]
    eligible.sort(key=lambda p: p[_PHOTO_LOG_NS_KEY])
    for p in eligible:
        log_ns = p[_PHOTO_LOG_NS_KEY]
        ts = Timestamp(sec=log_ns // 1_000_000_000,
                       nsec=log_ns % 1_000_000_000)

        bucket = p["topic"]
        raw = p.get("raw", {})
        camera = p.get("camera", "") or bucket
        photographer = raw.get("photographer", "") or ""
        description = (p.get("description", "") or "").strip()
        location = raw.get("location", "") or ""

        # Compose a single human-readable line. Log panel shows `message`
        # prominently; `name` becomes the source filter (handy as a per-camera
        # dropdown), and `file`/`line` get repurposed as the photo filename
        # and a stable index so users can find the underlying file later.
        bits: list[str] = [camera]
        if description:
            bits.append(description)
        elif photographer:
            bits.append(f"by {photographer}")
        if location:
            bits.append(f"@ {location}")
        message = " — ".join(bits)

        events_chan.log(Log(
            timestamp=ts,
            level=LogLevel.Info,
            message=message,
            name=f"camera/{bucket}",
            file=p["filename"],
            line=0,
        ), log_time=log_ns)
        n += 1
    return n


def write_milestones(chan: Channel, scene_start_dt: datetime) -> None:
    """`/milestones.name` — latest mission milestone, suitable for an
    Indicator panel. EVENTS is the canonical milestone list; we prepend
    a synthetic "Pre-launch" message at scene start so the Indicator
    has a value from t0 through liftoff. Sample-and-hold semantics
    leave each milestone displayed until the next one fires.
    """
    items: list[tuple[datetime, str]] = [(scene_start_dt, "Pre-launch")]
    for iso, _level, name, _msg in EVENTS:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        items.append((dt, name))

    items.sort(key=lambda x: x[0])
    last_name: str | None = None
    for dt, name in items:
        if not in_window(dt):
            continue
        if name == last_name:
            continue
        chan.log({"name": name}, log_time=ns_from(dt))
        last_name = name


def write_activity(chan: Channel, schedule: list[dict[str, Any]],
                   scene_start_dt: datetime) -> int:
    """`/orion/activity` — single-line StateTransitions strip showing
    sleep / piloting / science / suit-ops / etc. intervals. Each entry
    in `schedule` is `{start_utc, end_utc, activity, label}` from
    fetch_schedule.py.

    We emit one message at every interval start; sample-and-hold paints
    the band until the next start. A trailing "—" placeholder at the
    last `end_utc` lets the strip terminate cleanly instead of bleeding
    the final activity off the right edge of the panel.

    If `scene_start_dt` falls inside the first interval, we backfill an
    extra message at scene start so the strip is colored from t0
    instead of going blank for the first few minutes.
    """
    if not schedule:
        return 0

    n = 0
    last_value = None
    sched_sorted = sorted(schedule, key=lambda s: s["start_utc"])

    # Backfill at scene start with whichever interval is in progress at
    # that moment (handles e.g. a "Suit up" block that started 10 min
    # before MISSION_WINDOW_START). If scene start lands before every
    # interval, fall back to the first one so the strip isn't blank
    # while the spacecraft sits on the pad.
    if in_window(scene_start_dt):
        active = next(
            (e for e in sched_sorted
             if datetime.fromisoformat(e["start_utc"]) <= scene_start_dt
                < datetime.fromisoformat(e["end_utc"])),
            None,
        )
        if active is None and datetime.fromisoformat(
                sched_sorted[0]["start_utc"]) > scene_start_dt:
            active = sched_sorted[0]
        if active is not None:
            chan.log(
                {"activity": active["activity"], "label": active["label"]},
                log_time=ns_from(scene_start_dt),
            )
            last_value = (active["activity"], active["label"])
            n += 1

    last_end_dt: datetime | None = None
    for entry in sched_sorted:
        start_dt = datetime.fromisoformat(entry["start_utc"])
        end_dt = datetime.fromisoformat(entry["end_utc"])
        last_end_dt = end_dt
        if not in_window(start_dt):
            continue
        value = (entry["activity"], entry["label"])
        if value == last_value:
            continue
        chan.log(
            {"activity": entry["activity"], "label": entry["label"]},
            log_time=ns_from(start_dt),
        )
        last_value = value
        n += 1

    if last_end_dt is not None and in_window(last_end_dt):
        chan.log({"activity": "", "label": "—"}, log_time=ns_from(last_end_dt))
        n += 1

    return n


# AROW glb config: the AROW model authors its four solar arrays (`SAW1..SAW4`)
# in a folded-against-the-hull pose with no hinge metadata exposed in the
# glb. Two ways to handle them at build time:
#
#   1) `_AROW_PANEL_TRANSFORMS = None` (default) — strip the SAW nodes from
#      the scene; the URDF's primitive box panels take over visually.
#
#   2) `_AROW_PANEL_TRANSFORMS = { "SAW1": {"rotation": [...], "translation": [...]}, ... }`
#      — apply per-node transforms to deploy AROW's authored arrays. The
#      URDF's primitive panels are then stripped at embed time so they
#      don't double up. Use `tools/panel_tuner.html` to interactively pick
#      the rotation+translation values; click its "Copy Python" button and
#      paste the result over `_AROW_PANEL_TRANSFORMS` below.
#
# Either way the body mesh (CM/SM/engine/hardware) is preserved and the
# re-encoded glb is base64-embedded into a data: URL so the MCAP stays
# fully self-contained.
# Vendored AROW Orion mesh at models/orion.glb (originally extracted from
# NASA AROW and re-hosted by iandees/artemis-viewer). Kept in-repo so
# builds never depend on external GitHub hosting.
ORION_GLB_PATH = MODELS / "orion.glb"

# Match `<mesh filename="...orion.glb">` only — not bare "orion.glb" mentions
# in XML comments.
_URDF_ORION_MESH_RE = re.compile(
    r'(<mesh\s+filename=")([^"]*orion\.glb)(")',
    re.IGNORECASE,
)

# The four solar-array subassembly node names in the AROW glb.
_AROW_PANEL_NODES = ("SAW1", "SAW2", "SAW3", "SAW4")

# Per-panel deployment transforms — values copied verbatim from Ian Dees's
# artemis-viewer (https://github.com/iandees/artemis-viewer, public/js/main.js
# `sawTransforms`). He tuned them interactively against the AROW glb in his
# Three.js viewer's panel-positioning tool, since the SAW meshes use
# skinned-mesh renderers with bone chains for fold/unfold animation that
# couldn't be fully extracted from the Unity bundle. Using his values
# directly gives us NASA's authored deployed pose without re-doing the
# manual alignment work.
#
# Quaternion order: [qx, qy, qz, qw] (glTF convention). His source array is
# also (x, y, z, w) since `child.quaternion.set(...st.quat)` matches
# THREE.Quaternion.set(x, y, z, w).
#
# When None, the SAWs are stripped from the glb instead. Used by the
# `--no-deploy-panels` flag for a no-network fallback path.
_AROW_PANEL_TRANSFORMS = {
    "SAW1": {
        "rotation":    [0.5530, -0.5435, -0.4338, 0.4589],
        "translation": [0.4950,  0.0461,  0.4289],
    },
    "SAW2": {
        "rotation":    [-0.1437, -0.8356,  0.0006, 0.5302],
        "translation": [ 0.4024, -0.0734, -0.5767],
    },
    "SAW3": {
        "rotation":    [ 0.0711, -0.8427, 0.1274,  0.5183],
        "translation": [-0.7006, -0.0759, -0.0818],
    },
    "SAW4": {
        "rotation":    [-0.1440,  0.8499, -0.0031, -0.5069],
        "translation": [-0.4286, -0.0726,  0.5506],
    },
}

_GLB_MAGIC = 0x46546C67  # 'glTF'
_CHUNK_TYPE_JSON = 0x4E4F534A  # 'JSON'
_CHUNK_TYPE_BIN = 0x004E4942   # 'BIN\0'


# Earth + Moon textures — three.js's example texture set, served from
# raw.githubusercontent.com. Both are NASA imagery (public domain). Sizes:
# earth_atmos_2048 ≈ 280 KB, moon_1024 ≈ 50 KB. Cached on first download
# under data/cache/ and embedded as base64 buffer-views inside the
# generated planet GLBs, so the resulting MCAP is fully self-contained
# and Foxglove never fetches at runtime.
EARTH_TEXTURE_URL = "https://raw.githubusercontent.com/mrdoob/three.js/master/examples/textures/planets/earth_atmos_2048.jpg"
MOON_TEXTURE_URL = "https://raw.githubusercontent.com/mrdoob/three.js/master/examples/textures/planets/moon_1024.jpg"


def _download_texture(url: str, cache_name: str) -> bytes:
    """Cache-aware download. Returns the texture bytes."""
    cache_dir = DATA / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / cache_name
    if not cached.exists():
        print(f"[build]   downloading texture {url} → {cached}")
        urllib.request.urlretrieve(url, str(cached))
    return cached.read_bytes()


def _generate_uv_sphere(segments_u: int, segments_v: int):
    """Unit sphere with poles on ±Y so it lines up with the layout's
    `meshUpAxis: y_up` convention (Foxglove rotates Y-up meshes 90° around
    X to land the sphere's polar axis on scene-frame +Z, matching J2000
    ECI's north-on-+Z convention).

    Equirectangular UV: u wraps the equator (0 → 1 around longitude), v
    runs north-pole-top to south-pole-bottom (0 → 1 across latitude).

    Returns (positions, normals, uvs, indices) as flat Python lists.
    """
    positions: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    indices: list[int] = []
    for v in range(segments_v + 1):
        phi = math.pi * v / segments_v          # 0 (north pole) → π (south pole)
        y = math.cos(phi)
        rxz = math.sin(phi)
        for u in range(segments_u + 1):
            theta = 2.0 * math.pi * u / segments_u
            x = rxz * math.cos(theta)
            z = rxz * math.sin(theta)
            positions.append((x, y, z))
            normals.append((x, y, z))
            uvs.append((u / segments_u, v / segments_v))
    for v in range(segments_v):
        for u in range(segments_u):
            a = v * (segments_u + 1) + u
            b = a + 1
            c = a + (segments_u + 1)
            d = c + 1
            # Two triangles per quad. Winding chosen so outward-facing
            # normals point away from the sphere center under the default
            # glTF right-hand convention.
            indices.extend([a, b, d, a, d, c])
    return positions, normals, uvs, indices


def _pad4(buf: bytearray, fill: int = 0) -> None:
    while len(buf) % 4 != 0:
        buf.append(fill)


def _build_textured_sphere_glb(texture_bytes: bytes,
                               texture_mime: str,
                               segments_u: int = 64,
                               segments_v: int = 32) -> bytes:
    """Build a self-contained GLB of a unit sphere (radius 1, Y-up
    polar axis) with the given equirectangular texture mapped onto it.

    The GLB is a minimal glTF 2.0 asset: one mesh with POSITION /
    NORMAL / TEXCOORD_0 attributes, one PBR material whose
    baseColorTexture references one image stored as a buffer view
    pointing at the JPEG/PNG bytes. Sphere is sized as a unit sphere so
    consumers can scale via `ModelPrimitive.scale = (radius, radius,
    radius)` in scene units.
    """
    positions, normals, uvs, indices = _generate_uv_sphere(segments_u, segments_v)
    n_verts = len(positions)
    n_indices = len(indices)

    pos_bytes = b"".join(struct.pack("<fff", *p) for p in positions)
    nrm_bytes = b"".join(struct.pack("<fff", *n) for n in normals)
    uv_bytes = b"".join(struct.pack("<ff", *u) for u in uvs)
    idx_bytes = b"".join(struct.pack("<I", i) for i in indices)

    px = [p[0] for p in positions]
    py = [p[1] for p in positions]
    pz = [p[2] for p in positions]
    pos_min = [min(px), min(py), min(pz)]
    pos_max = [max(px), max(py), max(pz)]

    # Pack BIN chunk with each section 4-byte aligned (glTF 2.0 spec).
    bin_chunk = bytearray()
    bv_pos = len(bin_chunk); bin_chunk += pos_bytes; _pad4(bin_chunk)
    bv_nrm = len(bin_chunk); bin_chunk += nrm_bytes; _pad4(bin_chunk)
    bv_uv = len(bin_chunk); bin_chunk += uv_bytes; _pad4(bin_chunk)
    bv_idx = len(bin_chunk); bin_chunk += idx_bytes; _pad4(bin_chunk)
    bv_img = len(bin_chunk); bin_chunk += texture_bytes; _pad4(bin_chunk)

    glb_json = {
        "asset": {"version": "2.0", "generator": "artemis-foxglove build_mcap.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{
            "primitives": [{
                "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                "indices": 3,
                "material": 0,
            }],
        }],
        "materials": [{
            "pbrMetallicRoughness": {
                "baseColorTexture": {"index": 0},
                "metallicFactor": 0.0,
                "roughnessFactor": 1.0,
            },
            "doubleSided": False,
        }],
        "textures": [{"source": 0, "sampler": 0}],
        "samplers": [{
            "magFilter": 9729,   # LINEAR
            "minFilter": 9987,   # LINEAR_MIPMAP_LINEAR (three.js generates mipmaps)
            "wrapS": 10497,      # REPEAT  — longitude wraps around the equator
            "wrapT": 33071,      # CLAMP_TO_EDGE — latitude pinches at poles
        }],
        "images": [{"bufferView": 4, "mimeType": texture_mime}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "type": "VEC3",
             "count": n_verts, "min": pos_min, "max": pos_max},
            {"bufferView": 1, "componentType": 5126, "type": "VEC3",
             "count": n_verts},
            {"bufferView": 2, "componentType": 5126, "type": "VEC2",
             "count": n_verts},
            {"bufferView": 3, "componentType": 5125, "type": "SCALAR",
             "count": n_indices},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": bv_pos, "byteLength": len(pos_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": bv_nrm, "byteLength": len(nrm_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": bv_uv,  "byteLength": len(uv_bytes),  "target": 34962},
            {"buffer": 0, "byteOffset": bv_idx, "byteLength": len(idx_bytes), "target": 34963},
            {"buffer": 0, "byteOffset": bv_img, "byteLength": len(texture_bytes)},
        ],
        "buffers": [{"byteLength": len(bin_chunk)}],
    }

    json_chunk = json.dumps(glb_json, separators=(",", ":")).encode("utf-8")
    while len(json_chunk) % 4 != 0:
        json_chunk += b" "

    total_len = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
    out = bytearray()
    out += struct.pack("<III", _GLB_MAGIC, 2, total_len)
    out += struct.pack("<II", len(json_chunk), _CHUNK_TYPE_JSON) + json_chunk
    out += struct.pack("<II", len(bin_chunk),  _CHUNK_TYPE_BIN)  + bytes(bin_chunk)
    return bytes(out)


def _planet_sphere_glb(name: str, texture_url: str, texture_filename: str) -> bytes | None:
    """Cache-aware build: download texture, build a textured sphere GLB,
    cache the result. Returns None on download failure (caller falls back
    to a flat-color SpherePrimitive)."""
    cache_dir = DATA / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_glb = cache_dir / f"{name}-sphere.glb"
    if cached_glb.exists():
        return cached_glb.read_bytes()
    try:
        tex_bytes = _download_texture(texture_url, texture_filename)
    except Exception as e:
        print(f"[build]   WARN: failed to download {name} texture ({e}); "
              f"using flat-color sphere fallback")
        return None
    glb = _build_textured_sphere_glb(tex_bytes, "image/jpeg")
    cached_glb.write_bytes(glb)
    print(f"[build]   built {cached_glb.name} ({len(glb):,} bytes, "
          f"texture {len(tex_bytes):,} bytes)")
    return glb

# Regex for stripping `<visual name="panel_*">..</visual>` blocks out of
# the URDF text when AROW SAWs are deployed in-place (so we don't have
# AROW's panels and the URDF's primitive panels overlapping).
_URDF_PRIMITIVE_PANEL_RE = re.compile(
    r'\s*<visual\s+name="panel_[^"]+">.*?</visual>',
    re.DOTALL,
)


def _load_orion_glb() -> bytes:
    """Load the vendored AROW orion.glb from models/."""
    if not ORION_GLB_PATH.exists():
        raise FileNotFoundError(
            f"{ORION_GLB_PATH} missing — the mesh is vendored in models/; "
            "ensure orion.glb is present in the repo."
        )
    return ORION_GLB_PATH.read_bytes()


def _embed_orion_glb_in_urdf(urdf_text: str) -> str:
    """Replace the URDF mesh filename with an inline data: URL."""
    data_url = _orion_glb_data_url()
    new, n = _URDF_ORION_MESH_RE.subn(
        lambda m: f"{m.group(1)}{data_url}{m.group(3)}",
        urdf_text,
        count=1,
    )
    if n == 0:
        raise RuntimeError(
            f"URDF has no <mesh filename=\"...orion.glb\"> to embed "
            f"(expected {ORION_GLB_PATH.name})"
        )
    return new


def _deploy_glb_panels(glb_bytes: bytes) -> bytes:
    """Either strip SAW1..SAW4 from the AROW glb scene (if no per-panel
    transforms are configured) or apply tuned rotation+translation to each
    SAW node (so the AROW arrays render in a deployed pose).

    Strip mode (`_AROW_PANEL_TRANSFORMS is None`): orphan the SAW indices
    from every parent's children list and from each scene root. Their node
    entries stay in place so existing index references remain valid; they
    just aren't visited during scene traversal.

    Apply mode (`_AROW_PANEL_TRANSFORMS` is a dict): set each SAW node's
    rotation/translation to the tuned values. The transforms come from
    `tools/panel_tuner.html`'s "Copy Python" button.

    Operates on the JSON chunk only; BIN chunk is passed through
    unchanged. Pads the new JSON chunk to a 4-byte boundary as required by
    glTF 2.0.
    """
    if struct.unpack("<I", glb_bytes[:4])[0] != _GLB_MAGIC:
        raise ValueError("Not a glb file (bad magic)")
    version = struct.unpack("<I", glb_bytes[4:8])[0]
    if version != 2:
        raise ValueError(f"Unsupported glb version: {version}")

    json_chunk_len, json_chunk_type = struct.unpack("<II", glb_bytes[12:20])
    if json_chunk_type != _CHUNK_TYPE_JSON:
        raise ValueError("First chunk is not JSON")
    j = json.loads(glb_bytes[20:20 + json_chunk_len])

    if _AROW_PANEL_TRANSFORMS is None:
        # Strip mode.
        saw_indices = {i for i, n in enumerate(j.get("nodes", []))
                       if n.get("name") in _AROW_PANEL_NODES}
        if len(saw_indices) != len(_AROW_PANEL_NODES):
            raise RuntimeError(
                f"Expected to find {_AROW_PANEL_NODES} in glb, found indices {saw_indices}. "
                "AROW glb structure may have changed."
            )
        for node in j.get("nodes", []):
            if "children" in node:
                node["children"] = [c for c in node["children"] if c not in saw_indices]
        for scene in j.get("scenes", []):
            if "nodes" in scene:
                scene["nodes"] = [n for n in scene["nodes"] if n not in saw_indices]
    else:
        # Apply mode: write tuned rotation/translation onto each SAW node.
        for name, transform in _AROW_PANEL_TRANSFORMS.items():
            if name not in _AROW_PANEL_NODES:
                raise RuntimeError(f"_AROW_PANEL_TRANSFORMS has unknown SAW name {name!r}")
            node = next((n for n in j.get("nodes", []) if n.get("name") == name), None)
            if node is None:
                raise RuntimeError(f"AROW glb has no node named {name!r}")
            if "rotation" in transform:
                node["rotation"] = list(transform["rotation"])
            if "translation" in transform:
                node["translation"] = list(transform["translation"])

    new_json = json.dumps(j, separators=(",", ":")).encode("utf-8")
    pad = (4 - len(new_json) % 4) % 4
    new_json += b" " * pad

    bin_block = glb_bytes[20 + json_chunk_len:]  # includes BIN chunk header
    new_total = 12 + 8 + len(new_json) + len(bin_block)

    out = bytearray()
    out += struct.pack("<III", _GLB_MAGIC, version, new_total)
    out += struct.pack("<II", len(new_json), _CHUNK_TYPE_JSON) + new_json
    out += bin_block
    return bytes(out)


_DARK_SIDE_EMISSIVE = 0.00  # 0..1 floor brightness for shadowed faces

# Multiplier applied to the body's vertex colours (the AROW glb stores
# the spacecraft body as vertex-coloured geometry whose vertex colours
# average ~0.9 luminance — almost white). 1.0 lets that brightness
# through unchanged, which makes the body read as washed-out white at
# any non-zero emissive. Drop this to ~0.5 to bring the body back to a
# believable mid-grey before the emissive bloom adds on top. Does not
# affect the SAW solar panels (those use a different material with a
# real texture).
_BODY_BASE_TINT = 0.65

# Metalness of the synthetic body material. Three.js's PBR shader picks
# up ambient light very differently for metals vs dielectrics:
#   * 0.0 (dielectric): the body absorbs ambient × baseColor on every
#     face, lifting the shadow side to ~0.5 × ambient even with
#     emissive=0. Reads matte/plastic.
#   * 1.0 (metal):       no diffuse-from-ambient term, shadow side goes
#     near-black, _DARK_SIDE_EMISSIVE becomes the real floor knob.
#     Reads as slightly shinier/metallic on the lit side.
# 1.0 matches the glTF spec's default material (which is what the AROW
# body originally rendered with before we gave it an explicit material),
# so this value reproduces the original dark-side darkness; partial
# values mix the two responses.
_BODY_METALLIC_FACTOR = 0.9


def _lift_glb_dark_side(glb_bytes: bytes,
                        emissive: float = _DARK_SIDE_EMISSIVE,
                        body_tint: float = _BODY_BASE_TINT,
                        body_metallic: float = _BODY_METALLIC_FACTOR) -> bytes:
    """Lift the dark-side floor on every material in the glb without
    sacrificing PBR shading. Three concerns:

    1. Restore `baseColorFactor` to white on existing materials. The
       AROW glb ships with `baseColorFactor = [0.4, 0.4, 0.4, 1.0]`,
       dimming the texture to 40% of its authored brightness even on
       the fully-lit side. White restores it.

    2. Stamp each existing material with
       `emissiveFactor = [k, k, k]` and an `emissiveTexture` pointing
       at the same texture as `baseColorTexture`, so a fraction `k` of
       the texture is added regardless of viewing angle. Lit side keeps
       its specular highlights; dark side glows at `k` brightness with
       all the texture's spatial detail (panels, hatches, painted
       text) preserved.

    3. AROW's body geometry (CM_*, SM_*, Face_*) uses vertex colours
       and no UVs, and its primitives reference no glTF material —
       three.js falls back to its built-in default material, which we
       can't influence from the file. We synthesise a plain
       no-texture material here and stamp it onto every primitive with
       `material: None`. That material gets the same emissive floor
       (flat grey, since there's no texture to modulate it; vertex
       colours still come through unchanged on the lit side).

    Three.js's GLTFLoader honours `emissiveFactor`, `emissiveTexture`,
    and per-primitive `material` references natively; no extensions
    are needed. Reusing the existing texture index (rather than baking
    a new one) means the BIN chunk doesn't grow.

    `emissive=0.4` is a defensible middle ground — higher washes out
    the lit side, lower leaves shadowed faces murky. Tunable via the
    kwarg if we want a CLI flag later.

    Operates on the JSON chunk only; the BIN chunk (textures, vertex
    buffers) passes through unchanged.
    """
    if struct.unpack("<I", glb_bytes[:4])[0] != _GLB_MAGIC:
        raise ValueError("Not a glb file (bad magic)")
    version = struct.unpack("<I", glb_bytes[4:8])[0]
    if version != 2:
        raise ValueError(f"Unsupported glb version: {version}")

    json_chunk_len, json_chunk_type = struct.unpack("<II", glb_bytes[12:20])
    if json_chunk_type != _CHUNK_TYPE_JSON:
        raise ValueError("First chunk is not JSON")
    j = json.loads(glb_bytes[20:20 + json_chunk_len])

    materials = j.setdefault("materials", [])
    for m in materials:
        pbr = m.setdefault("pbrMetallicRoughness", {})
        if "baseColorFactor" in pbr:
            _r, _g, _b, a = pbr["baseColorFactor"]
            pbr["baseColorFactor"] = [1.0, 1.0, 1.0, a]
        m["emissiveFactor"] = [emissive, emissive, emissive]
        base_tex = pbr.get("baseColorTexture")
        if base_tex is not None and "emissiveTexture" not in m:
            m["emissiveTexture"] = {
                "index": base_tex["index"],
                **({"texCoord": base_tex["texCoord"]} if "texCoord" in base_tex else {}),
            }

    body_material_idx: int | None = None
    n_orphan_prims = 0
    for mesh in j.get("meshes", []):
        for prim in mesh.get("primitives", []):
            if prim.get("material") is None:
                if body_material_idx is None:
                    materials.append({
                        "name": "body_dark_side_floor",
                        "pbrMetallicRoughness": {
                            "baseColorFactor": [body_tint, body_tint, body_tint, 1.0],
                            "metallicFactor": body_metallic,
                            "roughnessFactor": 1.0,
                        },
                        "emissiveFactor": [emissive, emissive, emissive],
                    })
                    body_material_idx = len(materials) - 1
                prim["material"] = body_material_idx
                n_orphan_prims += 1

    new_json = json.dumps(j, separators=(",", ":")).encode("utf-8")
    pad = (4 - len(new_json) % 4) % 4
    new_json += b" " * pad

    bin_block = glb_bytes[20 + json_chunk_len:]
    new_total = 12 + 8 + len(new_json) + len(bin_block)

    out = bytearray()
    out += struct.pack("<III", _GLB_MAGIC, version, new_total)
    out += struct.pack("<II", len(new_json), _CHUNK_TYPE_JSON) + new_json
    out += bin_block
    print(f"[build]   /orion/urdf: dark-side floor = {emissive:.3f}, "
          f"body tint = {body_tint:.2f}, "
          f"body metallic = {body_metallic:.2f} "
          f"({len(materials)} material(s), {n_orphan_prims} body primitive(s) re-materialed)")
    return bytes(out)


def _orion_glb_data_url() -> str:
    """Build a self-contained `data:model/gltf-binary;base64,...` URL for the
    AROW orion.glb with the SAW1..SAW4 solar arrays deployed (or stripped)
    and a moderate emissive floor on every material so shadowed faces
    aren't pitch black under Foxglove's single directional light. PBR
    shading and specular highlights are preserved on the lit side.

    Foxglove's mesh loader supports data URLs (see app/packages/viz/src/panels/
    ThreeDeeRender/stories/common.ts: GLTF_AXES_MESH_RESOURCE). Embedding the
    glb inline in the URDF text means the MCAP carries everything Foxglove
    needs to render the spacecraft — no GitHub fetch, no file:// dependency.
    """
    original = _load_orion_glb()
    stripped = _deploy_glb_panels(original)
    lit = _lift_glb_dark_side(stripped)
    # Cache the final glb on disk too, for inspection / debugging
    out_path = DATA / "cache" / "orion-deployed.glb"
    out_path.write_bytes(lit)
    b64 = base64.b64encode(lit).decode("ascii")
    return f"data:model/gltf-binary;base64,{b64}"


def _rescale_urdf_xml(urdf_text: str) -> str:
    """Divide every distance-flavoured attribute in the URDF by SCENE_UNIT_M.

    The URDF authors `<mesh scale>`, `<origin xyz>`, and `<box size>` in the
    same unit system as the rest of the 3D scene (metres at SCENE_UNIT_M=1,
    megameters at SCENE_UNIT_M=1e6, …). When we downscale the scene we have
    to apply the same factor here or the spacecraft + primitive panels keep
    their old metre-scale values and dwarf everything else by 6+ orders of
    magnitude.

    `<origin rpy="…">` is angles in radians and untouched. Mass / inertia
    are also untouched (this URDF doesn't currently declare any).
    """
    if SCENE_UNIT_M == 1.0:
        return urdf_text

    import xml.etree.ElementTree as ET
    root = ET.fromstring(urdf_text)

    def _scale_attr(elem: ET.Element, key: str) -> None:
        raw = elem.get(key)
        if raw is None:
            return
        nums = [float(t) for t in raw.split()]
        elem.set(key, " ".join(f"{_to_units(v):.6g}" for v in nums))

    n_mesh = n_origin = n_box = 0
    for m_el in root.iter("mesh"):
        if m_el.get("scale"):
            _scale_attr(m_el, "scale")
            n_mesh += 1
    for o_el in root.iter("origin"):
        if o_el.get("xyz"):
            _scale_attr(o_el, "xyz")
            n_origin += 1
    for b_el in root.iter("box"):
        if b_el.get("size"):
            _scale_attr(b_el, "size")
            n_box += 1
    # cylinder/sphere primitives could appear later; rescale them too.
    for c_el in root.iter("cylinder"):
        for k in ("radius", "length"):
            if c_el.get(k):
                c_el.set(k, f"{_to_units(float(c_el.get(k))):.6g}")
    for s_el in root.iter("sphere"):
        if s_el.get("radius"):
            s_el.set("radius", f"{_to_units(float(s_el.get('radius'))):.6g}")

    print(f"[build]   /orion/urdf: rescaled by 1/{SCENE_UNIT_M:g} "
          f"({n_mesh} mesh / {n_origin} origin / {n_box} box attribute(s))")
    return ET.tostring(root, encoding="unicode")


def _validate_urdf_xml(urdf_text: str, source: str) -> None:
    """Parse the URDF as XML and assert it has a <robot> root, so build
    failures surface here instead of silently in Foxglove ("No robot found
    in URDF"). Covers the most common gotcha: a `--` sequence in a `<!-- -->`
    comment, which XML 1.0 forbids and which the URDF parser silently rejects.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(urdf_text)
    except ET.ParseError as e:
        raise RuntimeError(
            f"URDF text from {source} is not valid XML: {e}. "
            "Common cause: `--` (double hyphen) inside an XML comment block."
        ) from e
    if root.tag != "robot":
        raise RuntimeError(
            f"URDF text from {source} parsed as XML but root is <{root.tag}>, "
            "not <robot>. Foxglove will reject this with 'No robot found in URDF'."
        )


def write_robot_description(desc_chan: Channel,
                            urdf_text: str,
                            first_dt: datetime,
                            *,
                            deploy_panels: bool = True,
                            source: str = "<urdf>") -> None:
    """Publish the URDF text on /orion/urdf (std_msgs/msg/String) at
    mission start. Foxglove's URDF layer (sourceType="topic") subscribes here,
    parses the XML, and renders the spacecraft anchored to the `orion` frame.

    If `deploy_panels` is True and the URDF references orion.glb, we
    load models/orion.glb, run it through `_deploy_glb_panels` (which either
    strips the SAWs or applies tuned transforms — see
    `_AROW_PANEL_TRANSFORMS`), then base64-embed the result so the MCAP
    stays self-contained. When AROW SAWs are deployed in-place via tuned
    transforms, we also strip the URDF's primitive panel <visual> blocks
    so they don't double up with the now-visible AROW arrays.
    """
    if deploy_panels and _URDF_ORION_MESH_RE.search(urdf_text):
        urdf_text = _embed_orion_glb_in_urdf(urdf_text)
        if _AROW_PANEL_TRANSFORMS is not None:
            # As of the iandees-poses change `models/orion.urdf` no longer
            # carries primitive panel `<visual>` blocks, so this strip is
            # a no-op on the canonical URDF. We keep it as defensive
            # cleanup for forks/branches that may re-add primitives — in
            # the canonical-URDF case n_stripped is 0 and we skip the
            # "stripped N panel(s)" suffix to avoid implying we did work.
            urdf_text, n_stripped = _URDF_PRIMITIVE_PANEL_RE.subn('', urdf_text)
            suffix = (f" ({n_stripped} URDF primitive panel(s) stripped)"
                      if n_stripped else "")
            print(f"[build]   /orion/urdf: AROW glb embedded inline, "
                  f"SAWs deployed via iandees/artemis-viewer transforms"
                  f"{suffix}")
        else:
            print("[build]   /orion/urdf: AROW glb embedded inline with "
                  "SAWs stripped (URDF primitives in their place)")
    urdf_text = _rescale_urdf_xml(urdf_text)
    _validate_urdf_xml(urdf_text, source)
    desc_chan.log({"data": urdf_text}, log_time=ns_from(first_dt))


# ─────────────────────────── main ───────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None,
                        help=("MCAP path. Defaults to output/artemis-ii.mcap "
                              "or output/artemis-ii-<unit>.mcap when "
                              "--scene-unit-m != 1.0 (e.g. -Mm for 1e6)."))
    parser.add_argument("--scene-unit-m", type=float, default=1.0,
                        metavar="METRES_PER_UNIT",
                        help=("Number of metres one 3D scene unit represents. "
                              "Default 1.0 (status quo: scene in metres). Pass "
                              "1e6 to work in megameters — every 3D-channel "
                              "spatial value (TFs, sphere sizes, frustum, URDF "
                              "<mesh scale>/<origin xyz>/<box size>) is divided "
                              "by this factor, while plot/state values stay in "
                              "real km. Use this to avoid the Float32 precision "
                              "wall at orbital scale."))
    parser.add_argument("--no-photos", action="store_true",
                        help="Skip photo embedding (trajectory only)")
    parser.add_argument("--include-disabled-photos", action="store_true",
                        help="Include photos that Hank flagged as enabled=false. "
                             "(Off by default — these are training shots / dupes; "
                             "one is from 2025-01-30 and would drag the timeline back.)")
    parser.add_argument("--urdf", type=Path, default=MODELS / "orion.urdf",
                        help=("URDF file to embed on /orion/urdf. Defaults to "
                              "models/orion.urdf (AROW body mesh embedded inline "
                              "at build time with SAW solar arrays stripped, plus "
                              "URDF primitive panels). Use models/orion-primitives.urdf "
                              "for an offline build with simple primitives only."))
    parser.add_argument("--no-deploy-panels", action="store_true",
                        help=("Skip the SAW deployment step. The URDF is "
                              "embedded as-is with filename=\"orion.glb\"; "
                              "Foxglove cannot resolve that path from the "
                              "/orion/urdf topic, so prefer the default "
                              "deploy+embed path for distributable MCAPs."))
    args = parser.parse_args()

    if args.scene_unit_m <= 0:
        print("ERROR: --scene-unit-m must be positive", file=sys.stderr)
        return 1
    global SCENE_UNIT_M
    SCENE_UNIT_M = args.scene_unit_m
    if args.output is None:
        args.output = _scaled_default_output(SCENE_UNIT_M)
    print(f"[build] scene unit = {SCENE_UNIT_M:g} m / unit "
          f"({'metres' if SCENE_UNIT_M == 1.0 else f'1 unit = {SCENE_UNIT_M:g} m'})")

    args.output.parent.mkdir(parents=True, exist_ok=True)

    earth_csv = DATA / "horizons_earth.csv"
    moon_csv = DATA / "horizons_moon.csv"
    if not earth_csv.exists() or not moon_csv.exists():
        print(f"ERROR: missing Horizons CSVs. Run scripts/fetch_horizons.py first.",
              file=sys.stderr)
        return 1

    print("[build] loading horizons …")
    earth_rows = load_horizons(earth_csv)
    moon_rows = load_horizons(moon_csv)
    print(f"[build]   earth: {len(earth_rows)} samples in window")
    print(f"[build]   moon:  {len(moon_rows)} samples in window")
    if not earth_rows:
        print("ERROR: no Horizons rows fell inside the mission window.",
              file=sys.stderr)
        return 1

    photos: list[dict[str, Any]] = []
    if not args.no_photos:
        meta_path = DATA / "photos_meta.json"
        if meta_path.exists():
            photos = load_photos()
            print(f"[build] photos: {len(photos)} entries on disk")
        else:
            print("[build] photos: photos_meta.json missing — skipping image embed")

    schedule = load_schedule()
    if schedule:
        print(f"[build] schedule: {len(schedule)} crew-activity intervals")
    else:
        print("[build] schedule: data/schedule.json missing — skipping "
              "/orion/activity (run scripts/fetch_schedule.py)")

    print(f"[build] writing {args.output}")
    with foxglove.open_mcap(str(args.output), allow_overwrite=True) as writer:
        writer.write_metadata("mission", {
            "name": "Artemis II",
            "agency": "NASA",
            "liftoff_utc": LIFTOFF_UTC.isoformat(),
            "splashdown_utc": SPLASHDOWN_UTC.isoformat(),
            "ref_frame": "J2000 / ICRF (Earth-centered)",
            "units_3d": "meters (Foxglove convention)",
            "units_state": "kilometers / km/s (human-readable plots)",
            "source_trajectory": "JPL Horizons (-1024 vs 399/301)",
            "source_photos": "hankmt/Artemis-Timeline + NASA Flickr",
            "spice_ck": "not yet on NAIF; Artemis II archive expected ~6-9 mo post-mission",
        })

        tf_chan = FrameTransformChannel(topic="/tf")
        pose_chan = PoseInFrameChannel(topic="/orion/pose")
        scene_chan = SceneUpdateChannel(topic="/scene")
        trail_chan = SceneUpdateChannel(topic="/orion/trail")
        stars_chan = PointCloudChannel(topic="/scene/stars")
        camera_marker_chan = SceneUpdateChannel(topic="/camera/marker")
        events_chan = LogChannel(topic="/events")
        state_chan = Channel(topic="/orion/state", schema=STATE_SCHEMA)
        activity_chan = Channel(topic="/orion/activity", schema=ACTIVITY_SCHEMA)
        photo_meta_chan = Channel(topic="/photo/meta", schema=PHOTO_META_SCHEMA)
        milestones_chan = Channel(topic="/milestones", schema=MILESTONE_SCHEMA)

        # /robot_description — URDF as std_msgs/msg/String. Foxglove's URDF
        # layer matches on schema name, so this topic must declare *exactly*
        # `std_msgs/msg/String` (or `std_msgs/String`). We use a JSON encoding
        # for the message body since the SDK doesn't ship a ros1msg helper;
        # the URDF panel does `messageEvent.message.data` either way.
        ros_string_schema = Schema(
            name="std_msgs/msg/String",
            encoding="jsonschema",
            data=json.dumps({
                "title": "std_msgs/msg/String",
                "type": "object",
                "properties": {"data": {"type": "string"}},
            }).encode("utf-8"),
        )
        # Topic name deliberately != /robot_description: the URDF subsystem
        # special-cases that exact name in #shouldSubscribe (Urdfs.ts L888),
        # short-circuiting before the custom-layer subscription loop. So if
        # we publish on /robot_description, no custom URDF layer ever
        # subscribes and the layer silently renders nothing. Any other name
        # falls through to the layer-walking branch and works.
        desc_chan = Channel(
            topic="/orion/urdf",
            schema=ros_string_schema,
            message_encoding="json",
        )

        # All static / one-shot emissions are stamped at scene_start_dt
        # (= MISSION_WINDOW_START) so they appear from the moment the file
        # opens, even if the earliest real Horizons sample is hours later.
        # See `write_pre_horizons_anchor` for the orion-position stylization.
        scene_start_dt = MISSION_WINDOW_START

        write_static_scene(scene_chan, scene_start_dt)
        write_starfield(stars_chan, scene_start_dt)
        write_trajectory_trail(trail_chan, earth_rows, scene_start_dt)
        write_pre_horizons_anchor(
            tf_chan, pose_chan, state_chan,
            earth_rows[0],
            moon_rows[0] if moon_rows else None,
            scene_start_dt,
        )
        write_transforms_and_state(tf_chan, pose_chan, state_chan,
                                   earth_rows, moon_rows)
        write_milestones(milestones_chan, scene_start_dt)
        n_activity = write_activity(activity_chan, schedule, scene_start_dt)
        if n_activity:
            print(f"[build]   /orion/activity: {n_activity} interval boundaries")

        urdf_path: Path = args.urdf
        if urdf_path.exists():
            write_robot_description(desc_chan, urdf_path.read_text(),
                                    scene_start_dt,
                                    deploy_panels=not args.no_deploy_panels,
                                    source=str(urdf_path))
            print(f"[build]   /orion/urdf: 1 URDF ({urdf_path.name})")
        else:
            print(f"[build] WARN: {urdf_path} missing — URDF layer will be empty")

        cal_chan = write_camera_rig(tf_chan, camera_marker_chan, scene_start_dt)

        if photos:
            bumped = assign_unique_photo_timestamps(
                photos, include_disabled=args.include_disabled_photos)
            if bumped:
                print(f"[build]   photos: bumped {bumped} duplicate "
                      f"timestamp(s) by 1+ ns so each photo is "
                      f"individually seekable")
            counts = write_photos(photos, photo_meta_chan, cal_chan,
                                  camera_marker_chan,
                                  include_disabled=args.include_disabled_photos)
            n_logs = write_photo_log(events_chan, photos,
                                     include_disabled=args.include_disabled_photos)
            for t, c in sorted(counts.items()):
                print(f"[build]   {t}: {c} images")
            print(f"[build]   /events: {n_logs} photo log entries")

    print(f"[build] done → {args.output}")
    if SCENE_UNIT_M == 1.0:
        print(f"[build] Open the MCAP in Foxglove and import layout/artemis-ii.json")
    else:
        print(f"[build] Open the MCAP in Foxglove. NOTE: layout/artemis-ii.json's "
              f"cameraState.distance / near / far / axisSize / arrowScale and "
              f"the calibration topic distance/width are still authored in "
              f"metre units; divide them by {SCENE_UNIT_M:g} for this build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
