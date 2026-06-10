import {
  Immutable,
  PanelExtensionContext,
  RenderState,
  Time,
} from "@foxglove/extension";
import {
  ReactElement,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";
import { createRoot } from "react-dom/client";

// Topic we read photo metadata from. The Artemis build_mcap.py writes one
// JSON message per photo here with a payload shaped like PhotoMeta below.
const META_TOPIC = "/photo/meta";

type PhotoMeta = {
  filename?: string;
  camera?: string;
  topic?: string;
  description?: string;
  media_url?: string;
};

type Photo = {
  /** UTC nanoseconds since epoch (BigInt to avoid float64 precision loss) */
  timeNs: bigint;
  meta: PhotoMeta;
};

type Config = {
  /** Slideshow tick interval in ms */
  intervalMs: number;
  /** Camera bucket filter; null = all cameras */
  cameraFilter: string | null;
};

const DEFAULT_CONFIG: Config = { intervalMs: 1500, cameraFilter: null };

function timeToNs(t: Time): bigint {
  return BigInt(t.sec) * 1_000_000_000n + BigInt(t.nsec);
}

function nsToTime(ns: bigint): Time {
  const sec = Number(ns / 1_000_000_000n);
  const nsec = Number(ns % 1_000_000_000n);
  return { sec, nsec };
}

/** Binary search: largest index with photos[i].timeNs <= ns; -1 if none. */
function findLastAtOrBefore(photos: Photo[], ns: bigint): number {
  let lo = 0;
  let hi = photos.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const item = photos[mid];
    if (item && item.timeNs <= ns) {
      ans = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return ans;
}

function PhotoStepperPanel({
  context,
}: {
  context: PanelExtensionContext;
}): ReactElement {
  // ---- Persisted config ----
  const initial = (context.initialState as Partial<Config> | undefined) ?? {};
  const [config, setConfig] = useState<Config>({ ...DEFAULT_CONFIG, ...initial });

  // ---- Mirrored player state ----
  const [currentTime, setCurrentTime] = useState<Time | undefined>(undefined);
  const [photos, setPhotos] = useState<Photo[]>([]);
  const [renderDone, setRenderDone] = useState<(() => void) | undefined>(undefined);
  const [isPlaying, setIsPlaying] = useState(false);

  // ---- Set up the panel context ----
  useLayoutEffect(() => {
    context.onRender = (renderState: Immutable<RenderState>, done) => {
      setRenderDone(() => done);
      if (renderState.currentTime) {
        setCurrentTime({ ...renderState.currentTime });
      }
    };

    context.watch("currentTime");
    context.watch("didSeek");

    // Pull every /photo/meta message via subscribeMessageRange — the modern
    // preload API. We get an async iterator of batches; accumulate into a
    // sorted Photo[] for fast lookup.
    const unsubscribe = context.subscribeMessageRange?.({
      topic: META_TOPIC,
      async onNewRangeIterator(batchIterator) {
        const collected: Photo[] = [];
        for await (const batch of batchIterator) {
          for (const ev of batch) {
            collected.push({
              timeNs: timeToNs(ev.receiveTime),
              meta: ev.message as PhotoMeta,
            });
          }
        }
        collected.sort((a, b) =>
          a.timeNs < b.timeNs ? -1 : a.timeNs > b.timeNs ? 1 : 0,
        );
        setPhotos(collected);
      },
    });

    return () => {
      unsubscribe?.();
    };
  }, [context]);

  // Tell Foxglove rendering is complete.
  useEffect(() => {
    renderDone?.();
  }, [renderDone]);

  // ---- Filtered view + current index ----
  const filtered = useMemo(() => {
    if (config.cameraFilter == null) {
      return photos;
    }
    return photos.filter((p) => p.meta.topic === config.cameraFilter);
  }, [photos, config.cameraFilter]);

  const cameras = useMemo(() => {
    const s = new Set<string>();
    for (const p of photos) {
      if (p.meta.topic) {
        s.add(p.meta.topic);
      }
    }
    return Array.from(s).sort();
  }, [photos]);

  const currentNs = useMemo(
    () => (currentTime ? timeToNs(currentTime) : null),
    [currentTime],
  );

  // Authoritative selection state. We don't recompute the displayed photo
  // from `currentTime` on every render via `findLastAtOrBefore` because
  // that lookup is fundamentally lossy when two photos share the same
  // log_time (the build script tries to give every photo a unique ns,
  // but if a tie ever sneaks back in, `findLastAtOrBefore` would always
  // snap to the LAST photo in the tie group, making the earlier photos
  // unreachable). Instead we treat the panel as the source of truth
  // for the active idx, only re-deriving from `currentTime` when the
  // filtered list changes or an external actor (timeline scrubber,
  // milestone-click, etc.) moves the playhead off our current photo.
  const [selectedIdx, setSelectedIdx] = useState<number>(-1);

  // Reconcile selection when the playhead or filtered list changes. We
  // intentionally exclude `selectedIdx` from the dep array and read its
  // value via a functional setState so this effect only fires on
  // *external* playhead changes — never as a follow-up to our own
  // optimistic update inside `seekToIdx`. Without that, the effect
  // would run synchronously after every click while `currentTime` is
  // still stale, snapping `selectedIdx` back to its previous value.
  useEffect(() => {
    if (currentNs == null || filtered.length === 0) {
      setSelectedIdx((prev) => (prev === -1 ? prev : -1));
      return;
    }
    setSelectedIdx((prev) => {
      const sel = filtered[prev];
      if (sel && sel.timeNs === currentNs) {
        return prev;
      }
      return findLastAtOrBefore(filtered, currentNs);
    });
  }, [currentNs, filtered]);

  const seekToIdx = useCallback(
    (idx: number) => {
      if (context.seekPlayback == undefined) {
        return;
      }
      if (idx < 0 || idx >= filtered.length) {
        return;
      }
      const target = filtered[idx];
      if (!target) {
        return;
      }
      // Optimistic: claim the new selection synchronously. The
      // reconciliation effect won't re-run until `currentTime` updates
      // (and when it does, `filtered[idx].timeNs === currentNs` will
      // hold, so the effect leaves `selectedIdx` alone).
      setSelectedIdx(idx);
      context.seekPlayback(nsToTime(target.timeNs));
    },
    [context, filtered],
  );

  const goPrev = useCallback(() => {
    if (filtered.length === 0) {
      return;
    }
    if (selectedIdx < 0) {
      seekToIdx(filtered.length - 1);
      return;
    }
    seekToIdx(Math.max(0, selectedIdx - 1));
  }, [filtered.length, selectedIdx, seekToIdx]);

  const goNext = useCallback(() => {
    if (filtered.length === 0) {
      return;
    }
    if (selectedIdx < 0) {
      seekToIdx(0);
      return;
    }
    seekToIdx(Math.min(filtered.length - 1, selectedIdx + 1));
  }, [filtered.length, selectedIdx, seekToIdx]);

  const goFirst = useCallback(() => {
    seekToIdx(0);
  }, [seekToIdx]);

  const goLast = useCallback(() => {
    seekToIdx(filtered.length - 1);
  }, [seekToIdx, filtered.length]);

  // ---- Slideshow timer ----
  useEffect(() => {
    if (!isPlaying) {
      return;
    }
    const id = window.setInterval(() => {
      if (selectedIdx >= filtered.length - 1) {
        setIsPlaying(false);
        return;
      }
      goNext();
    }, Math.max(100, config.intervalMs));
    return () => {
      window.clearInterval(id);
    };
  }, [isPlaying, config.intervalMs, selectedIdx, filtered.length, goNext]);

  // ---- Keyboard shortcuts (scoped to this panel) ----
  useEffect(() => {
    const el = context.panelElement;
    el.setAttribute("tabindex", "0");

    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (
        t &&
        (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)
      ) {
        return;
      }
      if (e.key === "ArrowRight" || e.key === "j") {
        goNext();
        e.preventDefault();
      } else if (e.key === "ArrowLeft" || e.key === "k") {
        goPrev();
        e.preventDefault();
      } else if (e.key === " ") {
        setIsPlaying((v) => !v);
        e.preventDefault();
      } else if (e.key === "Home") {
        goFirst();
        e.preventDefault();
      } else if (e.key === "End") {
        goLast();
        e.preventDefault();
      }
    };
    el.addEventListener("keydown", onKey);
    return () => {
      el.removeEventListener("keydown", onKey);
    };
  }, [context.panelElement, goNext, goPrev, goFirst, goLast]);

  // ---- Persist config changes ----
  // Skip saveState when the patch leaves config unchanged (e.g. range
  // slider drags that map to the same step value). Foxglove warns
  // "Panel action resulted in identical config" otherwise, and the
  // dispatch is wasted work either way.
  const updateConfig = useCallback(
    (patch: Partial<Config>) => {
      setConfig((prev) => {
        const next = { ...prev, ...patch };
        const changed = (Object.keys(patch) as (keyof Config)[]).some(
          (k) => prev[k] !== next[k],
        );
        if (changed) {
          context.saveState(next);
        }
        return next;
      });
    },
    [context],
  );

  // ---- Render ----
  const current = selectedIdx >= 0 ? filtered[selectedIdx] : undefined;
  const total = filtered.length;
  const seekSupported = context.seekPlayback != undefined;

  const btn: React.CSSProperties = {
    background: "#12122a",
    border: "1px solid #2a2a4e",
    borderRadius: 4,
    color: "#e0e0e0",
    padding: "6px 10px",
    cursor: "pointer",
    fontWeight: 700,
    fontSize: 13,
    minWidth: 36,
  };
  const btnActive: React.CSSProperties = {
    ...btn,
    background: "#1565c0",
    borderColor: "#1976d2",
  };

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "#0a0a14",
        color: "#e0e0e0",
        fontFamily:
          "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        padding: 12,
        gap: 10,
        outline: "none",
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 8,
          borderBottom: "1px solid #1a1a2e",
          paddingBottom: 6,
        }}
      >
        <div
          style={{
            fontVariantNumeric: "tabular-nums",
            fontWeight: 700,
            fontSize: 13,
            color: "#4FC3F7",
          }}
        >
          {total === 0 ? "—" : `${selectedIdx + 1} / ${total}`}
        </div>
        <div
          style={{
            fontSize: 10,
            letterSpacing: "0.5px",
            textTransform: "uppercase",
            color: "#999",
          }}
        >
          {current?.meta.camera ?? current?.meta.topic ?? "no photo selected"}
        </div>
      </div>

      {/* Controls */}
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <button title="First (Home)" style={btn} onClick={goFirst}>
          ⏮
        </button>
        <button title="Previous (← / k)" style={btn} onClick={goPrev}>
          ←
        </button>
        <button
          title="Play / Pause (Space)"
          style={isPlaying ? btnActive : btn}
          onClick={() => {
            setIsPlaying((v) => !v);
          }}
        >
          {isPlaying ? "⏸" : "▶"}
        </button>
        <button title="Next (→ / j)" style={btn} onClick={goNext}>
          →
        </button>
        <button title="Last (End)" style={btn} onClick={goLast}>
          ⏭
        </button>

        <select
          style={{
            background: "#12122a",
            border: "1px solid #2a2a4e",
            color: "#e0e0e0",
            borderRadius: 4,
            padding: "5px 8px",
            fontSize: 12,
          }}
          value={config.cameraFilter ?? ""}
          onChange={(e) => {
            updateConfig({ cameraFilter: e.target.value || null });
          }}
        >
          <option value="">All cameras</option>
          {cameras.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </div>

      {/* Slideshow tick */}
      <div
        style={{
          display: "flex",
          gap: 6,
          alignItems: "center",
          fontSize: 11,
          color: "#aaa",
        }}
      >
        <span>Slideshow tick</span>
        <input
          type="range"
          min={200}
          max={5000}
          step={100}
          value={config.intervalMs}
          onChange={(e) => {
            updateConfig({ intervalMs: Number(e.target.value) });
          }}
          style={{ flex: 1 }}
        />
        <span style={{ minWidth: 50, textAlign: "right" }}>
          {config.intervalMs} ms
        </span>
      </div>

      {/* Metadata */}
      <div
        style={{
          flex: 1,
          minHeight: 0,
          background: "rgba(0,0,0,0.25)",
          border: "1px solid #1a1a2e",
          borderRadius: 6,
          padding: 10,
          overflowY: "auto",
          fontSize: 12,
          lineHeight: 1.5,
        }}
      >
        {!seekSupported && (
          <div style={{ color: "#ffb74d", fontSize: 11 }}>
            ⚠ This data source doesn&apos;t support seeking — buttons are
            non-functional.
          </div>
        )}
        {!current && total === 0 && (
          <div style={{ color: "#ffb74d", fontSize: 11 }}>
            Waiting for {META_TOPIC} messages…
          </div>
        )}
        {current && (
          <>
            <Field label="Filename" value={current.meta.filename ?? "—"} />
            <Field label="Camera" value={current.meta.camera ?? "—"} />
            <Field label="Topic bucket" value={current.meta.topic ?? "—"} />
            {current.meta.description != undefined && (
              <Field label="Description" value={current.meta.description} />
            )}
            {current.meta.media_url != undefined && (
              <>
                <div
                  style={{
                    color: "#888",
                    fontSize: 10,
                    textTransform: "uppercase",
                    letterSpacing: "0.5px",
                  }}
                >
                  Full-res
                </div>
                <div
                  style={{
                    color: "#ddd",
                    wordBreak: "break-word",
                    marginBottom: 6,
                  }}
                >
                  <a
                    href={current.meta.media_url}
                    target="_blank"
                    rel="noreferrer"
                    style={{ color: "#4FC3F7" }}
                  >
                    {current.meta.media_url}
                  </a>
                </div>
              </>
            )}
            <Field
              label="UTC time"
              value={new Date(
                Number(current.timeNs / 1_000_000n),
              ).toISOString()}
            />
          </>
        )}
      </div>

      <div style={{ fontSize: 10, color: "#666" }}>
        ←/→ step · Space play/pause · Home/End jump · click panel first to
        capture keys
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }): ReactElement {
  return (
    <>
      <div
        style={{
          color: "#888",
          fontSize: 10,
          textTransform: "uppercase",
          letterSpacing: "0.5px",
        }}
      >
        {label}
      </div>
      <div
        style={{ color: "#ddd", wordBreak: "break-word", marginBottom: 6 }}
      >
        {value}
      </div>
    </>
  );
}

export function initPhotoStepperPanel(context: PanelExtensionContext): () => void {
  const root = createRoot(context.panelElement);
  root.render(<PhotoStepperPanel context={context} />);
  return () => {
    root.unmount();
  };
}
