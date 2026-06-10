# Photo Stepper — Foxglove extension

Adds a custom panel that lets you step through the Artemis II photos one at a
time, or run a slideshow that jumps from photo timestamp to photo timestamp.
Each step calls `context.seekPlayback()`, so the 3D panel, plots, indicator,
and image panel all snap to the same moment.

Scaffolded with `npm init foxglove-extension@latest`.

## What it does

- Forward / back buttons with keyboard shortcuts:
  - `→` / `j` — next photo
  - `←` / `k` — previous photo
  - `Space` — toggle slideshow play/pause
  - `Home` / `End` — first / last photo
- Slideshow mode with adjustable tick interval (200 ms – 5 s)
- Per-camera filter (Nikon D5 / Z9 / GoPro / iPhone / exterior / crew / ground)
- Current photo's filename, camera, description, full-res Flickr URL, UTC time
- "N of M" counter

The panel subscribes to `/photo/meta` via `subscribeMessageRange`, so the full
list of photo timestamps is available immediately on mount — no waiting for
playback to surface metadata one-by-one.

## Develop

```sh
npm install              # install dev dependencies
npm run lint             # eslint
npm run build            # produces dist/extension.js
npm run local-install    # builds + installs into ~/.foxglove-studio/extensions (desktop)
npm run package          # produces a .foxe file for web/distribution
```

## Install into the Foxglove web app

```sh
npm run package
```

That produces `foxgloveinternal.artemis-foxglove-<version>.foxe`. In
app.foxglove.dev, drag-and-drop the `.foxe` onto the window, or go to your
profile → Extensions → Install from file.

## Install into the Foxglove desktop app

```sh
npm run local-install
```

Then restart Foxglove (or `Ctrl-R` to refresh).

## Layout integration

The panel ID used in `layout/artemis-ii.json` is:

```
artemis-foxglove.photo-stepper!stepper
```

The format is `<package-name>.<panel-name>!<instance-id>`, where:

- `<package-name>` is the `name` field in this extension's `package.json`
  (`artemis-foxglove`)
- `<panel-name>` is the `name` argument passed to
  `extensionContext.registerPanel()` (`photo-stepper`)
- `<instance-id>` is anything unique — the layout uses `stepper`

If you change either name field, update the layout JSON to match.

## Source layout

```
extension/
├── package.json        npm + create-foxglove-extension config
├── tsconfig.json       extends create-foxglove-extension/tsconfig
├── eslint.config.js    @foxglove/eslint-plugin rules
├── CHANGELOG.md
├── LICENSE
├── src/
│   ├── index.ts            activate() — registers the panel
│   └── PhotoStepperPanel.tsx the React panel itself
└── README.md           this file
```

## Notes

The panel doesn't render the image inline — that stays in the dedicated Image
panel beside it. The stepper just shows metadata and drives the player. If
you'd rather have a self-contained "scrubber + image" combo, swap the metadata
box for an `<img src={current.meta.media_url}>`.

Slideshow mode is decoupled from the player clock. When paused, the player
doesn't advance between ticks; when the player is also playing, the slideshow
keeps jumping. Use one or the other.

Keyboard shortcuts are scoped to the panel element — click into the panel
once so it has focus, then use the keys. `tabindex="0"` is set automatically.
