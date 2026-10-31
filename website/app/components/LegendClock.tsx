import { useEffect, useRef, useState } from "react";

import type { ClockBattle } from "../lib/dashboard";
import { nextResetMs } from "../lib/dashboard";
import { Stars } from "./GameIcons";

const DAY_MINUTES = 1440;
/** The dial's radius in the face's units; the day gone is drawn on it. */
const RING = 100;
/**
 * Each battle is a short thin tick across this span, the same for both
 * kinds: attacks outside the dial, defenses inside.
 */
const TICK = { attack: [RING + 5, RING + 17], defense: [RING - 17, RING - 5] } as const;
/** A mark's middle, and its button's: the middle of its ticks. */
const MARK_RADIUS = { attack: RING + 11, defense: RING - 11 } as const;
/** Battles sharing a mark are ticks this far apart along the dial. */
const TICK_GAP = 7;
/** A mark's button is this tall, and this long plus a gap for each tick after the first. */
const HIT = 28;
/** The least room between two marks' buttons on one track. */
const MARK_GAP = 3;
/**
 * The face's box: the dial and its attack ticks, with room on top for the
 * Reset's name, so the dial fills as much of the card as it can.
 */
const FACE = { left: -124, top: -140, width: 248, height: 264 } as const;
export function timeFormatter(timeZone: string): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone,
  });
}

export function signed(value: number): string {
  if (value > 0) return `+${value.toLocaleString("en-US")}`;
  if (value < 0) return `−${Math.abs(value).toLocaleString("en-US")}`;
  return "0";
}

/** Whole minutes left in the Legend day, and the "9h 05m" the clocks show for them. */
export function resetCountdown(nowMs: number): { left: number; text: string } {
  const left = Math.ceil((nextResetMs(nowMs) - nowMs) / 60_000);
  return {
    left,
    text: `${Math.floor(left / 60)}h ${String(left % 60).padStart(2, "0")}m`,
  };
}

/**
 * A time zone as a Clasher says it, with the short name the player page's
 * times use: "Europe/London" is "London time (GMT)", or (BST) in summer.
 */
export function zoneName(timeZone: string): string {
  const place = timeZone.split("/").at(-1)?.replaceAll("_", " ");
  if (!place || timeZone === "UTC") return "UTC time";
  const short = new Intl.DateTimeFormat("en-GB", { timeZone, timeZoneName: "short" })
    .formatToParts(Date.now())
    .find((part) => part.type === "timeZoneName")?.value;
  return short ? `${place} time (${short})` : `${place} time`;
}

/** The point on the dial `minutes` after the Reset, which sits on top. */
function point(minutes: number, radius: number): [number, number] {
  const angle = (minutes / DAY_MINUTES) * 2 * Math.PI;
  return [Math.sin(angle) * radius, -Math.cos(angle) * radius];
}

export interface ClockMark {
  kind: "attack" | "defense";
  /** Where the mark sits, in minutes after the Reset: its battles' middle time. */
  minutes: number;
  /** One battle, or a group close enough to share one mark, oldest first. */
  battles: ClockBattle[];
}

/** Units along a track as minutes of the day. */
function minutesOf(kind: ClockMark["kind"], units: number): number {
  return (units / (2 * Math.PI * MARK_RADIUS[kind])) * DAY_MINUTES;
}

/** How many minutes of a track a mark of this many battles takes, with room either side. */
function markMinutes(kind: ClockMark["kind"], count: number): number {
  return minutesOf(kind, HIT + (count - 1) * TICK_GAP + MARK_GAP);
}

/**
 * Where "Now" is written: outside the ring, 66 minutes ahead of the dot where
 * no battle has happened yet, so it never reads as part of the countdown; in
 * the day's last 100 minutes, where ahead is the day's start, straight in
 * from the dot instead.
 */
function nowLabel(now: number): [number, number] {
  return now > DAY_MINUTES - 100 ? point(now, RING - 38) : point(now + 66, RING + 10);
}

/**
 * How near now's dot a tick's line may end: the dot with its ring (11), half
 * a tick's width (1.75) and a gap, so nothing touches the dot.
 */
const NOW_CLEAR = 16;

/**
 * A tick's span along its line, cut back at the dial's end so it stops clear
 * of now's dot: a battle a few minutes ago still shows, beside the dot.
 */
function clearOfNow(
  kind: ClockMark["kind"],
  minutes: number,
  now: number | null,
): [number, number] {
  let [inner, outer] = TICK[kind];
  if (now === null) return [inner, outer];
  const turn = ((minutes - now) / DAY_MINUTES) * 2 * Math.PI;
  const across = RING * Math.sin(turn);
  if (Math.abs(across) >= NOW_CLEAR || Math.cos(turn) <= 0) return [inner, outer];
  const along = RING * Math.cos(turn);
  const half = Math.sqrt(NOW_CLEAR ** 2 - across ** 2);
  if (kind === "defense") outer = Math.min(outer, along - half);
  else inner = Math.max(inner, along + half);
  return [inner, outer];
}

/** Where each of a mark's ticks sits: side by side round its middle time. */
function tickMinutes(mark: ClockMark): number[] {
  const gap = minutesOf(mark.kind, TICK_GAP);
  return mark.battles.map(
    (_, index) => mark.minutes + (index - (mark.battles.length - 1) / 2) * gap,
  );
}

/**
 * One mark per battle on its kind's track, at the battle's own time. Battles
 * whose marks would touch become one mark at their middle time, drawn as
 * ticks side by side, so the dial stays calm and no mark hides another.
 */
export function clockMarks(battles: ClockBattle[], dayStartMs: number): ClockMark[] {
  const marks: ClockMark[] = [];
  for (const kind of ["attack", "defense"] as const) {
    const sorted = battles
      .filter((battle) => battle.kind === kind)
      .map((battle) => ({ battle, minutes: (battle.at - dayStartMs) / 60_000 }))
      .filter(({ minutes }) => minutes >= 0 && minutes <= DAY_MINUTES)
      .sort((a, b) => a.minutes - b.minutes);
    const groups = sorted.map((item) => [item]);
    const middle = (group: typeof sorted) =>
      group.reduce((sum, item) => sum + item.minutes, 0) / group.length;
    // Join the closest touching neighbours first, until no two marks touch.
    for (;;) {
      let join = -1;
      let closest = Infinity;
      for (let index = 1; index < groups.length; index += 1) {
        const before = groups[index - 1]!;
        const after = groups[index]!;
        const apart = middle(after) - middle(before);
        const room =
          (markMinutes(kind, before.length) + markMinutes(kind, after.length)) / 2;
        if (apart < room && apart < closest) {
          join = index;
          closest = apart;
        }
      }
      if (join < 0) break;
      groups.splice(join - 1, 2, [...groups[join - 1]!, ...groups[join]!]);
    }
    for (const group of groups)
      marks.push({
        kind,
        minutes: middle(group),
        battles: group.map(({ battle }) => battle),
      });
  }
  return marks;
}

function markLabel(mark: ClockMark, format: Intl.DateTimeFormat): string {
  const count = mark.battles.length;
  const noun = mark.kind === "attack" ? "Attack" : "Defense";
  if (count === 1) return `${noun} at ${format.format(mark.battles[0]!.at)}`;
  return `${count} ${noun.toLowerCase()}s, ${format.format(mark.battles[0]!.at)} to ${format.format(mark.battles.at(-1)!.at)}`;
}

/** One battle in words: who, stars, destruction and trophies. */
function battleWords(battle: ClockBattle, kind: ClockMark["kind"]): string {
  const stars = `${battle.stars} star${battle.stars === 1 ? "" : "s"}`;
  return `${kind === "attack" ? "on" : "against"} ${battle.opponent ?? "an unknown player"}: ${stars}, ${battle.destruction}%, ${signed(battle.trophyChange)} trophies`;
}

/**
 * What a mark holds, for its tooltip and screen readers: "Attack at 08:25
 * on Uma07: 3 stars, 100%, +40 trophies", or a group's span then each
 * battle on its own line.
 */
export function markReadout(mark: ClockMark, format: Intl.DateTimeFormat): string {
  const [first] = mark.battles;
  if (mark.battles.length === 1 && first)
    return `${markLabel(mark, format)} ${battleWords(first, mark.kind)}`;
  return [
    `${markLabel(mark, format)}:`,
    ...mark.battles.map(
      (battle) => `${format.format(battle.at)} ${battleWords(battle, mark.kind)}`,
    ),
  ].join("\n");
}

/** The battle behind one mark: when, who, stars, % and trophies. */
function MarkDetail({
  mark,
  format,
  phone,
  anchor,
  onClose,
}: {
  mark: ClockMark;
  format: Intl.DateTimeFormat;
  phone: boolean;
  anchor: DOMRect | null;
  onClose: () => void;
}) {
  const style =
    !phone && anchor
      ? {
          left:
            anchor.right + 306 < document.documentElement.clientWidth
              ? anchor.right + 8
              : Math.max(8, anchor.left - 298),
          top: anchor.top - 10,
        }
      : undefined;
  return (
    <>
      {phone ? <div className="clock-sheet-back" onClick={onClose} /> : null}
      <div
        className={phone ? "clock-detail clock-sheet" : "clock-detail clock-popover"}
        style={style}
        role="dialog"
        aria-label={markLabel(mark, format)}
      >
        <div className="clock-detail-head">
          <b>{markLabel(mark, format)}</b>
          <button type="button" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        {mark.battles.map((battle) => (
          <div className="clock-detail-row" key={battle.at}>
            {mark.battles.length > 1 ? (
              <span className="clock-detail-time">{format.format(battle.at)}</span>
            ) : null}
            <bdi className="dash-name" title={battle.opponent ?? undefined}>
              {battle.opponent ?? "Unknown player"}
            </bdi>
            <span className="clock-detail-hit">
              <Stars count={battle.stars} /> {battle.destruction}%
            </span>
            <b className={battle.trophyChange > 0 ? "positive" : "negative"}>
              {signed(battle.trophyChange)}
            </b>
          </div>
        ))}
      </div>
    </>
  );
}

/** Units of the face as a share of its box's width or height. */
const share = (units: number, of: number) => `${((units / of) * 100).toFixed(2)}%`;

/** A mark's button: over its ticks, along the dial, in the face's box. */
function markBox(mark: ClockMark): React.CSSProperties {
  const [x, y] = point(mark.minutes, MARK_RADIUS[mark.kind]);
  return {
    left: share(x - FACE.left, FACE.width),
    top: share(y - FACE.top, FACE.height),
    width: share(HIT + (mark.battles.length - 1) * TICK_GAP, FACE.width),
    height: share(HIT, FACE.height),
    transform: `translate(-50%, -50%) rotate(${((mark.minutes / DAY_MINUTES) * 360).toFixed(1)}deg)`,
  };
}

/**
 * The Legend clock: a 24-hour dial in the chosen time zone with the Reset on
 * top, the Legend day gone so far as an ember arc from the Reset to now and
 * the rest faded, and the countdown big in the middle over the Reset's local
 * time. Each battle is a thin tick on the dial at its time: attacks outside
 * in ember, defenses inside in ink. It draws nothing time-based until `nowMs` is known
 * in the browser, so the server never guesses a time zone.
 */
export function LegendClock({
  battles,
  nowMs,
  timeZone,
}: {
  battles: ClockBattle[];
  nowMs: number | null;
  timeZone: string;
}) {
  const [open, setOpen] = useState<{ index: number; anchor: DOMRect | null } | null>(
    null,
  );
  const [phone, setPhone] = useState(false);
  // A phone or iPad starts with the small clock; the full one has battles to tap.
  const [compact, setCompact] = useState(false);
  const [full, setFull] = useState(false);
  const detail = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const phoneQuery = window.matchMedia("(max-width: 40rem)");
    const compactQuery = window.matchMedia("(max-width: 64rem)");
    const update = () => {
      setPhone(phoneQuery.matches);
      setCompact(compactQuery.matches);
    };
    update();
    for (const query of [phoneQuery, compactQuery])
      query.addEventListener("change", update);
    return () => {
      for (const query of [phoneQuery, compactQuery])
        query.removeEventListener("change", update);
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const close = (event: Event) => {
      if (event instanceof KeyboardEvent && event.key !== "Escape") return;
      const target = event.target as Element | null;
      if (event.type === "click" && target?.closest(".clock-mark, .clock-detail")) return;
      setOpen(null);
    };
    document.addEventListener("keydown", close);
    document.addEventListener("click", close);
    return () => {
      document.removeEventListener("keydown", close);
      document.removeEventListener("click", close);
    };
  }, [open]);

  const resetMs = nowMs === null ? null : nextResetMs(nowMs);
  const dayStart = resetMs === null ? null : resetMs - DAY_MINUTES * 60_000;
  const format = timeFormatter(timeZone);
  const elapsed =
    nowMs === null || dayStart === null ? null : (nowMs - dayStart) / 60_000;
  const countdown = nowMs === null ? "–" : resetCountdown(nowMs).text;
  const resetLabel = resetMs === null ? "--:--" : format.format(resetMs);
  const marks = dayStart === null ? [] : clockMarks(battles, dayStart);
  const [arcX, arcY] = point(elapsed ?? 0, RING);
  const toggle = (index: number, target: Element) =>
    setOpen((current) =>
      current?.index === index ? null : { index, anchor: target.getBoundingClientRect() },
    );
  const openMark = open ? marks[open.index] : undefined;
  const small = compact && !full;

  return (
    <div className="legend-clock" ref={detail} data-full={full || undefined}>
      <div className="legend-clock-dial">
        <svg
          className="legend-clock-face"
          viewBox={`${FACE.left} ${FACE.top} ${FACE.width} ${FACE.height}`}
          role="img"
          aria-label={`Legend clock: ${countdown} to Reset at ${resetLabel}`}
        >
          <circle className="clock-dial" r={RING} />
          {/* The Legend day so far: from the Reset on top round to now. */}
          {elapsed !== null && elapsed > 0 ? (
            <path
              className="clock-gone"
              d={`M0 ${-RING}A${RING} ${RING} 0 ${elapsed > DAY_MINUTES / 2 ? 1 : 0} 1 ${arcX.toFixed(1)} ${arcY.toFixed(1)}`}
            />
          ) : null}
          {/* The Reset named on top, where the day starts; its time is in the middle. */}
          <text className="clock-reset-label" y={-RING - 25}>
            Reset
          </text>
          {/* Each battle a tick at its time; a mark's battles side by side. */}
          {marks.flatMap((mark) =>
            tickMinutes(mark).map((minutes, index) => {
              const [inner, outer] = clearOfNow(mark.kind, minutes, elapsed);
              const [x1, y1] = point(minutes, inner);
              const [x2, y2] = point(minutes, outer);
              return (
                <line
                  key={`${mark.kind}-${mark.battles[index]!.at}-${index}`}
                  className={`clock-tick clock-tick-${mark.kind}`}
                  x1={x1.toFixed(1)}
                  y1={y1.toFixed(1)}
                  x2={x2.toFixed(1)}
                  y2={y2.toFixed(1)}
                />
              );
            }),
          )}
          {elapsed !== null ? (
            <>
              <circle className="clock-now" cx={arcX} cy={arcY} r="9" />
              <text
                className="clock-now-label"
                x={nowLabel(elapsed)[0]}
                y={nowLabel(elapsed)[1] + 4}
                textAnchor="middle"
              >
                Now
              </text>
            </>
          ) : null}
          <text className="clock-countdown" y="4">
            {countdown}
          </text>
          <text className="clock-caption" y="26">
            to Reset at {resetLabel}
          </text>
        </svg>
        {/* Over each mark's ticks, a button that names its battles when
            pointed at, and opens them when tapped. */}
        {marks.map((mark, index) => (
          <button
            key={`${mark.kind}-${mark.battles[0]!.at}`}
            type="button"
            className={`clock-mark clock-mark-${mark.kind}`}
            style={markBox(mark)}
            tabIndex={small ? -1 : 0}
            title={markReadout(mark, format)}
            aria-label={markReadout(mark, format)}
            aria-expanded={open?.index === index}
            onClick={(event) => toggle(index, event.currentTarget)}
          />
        ))}
      </div>
      {/* The countdown beside a small dial on a phone; today's count of each
          kind under the dial, or as columns beside it when the card spans a
          row; and the phone's switch to the full clock and back. */}
      <p className="legend-clock-summary" aria-hidden="true">
        <b>{countdown}</b> to Reset at {resetLabel}
      </p>
      <p className="legend-clock-key">
        {(["attack", "defense"] as const).map((kind) => {
          const total = battles.filter((battle) => battle.kind === kind).length;
          return (
            <span key={kind} className={`legend-clock-key-${kind}`}>
              {/* The dial's own tick, so the line names the ticks. */}
              <i className={`clock-key-tick clock-tick-${kind}`} aria-hidden="true" />
              <span>
                <b>{total}</b> {kind}
                {total === 1 ? "" : "s"}
              </span>
            </span>
          );
        })}
      </p>
      <button
        type="button"
        className="button secondary legend-clock-toggle"
        onClick={() => {
          setOpen(null);
          setFull(!full);
        }}
      >
        {/* One line in one span, as the button's own layout would drop the
            space between two; a small phone says "Full clock", and a screen
            reader hears it all. */}
        <span>
          <span className="legend-clock-toggle-verb">Show </span>
          <span className="legend-clock-toggle-noun">
            {full ? "small clock" : "full clock"}
          </span>
        </span>
      </button>
      {openMark ? (
        <MarkDetail
          mark={openMark}
          format={format}
          phone={phone}
          anchor={open?.anchor ?? null}
          onClose={() => setOpen(null)}
        />
      ) : null}
    </div>
  );
}
