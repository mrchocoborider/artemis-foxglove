#!/usr/bin/env python3
"""
Fetch photo metadata from Hank Green's Artemis-Timeline repo, then download
and downsample each photo from Cloudflare R2 (artemistimeline.com/web/...).

The repo's photos.js is a tiny JS module that just sets `window.PHOTOS = [...]`.
We strip the wrapper, parse the JSON, and walk each entry. Most entries map to
an image on https://artemistimeline.com/web/<filename>; some are mp4/yt embeds
which we skip for the image pipeline.

Output:
    data/photos_meta.json   normalized list of photo entries
    data/web/<filename>     downsampled JPEGs (longest edge = --max-edge)

Re-runnable: skips files that already exist on disk.

Usage:
    python scripts/fetch_photos.py
    python scripts/fetch_photos.py --max-edge 1024
    python scripts/fetch_photos.py --skip-download  # metadata only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image

PHOTOS_JS_URL = "https://raw.githubusercontent.com/hankmt/Artemis-Timeline/main/photos.js"
MEDIA_BASE = "https://artemistimeline.com/web"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WEB_DIR = DATA_DIR / "web"

# Cameras / sources we care about for the image-panel pipeline. Extension-based
# routing — see Hank's README for the filename conventions.
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
VIDEO_PREFIXES = ("ig-", "yt-")
VIDEO_EXTS = {".mp4", ".mov"}


def _find_balanced_arrays(text: str) -> list[str]:
    """Return every top-level '[...]' substring in text, respecting strings
    and escape sequences. photos.js typically contains two assignments
    (window.PHOTOS = [...] and window.AUDIO = [...]); we yield each array."""
    arrays: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "[":
            i += 1
            continue
        depth = 0
        in_str = False
        quote: str | None = None
        start = i
        while i < n:
            c = text[i]
            if in_str:
                if c == "\\":  # skip the escaped char
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
                    arrays.append(text[start : i + 1])
                    i += 1
                    break
            i += 1
        else:
            # Reached end of text without closing — abort
            break
    return arrays


def parse_photos_js(text: str) -> list[dict[str, Any]]:
    """Hank's photos.js wraps one or more arrays in JS assignments.
    Find each balanced [...] block, JSON-parse it, and merge dict entries.

    Entries that look like photo records (have a filename/file key) are kept;
    audio entries (which use 'src' or 'audio') are skipped here — the photo
    pipeline only ingests image entries.
    """
    raw_arrays = _find_balanced_arrays(text)
    if not raw_arrays:
        raise ValueError("Couldn't find any JSON array in photos.js")

    merged: list[dict[str, Any]] = []
    for raw in raw_arrays:
        # Tolerate trailing commas (hand-edited JS file).
        payload = re.sub(r",(\s*[\]}])", r"\1", raw)
        try:
            arr = json.loads(payload)
        except json.JSONDecodeError as e:
            print(f"[photos]   skipping non-JSON-parseable array ({len(raw)} ch): {e}")
            continue
        if not isinstance(arr, list):
            continue
        for entry in arr:
            if isinstance(entry, dict) and (entry.get("file") or entry.get("filename")):
                merged.append(entry)

    if not merged:
        raise ValueError("No photo-like entries found in photos.js")
    return merged


def classify(entry: dict[str, Any]) -> str:
    """Bucket entries into camera topics for Foxglove."""
    fn = (entry.get("file") or entry.get("filename") or "").lower()
    camera = (entry.get("camera") or "").lower()
    if fn.startswith(VIDEO_PREFIXES) or any(fn.endswith(e) for e in VIDEO_EXTS):
        return "video"
    if "d5" in camera:
        return "nikon_d5"
    if "z9" in camera:
        return "nikon_z9"
    if "gopro" in camera:
        return "gopro"
    if "iphone" in camera:
        return "iphone"
    if "pixelink" in camera or "exterior" in camera:
        return "exterior"
    # Ground photographers (KSC-..., NHQ...) and unclassified fallback:
    if fn.startswith("ksc-") or fn.startswith("nhq"):
        return "ground"
    return "crew"


def download_and_resize(url: str, dst: Path, max_edge: int, session: requests.Session) -> bool:
    if dst.exists():
        return True
    tmp = dst.with_suffix(dst.suffix + ".part")
    try:
        r = session.get(url, timeout=60, stream=True)
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(64 * 1024):
                f.write(chunk)
        # Resize in-place
        with Image.open(tmp) as im:
            im = im.convert("RGB")
            w, h = im.size
            longest = max(w, h)
            if longest > max_edge:
                scale = max_edge / longest
                im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            im.save(dst, format="JPEG", quality=85, optimize=True)
        tmp.unlink(missing_ok=True)
        return True
    except Exception as e:
        print(f"  ERR {url}: {e}")
        tmp.unlink(missing_ok=True)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-edge", type=int, default=1280,
                        help="Longest-edge pixel target for downsampled photos")
    parser.add_argument("--skip-download", action="store_true",
                        help="Only fetch metadata, don't download images")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap number of photos for testing (0 = all)")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)

    sess = requests.Session()
    sess.headers["User-Agent"] = "artemis-foxglove/0.1 (https://artemistimeline.com)"

    print(f"[photos] fetching metadata: {PHOTOS_JS_URL}")
    r = sess.get(PHOTOS_JS_URL, timeout=30)
    r.raise_for_status()
    photos = parse_photos_js(r.text)
    print(f"[photos] parsed {len(photos)} entries")

    # Normalize each entry — different generations of photos.js have used
    # slightly different keys.
    normalized: list[dict[str, Any]] = []
    for p in photos:
        filename = p.get("file") or p.get("filename")
        if not filename:
            continue
        ts = p.get("time") or p.get("timestamp") or p.get("dt")
        if not ts:
            continue
        ext = Path(filename).suffix.lower()
        normalized.append({
            "filename": filename,
            "time_edt": ts,                          # raw timestamp string (EDT)
            "camera": p.get("camera") or "",
            "description": p.get("description") or p.get("title") or "",
            "topic": classify(p),
            "is_image": ext in IMAGE_EXTS,
            "is_video": ext in VIDEO_EXTS or filename.lower().startswith(VIDEO_PREFIXES),
            "media_url": f"{MEDIA_BASE}/{filename}",
            "raw": p,
        })

    meta_path = DATA_DIR / "photos_meta.json"
    meta_path.write_text(json.dumps(normalized, indent=2))
    print(f"[photos] wrote {len(normalized)} entries → {meta_path}")

    if args.skip_download:
        return 0

    images = [n for n in normalized if n["is_image"]]
    if args.limit:
        images = images[: args.limit]
    print(f"[photos] downloading + resizing {len(images)} images → {WEB_DIR}")

    ok = 0
    for i, n in enumerate(images, 1):
        dst = WEB_DIR / n["filename"]
        if download_and_resize(n["media_url"], dst, args.max_edge, sess):
            ok += 1
        if i % 25 == 0:
            print(f"  ... {i}/{len(images)} ({ok} ok)")
        # Be polite to R2
        time.sleep(0.05)

    print(f"[photos] done: {ok}/{len(images)} images cached")
    return 0


if __name__ == "__main__":
    sys.exit(main())
