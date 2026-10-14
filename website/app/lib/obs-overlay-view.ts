/**
 * The OBS overlay itself, drawn into one element. It runs in OBS's browser
 * source through the private link, and in the account page's preview.
 *
 * The box is exactly w × h stream pixels: its shape picks the arrangement
 * (card, wide card, strip or column), the content is laid out at its natural
 * size and scaled to fit, and the least important parts are left out first
 * when it would get too small to read. Squares, totals and the pop-up change
 * together, from one update.
 */
import {
  OVERLAY_FONTS,
  OVERLAY_PARTS,
  PLAYER_POLL_MS,
  SETTINGS_POLL_MS,
  type OverlayBattle,
  type OverlayPlayer,
} from "./obs-overlay";

type Part = (typeof OVERLAY_PARTS)[number];
type Font = keyof typeof OVERLAY_FONTS;
type Arrangement = "card" | "wide" | "strip" | "column";

/** What the overlay tells the page around it after each layout. */
export interface OverlaySize {
  type: "cl-overlay-size";
  layout: Arrangement;
  scale: number;
  hidden: Part[];
}

interface Config {
  tag: string;
  show: Set<Part>;
  text: "light" | "dark";
  bg: "card" | "tint" | "none";
  popup: boolean;
  screen: boolean;
  x: number;
  y: number;
  demo: boolean;
  font: Font;
  w: number;
  h: number;
}

interface View extends Partial<OverlayPlayer> {
  error?: string;
}

const FONT_FAMILY: Record<Font, string> = {
  clashlens: "Lilita One",
  clean: "Inter",
  esports: "Barlow Condensed",
  tech: "Rajdhani",
  arcade: "Bungee",
};
const MIN_SCALE = 0.6;
const MAX_SCALE = 4;
// Left out first when space runs short.
const DROP_ORDER: Part[] = ["updated", "day", "net", "rank"];
const TOAST_MS = 6_000;
const DEMO_FIRST_MS = 800;
const DEMO_EVERY_MS = 12_000;
const DEMO_OPPONENTS = [
  "Joker",
  "STKR⚡MIRACLE",
  "VANSH",
  "Ali Clash",
  "KaKa",
  "Nightfall",
  "Moe 🇩🇪",
  "Ruby",
  "Baby Hog",
  "Torch",
];

const ICONS = `<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="ov-i-trophy" viewBox="0 0 24 24"><path fill="currentColor" d="M6 2h12v2h3v4a5 5 0 0 1-4.6 5A6 6 0 0 1 13 16.9V19h3v3H8v-3h3v-2.1A6 6 0 0 1 7.6 13 5 5 0 0 1 3 8V4h3V2Zm0 4H5v2a3 3 0 0 0 1.4 2.5A6 6 0 0 1 6 9V6Zm12 0v3a6 6 0 0 1-.4 1.5A3 3 0 0 0 19 8V6h-1Z"/></symbol>
  <symbol id="ov-i-sword" viewBox="0 0 24 24"><path fill="currentColor" d="M20.5 2 22 3.5 11.8 13.7l1.4 1.4-1.4 1.4-1.6-1.6-3.5 3.5.9.9-1.4 1.4-3.6-3.6 1.4-1.4.9.9 3.5-3.5-1.6-1.6 1.4-1.4 1.4 1.4L20.5 2Z"/></symbol>
  <symbol id="ov-i-shield" viewBox="0 0 24 24"><path fill="currentColor" d="M12 2 20 5v6c0 5.2-3.4 9.4-8 11-4.6-1.6-8-5.8-8-11V5l8-3Z"/></symbol>
</svg>`;

const icon = (id: string, cls = "") =>
  `<svg class="ov-ico ${cls}"><use href="#ov-i-${id}"/></svg>`;
const fmt = (n: number | null | undefined) =>
  n == null ? "–" : Number(n).toLocaleString("en-GB");
const esc = (s: unknown) =>
  String(s ?? "").replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c] ?? c,
  );

/** The overlay's settings from an address query. */
export function readConfig(search: string): Config {
  const q = new URLSearchParams(search);
  const pick = <T extends string>(
    value: string | null,
    allowed: readonly T[],
    fallback: T,
  ) => (allowed.includes(value as T) ? (value as T) : fallback);
  const number = (key: string) =>
    Math.min(4000, Math.max(0, Number.parseInt(q.get(key) ?? "", 10) || 0));
  const show = q.has("show")
    ? new Set(
        (q.get("show") ?? "")
          .split(",")
          .filter((part): part is Part =>
            (OVERLAY_PARTS as readonly string[]).includes(part),
          ),
      )
    : new Set<Part>(OVERLAY_PARTS);
  return {
    tag: (q.get("p") ?? "").replace(/^#/, "").toUpperCase(),
    show,
    text: q.get("text") === "dark" ? "dark" : "light",
    bg: pick(q.get("bg"), ["card", "tint", "none"], "card"),
    popup: q.get("popup") !== "0",
    // Full-screen mode: the browser source is the whole stream and the
    // overlay sits at x, y inside it, where the streamer placed it.
    screen: q.get("fit") === "screen",
    x: number("x"),
    y: number("y"),
    demo: q.get("demo") === "1",
    font: pick(q.get("font"), Object.keys(OVERLAY_FONTS) as Font[], "clashlens"),
    w: number("w"),
    h: number("h"),
  };
}

/** The box's shape picks the arrangement: wide is a strip, tall is a column. */
export function arrangementFor(w: number, h: number): Arrangement {
  const ratio = w / h;
  return ratio >= 4.5
    ? "strip"
    : ratio >= 2.1
      ? "wide"
      : ratio <= 0.8
        ? "column"
        : "card";
}

/** A battle is good, middling or bad for the player shown. */
function tone(b: OverlayBattle): "good" | "mid" | "bad" {
  if (b.kind === "attack") return b.stars === 3 ? "good" : b.stars === 2 ? "mid" : "bad";
  return b.stars === 3 ? "bad" : b.stars === 2 ? "mid" : "good";
}

function ago(iso: string | undefined): { text: string; stale: boolean } | null {
  if (!iso) return null;
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 90) return { text: "Updated just now", stale: false };
  const m = Math.round(s / 60);
  if (m < 60) return { text: `Updated ${m} min ago`, stale: m >= 15 };
  return { text: `Updated ${Math.round(m / 60)} h ago`, stale: true };
}

const signed = (n: number | null, kind: "gain" | "loss") =>
  n === null ? "–" : kind === "gain" ? `+${fmt(n)}` : `−${fmt(n)}`;
const netText = (n: number | null) =>
  n === null ? "–" : n > 0 ? `+${fmt(n)}` : n < 0 ? `−${fmt(-n)}` : "±0";

function demoBattle(kind: OverlayBattle["kind"], i: number): OverlayBattle {
  const stars = [3, 3, 3, 2, 3, 1, 3, 2][(i * 3 + (kind === "attack" ? 0 : 5)) % 8];
  const destruction =
    stars === 3 ? 100 : stars === 2 ? 60 + ((i * 7) % 39) : 30 + ((i * 5) % 20);
  const trophies = stars === 3 ? 40 : stars === 2 ? 16 + ((i * 3) % 17) : 5 + (i % 9);
  return {
    id: `demo-${kind}-${i}-${Date.now()}`,
    kind,
    at: new Date().toISOString(),
    stars,
    destruction,
    trophies: kind === "attack" ? trophies : -trophies,
    opponent:
      DEMO_OPPONENTS[(i * 3 + (kind === "attack" ? 1 : 4)) % DEMO_OPPONENTS.length],
  };
}

/**
 * Draw the overlay into `root` and keep it live. With `link`, it follows the
 * saved settings of that private link, read every 3 seconds; a replaced link
 * shows nothing. Without, it takes its settings from `search` and from the
 * page around it (the account page's preview). Returns a stop function.
 */
export function mountOverlay(
  root: HTMLElement,
  { link, search = "" }: { link: string | null; search?: string },
): () => void {
  root.innerHTML = `<div class="ov-stage"></div>${ICONS}`;
  const stage = root.querySelector<HTMLElement>(".ov-stage")!;
  let cfg = readConfig("");
  let data: View | null = null;
  let seen: Set<string> | null = null;
  let freshIds = new Set<string>();
  let activeToast: OverlayBattle[] | null = null;
  let hidden: Part[] = [];
  let stopped = false;
  const timers = new Set<ReturnType<typeof setTimeout>>();
  let pollTimer: ReturnType<typeof setTimeout> | undefined;
  let demoTimer: ReturnType<typeof setTimeout> | undefined;
  let toastTimer: ReturnType<typeof setTimeout> | undefined;
  const later = (run: () => void, ms: number) => {
    const timer = setTimeout(() => {
      timers.delete(timer);
      if (!stopped) run();
    }, ms);
    timers.add(timer);
    return timer;
  };
  const cancel = (timer: ReturnType<typeof setTimeout> | undefined) => {
    if (timer === undefined) return;
    clearTimeout(timer);
    timers.delete(timer);
  };

  function squares(list: OverlayBattle[], withNumbers: boolean) {
    let html = "";
    for (let i = 0; i < 8; i++) {
      const b = list[i];
      if (!b) {
        html += `<span class="ov-sq ov-open"></span>`;
        continue;
      }
      html += `<span class="ov-sq ov-${tone(b)} ${freshIds.has(b.id) ? "ov-fresh" : ""}" title="${b.stars}★ ${b.destruction}%">${withNumbers ? Math.abs(b.trophies) : ""}</span>`;
    }
    return html;
  }

  // Small and quiet: "Powered by [mark] clashlens.net", always shown.
  function brand(layout: Arrangement) {
    const mark = `<img src="/images/mark-cl-block.svg" alt=""><span class="ov-cl">clashlens.net</span>`;
    if (layout === "strip")
      return `<span class="ov-brand"><span>Powered by</span><span class="ov-row">${mark}</span></span>`;
    return `<span class="ov-brand"><span>Powered by</span>${mark}</span>`;
  }

  function model() {
    const d = data ?? {};
    const day = d.day ?? null;
    const battles = day ? day.battles : [];
    const attacks = battles.filter((b) => b.kind === "attack");
    const defenses = battles.filter((b) => b.kind === "defense");
    const gained = day?.gained ?? null;
    const lost = day?.lost ?? null;
    return {
      name: d.name ?? (cfg.tag ? `#${cfg.tag}` : "Pick a player"),
      legend: d.state === "legend",
      state: d.state,
      error: d.error,
      trophies: d.trophies,
      rank: d.rank,
      dayLabel: day && day.number ? `Day ${day.number} of ${day.of}` : null,
      attacks,
      defenses,
      gained,
      lost,
      net: day?.net ?? null,
      left:
        gained === null || lost === null
          ? null
          : 8 - attacks.length + (8 - defenses.length),
      updated: ago(d.profileReadAt),
    };
  }
  type Model = ReturnType<typeof model>;

  const rankText = (m: Model) => (m.rank ? `#${fmt(m.rank)}` : "–");
  function notLegend(m: Model) {
    if (m.error) return "Clash Lens is not answering right now";
    if (!data) return "Loading…";
    if (m.state === "not_in_legend") return "Not in Legend League right now";
    return "No Legend day to show yet";
  }
  const trophyHtml = (m: Model, cls: string) =>
    `<span class="${cls} ov-lilita ov-num">${icon("trophy", "ov-trophy-ico")} ${fmt(m.trophies)}</span>`;
  const netValue = (m: Model) =>
    `<b class="ov-num ${m.net === null ? "" : m.net >= 0 ? "ov-gain" : "ov-loss"}">${netText(m.net)}</b>`;

  function renderCard(m: Model, show: Set<Part>) {
    const upd =
      m.updated && show.has("updated")
        ? `<span class="${m.updated.stale ? "ov-stale" : "ov-muted"}">${m.updated.text}</span>`
        : "<span></span>";
    const day =
      show.has("day") && m.dayLabel
        ? `<span class="ov-day ov-muted">${m.dayLabel}</span>`
        : "";
    let left = `<div class="ov-top"><span class="ov-name">${esc(m.name)}</span>${day}</div>`;
    let body = "";
    if (!m.legend) {
      left += `<div class="ov-msg ov-muted" style="margin:12px 0 6px">${notLegend(m)}</div>`;
      if (m.trophies != null && show.has("trophies"))
        left += `<div class="ov-hero">${trophyHtml(m, "ov-trophies")}</div>`;
    } else {
      const hero = [];
      if (show.has("trophies")) hero.push(trophyHtml(m, "ov-trophies"));
      if (show.has("rank"))
        hero.push(
          `<span class="ov-rank ov-num"><small class="ov-muted">Rank</small>${rankText(m)}</span>`,
        );
      if (hero.length) left += `<div class="ov-hero">${hero.join("")}</div>`;
      const rows = [];
      if (show.has("attacks"))
        rows.push(
          `<div class="ov-row">${icon("sword")}<span class="ov-squares">${squares(m.attacks, true)}</span><span class="ov-tot ov-gain ov-num">${signed(m.gained, "gain")}</span></div>`,
        );
      if (show.has("defenses"))
        rows.push(
          `<div class="ov-row">${icon("shield")}<span class="ov-squares">${squares(m.defenses, true)}</span><span class="ov-tot ov-loss ov-num">${signed(m.lost, "loss")}</span></div>`,
        );
      if (rows.length) body += `<div class="ov-rows">${rows.join("")}</div>`;
      if (show.has("net"))
        body += `<div class="ov-net"><span class="ov-muted">${m.left === null ? "" : m.left ? `${m.left} battle${m.left === 1 ? "" : "s"} left` : "All 16 battles done"}</span><span>Net today ${netValue(m)}</span></div>`;
    }
    // A normal card stacks left over right; a wide card puts them side by side.
    return `<div class="ov-box">
      <div class="ov-main"><div class="ov-left">${left}</div><div class="ov-right">${body}</div></div>
      <div class="ov-foot">${brand("card")}${upd}</div>
      <div class="ov-toast"></div>
    </div>`;
  }

  function renderStrip(m: Model, show: Set<Part>) {
    const parts = [`<span class="ov-name">${esc(m.name)}</span>`];
    if (!m.legend) {
      parts.push(`<span class="ov-msg ov-muted">${notLegend(m)}</span>`);
      if (m.trophies != null && show.has("trophies"))
        parts.push(trophyHtml(m, "ov-part ov-trophies"));
    } else {
      if (show.has("trophies")) parts.push(trophyHtml(m, "ov-part ov-trophies"));
      if (show.has("rank"))
        parts.push(`<span class="ov-part ov-rank ov-num">${rankText(m)}</span>`);
      if (show.has("attacks"))
        parts.push(
          `<span class="ov-sep"></span><span class="ov-part">${icon("sword")}<span class="ov-squares">${squares(m.attacks, false)}</span><span class="ov-tot ov-gain ov-num">${signed(m.gained, "gain")}</span></span>`,
        );
      if (show.has("defenses"))
        parts.push(
          `<span class="ov-part">${icon("shield")}<span class="ov-squares">${squares(m.defenses, false)}</span><span class="ov-tot ov-loss ov-num">${signed(m.lost, "loss")}</span></span>`,
        );
      if (show.has("net"))
        parts.push(
          `<span class="ov-sep"></span><span class="ov-part ov-net">Net ${netValue(m)}</span>`,
        );
    }
    const meta = [];
    if (show.has("day") && m.dayLabel)
      meta.push(`<span class="ov-muted">${m.dayLabel}</span>`);
    if (show.has("updated") && m.updated)
      meta.push(
        `<span class="${m.updated.stale ? "ov-stale" : "ov-muted"}">${m.updated.text.replace("Updated ", "")}</span>`,
      );
    if (meta.length) parts.push(`<span class="ov-meta">${meta.join("")}</span>`);
    parts.push(`<span class="ov-sep"></span>`, brand("strip"));
    return `<div class="ov-box">${parts.join("")}<div class="ov-toast"></div></div>`;
  }

  function renderColumn(m: Model, show: Set<Part>) {
    const parts = [`<span class="ov-name">${esc(m.name)}</span>`];
    if (show.has("day") && m.dayLabel)
      parts.push(`<span class="ov-day ov-muted">${m.dayLabel}</span>`);
    if (!m.legend) {
      parts.push(`<span class="ov-msg ov-muted">${notLegend(m)}</span>`);
      if (m.trophies != null && show.has("trophies"))
        parts.push(
          `<span class="ov-trophies ov-lilita ov-num">${fmt(m.trophies)}</span>`,
        );
    } else {
      if (show.has("trophies")) parts.push(trophyHtml(m, "ov-trophies"));
      if (show.has("rank"))
        parts.push(
          `<span class="ov-rank ov-num"><span class="ov-muted">Rank</span> ${rankText(m)}</span>`,
        );
      if (show.has("attacks"))
        parts.push(
          `<div class="ov-block"><span class="ov-head">${icon("sword")} Attacks <span class="ov-tot ov-gain ov-num">${signed(m.gained, "gain")}</span></span><span class="ov-squares">${squares(m.attacks, true)}</span></div>`,
        );
      if (show.has("defenses"))
        parts.push(
          `<div class="ov-block"><span class="ov-head">${icon("shield")} Defenses <span class="ov-tot ov-loss ov-num">${signed(m.lost, "loss")}</span></span><span class="ov-squares">${squares(m.defenses, true)}</span></div>`,
        );
      if (show.has("net"))
        parts.push(`<span class="ov-net">Net today ${netValue(m)}</span>`);
    }
    if (show.has("updated") && m.updated)
      parts.push(
        `<span class="ov-updated ${m.updated.stale ? "ov-stale" : "ov-muted"}">${m.updated.text}</span>`,
      );
    parts.push(brand("column"));
    return `<div class="ov-box">${parts.join("")}<div class="ov-toast"></div></div>`;
  }

  function draw(layout: Arrangement, m: Model, show: Set<Part>) {
    // A wide card is the card, side by side.
    return layout === "strip"
      ? renderStrip(m, show)
      : layout === "column"
        ? renderColumn(m, show)
        : renderCard(m, show);
  }

  function render() {
    if (stopped) return;
    if (!cfg.w || !cfg.h) {
      stage.innerHTML = "";
      return;
    }
    const m = model();
    const layout = arrangementFor(cfg.w, cfg.h);
    root.className = `obs-overlay ov-${layout === "wide" ? "card ov-wide" : layout} ov-${cfg.text}-text ov-bg-${cfg.bg} ov-font-${cfg.font}`;
    stage.style.left = `${cfg.screen ? cfg.x : 0}px`;
    stage.style.top = `${cfg.screen ? cfg.y : 0}px`;
    const shadow = cfg.bg === "card" ? 6 : 0;
    // Lay it out at its natural size, then scale it to fit the box; if it
    // would get too small to read, leave out the least important parts.
    const shown = new Set(cfg.show);
    let scale: number;
    hidden = [];
    stage.style.transform = "none";
    for (;;) {
      stage.innerHTML = draw(layout, m, shown);
      const natural = stage.firstElementChild as HTMLElement;
      scale = Math.min(
        cfg.w / natural.offsetWidth,
        cfg.h / (natural.offsetHeight + shadow),
        MAX_SCALE,
      );
      const next = DROP_ORDER.find((part) => shown.has(part));
      if (scale >= MIN_SCALE || !next) break;
      shown.delete(next);
      hidden.push(next);
    }
    const box = stage.firstElementChild as HTMLElement;
    box.style.width = `${cfg.w / scale}px`;
    box.style.height = `${cfg.h / scale - shadow}px`;
    stage.style.transform = `scale(${scale})`;
    freshIds = new Set();
    if (activeToast) {
      const el = stage.querySelector<HTMLElement>(".ov-toast")!;
      const last = activeToast[activeToast.length - 1];
      el.style.transition = "none";
      el.className = `ov-toast ov-${last.kind} ov-${tone(last)} ov-show`;
      el.innerHTML = toastHtml(activeToast);
      void el.offsetWidth;
      el.style.transition = "";
    }
    if (window.parent !== window) {
      const message: OverlaySize = {
        type: "cl-overlay-size",
        layout,
        scale,
        hidden,
      };
      window.parent.postMessage(message, location.origin);
    }
  }

  // ---------- pop-up ----------
  function toastHtml(list: OverlayBattle[]) {
    if (list.length > 1) {
      const sum = list.reduce((total, b) => total + b.trophies, 0);
      const each = list
        .map(
          (b) =>
            `${b.trophies >= 0 ? "+" : "−"}${Math.abs(b.trophies)} ${"★".repeat(b.stars)}`,
        )
        .join(" · ");
      return `<span class="ov-big" style="color:${sum >= 0 ? "#69db7c" : "#ff8787"}">${sum >= 0 ? "+" : "−"}${Math.abs(sum)}</span>
        <span style="min-width:0;display:grid"><span class="ov-label">${list.length} new battles</span><span class="ov-who">${each}</span></span>`;
    }
    const b = list[0];
    const stars = "★".repeat(b.stars) + "☆".repeat(3 - b.stars);
    const sign = b.trophies >= 0 ? `+${b.trophies}` : `−${Math.abs(b.trophies)}`;
    return `<span class="ov-big" style="color:${b.trophies >= 0 ? "#69db7c" : "#ff8787"}">${sign}</span>
      <span style="min-width:0;display:grid">
        <span class="ov-label">${b.kind === "attack" ? "Attack" : "Defense"} · <span class="ov-stars">${stars}</span> ${b.destruction}%</span>
        <span class="ov-who">${b.kind === "attack" ? "vs" : "by"} ${esc(b.opponent ?? "unknown")}</span>
      </span>`;
  }

  // One pop-up per update, shown the moment the squares change. Battles that
  // land together share one pop-up; a newer update replaces the one showing.
  function showToast(list: OverlayBattle[]) {
    if (!cfg.popup || !list.length) return;
    cancel(toastTimer);
    const el = stage.querySelector<HTMLElement>(".ov-toast");
    if (!el) return;
    const last = list[list.length - 1];
    el.className = `ov-toast ov-${last.kind} ov-${tone(last)}`;
    el.innerHTML = toastHtml(list);
    activeToast = list;
    void el.offsetWidth;
    el.classList.add("ov-show");
    toastTimer = later(() => {
      activeToast = null;
      stage.querySelector(".ov-toast")?.classList.remove("ov-show");
    }, TOAST_MS);
  }

  // The squares, totals and pop-up all come from this one update.
  function accept(next: View, quiet = false) {
    const battles = next.day?.battles ?? [];
    const fresh = seen && !quiet ? battles.filter((b) => !seen!.has(b.id)) : [];
    seen = new Set(battles.map((b) => b.id));
    data = next;
    fresh.forEach((b) => freshIds.add(b.id));
    render();
    showToast(fresh);
  }

  // ---------- live data ----------
  const playerUrl = (tag: string) =>
    `/resources/players/${encodeURIComponent(`#${tag}`)}/overlay`;
  async function readPlayer(tag: string): Promise<View> {
    const res = await fetch(playerUrl(tag), { cache: "no-store" });
    const next = (await res.json()) as View;
    if (!res.ok) throw new Error(next.error ?? "error");
    return next;
  }

  async function poll() {
    cancel(pollTimer);
    if (cfg.demo || !cfg.tag) return;
    const tag = cfg.tag;
    try {
      const next = await readPlayer(tag);
      if (tag === cfg.tag && !cfg.demo) accept(next);
    } catch {
      if (tag === cfg.tag && !cfg.demo) accept({ ...(data ?? {}), error: "offline" });
    }
    if (tag === cfg.tag && !cfg.demo) pollTimer = later(poll, PLAYER_POLL_MS);
  }

  // ---------- demo: a made-up day that plays a battle every 12 seconds ----------
  function stopDemo() {
    cancel(demoTimer);
    demoTimer = undefined;
  }
  function startDemo() {
    stopDemo();
    const base = data;
    let step = 0;
    const plan: Array<[OverlayBattle["kind"], number]> = [];
    for (let i = 0; i < 8; i++) plan.push(["attack", i], ["defense", i]);
    const day = { number: 9, of: 28, battles: [] as OverlayBattle[], gained: 0, lost: 0 };
    let trophies = 5064;
    const push = (quiet = false) => {
      const [kind, i] = plan[step % 16];
      if (step % 16 === 0) {
        day.battles = [];
        day.gained = 0;
        day.lost = 0;
        trophies = 5064;
      }
      const b = demoBattle(kind, i);
      day.battles = [...day.battles, b];
      if (b.trophies > 0) day.gained += b.trophies;
      else day.lost -= b.trophies;
      trophies += b.trophies;
      step++;
      accept(
        {
          state: "legend",
          name: base?.name ?? data?.name ?? "Demo player",
          trophies,
          rank: 4100 - Math.round((trophies - 5064) * 6),
          profileReadAt: new Date().toISOString(),
          day: { ...day, net: day.gained - day.lost },
        },
        quiet,
      );
    };
    seen = null;
    for (let k = 0; k < 5; k++) push(true); // the starting battles make no pop-ups
    // The first made-up battle lands straight away, then one every 12 seconds.
    const every = () => {
      push();
      demoTimer = later(every, DEMO_EVERY_MS);
    };
    demoTimer = later(every, DEMO_FIRST_MS);
  }

  function apply(next: string, first: boolean) {
    const prev = cfg;
    cfg = readConfig(next);
    if (cfg.font !== prev.font && cfg.font !== "clashlens") {
      // Lay out again once the font is in, so the fit is measured with it.
      document.fonts.load(`700 24px "${FONT_FAMILY[cfg.font]}"`).then(render, () => {});
    }
    if (cfg.demo) {
      cancel(pollTimer);
      if (!first && !prev.demo && prev.tag === cfg.tag && data?.name) startDemo();
      else if (!prev.demo || first || prev.tag !== cfg.tag) {
        if (prev.tag !== cfg.tag) data = null;
        const start = () => {
          if (cfg.demo) startDemo();
        };
        if (cfg.tag)
          readPlayer(cfg.tag)
            .then(
              (d) => (data = d),
              () => {},
            )
            .finally(start);
        else start();
      } else render();
      return;
    }
    stopDemo();
    if (first || prev.tag !== cfg.tag || prev.demo) {
      data = null;
      seen = null;
      render();
      void poll();
    } else render();
  }

  const onMessage = (event: MessageEvent) => {
    if (event.origin !== location.origin || link !== null) return;
    const msg = (event.data ?? {}) as { type?: string; search?: string; kind?: string };
    if (msg.type === "cl-overlay-settings" && typeof msg.search === "string")
      apply(msg.search, false);
    if (msg.type === "cl-overlay-test") {
      const kind = msg.kind === "defense" ? "defense" : "attack";
      showToast([{ ...demoBattle(kind, Math.floor(Math.random() * 8)), id: "test" }]);
    }
  };
  window.addEventListener("message", onMessage);
  // Keep "Updated N min ago" honest.
  const tick = setInterval(() => {
    if (data && !cfg.demo) render();
  }, 30_000);
  void document.fonts.ready.then(render);

  if (link !== null) {
    // A saved link: the settings live on Clash Lens and are read again every
    // 3 seconds, so a change on the account page shows here with no new address.
    let applied: string | null = null;
    let started = false;
    const follow = async () => {
      let wait = SETTINGS_POLL_MS;
      try {
        const res = await fetch(`/o/${encodeURIComponent(link)}/settings`, {
          cache: "no-store",
        });
        if (res.status === 404) {
          // A replaced link shows nothing, and checks rarely.
          cancel(pollTimer);
          stopDemo();
          cfg = readConfig("");
          data = null;
          stage.innerHTML = "";
          started = false;
          applied = null;
          wait = 60_000;
        } else if (res.ok) {
          const saved = (await res.json()) as {
            settings: Record<string, string>;
            version: number;
            tag: string | null;
          };
          const key = `${saved.version} ${saved.tag}`;
          if (key !== applied) {
            applied = key;
            const query = new URLSearchParams({
              ...saved.settings,
              p: (saved.tag ?? "").replace(/^#/, ""),
            });
            apply(`?${query}`, !started);
            started = true;
          }
        }
      } catch {
        // Keep showing the last settings.
      }
      later(follow, wait);
    };
    void follow();
  } else {
    apply(search, true);
    // The account page answers with its newest settings, made while this loaded.
    if (window.parent !== window)
      window.parent.postMessage({ type: "cl-overlay-ready" }, location.origin);
  }

  return () => {
    stopped = true;
    for (const timer of timers) clearTimeout(timer);
    timers.clear();
    clearInterval(tick);
    window.removeEventListener("message", onMessage);
  };
}
