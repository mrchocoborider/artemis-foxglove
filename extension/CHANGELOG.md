# Changelog

## 0.1.0

Initial release.

- Adds a `photo-stepper` panel that subscribes to `/photo/meta` and lets you
  step through Artemis II photos one at a time via buttons or keyboard
  shortcuts (arrow keys, `j` / `k`, `Home` / `End`).
- Slideshow play/pause mode (`Space`) with adjustable tick interval
  (200 ms – 5 s).
- Per-camera filter (Nikon D5 / Z9, GoPro, iPhone, exterior, crew, ground).
- Drives the player by calling `context.seekPlayback()` so the 3D panel,
  plots, image panel, and other timeline-synced views all snap to the
  selected photo's timestamp.
