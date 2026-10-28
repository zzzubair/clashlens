import type { LinkedPlayerCard } from "../lib/account-contracts";
import type { ClockBattle, PlayerDay, RankRange } from "../lib/dashboard";
import { DashboardIcon } from "./DashboardIcon";
import { CardHead, updatedAgo } from "./DashboardParts";
import { TrophyMark } from "./LeaderboardShared";
import { resetCountdown, signed, starText } from "./LegendClock";

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
        <span className="dash-label">
          <DashboardIcon name={kind === "attack" ? "swords" : "shield"} />
        </span>
        <span className="dash-muted">
          {count ?? "–"} of {SLOTS} {noun}s
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
              <span className="dash-stars">{starText(battle.stars)}</span>
              {signed(battle.trophyChange)}
            </li>
          );
        })}
      </ol>
    </div>
  );
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
  const openDefenses = day?.openDefenses ?? null;
  // The oldest read shown, so the card never looks newer than it is.
  const observed = [day?.observedAtMs ?? null, rangeAtMs].filter(
    (at): at is number => at !== null,
  );
  const countdown = nowMs === null ? null : resetCountdown(nowMs);
  return (
    <div className="legend-day-card">
      {/* The tab above shows whose day this is; screen readers hear it here. */}
      <CardHead
        title={
          <>
            {day?.dayNumber ? `Legend day ${day.dayNumber}` : "Legend day"}
            <span className="sr-only">, {player.name ?? player.tag}</span>
          </>
        }
        meta={updatedAgo(observed.length ? Math.min(...observed) : null, nowMs)}
      />
      <div className="legend-day-numbers">
        <span className="legend-day-score">
          <span className="sr-only">Trophies</span>
          <TrophyMark />
          <strong className="dash-big">
            {trophies === null ? "–" : trophies.toLocaleString("en-US")}
          </strong>
          <span className="sr-only">Net today</span>
          {net === null ? (
            <span className="sr-only">–</span>
          ) : (
            <>
              <span
                className={`legend-day-net ${net > 0 ? "positive" : net < 0 ? "negative" : ""}`}
              >
                {signed(net)}
              </span>
              <span className="legend-day-net-label" aria-hidden="true">
                today
              </span>
            </>
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
      <div className="legend-day-ranks">
        <div>
          <span className="dash-label">Last Reset</span>
          <b>{rankText(day?.lastResetRank ?? null)}</b>
        </div>
        <i aria-hidden="true" />
        <div className="legend-day-now">
          <span className="dash-label">Now</span>
          <b className="rank-mark">{rankText(player.rank)}</b>
        </div>
        <i aria-hidden="true" />
        <div className="legend-day-range">
          <span className="dash-label">Next Reset</span>
          <b>{range ? `${rankText(range.best)}–${rankText(range.worst)}` : "–"}</b>
          <span className="dash-estimate">estimate</span>
        </div>
        {projected ? (
          <div className="legend-day-projected">
            <span className="dash-label">Projected</span>
            <strong>{rankText(projected)} at Reset</strong>
            <span className="sr-only">
              An estimate from how players in your spot finished on recent days. It can be
              hundreds of places out while battles are left, and gets closer through the
              day.
            </span>
          </div>
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
      {openDefenses !== null && openDefenses > 0 ? (
        <p className="dash-chip">
          {openDefenses} defense{openDefenses === 1 ? "" : "s"} remain
          {day?.autoDefenseEach !== null && day?.autoDefenseEach !== undefined ? (
            <>
              {" "}
              · auto defense <b>{signed(-day.autoDefenseEach)}</b> each
            </>
          ) : null}
        </p>
      ) : null}
    </div>
  );
}
