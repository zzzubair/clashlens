import type { LinkedPlayerCard } from "../lib/account-contracts";
import type { ClockBattle, PlayerDay, RankRange } from "../lib/dashboard";
import { CardHead, updatedAgo } from "./DashboardParts";
import { ShieldIcon, Stars, SwordIcon } from "./GameIcons";
import { TrophyMark } from "./LeaderboardShared";
import { resetCountdown, signed } from "./LegendClock";

const SLOTS = 8;

function rankText(rank: number | null): string {
  return rank === null ? "–" : `#${rank.toLocaleString("en-US")}`;
}

function BattleRow({
  battles,
  count,
  total,
  noun,
  kind,
}: {
  battles: ClockBattle[];
  count: number | null;
  total: number | null;
  noun: string;
  kind: "attack" | "defense";
}) {
  const shown = `${count ?? "–"} of ${SLOTS} ${noun}s`;
  return (
    <div className={`legend-day-battles legend-day-${kind}s`}>
      <div className="legend-day-battles-head">
        {kind === "attack" ? <SwordIcon /> : <ShieldIcon />}
        <span className="legend-day-battles-name">
          {kind === "attack" ? "Attacks" : "Defenses"}
        </span>
        <span className="legend-day-battles-count" aria-hidden="true">
          {count ?? "–"}/{SLOTS}
        </span>
        {total !== null && count ? (
          <b className={total > 0 ? "positive" : total < 0 ? "negative" : ""}>
            {signed(total)}
          </b>
        ) : null}
      </div>
      <ol className="dash-star-boxes" aria-label={shown}>
        {Array.from({ length: SLOTS }, (_, index) => {
          const battle = battles[index];
          if (!battle)
            return <li key={index} className="dash-star-box dash-star-box-empty" />;
          return (
            <li
              key={index}
              className={`dash-star-box dash-star-box-${kind}`}
              aria-label={`${battle.stars} stars, ${signed(battle.trophyChange)} trophies`}
            >
              <Stars count={battle.stars} />
              <b>{signed(battle.trophyChange)}</b>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/** When the card's numbers were read: the older of the day and the board behind the range. */
export function dayReadAt(
  day: PlayerDay | null,
  rangeAtMs: number | null,
): number | null {
  const times = [day?.observedAtMs ?? null, rangeAtMs].filter(
    (at): at is number => at !== null,
  );
  return times.length ? Math.min(...times) : null;
}

/**
 * Card 1: live trophies, net today, the rank at the last Reset, the live rank,
 * the range the next Reset can still land in and the projected place in it.
 */
export function LegendDayCard({
  player,
  day,
  range,
  rangeAtMs = null,
  projected = null,
  nowMs = null,
}: {
  player: LinkedPlayerCard;
  day: PlayerDay | null;
  range: RankRange | null;
  /** When the board behind `range` was read. */
  rangeAtMs?: number | null;
  projected?: number | null;
  nowMs?: number | null;
}) {
  const battles = [...(day?.battles ?? [])].sort((a, b) => a.at - b.at);
  const attacks = battles.filter((battle) => battle.kind === "attack");
  const defenses = battles.filter((battle) => battle.kind === "defense");
  const complete = day?.complete === true;
  const sum = (list: ClockBattle[]) =>
    list.reduce((total, battle) => total + battle.trophyChange, 0);
  const attackCount = complete ? attacks.length : (day?.attacks ?? null);
  const defenseCount = complete ? defenses.length : (day?.defenses ?? null);
  const net = day?.net ?? null;
  const trophies = day ? day.trophies : player.trophies;
  const countdown = nowMs === null ? null : resetCountdown(nowMs);
  const fresh = updatedAgo(dayReadAt(day, rangeAtMs), nowMs);
  return (
    <div className="legend-day-card">
      {/* The tab above shows whose day this is; screen readers hear it here.
          How old the numbers are is said once for the page, here. */}
      <CardHead
        meta={
          fresh ? (
            <span className="legend-day-fresh">{fresh.replace(/^u/, "U")}</span>
          ) : null
        }
        icon="cal"
        title={
          <>
            {day?.dayNumber ? `Legend day ${day.dayNumber}` : "Legend day"}
            <span className="sr-only">, {player.name ?? player.tag}</span>
          </>
        }
      />
      <div className="legend-day-numbers">
        <span className="legend-day-score">
          <span className="sr-only">Trophies</span>
          {/* A Legend day means the account is in Legend League: its badge. */}
          {day ? (
            <img
              className="legend-day-badge"
              src="/images/legend-league.webp"
              alt=""
              width={151}
              height={160}
            />
          ) : (
            <TrophyMark />
          )}
          <strong className="dash-big">
            {trophies === null ? "–" : trophies.toLocaleString("en-US")}
          </strong>
          <span className="sr-only">Net today</span>
          {net === null ? (
            <span className="sr-only">–</span>
          ) : (
            <span
              className={`legend-day-net ${net > 0 ? "positive" : net < 0 ? "negative" : ""}`}
            >
              {signed(net)}
              <span className="legend-day-net-label"> today</span>
            </span>
          )}
        </span>
        {countdown ? (
          <span className="legend-day-countdown">
            <svg viewBox="0 0 36 36" aria-hidden="true">
              <circle cx="18" cy="18" r="15" />
              <circle
                className="legend-day-countdown-left"
                cx="18"
                cy="18"
                r="15"
                pathLength="1440"
                strokeDasharray={`${countdown.left} 1440`}
              />
            </svg>
            <b>{countdown.text}</b> to Reset
          </span>
        ) : null}
      </div>
      {/* Last Reset and Now are seen; the next Reset is a forecast, so it
          is named the projected place, drawn lighter, and its ember diamond
          sits on the Next Reset range it always falls in, on the line under
          the three. Before a projection the range is Next Reset's number. */}
      <div className="legend-day-places">
        <dl className="legend-day-ranks">
          <div>
            <dt>Last Reset</dt>
            <dd>{rankText(day?.lastResetRank ?? null)}</dd>
          </div>
          <div className="legend-day-now">
            <dt>Now</dt>
            <dd>
              <b className="rank-mark">{rankText(player.rank)}</b>
            </dd>
          </div>
          <div className="legend-day-next">
            <dt>
              {projected ? (
                <>
                  <i className="legend-day-diamond" aria-hidden="true" />
                  Projected place
                </>
              ) : (
                "Next Reset"
              )}
            </dt>
            <dd>
              {projected ? (
                <span className="legend-day-projected">
                  {rankText(projected)}
                  <span className="sr-only">
                    {" "}
                    at Reset. An estimate from how players in your spot finished on recent
                    days. It can be hundreds of places out while battles are left, and
                    gets closer through the day.
                  </span>
                </span>
              ) : range ? (
                <span className="legend-day-range">
                  <span>{rankText(range.best)}</span>
                  <span className="legend-day-range-dash">–</span>
                  <span>{rankText(range.worst)}</span>
                  <span className="sr-only"> estimate</span>
                </span>
              ) : (
                "–"
              )}
            </dd>
          </div>
        </dl>
        {projected && range ? (
          <p className="legend-day-range legend-day-range-line">
            <span className="legend-day-range-name">Next Reset range</span>
            <span>{rankText(range.best)}</span>
            <span
              className="legend-day-range-bar"
              style={
                {
                  "--at": `${(100 * (projected - range.best)) / Math.max(1, range.worst - range.best)}%`,
                } as React.CSSProperties
              }
              aria-hidden="true"
            />
            <span>{rankText(range.worst)}</span>
          </p>
        ) : null}
      </div>
      <div className="legend-day-slots">
        <BattleRow
          battles={attacks}
          count={attackCount}
          total={complete ? sum(attacks) : null}
          noun="attack"
          kind="attack"
        />
        <BattleRow
          battles={defenses}
          count={defenseCount}
          total={complete ? sum(defenses) : null}
          noun="defense"
          kind="defense"
        />
      </div>
    </div>
  );
}
