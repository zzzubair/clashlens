import { useEffect, useRef, useState } from "react";

import type { ClockBattle } from "../lib/dashboard";
import { nextResetMs } from "../lib/dashboard";

const DAY_MINUTES = 1440;
const RING = 100;
/** Battles closer than this share one mark on the dial. */
// About one icon's width on each ring (attacks outer, defenses inner), so no
// two icons overlap.
const MERGE_MINUTES = { attack: 45, defense: 55 } as const;
/** Battles sit on two tracks hugging the dial, clear of the hours inside it:
    defenses on the inner one, attacks on the outer. */
const MARK_RADIUS = { attack: 136, defense: 114 } as const;
/** Each mark's bead, sized so neighbours on a track never touch. */
const BEAD = 12.5;

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

/** "★★☆" for two stars. */
/** Whole minutes left in the Legend day, and the "9h 05m" the clocks show for them. */
export function resetCountdown(nowMs: number): { left: number; text: string } {
  const left = Math.ceil((nextResetMs(nowMs) - nowMs) / 60_000);
  return {
    left,
    text: `${Math.floor(left / 60)}h ${String(left % 60).padStart(2, "0")}m`,
  };
}

export function starText(stars: number): string {
  const shown = Math.max(0, Math.min(3, stars));
  return "★".repeat(shown) + "☆".repeat(3 - shown);
}

/**
 * A time zone as a Clasher says it, with the short name the player page's
 * times use: "Europe/London" is "London time (GMT)", or (BST) in summer.
 */
export function zoneName(timeZone: string): string {
  const place = timeZone.split("/").at(-1)?.replaceAll("_", " ");
  if (!place || timeZone === "UTC") return "UTC";
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
  /** Minutes after the Reset of the first battle in the mark. */
  minutes: number;
  battles: ClockBattle[];
}

/** Group each kind's battles into marks; a battle within MERGE_MINUTES of the last one joins it. */
export function clockMarks(battles: ClockBattle[], dayStartMs: number): ClockMark[] {
  const marks: ClockMark[] = [];
  for (const kind of ["attack", "defense"] as const) {
    let current: ClockMark | null = null;
    let lastMinutes = 0;
    const sorted = battles
      .filter((battle) => battle.kind === kind)
      .map((battle) => ({ battle, minutes: (battle.at - dayStartMs) / 60_000 }))
      .filter(({ minutes }) => minutes >= 0 && minutes <= DAY_MINUTES)
      .sort((a, b) => a.minutes - b.minutes);
    for (const { battle, minutes } of sorted) {
      if (current && minutes - lastMinutes <= MERGE_MINUTES[kind]) {
        current.battles.push(battle);
      } else {
        current = { kind, minutes, battles: [battle] };
        marks.push(current);
      }
      lastMinutes = minutes;
    }
  }
  return marks;
}

const SWORD = (
  <g transform="rotate(45)">
    <path className="clock-sword-blade" d="M0,-10 L2.2,-7 L2.2,3 L-2.2,3 L-2.2,-7 Z" />
    <rect className="clock-ink" x="-5.5" y="3" width="11" height="2.4" rx="1" />
    <rect className="clock-ink" x="-1.2" y="5.4" width="2.4" height="4" />
    <circle className="clock-ink" cy="10.4" r="1.6" />
  </g>
);

const SHIELD = (
  <path
    className="clock-shield"
    d="M0,-8.5 L7.5,-5.5 L7.5,0.5 C7.5,5.5 3.5,8 0,10 C-3.5,8 -7.5,5.5 -7.5,0.5 L-7.5,-5.5 Z"
  />
);

function markLabel(mark: ClockMark): string {
  const count = mark.battles.length;
  const noun = mark.kind === "attack" ? "attack" : "defense";
  return `${count} ${noun}${count > 1 ? "s" : ""}`;
}

/** The battles behind one mark: name · stars % · trophies. */
function MarkDetail({
  mark,
  phone,
  anchor,
  onClose,
}: {
  mark: ClockMark;
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
  const count = mark.battles.length;
  return (
    <>
      {phone ? <div className="clock-sheet-back" onClick={onClose} /> : null}
      <div
        className={phone ? "clock-detail clock-sheet" : "clock-detail clock-popover"}
        style={style}
        role="dialog"
        aria-label={markLabel(mark)}
      >
        <div className="clock-detail-head">
          <b>
            {count > 1 ? markLabel(mark) : mark.kind === "attack" ? "Attack" : "Defense"}
          </b>
          <button type="button" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        {mark.battles.map((battle, index) => (
          <div key={index} className="clock-detail-row">
            <bdi className="dash-name" title={battle.opponent ?? undefined}>
              {battle.opponent ?? "Unknown player"}
            </bdi>
            <span className="dash-stars">
              {starText(battle.stars)} {battle.destruction}%
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

/**
 * The Legend clock: a 24-hour dial in the chosen time zone with the Reset on
 * top, the time left as an orange arc and the countdown in the middle. It
 * draws nothing time-based until `nowMs` is known in the browser, so the
 * server never guesses a time zone.
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
  const left = nowMs === null ? null : resetCountdown(nowMs).left;
  const countdown = nowMs === null ? "–" : resetCountdown(nowMs).text;
  const resetLabel = resetMs === null ? "--:--" : format.format(resetMs);
  const marks = dayStart === null ? [] : clockMarks(battles, dayStart);
  const [arcX, arcY] = point(elapsed ?? 0, RING);
  const [handX, handY] = point(elapsed ?? 0, RING - 6);
  const toggle = (index: number, target: Element) =>
    setOpen((current) =>
      current?.index === index ? null : { index, anchor: target.getBoundingClientRect() },
    );
  const openMark = open ? marks[open.index] : undefined;
  const small = compact && !full;

  return (
    <div className="legend-clock" ref={detail} data-full={full || undefined}>
      <svg
        className="legend-clock-face"
        viewBox="-152 -152 304 304"
        role="group"
        aria-label={`Legend clock: ${countdown} to Reset at ${resetLabel}`}
      >
        <circle className="clock-dial" r={RING} />
        {elapsed !== null && left !== null && left > 0 ? (
          <path
            className="clock-left"
            d={`M${arcX.toFixed(1)} ${arcY.toFixed(1)}A${RING} ${RING} 0 ${left > DAY_MINUTES / 2 ? 1 : 0} 1 0 ${-RING}`}
          />
        ) : null}
        {/* The two tracks the battles sit on. */}
        <circle className="clock-track" r={MARK_RADIUS.defense} />
        <circle className="clock-track" r={MARK_RADIUS.attack} />
        {/* The Reset: an ember pointer and tick at the top, its time on an
            ember tag under it; "to Reset" in the middle names it. */}
        <line className="clock-reset-tick" x1="0" y1={-RING} x2="0" y2={-RING + 14} />
        <path className="clock-reset-pin" d={`M-7 ${-RING - 11}h14l-7 11z`} />
        <rect className="clock-reset-tag" x="-26" y="-75" width="52" height="21" rx="6" />
        <text className="clock-reset-label" x="0" y="-59.5">
          {resetLabel}
        </text>
        {marks.map((mark, index) => {
          // A shared mark sits at the middle of its battles' times.
          const at =
            mark.battles.reduce((sum, battle) => sum + battle.at, 0) /
              mark.battles.length -
            (dayStart ?? 0);
          const radius = MARK_RADIUS[mark.kind];
          const [x, y] = point(at / 60_000, radius);
          return (
            <g
              key={`${mark.kind}-${mark.minutes}`}
              className={`clock-mark clock-mark-${mark.kind}`}
              role="button"
              tabIndex={small ? -1 : 0}
              aria-label={markLabel(mark)}
              aria-expanded={open?.index === index}
              onClick={(event) => toggle(index, event.currentTarget)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  toggle(index, event.currentTarget);
                }
              }}
            >
              <g transform={`translate(${x.toFixed(1)} ${y.toFixed(1)})`}>
                <circle r={BEAD + 4} className="clock-hit" />
                {/* A bead on its kind's track: one battle shows its kind's
                    icon, battles close together share one with their count. */}
                <circle r={BEAD} className="clock-bead" />
                {mark.battles.length > 1 ? (
                  <text className="clock-count-text" y="5">
                    {mark.battles.length}
                  </text>
                ) : (
                  <g transform="scale(0.85)">{mark.kind === "attack" ? SWORD : SHIELD}</g>
                )}
              </g>
            </g>
          );
        })}
        {elapsed !== null ? (
          <line className="clock-hand" x1="0" y1="0" x2={handX} y2={handY} />
        ) : null}
        {/* Hours every 6, clear of the ring and the middle, in place of
            ticks; the Reset's own tag is on top. The hand stands in for an
            hour it is passing, so it never crosses one. */}
        {dayStart === null
          ? null
          : [360, 720, 1080]
              .filter((minutes) => elapsed === null || Math.abs(elapsed - minutes) >= 60)
              .map((minutes) => {
                const [x, y] = point(minutes, 68);
                return (
                  <text key={minutes} className="clock-hour" x={x} y={y + 5}>
                    {format.format(dayStart + minutes * 60_000)}
                  </text>
                );
              })}
        <circle className="clock-center" r="36" />
        <text className="clock-countdown" y="4">
          {countdown}
        </text>
        <text className="clock-caption" y="19">
          to Reset
        </text>
      </svg>
      {/* The countdown beside a small dial on a phone; today's count of each
          mark under the dial, or as columns beside it when the card spans a
          row; and the phone's switch to the full clock and back. */}
      <p className="legend-clock-summary" aria-hidden="true">
        <b>{countdown}</b> to Reset
        <span className="legend-clock-at"> at {resetLabel}</span>
      </p>
      <p className="legend-clock-key" aria-hidden="true">
        {/* The orange arc is the part of the Legend day still to play; beside
            the dial it names the Reset it runs to instead. */}
        <span>
          <i className="legend-clock-key-arc" />
          <span className="legend-clock-key-left">Time left</span>
          <span className="legend-clock-key-reset">
            <b>{resetLabel}</b> Reset · {zoneName(timeZone)}
          </span>
        </span>
        {(["attack", "defense"] as const).map((kind) => {
          const total = battles.filter((battle) => battle.kind === kind).length;
          return (
            <span key={kind}>
              <svg viewBox="-13 -13 26 26">{kind === "attack" ? SWORD : SHIELD}</svg>
              <span>
                <b>{total}</b> {kind}
                {total === 1 ? "" : "s"}
              </span>
            </span>
          );
        })}
        {/* A bead with a number holds that many battles close together. */}
        {marks.some((mark) => mark.battles.length > 1) ? (
          <span className="legend-clock-key-group">
            <i>2</i>
            <span>battles close together</span>
          </span>
        ) : null}
      </p>
      <button
        type="button"
        className="button secondary legend-clock-toggle"
        onClick={() => {
          setOpen(null);
          setFull(!full);
        }}
      >
        {full ? "Show small clock" : "Show full clock"}
      </button>
      {openMark ? (
        <MarkDetail
          mark={openMark}
          phone={phone}
          anchor={open?.anchor ?? null}
          onClose={() => setOpen(null)}
        />
      ) : null}
    </div>
  );
}
