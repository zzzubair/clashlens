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
  origin: string;
  error: WebsiteErrorResponse | null;
}

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
type Part = string;
type Corner = "tl" | "tc" | "tr" | "bl" | "bc" | "br";

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
export interface Place {
  x: number;
  y: number;
  w: number;
  h: number;
  cw: number;
  ch: number;
  anchor: Corner | null;
}

function fromSaved(overlay: OverlayAccount): { form: Form; place: Place } {
  const contract = overlay.settingsContract;
  const st: OverlaySettings = overlay.settings;
  const parts = Object.keys(PART_LABELS).filter((part) => contract.parts.includes(part));
  const pick = <T extends string>(
    value: string | undefined,
    allowed: readonly T[],
    fallback: T | undefined,
  ) => (allowed.includes(value as T) ? (value as T) : (fallback as T));
  const cw = Number(st.cw) || Number(contract.defaults.cw);
  const ch = Number(st.ch) || Number(contract.defaults.ch);
  const show = st.show;
  return {
    form: {
      tag: overlay.tag ?? overlay.players[0]?.tag ?? null,
      text: pick(st.text, contract.choices.text, contract.defaults.text) as Form["text"],
      bg: pick(st.bg, contract.choices.bg, contract.defaults.bg) as Form["bg"],
      font: pick(
        st.font,
        contract.choices.font,
        contract.defaults.font ?? "clashlens",
      ) as Form["font"],
      show:
        show === undefined
          ? [...parts]
          : parts.filter((part) => show.split(",").includes(part)),
      popup: st.popup !== "0",
      demo: st.demo === "1",
      fit: st.fit === "overlay" ? "overlay" : "screen",
    },
    place: {
      w: Number(st.w) || Number(contract.defaults.w),
      h: Number(st.h) || Number(contract.defaults.h),
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

function overlayQuery(form: Form, place: Place, parts: string[]): Record<string, string> {
  const q: Record<string, string> = {
    p: (form.tag ?? "").replace("#", ""),
    w: String(Math.round(place.w)),
    h: String(Math.round(place.h)),
    text: form.text,
    bg: form.bg,
  };
  if (form.font !== "clashlens") q.font = form.font;
  if (form.show.length !== parts.length) q.show = form.show.join(",");
  if (!form.popup) q.popup = "0";
  if (form.demo) q.demo = "1";
  return q;
}

function savedSettings(form: Form, place: Place, parts: string[]): OverlaySettings {
  const q = overlayQuery(form, place, parts);
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
            <Link to="/account/verify-player?return=%2Faccount%2Fobs-overlay">
              Link a player
            </Link>
            , then come back here.
          </p>
        </section>
      ) : (
        <OverlaySetup overlay={data.overlay} origin={data.origin} />
      )}
    </main>
  );
}

export type SaveStatus = "untouched" | "saving" | "saved" | "notSaved";

const SAVE_STATUS: Record<SaveStatus, { text: string; ok: boolean }> = {
  untouched: { text: "Changes save by themselves.", ok: false },
  saving: { text: "Saving…", ok: false },
  saved: { text: "Saved. Your overlay in OBS updates within 3 seconds.", ok: true },
  notSaved: { text: "Not saved. Check your connection.", ok: false },
};

export type LinkStatus = "made" | "unconfirmed" | null;

const LINK_STATUS: Record<"made" | "unconfirmed", { text: string; ok: boolean }> = {
  made: {
    text: "New link made. The old one is deleted and shows nothing. Paste the new one into OBS.",
    ok: true,
  },
  unconfirmed: {
    text: "Couldn't confirm the new link. The old one may already be retired. Try again to get the current link.",
    ok: false,
  },
};

export type Autosave = { phase: "idle" | "sending"; saved: string | null };

export type Choices = { json: string; tag: string | null; settings: OverlaySettings };

export async function runAutosave(
  autosave: { current: Autosave },
  latest: () => Choices,
  send: (next: Choices) => Promise<unknown>,
  show: (status: SaveStatus) => void,
): Promise<void> {
  if (autosave.current.phase === "sending") return;
  autosave.current = { ...autosave.current, phase: "sending" };
  try {
    while (latest().json !== autosave.current.saved) {
      const next = latest();
      show("saving");
      await send(next);
      autosave.current = { phase: "sending", saved: next.json };
    }
    show("saved");
  } catch {
    autosave.current = { phase: "sending", saved: null };
    show("notSaved");
  } finally {
    autosave.current = { ...autosave.current, phase: "idle" };
  }
}

export type PointerDrag = {
  sx: number;
  sy: number;
  start: Place;
  dir: string | null;
  k: number;
};

export type Guides = { v: number | null; h: number | null };

export function startDrag(
  place: Place,
  sx: number,
  sy: number,
  dir: string | null,
  k: number,
): PointerDrag {
  return { sx, sy, start: { ...place, anchor: null }, dir, k };
}

export function dragTo(
  d: PointerDrag | null,
  px: number,
  py: number,
  shift: boolean,
): { place: Place; guides: Guides | null } | null {
  if (!d) return null;
  const dx = (px - d.sx) / d.k;
  const dy = (py - d.sy) / d.k;
  const { cw, ch } = d.start;
  if (d.dir) {
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
    return { place: { ...d.start, x, y, w, h }, guides: null };
  }
  let x = d.start.x + dx;
  let y = d.start.y + dy;
  const snap = shift ? 14 / d.k : 0;
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
  return { place: clamp({ ...d.start, x, y }), guides: { v, h: hz } };
}

export function releaseDrag(): { drag: null; guides: Guides } {
  return { drag: null, guides: { v: null, h: null } };
}

export type PreviewLink =
  { kind: "waiting" } | { kind: "shown"; fit: { text: string; warn: boolean } };

export function previewMessage(data: unknown): "ready" | PreviewLink | null {
  const message = data as OverlaySize | { type?: string };
  if (message.type === "cl-overlay-ready") return "ready";
  if (message.type !== "cl-overlay-size") return null;
  const size = message as OverlaySize;
  let text = `Shown as ${SHAPE_NAME[size.layout]}.`;
  if (size.hidden.length)
    text += ` Left out to fit: ${size.hidden.map((part) => LEFT_OUT[part]).join(", ")}.`;
  if (size.scale < 0.6) text += " Too small to read on stream: make it bigger.";
  return {
    kind: "shown",
    fit: { text, warn: size.scale < 0.6 || size.hidden.length > 0 },
  };
}

function OverlaySetup({ overlay, origin }: { overlay: OverlayAccount; origin: string }) {
  const parts = Object.keys(PART_LABELS).filter((part) =>
    overlay.settingsContract.parts.includes(part),
  );
  const initial = useRef(fromSaved(overlay));
  const [form, setForm] = useState<Form>(initial.current.form);
  const [place, setPlace] = useState<Place>(() => clamp(initial.current.place));
  const [link, setLink] = useState(overlay.link);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("untouched");
  const [linkStatus, setLinkStatus] = useState<LinkStatus>(null);
  const page = useRef<AbortController | null>(null);
  const [copyState, setCopyState] = useState<null | "copied" | "selected">(null);
  const [confirmReplace, setConfirmReplace] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const [preview, setPreview] = useState<PreviewLink>({ kind: "waiting" });
  const [guides, setGuides] = useState<Guides>({
    v: null,
    h: null,
  });
  const [scale, setScale] = useState(0);
  const [drag, setDrag] = useState<PointerDrag | null>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const [initialQuery] = useState(() =>
    new URLSearchParams(overlayQuery(form, place, parts)).toString(),
  );
  const query = new URLSearchParams(overlayQuery(form, place, parts)).toString();
  const settings = savedSettings(form, place, parts);
  const savedJson = JSON.stringify({ tag: form.tag, settings });
  const autosave = useRef<Autosave>({
    phase: "idle",
    saved: JSON.stringify({
      tag: overlay.tag,
      settings: savedSettings(initial.current.form, initial.current.place, parts),
    }),
  });
  const latest = useRef<Choices>({ json: savedJson, tag: form.tag, settings });
  latest.current = { json: savedJson, tag: form.tag, settings };
  const address = `${origin}/o/${link}`;
  const screen = form.fit === "screen";
  const [w, h] = screen
    ? [place.cw, place.ch]
    : [Math.round(place.w), Math.round(place.h)];
  const update = (change: Partial<Form>) =>
    setForm((current) => ({ ...current, ...change }));

  useEffect(() => {
    const element = wrap.current;
    if (!element) return;
    const measure = () => setScale(element.clientWidth / place.cw);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [place.cw]);

  useEffect(() => {
    frame.current?.contentWindow?.postMessage(
      {
        type: "cl-overlay-settings",
        contract: overlay.settingsContract,
        search: `?${query}`,
      },
      location.origin,
    );
  }, [query]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (
        event.origin !== location.origin ||
        event.source !== frame.current?.contentWindow
      )
        return;
      const answer = previewMessage(event.data);
      if (answer === "ready") {
        frame.current?.contentWindow?.postMessage(
          {
            type: "cl-overlay-settings",
            contract: overlay.settingsContract,
            search: `?${queryRef.current}`,
          },
          location.origin,
        );
        return;
      }
      if (answer) setPreview(answer);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);
  const queryRef = useRef(query);
  queryRef.current = query;

  useEffect(() => {
    const controller = new AbortController();
    page.current = controller;
    return () => controller.abort();
  }, []);

  const saveNow = () => {
    void runAutosave(
      autosave,
      () => latest.current,
      (next) =>
        post<{ version: number }>(
          {
            intent: "save",
            tag: next.tag ?? "",
            settings: JSON.stringify(next.settings),
          },
          page.current?.signal,
        ),
      setSaveStatus,
    );
  };

  useEffect(() => {
    if (savedJson === autosave.current.saved) return;
    const timer = setTimeout(saveNow, 300);
    return () => clearTimeout(timer);
  }, [savedJson]);

  function resize(w: number, h: number) {
    setPlace((current) => settle({ ...current, w, h }));
  }

  function onPointerDown(event: PointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    const dir = (event.target as HTMLElement).dataset.dir ?? null;
    setDrag(startDrag(place, event.clientX, event.clientY, dir, scale));
    setPlace((current) => ({ ...current, anchor: null }));
  }

  function onPointerMove(event: PointerEvent<HTMLDivElement>) {
    const moved = dragTo(drag, event.clientX, event.clientY, event.shiftKey);
    if (!moved) return;
    if (moved.guides) setGuides(moved.guides);
    setPlace(moved.place);
  }

  function endDrag() {
    const ended = releaseDrag();
    setDrag(ended.drag);
    setGuides(ended.guides);
  }

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
    if (replacing) return;
    setReplacing(true);
    setCopyState(null);
    try {
      const made = await post<{ link: string }>(
        { intent: "new-link", replaces: link },
        page.current?.signal,
      );
      setLink(made.link);
      setConfirmReplace(false);
      setLinkStatus("made");
    } catch {
      setLinkStatus("unconfirmed");
    } finally {
      setReplacing(false);
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(address);
      setCopyState("copied");
    } catch {
      const input = document.getElementById("obs-address") as HTMLInputElement | null;
      input?.select();
      setCopyState("selected");
    }
    setTimeout(() => setCopyState(null), 2500);
  }

  const box = { left: place.x, top: place.y, width: place.w, height: place.h };
  const shape = arrangementFor(place.w, place.h);
  return (
    <div className="obs-layout">
      <form onSubmit={(event) => event.preventDefault()}>
        <section className="form-panel" aria-labelledby="obs-player">
          <h2 id="obs-player">Player</h2>
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
          <h2 id="obs-layout">Layout</h2>
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
            <p
              className={`obs-fitinfo${preview.kind === "shown" && preview.fit.warn ? " warn" : ""}`}
              aria-live="polite"
            >
              {preview.kind === "shown" ? preview.fit.text : ""}
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
          <h2 id="obs-shows">What it shows</h2>
          <div className="obs-checks">
            {parts.map((part) => (
              <label key={part}>
                <input
                  type="checkbox"
                  name="show"
                  value={part}
                  checked={form.show.includes(part)}
                  onChange={(event) =>
                    update({
                      show: parts.filter((each) =>
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

      <div className="obs-side">
        <div
          className="obs-stagewrap"
          ref={wrap}
          style={{ aspectRatio: `${place.cw} / ${place.ch}` }}
        >
          <p className="obs-stage-hint" aria-hidden="true">
            Drag to move it, drag a dot to resize
          </p>
          <div
            className="obs-canvas"
            style={
              {
                width: place.cw,
                height: place.ch,
                transform: `scale(${scale})`,
                "--obs-k": scale || 1,
              } as CSSProperties
            }
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
              className={`obs-handle${drag ? " dragging" : ""}`}
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
                x {Math.round(place.x)}, y {Math.round(place.y)} · {Math.round(place.w)} ×{" "}
                {Math.round(place.h)}
              </span>
              {["nw", "n", "ne", "e", "se", "s", "sw", "w"].map((dir) => (
                <i className="obs-rz" data-dir={dir} key={dir} />
              ))}
            </div>
          </div>
        </div>
        <section className="form-panel" aria-labelledby="obs-preview">
          <div className="obs-preview-head">
            <h2 id="obs-preview">
              Preview{" "}
              <span className="obs-help">
                your stream, to scale.
                <span className="obs-help-keys">
                  {" "}
                  Drag the overlay to move it (hold Shift to snap to edges), drag an edge
                  or corner to resize, or click it and use the arrow keys.
                </span>
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
          <h2 id="obs-add">Add it to OBS</h2>
          <p className="obs-help">
            Your own private link. Paste it into OBS once: every change you make on this
            page shows on stream within 3 seconds, with no new link.
          </p>
          <AddressRow
            address={address}
            known={!replacing && linkStatus !== "unconfirmed"}
            onCopy={copy}
          />
          <div className="obs-under">
            <StatusLine saveStatus={saveStatus} linkStatus={linkStatus} />
            {saveStatus === "notSaved" ? (
              <button type="button" className="button button-secondary" onClick={saveNow}>
                Try again
              </button>
            ) : null}
            {copyState === "copied" ? (
              <span className="obs-copied">Copied. Paste it into OBS.</span>
            ) : copyState === "selected" ? (
              <span className="obs-copied">
                Selected. Press Copy, then paste into OBS.
              </span>
            ) : null}
            {confirmReplace ? (
              <ReplaceLinkConfirm
                replacing={replacing}
                unconfirmed={linkStatus === "unconfirmed"}
                onConfirm={() => void replaceLink()}
                onCancel={() => setConfirmReplace(false)}
              />
            ) : (
              <ReplaceAction
                replacing={replacing}
                unconfirmed={linkStatus === "unconfirmed"}
                onRetry={() => void replaceLink()}
                onAsk={() => setConfirmReplace(true)}
              />
            )}
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

export function StatusLine({
  saveStatus,
  linkStatus,
}: {
  saveStatus: SaveStatus;
  linkStatus: LinkStatus;
}) {
  const lines = [
    SAVE_STATUS[saveStatus],
    ...(linkStatus ? [LINK_STATUS[linkStatus]] : []),
  ];
  return lines.map((line) => (
    <span key={line.text} className={`obs-saved${line.ok ? " ok" : ""}`} role="status">
      {line.text}
    </span>
  ));
}

export function AddressRow({
  address,
  known,
  onCopy,
}: {
  address: string;
  known: boolean;
  onCopy: () => void;
}) {
  return (
    <div className="obs-addr">
      <input
        id="obs-address"
        readOnly
        value={known ? address : ""}
        placeholder="Waiting for the current link…"
        aria-label="Overlay address"
      />
      <button
        type="button"
        className="button button-primary"
        disabled={!known}
        onClick={onCopy}
      >
        Copy
      </button>
    </div>
  );
}

export function ReplaceAction({
  replacing,
  unconfirmed,
  onRetry,
  onAsk,
}: {
  replacing: boolean;
  unconfirmed: boolean;
  onRetry: () => void;
  onAsk: () => void;
}) {
  return (
    <button
      type="button"
      className="button button-secondary obs-replace"
      disabled={replacing}
      onClick={unconfirmed ? onRetry : onAsk}
    >
      {replacing ? "Making link…" : unconfirmed ? "Try again" : "Make a new link"}
    </button>
  );
}

export function ReplaceLinkConfirm({
  replacing,
  unconfirmed,
  onConfirm,
  onCancel,
}: {
  replacing: boolean;
  unconfirmed: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <span className="obs-replace" role="group" aria-label="Confirm new link">
      <span className="obs-help">
        The old link stops working immediately. Make a new link?{" "}
      </span>
      <button
        type="button"
        className="button button-primary"
        disabled={replacing}
        onClick={onConfirm}
      >
        {replacing ? "Making link…" : unconfirmed ? "Try again" : "Yes, make new link"}
      </button>{" "}
      <button
        type="button"
        className="button button-secondary"
        disabled={replacing}
        onClick={onCancel}
      >
        Cancel
      </button>
    </span>
  );
}

export async function post<T>(
  values: Record<string, string>,
  page?: AbortSignal,
): Promise<T> {
  if (page?.aborted) throw new DOMException("Left the page", "AbortError");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15_000);
  const leave = () => controller.abort();
  page?.addEventListener("abort", leave);
  try {
    const res = await fetch(SAVE, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(values),
      redirect: "manual",
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(String(res.status));
    return (await res.json()) as T;
  } finally {
    clearTimeout(timeout);
    page?.removeEventListener("abort", leave);
  }
}
