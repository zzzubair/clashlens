import { useEffect, useRef, useState } from "react";

import type { ClockBattle } from "../lib/dashboard";
import { nextResetMs } from "../lib/dashboard";

const DAY_MINUTES = 1440;
const RING = 100;
/** Battles closer than this share one mark on the dial. */
const MERGE_MINUTES = 20;

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
export function starText(stars: number): string {
  const shown = Math.max(0, Math.min(3, stars));
  return "★".repeat(shown) + "☆".repeat(3 - shown);
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

/** Group each kind's battles into marks; a battle within 20 minutes of the last one joins it. */
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
      if (current && minutes - lastMinutes <= MERGE_MINUTES) {
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
  const detail = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const query = window.matchMedia("(max-width: 40rem)");
    const update = () => setPhone(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
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
  const left = elapsed === null ? null : Math.ceil(DAY_MINUTES - elapsed);
  const countdown =
    left === null
      ? "–"
      : `${Math.floor(left / 60)}h ${String(left % 60).padStart(2, "0")}m`;
  const resetLabel = resetMs === null ? "--:--" : format.format(resetMs);
  const marks = dayStart === null ? [] : clockMarks(battles, dayStart);
  const [arcX, arcY] = point(elapsed ?? 0, RING - 8);
  const [handX, handY] = point(elapsed ?? 0, RING - 4);
  const toggle = (index: number, target: Element) =>
    setOpen((current) =>
      current?.index === index ? null : { index, anchor: target.getBoundingClientRect() },
    );
  const openMark = open ? marks[open.index] : undefined;

  return (
    <div className="legend-clock" ref={detail}>
      <svg
        className="legend-clock-face"
        viewBox="-138 -138 276 276"
        role="group"
        aria-label={`Legend clock: ${countdown} to Reset at ${resetLabel}`}
      >
        <circle className="clock-dial" r={RING} />
        {elapsed !== null && left !== null && left > 0 ? (
          <path
            className="clock-left"
            d={`M${arcX.toFixed(1)} ${arcY.toFixed(1)}A${RING - 8} ${RING - 8} 0 ${left > DAY_MINUTES / 2 ? 1 : 0} 1 0 ${-(RING - 8)}`}
          />
        ) : null}
        {Array.from({ length: 8 }, (_, index) => {
          const minutes = index * 180;
          const [x1, y1] = point(minutes, RING);
          const [x2, y2] = point(minutes, RING - 12);
          const [lx, ly] = point(minutes, RING + 18);
          return (
            <g key={minutes}>
              <line className="clock-tick" x1={x1} y1={y1} x2={x2} y2={y2} />
              {index > 0 && dayStart !== null ? (
                <text className="clock-hour" x={lx} y={ly + 4}>
                  {format.format(dayStart + minutes * 60_000)}
                </text>
              ) : null}
            </g>
          );
        })}
        <rect
          className="clock-reset-tab"
          x="-40"
          y="-132"
          width="80"
          height="22"
          rx="5"
        />
        <text className="clock-reset-label" x="0" y="-117">
          RESET {resetLabel}
        </text>
        {marks.map((mark, index) => {
          const [x, y] = point(mark.minutes, mark.kind === "attack" ? 78 : 56);
          return (
            <g
              key={`${mark.kind}-${mark.minutes}`}
              className="clock-mark"
              transform={`translate(${x.toFixed(1)} ${y.toFixed(1)})`}
              role="button"
              tabIndex={0}
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
              <circle r="15" className="clock-hit" />
              <g transform="scale(1.3)">{mark.kind === "attack" ? SWORD : SHIELD}</g>
              {mark.battles.length > 1 ? (
                <>
                  <circle className="clock-count" cx="11" cy="-10" r="7" />
                  <text className="clock-count-text" x="11" y="-6.6">
                    {mark.battles.length}
                  </text>
                </>
              ) : null}
            </g>
          );
        })}
        {elapsed !== null ? (
          <line className="clock-hand" x1="0" y1="0" x2={handX} y2={handY} />
        ) : null}
        <circle className="clock-center" r="40" />
        <text className="clock-countdown" y="3">
          {countdown}
        </text>
        <text className="clock-caption" y="18">
          to Reset
        </text>
      </svg>
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
