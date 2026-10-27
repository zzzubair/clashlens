import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
} from "react";
import { Link, redirect, useLoaderData } from "react-router";

import type { BackHandle } from "../components/BackLink";
import type { WebsiteErrorResponse } from "../lib/contracts";
import {
  OVERLAY_FONTS,
  OVERLAY_PARTS,
  type OverlayAccount,
  type OverlaySettings,
} from "../lib/obs-overlay";
import { arrangementFor, type OverlaySize } from "../lib/obs-overlay-view";
import type { Route } from "./+types/account.obs-overlay";
import "../obs-overlay.css";

const PAGE = "/account/obs-overlay";
const SAVE = "/account/obs-overlay/save";

export const handle: BackHandle = { back: { to: "/account", label: "Your profile" } };

export function meta() {
  return [{ title: "Stream overlay · Clash Lens" }];
}

export interface ObsOverlayLoaderData {
  overlay: OverlayAccount | null;
  /** The site's public address, for the overlay's link. */
  origin: string;
  error: WebsiteErrorResponse | null;
}

/** GET /account/obs-overlay — set up the OBS overlay and copy its private link. */
export async function loader({
  request,
}: Route.LoaderArgs): Promise<ObsOverlayLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { getWebsiteConfig } = await import("../server/config.server");
  const origin = getWebsiteConfig().publicOrigin.origin;
  try {
    const { getOverlay } = await import("../services/obs-overlay.server");
    return { overlay: await getOverlay(identity), origin, error: null };
  } catch (error) {
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(error)) throw redirect("/account/setup");
    const { safeWebsiteError } = await import("../server/errors.server");
    return { overlay: null, origin, error: safeWebsiteError(error) };
  }
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

type Shape = OverlaySize["layout"];
type Fit = "screen" | "overlay";
type Part = (typeof OVERLAY_PARTS)[number];
type Corner = "tl" | "tc" | "tr" | "bl" | "bc" | "br";

// Starting sizes for each shape, in stream pixels (each one's natural size).
const PRESETS: Record<Shape, [number, number]> = {
  card: [366, 222],
  wide: [583, 146],
  strip: [1110, 66],
  column: [190, 440],
};
const SHAPES: Array<[Shape, string, string, [number, number]]> = [
  ["card", "Card", "a free corner", [44, 28]],
  ["wide", "Wide card", "under the game", [62, 16]],
  ["strip", "Slim strip", "top or bottom edge", [84, 8]],
  ["column", "Tall column", "beside the game", [16, 48]],
];
const MIN_W = 160;
const MIN_H = 56;
const STREAM_SIZES = ["1920x1080", "1280x720", "2560x1440"];
const CORNERS: Array<[Corner, string, string]> = [
  ["tl", "↖", "Top left"],
  ["tc", "↑", "Top middle"],
  ["tr", "↗", "Top right"],
  ["bl", "↙", "Bottom left"],
  ["bc", "↓", "Bottom middle"],
  ["br", "↘", "Bottom right"],
];
const PART_LABELS: Record<Part, string> = {
  trophies: "Trophies",
  rank: "Rank",
  attacks: "Attack squares",
  defenses: "Defense squares",
  net: "Net today",
  day: "Legend day number",
  updated: '"Updated" time',
};
const FONT_SAMPLE: Record<keyof typeof OVERLAY_FONTS, CSSProperties> = {
  clashlens: { fontFamily: "'Lilita One'" },
  clean: { fontFamily: "Inter", fontWeight: 800 },
  esports: { fontFamily: "'Barlow Condensed'", fontWeight: 700 },
  tech: { fontFamily: "Rajdhani", fontWeight: 700 },
  arcade: { fontFamily: "Bungee", fontWeight: 400 },
};
const LEFT_OUT: Partial<Record<Part, string>> = {
  updated: '"Updated" time',
  day: "Legend day number",
  net: "net today",
  rank: "rank",
};
const SHAPE_NAME: Record<Shape, string> = {
  card: "a card",
  wide: "a wide card",
  strip: "a strip",
  column: "a column",
};
const HELP: Record<Fit, string> = {
  screen:
    "OBS gets one see-through layer the size of your whole stream, and the overlay sits exactly where you put it here. Every change, the position included, shows live.",
  overlay:
    "OBS gets just the overlay, and you move it around in OBS yourself. A new layout or size also needs the new width and height typed into OBS.",
};

const margin = (cw: number) => Math.round((40 * cw) / 1920);

interface Form {
  tag: string | null;
  text: "light" | "dark";
  bg: "card" | "tint" | "none";
  font: keyof typeof OVERLAY_FONTS;
  show: Part[];
  popup: boolean;
  demo: boolean;
  fit: Fit;
}
interface Place {
  x: number;
  y: number;
  w: number;
  h: number;
  cw: number;
  ch: number;
  anchor: Corner | null;
}

function fromSaved(overlay: OverlayAccount): { form: Form; place: Place } {
  const st: OverlaySettings = overlay.settings;
  const pick = <T extends string>(
    value: string | undefined,
    allowed: readonly T[],
    fallback: T,
  ) => (allowed.includes(value as T) ? (value as T) : fallback);
  const cw = Number(st.cw) || 1920;
  const ch = Number(st.ch) || 1080;
  return {
    form: {
      tag: overlay.tag ?? overlay.players[0]?.tag ?? null,
      text: pick(st.text, ["light", "dark"], "light"),
      bg: pick(st.bg, ["card", "tint", "none"], "card"),
      font: pick(
        st.font,
        Object.keys(OVERLAY_FONTS) as Array<keyof typeof OVERLAY_FONTS>,
        "clashlens",
      ),
      show:
        st.show === undefined
          ? [...OVERLAY_PARTS]
          : OVERLAY_PARTS.filter((part) => st.show.split(",").includes(part)),
      popup: st.popup !== "0",
      demo: st.demo === "1",
      fit: st.fit === "overlay" ? "overlay" : "screen",
    },
    place: {
      w: Number(st.w) || PRESETS.card[0],
      h: Number(st.h) || PRESETS.card[1],
      cw,
      ch,
      x: Number(st.x) || margin(cw),
      y: Number(st.y) || margin(cw),
      anchor:
        (CORNERS.find(([corner]) => corner === st.snap)?.[0] as Corner | undefined) ??
        null,
    },
  };
}

/** The overlay's address query: what it shows, at what size. */
function overlayQuery(form: Form, place: Place): Record<string, string> {
  const q: Record<string, string> = {
    p: (form.tag ?? "").replace("#", ""),
    w: String(Math.round(place.w)),
    h: String(Math.round(place.h)),
    text: form.text,
    bg: form.bg,
  };
  if (form.font !== "clashlens") q.font = form.font;
  if (form.show.length !== OVERLAY_PARTS.length) q.show = form.show.join(",");
  if (!form.popup) q.popup = "0";
  if (form.demo) q.demo = "1";
  return q;
}

/** Everything saved against the private link: the query, and where it sits. */
function savedSettings(form: Form, place: Place): OverlaySettings {
  const q = overlayQuery(form, place);
  delete q.p;
  return {
    ...q,
    fit: form.fit,
    x: String(Math.round(place.x)),
    y: String(Math.round(place.y)),
    cw: String(place.cw),
    ch: String(place.ch),
    ...(place.anchor ? { snap: place.anchor } : {}),
  };
}

function clamp(place: Place): Place {
  const w = Math.min(place.w, place.cw);
  const h = Math.min(place.h, place.ch);
  return {
    ...place,
    w,
    h,
    x: Math.min(Math.max(0, place.x), place.cw - w),
    y: Math.min(Math.max(0, place.y), place.ch - h),
  };
}

function snapTo(place: Place, where: Corner): Place {
  const m = margin(place.cw);
  return clamp({
    ...place,
    anchor: where,
    x: where.includes("l")
      ? m
      : where.includes("r")
        ? place.cw - m - place.w
        : (place.cw - place.w) / 2,
    y: where.includes("t") ? m : place.ch - m - place.h,
  });
}

// A new size keeps the overlay in its snapped spot, else where it was.
const settle = (place: Place) =>
  place.anchor ? snapTo(place, place.anchor) : clamp(place);

export default function ObsOverlayRoute() {
  const data = useLoaderData<typeof loader>();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell obs-settings">
      <section className="hero" aria-labelledby="obs-overlay-title">
        <h1 id="obs-overlay-title">Stream overlay</h1>
        <p className="lede">Your live Legend day, on stream.</p>
      </section>
      {data.overlay === null ? (
        <aside className="notice notice-unavailable" role="alert">
          <strong>Your Stream overlay could not be loaded.</strong>{" "}
          <a href={PAGE}>Try again</a>
        </aside>
      ) : data.overlay.players.length === 0 ? (
        <section className="form-panel">
          <h2>Link a player first</h2>
          <p className="obs-help">
            The overlay shows one of your verified players.{" "}
            <Link to="/account/verify-player">Link a player</Link>, then come back here.
          </p>
        </section>
      ) : (
        <OverlaySetup overlay={data.overlay} origin={data.origin} />
      )}
    </main>
  );
}

function OverlaySetup({ overlay, origin }: { overlay: OverlayAccount; origin: string }) {
  const initial = useRef(fromSaved(overlay));
  const [form, setForm] = useState<Form>(initial.current.form);
  const [place, setPlace] = useState<Place>(() => clamp(initial.current.place));
  const [link, setLink] = useState(overlay.link);
  const [saved, setSaved] = useState<{ text: string; ok: boolean }>({
    text: "Changes save by themselves.",
    ok: false,
  });
  const [copied, setCopied] = useState(false);
  const [fitInfo, setFitInfo] = useState<{ text: string; warn: boolean }>({
    text: "",
    warn: false,
  });
  const [guides, setGuides] = useState<{ v: number | null; h: number | null }>({
    v: null,
    h: null,
  });
  const [scale, setScale] = useState(0);
  const [dragging, setDragging] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const drag = useRef<{
    sx: number;
    sy: number;
    start: Place;
    dir: string | null;
    k: number;
  } | null>(null);
  const [initialQuery] = useState(() =>
    new URLSearchParams(overlayQuery(form, place)).toString(),
  );
  const query = new URLSearchParams(overlayQuery(form, place)).toString();
  const settings = savedSettings(form, place);
  const savedJson = JSON.stringify({ tag: form.tag, settings });
  const lastSaved = useRef(
    JSON.stringify({
      tag: overlay.tag,
      settings: savedSettings(initial.current.form, initial.current.place),
    }),
  );
  const latest = useRef({ json: savedJson, tag: form.tag, settings });
  latest.current = { json: savedJson, tag: form.tag, settings };
  const saving = useRef(false);
  const address = `${origin}/o/${link}`;
  const screen = form.fit === "screen";
  const [w, h] = screen
    ? [place.cw, place.ch]
    : [Math.round(place.w), Math.round(place.h)];
  const update = (change: Partial<Form>) =>
    setForm((current) => ({ ...current, ...change }));

  // The preview scales the whole stream down to the page's width.
  useEffect(() => {
    const element = wrap.current;
    if (!element) return;
    const measure = () => setScale(element.clientWidth / place.cw);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [place.cw]);

  // The preview takes every change at once, without loading again.
  useEffect(() => {
    frame.current?.contentWindow?.postMessage(
      { type: "cl-overlay-settings", search: `?${query}` },
      location.origin,
    );
  }, [query]);

  // The overlay says how it arranged itself in the box, and what it left out.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (
        event.origin !== location.origin ||
        event.source !== frame.current?.contentWindow
      )
        return;
      const message = event.data as OverlaySize | { type?: string };
      if (message.type === "cl-overlay-ready") {
        frame.current?.contentWindow?.postMessage(
          { type: "cl-overlay-settings", search: `?${queryRef.current}` },
          location.origin,
        );
        return;
      }
      if (message.type !== "cl-overlay-size") return;
      const size = message as OverlaySize;
      let text = `Shown as ${SHAPE_NAME[size.layout]}.`;
      if (size.hidden.length)
        text += ` Left out to fit: ${size.hidden.map((part) => LEFT_OUT[part]).join(", ")}.`;
      if (size.scale < 0.6) text += " Too small to read on stream: make it bigger.";
      setFitInfo({ text, warn: size.scale < 0.6 || size.hidden.length > 0 });
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);
  const queryRef = useRef(query);
  queryRef.current = query;

  // Saved on Clash Lens against the private link, 0.3 seconds after the last
  // change, one save at a time: the newest choices go once the one sending lands.
  useEffect(() => {
    if (savedJson === lastSaved.current) return;
    const timer = setTimeout(async () => {
      if (saving.current) return;
      saving.current = true;
      try {
        while (latest.current.json !== lastSaved.current) {
          const next = latest.current;
          setSaved({ text: "Saving…", ok: false });
          await post<{ version: number }>({
            intent: "save",
            tag: next.tag ?? "",
            settings: JSON.stringify(next.settings),
          });
          lastSaved.current = next.json;
        }
        setSaved({
          text: "Saved. Your overlay in OBS updates within 3 seconds.",
          ok: true,
        });
      } catch {
        setSaved({ text: "Not saved. Check your connection.", ok: false });
      } finally {
        saving.current = false;
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [savedJson]);

  function resize(w: number, h: number) {
    setPlace((current) => settle({ ...current, w, h }));
  }

  function onPointerDown(event: PointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    const dir = (event.target as HTMLElement).dataset.dir ?? null;
    drag.current = {
      sx: event.clientX,
      sy: event.clientY,
      start: { ...place, anchor: null },
      dir,
      k: scale,
    };
    setPlace((current) => ({ ...current, anchor: null }));
    setDragging(true);
  }

  function onPointerMove(event: PointerEvent<HTMLDivElement>) {
    const d = drag.current;
    if (!d) return;
    const dx = (event.clientX - d.sx) / d.k;
    const dy = (event.clientY - d.sy) / d.k;
    const { cw, ch } = d.start;
    if (d.dir) {
      // Resize from the grabbed edge or corner; the opposite edge stays put.
      let { x, y, w, h } = d.start;
      if (d.dir.includes("e")) w = Math.min(cw - x, Math.max(MIN_W, w + dx));
      if (d.dir.includes("s")) h = Math.min(ch - y, Math.max(MIN_H, h + dy));
      if (d.dir.includes("w")) {
        const nw = Math.min(x + w, Math.max(MIN_W, w - dx));
        x = x + w - nw;
        w = nw;
      }
      if (d.dir.includes("n")) {
        const nh = Math.min(y + h, Math.max(MIN_H, h - dy));
        y = y + h - nh;
        h = nh;
      }
      setPlace({ ...d.start, x, y, w, h });
      return;
    }
    let x = d.start.x + dx;
    let y = d.start.y + dy;
    // Free by default: it stays exactly where it is dropped. Hold Shift to
    // snap to the edges and the middle.
    const snap = event.shiftKey ? 14 / d.k : 0;
    const m = margin(cw);
    const { w, h } = d.start;
    let v: number | null = null;
    let hz: number | null = null;
    if (snap) {
      for (const [at, line] of [
        [m, m],
        [cw - m - w, cw - m],
        [(cw - w) / 2, cw / 2],
      ]) {
        if (Math.abs(x - at) < snap) {
          x = at;
          v = line;
          break;
        }
      }
      for (const [at, line] of [
        [m, m],
        [ch - m - h, ch - m],
        [(ch - h) / 2, ch / 2],
      ]) {
        if (Math.abs(y - at) < snap) {
          y = at;
          hz = line;
          break;
        }
      }
    }
    setGuides({ v, h: hz });
    setPlace(clamp({ ...d.start, x, y }));
  }

  function endDrag() {
    drag.current = null;
    setDragging(false);
    setGuides({ v: null, h: null });
  }

  // Arrow keys nudge it 1 pixel (10 with Shift) for an exact gap.
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const step = event.shiftKey ? 10 : 1;
    const move = (
      {
        ArrowLeft: [-step, 0],
        ArrowRight: [step, 0],
        ArrowUp: [0, -step],
        ArrowDown: [0, step],
      } as Record<string, [number, number]>
    )[event.key];
    if (!move) return;
    event.preventDefault();
    setPlace((current) =>
      clamp({ ...current, anchor: null, x: current.x + move[0], y: current.y + move[1] }),
    );
  }

  async function replaceLink() {
    if (
      !window.confirm(
        "Make a new link? The old one is deleted for good and shows nothing from now on, so you'll need to paste the new one into OBS.",
      )
    )
      return;
    try {
      const made = await post<{ link: string }>({ intent: "new-link", replaces: link });
      setLink(made.link);
      setSaved({
        text: "New link made. The old one is deleted and shows nothing. Paste the new one into OBS.",
        ok: true,
      });
    } catch {
      setSaved({ text: "No new link made. Check your connection.", ok: false });
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(address);
    } catch {
      const input = document.getElementById("obs-address") as HTMLInputElement | null;
      input?.select();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2500);
  }

  const box = { left: place.x, top: place.y, width: place.w, height: place.h };
  const shape = arrangementFor(place.w, place.h);
  return (
    <div className="obs-layout">
      <form onSubmit={(event) => event.preventDefault()}>
        <section className="form-panel" aria-labelledby="obs-player">
          <h2 id="obs-player">
            <span className="obs-step">1</span> Player
          </h2>
          <div className="obs-players">
            {overlay.players.map((player) => (
              <label className="obs-player" key={player.tag}>
                <input
                  type="radio"
                  name="player"
                  value={player.tag}
                  checked={form.tag === player.tag}
                  onChange={() => update({ tag: player.tag })}
                />
                <span>
                  <span className="obs-player-name">{player.name ?? player.tag}</span>
                  <br />
                  <span className="obs-player-tag">{player.tag}</span>
                </span>
                <span className={`obs-badge${player.league === "Legend I" ? " ok" : ""}`}>
                  {player.league === "Legend I"
                    ? "Legend I"
                    : player.league
                      ? "Not in Legend"
                      : "No data"}
                </span>
              </label>
            ))}
          </div>
        </section>

        <section className="form-panel" aria-labelledby="obs-layout">
          <h2 id="obs-layout">
            <span className="obs-step">2</span> Layout
          </h2>
          <p className="obs-help">Pick a shape, then drag its edges in the preview.</p>
          <div className="obs-shapes" role="radiogroup" aria-label="Shape">
            {SHAPES.map(([value, label, hint, [pw, ph]]) => (
              <label className="obs-shape" key={value}>
                <input
                  type="radio"
                  name="shape"
                  value={value}
                  checked={shape === value}
                  onChange={() => {}}
                  onClick={() => {
                    const [sw, sh] = PRESETS[value];
                    resize((sw * place.cw) / 1920, (sh * place.cw) / 1920);
                  }}
                />
                <span className="obs-shape-pic" aria-hidden="true">
                  <i style={{ width: pw, height: ph }} />
                </span>
                {label}
                <small>{hint}</small>
              </label>
            ))}
          </div>
          <div className="obs-field">
            <span className="obs-label">Size on stream</span>
            <SizeInputs place={place} onChange={resize} />
            <p className={`obs-fitinfo${fitInfo.warn ? " warn" : ""}`} aria-live="polite">
              {fitInfo.text}
            </p>
          </div>
          <div className="obs-field">
            <span className="obs-label">Font</span>
            <div className="obs-fonts" role="radiogroup" aria-label="Font">
              {(Object.keys(OVERLAY_FONTS) as Array<keyof typeof OVERLAY_FONTS>).map(
                (font) => (
                  <label className="obs-font" key={font}>
                    <input
                      type="radio"
                      name="font"
                      value={font}
                      checked={form.font === font}
                      onChange={() => update({ font })}
                    />
                    <b style={FONT_SAMPLE[font]} aria-hidden="true">
                      5,091 +40
                    </b>
                    <small>{OVERLAY_FONTS[font]}</small>
                  </label>
                ),
              )}
            </div>
          </div>
          <div className="obs-field">
            <span className="obs-label" id="obs-text">
              Text
            </span>
            <Segments
              label="obs-text"
              name="text"
              value={form.text}
              options={[
                ["light", "Light"],
                ["dark", "Dark"],
              ]}
              onChange={(text) => update({ text })}
            />
          </div>
          <div className="obs-field">
            <span className="obs-label" id="obs-bg">
              Background
            </span>
            <Segments
              label="obs-bg"
              name="bg"
              value={form.bg}
              options={[
                ["card", "Card"],
                ["tint", "See-through"],
                ["none", "None"],
              ]}
              onChange={(bg) => update({ bg })}
            />
          </div>
        </section>

        <section className="form-panel" aria-labelledby="obs-shows">
          <h2 id="obs-shows">
            <span className="obs-step">3</span> What it shows
          </h2>
          <div className="obs-checks">
            {OVERLAY_PARTS.map((part) => (
              <label key={part}>
                <input
                  type="checkbox"
                  name="show"
                  value={part}
                  checked={form.show.includes(part)}
                  onChange={(event) =>
                    update({
                      show: OVERLAY_PARTS.filter((each) =>
                        each === part ? event.target.checked : form.show.includes(each),
                      ),
                    })
                  }
                />
                {PART_LABELS[part]}
              </label>
            ))}
          </div>
          <label className="obs-toggle">
            <input
              type="checkbox"
              name="popup"
              checked={form.popup}
              onChange={(event) => update({ popup: event.target.checked })}
            />
            <span>
              Pop-up when a battle lands
              <small>Shows the trophies, stars and opponent for 6 seconds.</small>
            </span>
          </label>
        </section>
      </form>

      <div>
        <section className="form-panel" aria-labelledby="obs-preview">
          <div className="obs-preview-head">
            <h2 id="obs-preview">
              Preview{" "}
              <span className="obs-help">
                your stream, to scale. Drag the overlay to move it (hold Shift to snap to
                edges), drag an edge or corner to resize, or click it and use the arrow
                keys.
              </span>
            </h2>
            <div className="obs-pos" role="group" aria-label="Snap to">
              {CORNERS.map(([corner, arrow, title]) => (
                <button
                  type="button"
                  className="button button-secondary"
                  key={corner}
                  title={title}
                  aria-label={title}
                  aria-pressed={place.anchor === corner}
                  onClick={() => setPlace((current) => snapTo(current, corner))}
                >
                  {arrow}
                </button>
              ))}
            </div>
          </div>
          <div className="obs-place">
            <div className="obs-place-row">
              <Segments
                label="obs-placement"
                ariaLabel="Placement"
                name="fit"
                value={form.fit}
                options={[
                  ["screen", "Place it here"],
                  ["overlay", "Place it in OBS"],
                ]}
                onChange={(fit) => update({ fit })}
              />
              <label>
                Stream size{" "}
                <select
                  value={`${place.cw}x${place.ch}`}
                  onChange={(event) => {
                    const [cw, ch] = event.target.value.split("x").map(Number);
                    setPlace((current) =>
                      settle({
                        ...current,
                        x: (current.x * cw) / current.cw,
                        y: (current.y * ch) / current.ch,
                        cw,
                        ch,
                      }),
                    );
                  }}
                >
                  {STREAM_SIZES.map((size) => (
                    <option key={size} value={size}>
                      {size.replace("x", " × ")}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <p className="obs-help">{HELP[form.fit]}</p>
          </div>
          <div
            className="obs-stagewrap"
            ref={wrap}
            style={{ aspectRatio: `${place.cw} / ${place.ch}` }}
          >
            <div
              className="obs-canvas"
              style={{ width: place.cw, height: place.ch, transform: `scale(${scale})` }}
            >
              <div className="obs-cam">camera</div>
              <iframe
                ref={frame}
                title="Overlay preview"
                src={`/account/obs-overlay/preview?${initialQuery}`}
                style={box}
              />
              {guides.v !== null ? (
                <div className="obs-guide v" style={{ left: guides.v }} />
              ) : null}
              {guides.h !== null ? (
                <div className="obs-guide h" style={{ top: guides.h }} />
              ) : null}
              <div
                className={`obs-handle${dragging ? " dragging" : ""}`}
                style={box}
                role="group"
                tabIndex={0}
                aria-label="Drag to place the overlay; drag an edge or corner to resize"
                onPointerDown={onPointerDown}
                onPointerMove={onPointerMove}
                onPointerUp={endDrag}
                onPointerCancel={endDrag}
                onKeyDown={onKeyDown}
              >
                <span className="obs-tip">
                  x {Math.round(place.x)}, y {Math.round(place.y)} · {Math.round(place.w)}{" "}
                  × {Math.round(place.h)}
                </span>
                {["nw", "n", "ne", "e", "se", "s", "sw", "w"].map((dir) => (
                  <i className="obs-rz" data-dir={dir} key={dir} />
                ))}
              </div>
            </div>
          </div>
          <div className="obs-under">
            <button
              type="button"
              className="button obs-demo"
              aria-pressed={form.demo}
              onClick={() => update({ demo: !form.demo })}
            >
              {form.demo ? "■ Stop demo" : "▶ Start demo"}
            </button>
            <button
              type="button"
              className="button button-secondary"
              onClick={() =>
                frame.current?.contentWindow?.postMessage(
                  { type: "cl-overlay-test", kind: "attack" },
                  location.origin,
                )
              }
            >
              Test attack pop-up
            </button>
            <button
              type="button"
              className="button button-secondary"
              onClick={() =>
                frame.current?.contentWindow?.postMessage(
                  { type: "cl-overlay-test", kind: "defense" },
                  location.origin,
                )
              }
            >
              Test defense pop-up
            </button>
            <p className={`obs-demonote${form.demo ? " on" : ""}`}>
              {form.demo
                ? "Demo is on: your overlay in OBS is showing made-up battles, not your real day. Stop it before you go live."
                : "Demo plays made-up battles every 12 seconds, here and in OBS, so you can see it working before a real battle lands. It's only a test: stop it before you go live."}
            </p>
          </div>
        </section>

        <section className="form-panel" aria-labelledby="obs-add">
          <h2 id="obs-add">
            <span className="obs-step">4</span> Add it to OBS
          </h2>
          <p className="obs-help">
            Your own private link. Paste it into OBS once: every change you make on this
            page shows on stream within 3 seconds, with no new link.
          </p>
          <div className="obs-addr">
            <input
              id="obs-address"
              readOnly
              value={address}
              aria-label="Overlay address"
            />
            <button type="button" className="button button-primary" onClick={copy}>
              Copy
            </button>
          </div>
          <div className="obs-under">
            <span className={`obs-saved${saved.ok ? " ok" : ""}`} role="status">
              {saved.text}
            </span>
            {copied ? (
              <span className="obs-copied">Copied. Paste it into OBS.</span>
            ) : null}
            <button
              type="button"
              className="button button-secondary obs-replace"
              onClick={replaceLink}
            >
              Make a new link
            </button>
          </div>
          <div className="obs-dims">
            <div className="obs-dim">
              <span>Width</span>
              <b>{w}</b>
            </div>
            <div className="obs-dim">
              <span>Height</span>
              <b>{h}</b>
            </div>
          </div>
          <ol className="obs-steps">{steps(form.fit, w, h)}</ol>
          <p className="obs-note">
            Clash Lens checks each Legend player about every 2 to 3 minutes, and every 30
            seconds while your overlay is open in OBS, so a battle shows up on the overlay
            a couple of minutes after it ends, not the second it ends.
          </p>
        </section>
      </div>
    </div>
  );
}

function SizeInputs({
  place,
  onChange,
}: {
  place: Place;
  onChange: (w: number, h: number) => void;
}) {
  // Typed sizes apply when the field is left, as the overlay can't be smaller
  // than 160 × 56 or bigger than the stream.
  const [typed, setTyped] = useState<{ w: string; h: string } | null>(null);
  const shown = typed ?? {
    w: String(Math.round(place.w)),
    h: String(Math.round(place.h)),
  };
  const commit = () => {
    if (!typed) return;
    onChange(
      Math.min(place.cw, Math.max(MIN_W, Number(typed.w) || place.w)),
      Math.min(place.ch, Math.max(MIN_H, Number(typed.h) || place.h)),
    );
    setTyped(null);
  };
  return (
    <div className="obs-wh">
      {(["w", "h"] as const).map((key) => (
        <label key={key}>
          {key === "w" ? "Width" : "Height"}{" "}
          <input
            type="number"
            min={key === "w" ? MIN_W : MIN_H}
            max={key === "w" ? 2560 : 1440}
            step={1}
            value={shown[key]}
            onChange={(event) => setTyped({ ...shown, [key]: event.target.value })}
            onBlur={commit}
            onKeyDown={(event) => {
              if (event.key === "Enter") commit();
            }}
          />
        </label>
      ))}
    </div>
  );
}

function Segments<T extends string>({
  label,
  ariaLabel,
  name,
  value,
  options,
  onChange,
}: {
  label: string;
  ariaLabel?: string;
  name: string;
  value: T;
  options: Array<[T, string]>;
  onChange: (value: T) => void;
}) {
  return (
    <span
      className="obs-seg"
      role="radiogroup"
      {...(ariaLabel ? { "aria-label": ariaLabel } : { "aria-labelledby": label })}
    >
      {options.map(([option, text]) => (
        <label key={option}>
          <input
            type="radio"
            name={name}
            value={option}
            checked={value === option}
            onChange={() => onChange(option)}
          />
          {text}
        </label>
      ))}
    </span>
  );
}

function steps(fit: Fit, w: number, h: number): ReactNode[] {
  const browser = (
    <>
      In OBS, under <b>Sources</b>, click <kbd>+</kbd> and choose <b>Browser</b>. Name it
      &quot;Clash Lens&quot;.
    </>
  );
  const items =
    fit === "screen"
      ? [
          browser,
          <>
            Paste the address into <b>URL</b>. Set <b>Width</b> {w} and <b>Height</b> {h},
            the size of your whole stream. Click <b>OK</b>.
          </>,
          <>
            It already sits where you placed it here. If it doesn&apos;t fill the screen,
            right-click it, then <b>Transform</b> › <b>Fit to screen</b>.
          </>,
          <>
            Click the <b>padlock</b> next to it in Sources. It covers the whole screen, so
            locking it stops it catching clicks meant for your other sources.
          </>,
          <>
            That&apos;s it. To move it, resize it, or change what it shows, just change it
            here: OBS picks it up within 3 seconds.
          </>,
        ]
      : [
          browser,
          <>
            Paste the address into <b>URL</b>. Set <b>Width</b> {w} and <b>Height</b> {h}.
            Click <b>OK</b>.
          </>,
          <>
            Drag it where you want it in OBS. Changes here show within 3 seconds, but if
            you change the <b>Layout</b> or <b>Size</b>, also set the new width and height
            in the source&apos;s <b>Properties</b> so it isn&apos;t cut off.
          </>,
        ];
  return items.map((item, index) => <li key={index}>{item}</li>);
}

/** The save route's own JSON answer. Anything else throws, a redirect to sign
 * in included. */
async function post<T>(values: Record<string, string>): Promise<T> {
  const res = await fetch(SAVE, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(values),
    redirect: "manual",
  });
  if (!res.ok) throw new Error(String(res.status));
  return (await res.json()) as T;
}
