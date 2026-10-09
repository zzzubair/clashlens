import type { LinkedPlayerCard } from "../lib/account-contracts";
import type { ClockBattle, PlayerDay, RankRange } from "../lib/dashboard";
import { signed, starText } from "./LegendClock";

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
  const shown = count ?? battles.length;
  return (
    <div className="legend-day-battles">
      <div>
        {total !== null ? (
          <b className={total > 0 ? "positive" : total < 0 ? "negative" : ""}>
            {signed(total)}
          </b>
        ) : (
          <b>–</b>
        )}
        <span className="dash-muted">
          {shown} {noun}
          {shown === 1 ? "" : "s"}
        </span>
      </div>
      <ol className="dash-star-boxes" aria-label={`${shown} ${noun}s`}>
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
 * Card 1: live trophies, net today, the rank at the last Reset, the live rank
 * among tracked players and the range the next Reset can still land in.
 */
export function LegendDayCard({
  player,
  day,
  range,
}: {
  player: LinkedPlayerCard;
  day: PlayerDay | null;
  range: RankRange | null;
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
  // From the server, which follows the game's Season Day 1 rule.
  const openDefenses = day?.openDefenses ?? null;
  return (
    <div className="legend-day-card">
      <div className="legend-day-numbers">
        <div>
          <span className="dash-label">Trophies</span>
          <span className="dash-big">
            {player.trophies === null ? "–" : player.trophies.toLocaleString("en-US")}
          </span>
        </div>
        <div>
          <span className="dash-label">Net today</span>
          <span
            className={`dash-big ${net === null ? "" : net > 0 ? "positive" : net < 0 ? "negative" : ""}`}
          >
            {net === null ? "–" : signed(net)}
          </span>
        </div>
      </div>
      <div className="legend-day-ranks">
        <div>
          <span className="dash-label">Last Reset</span>
          <b>{rankText(day?.lastResetRank ?? null)}</b>
        </div>
        <span className="legend-day-arrow" aria-hidden="true">
          →
        </span>
        <div>
          <span className="dash-label">Now</span>
          <b>{rankText(player.rank)}</b>
        </div>
        <span className="legend-day-arrow" aria-hidden="true">
          →
        </span>
        <div className="legend-day-range">
          <span className="dash-label">Next Reset</span>
          <b>{range ? `${rankText(range.best)} – ${rankText(range.worst)}` : "–"}</b>
          <span className="dash-estimate">estimate · narrows as the day goes on</span>
        </div>
      </div>
      <p className="dash-muted legend-day-board">among tracked players</p>
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
