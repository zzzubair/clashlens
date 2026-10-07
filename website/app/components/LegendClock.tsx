import type { LinkedPlayerCard } from "../lib/account-contracts";
import type { CardSize, ClockBattle, PlayerDay } from "../lib/dashboard";
import { nextResetMs } from "../lib/dashboard";

const DAY_MINUTES = 1440;
const CENTER = 150;
const RING = 98;
const SLOTS = 8;

export function timeFormatter(timeZone: string): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone,
  });
}

/** A short zone name such as "BST", or the zone's own name when there is none. */
export function timeZoneLabel(timeZone: string, atMs: number): string {
  const part = new Intl.DateTimeFormat("en-GB", { timeZone, timeZoneName: "short" })
    .formatToParts(atMs)
    .find((item) => item.type === "timeZoneName");
  return part?.value ?? timeZone;
}

function point(minutes: number, radius: number): [number, number] {
  const angle = (minutes / DAY_MINUTES) * 2 * Math.PI - Math.PI / 2;
  return [CENTER + radius * Math.cos(angle), CENTER + radius * Math.sin(angle)];
}

function signed(value: number): string {
  if (value > 0) return `+${value.toLocaleString("en-US")}`;
  if (value < 0) return `−${Math.abs(value).toLocaleString("en-US")}`;
  return "0";
}

/**
 * A 24-hour ring with the Reset on top and real clock times in the chosen
 * time zone. Battles sit at the time they landed. Renders its times only once
 * `nowMs` is known in the browser, so the server never guesses a time zone.
 */
function ClockFace({
  nowMs,
  timeZone,
  battles,
}: {
  nowMs: number | null;
  timeZone: string;
  battles: ClockBattle[];
}) {
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
  // A short hand inside the ring, clear of the countdown in the middle.
  const [handStartX, handStartY] = point(elapsed ?? 0, RING - 36);
  const [handX, handY] = point(elapsed ?? 0, RING - 10);
  const [elapsedX, elapsedY] = point(elapsed ?? 0, RING);
  return (
    <svg
      className="legend-clock-face"
      viewBox="0 0 300 300"
      role="img"
      aria-label={`Legend clock: ${countdown} to Reset at ${resetLabel}`}
    >
      <circle className="clock-track" cx={CENTER} cy={CENTER} r={RING} />
      {elapsed !== null && elapsed > 0 ? (
        <path
          className="clock-elapsed"
          d={`M${CENTER} ${CENTER - RING}A${RING} ${RING} 0 ${elapsed > DAY_MINUTES / 2 ? 1 : 0} 1 ${elapsedX.toFixed(1)} ${elapsedY.toFixed(1)}`}
        />
      ) : null}
      {dayStart !== null
        ? Array.from({ length: 7 }, (_, index) => {
            const minutes = (index + 1) * 180;
            const [x, y] = point(minutes, RING + 30);
            return (
              <text key={minutes} className="clock-hour" x={x} y={y + 4}>
                {format.format(dayStart + minutes * 60_000)}
              </text>
            );
          })
        : null}
      <rect className="clock-reset-tab" x={CENTER - 30} y={14} width="60" height="26" />
      <text className="clock-reset-label" x={CENTER} y={25}>
        RESET
      </text>
      <text className="clock-reset-time" x={CENTER} y={36}>
        {resetLabel}
      </text>
      {dayStart !== null
        ? battles.map((battle, index) => {
            const minutes = (battle.at - dayStart) / 60_000;
            if (minutes < 0 || minutes > DAY_MINUTES) return null;
            if (battle.kind === "attack") {
              const [x, y] = point(minutes, RING);
              return <circle key={index} className="clock-attack" cx={x} cy={y} r="6" />;
            }
            const [x, y] = point(minutes, RING - 22);
            return (
              <rect
                key={index}
                className={
                  battle.stars === 3 ? "clock-defense-lost" : "clock-defense-held"
                }
                x={x - 5}
                y={y - 5}
                width="10"
                height="10"
              />
            );
          })
        : null}
      {elapsed !== null ? (
        <line
          className="clock-hand"
          x1={handStartX.toFixed(1)}
          y1={handStartY.toFixed(1)}
          x2={handX.toFixed(1)}
          y2={handY.toFixed(1)}
        />
      ) : null}
      <text className="clock-countdown" x={CENTER} y={CENTER + 6}>
        {countdown}
      </text>
      <text className="clock-caption" x={CENTER} y={CENTER + 28}>
        to Reset
      </text>
      {nowMs !== null ? (
        <text className="clock-caption" x={CENTER} y={CENTER + 44}>
          now {format.format(nowMs)}
        </text>
      ) : null}
    </svg>
  );
}

function BattleRow({
  label,
  battles,
  count,
  complete,
  kind,
}: {
  label: string;
  battles: ClockBattle[];
  count: number | null;
  complete: boolean;
  kind: "attack" | "defense";
}) {
  const total = battles.reduce((sum, battle) => sum + battle.trophyChange, 0);
  return (
    <div className="clock-battles">
      <p className="clock-battles-title">
        {label} {count ?? "–"}/{SLOTS}{" "}
        {complete ? (
          <span className={total > 0 ? "positive" : total < 0 ? "negative" : ""}>
            {signed(total)}
          </span>
        ) : null}
      </p>
      <ol className="clock-boxes">
        {Array.from({ length: SLOTS }, (_, index) => {
          const battle = battles[index];
          if (!battle) return <li key={index} className="clock-box clock-box-empty" />;
          const tone =
            kind === "attack" ? "attack" : battle.stars === 3 ? "lost" : "held";
          return (
            <li
              key={index}
              className={`clock-box clock-box-${tone}`}
              aria-label={`${battle.stars} stars, ${signed(battle.trophyChange)} trophies`}
            >
              <b>{battle.stars}★</b>
              <span>{signed(battle.trophyChange)}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/**
 * The Legend clock card body: clock, live trophies and rank, today's battles.
 * `day` and `today` are null once the Legend day they were read for is over.
 */
export function LegendClock({
  player,
  day,
  today,
  size,
  nowMs,
  timeZone,
}: {
  player: LinkedPlayerCard;
  day: PlayerDay | null;
  today: LinkedPlayerCard["today"];
  size: CardSize;
  nowMs: number | null;
  timeZone: string;
}) {
  const battles = [...(day?.battles ?? [])].sort((a, b) => a.at - b.at);
  const attacks = battles.filter((battle) => battle.kind === "attack");
  const defenses = battles.filter((battle) => battle.kind === "defense");
  const complete = day?.complete === true;
  const net = today?.net ?? null;
  return (
    <div className={`legend-clock legend-clock-${size}`}>
      <ClockFace nowMs={nowMs} timeZone={timeZone} battles={battles} />
      <div className="legend-clock-side">
        <dl className="clock-tiles">
          <div>
            <dt>
              <span className="live-dot" aria-hidden="true" /> Trophies
            </dt>
            <dd>
              {player.trophies === null ? "–" : player.trophies.toLocaleString("en-US")}
            </dd>
            {net !== null ? (
              <dd className="clock-tile-note">{signed(net)} today</dd>
            ) : null}
          </div>
          <div>
            <dt>
              <span className="live-dot" aria-hidden="true" /> Rank
            </dt>
            <dd>
              {player.rank === null ? "–" : `#${player.rank.toLocaleString("en-US")}`}
            </dd>
            {player.rank === null ? (
              <dd className="clock-tile-note">not on the board</dd>
            ) : null}
          </div>
          {size !== "s" ? (
            <div className="clock-tile-estimate">
              <dt>At Reset</dt>
              <dd>≈ –</dd>
              <dd className="clock-tile-note">estimate · coming</dd>
            </div>
          ) : null}
        </dl>
        {size !== "s" ? (
          <>
            <BattleRow
              label="Attacks"
              battles={attacks}
              count={complete ? attacks.length : (today?.attacks ?? null)}
              complete={complete}
              kind="attack"
            />
            <BattleRow
              label="Defenses"
              battles={defenses}
              count={complete ? defenses.length : (today?.defenses ?? null)}
              complete={complete}
              kind="defense"
            />
          </>
        ) : null}
      </div>
      {size !== "s" ? (
        <ul className="clock-key">
          <li>
            <span className="key-attack" aria-hidden="true" /> your attack
          </li>
          <li>
            <span className="key-lost" aria-hidden="true" /> tripled you
          </li>
          <li>
            <span className="key-held" aria-hidden="true" /> you held
          </li>
        </ul>
      ) : null}
    </div>
  );
}
