import type { LinkedPlayerCard } from "../lib/account-contracts";
import type { ClockBattle, PlayerDay, RankRange } from "../lib/dashboard";
import { CardHead, Name, updatedAgo } from "./DashboardParts";
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
  const shown =
    count === null ? `– ${noun}s` : `${count} ${noun}${count === 1 ? "" : "s"}`;
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
        <span className="dash-muted">{shown}</span>
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
 * the range the next Reset can still land in and the likely part of it.
 */
export function LegendDayCard({
  player,
  day,
  range,
  likely = null,
  nowMs = null,
}: {
  player: LinkedPlayerCard;
  day: PlayerDay | null;
  range: RankRange | null;
  likely?: RankRange | null;
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
  const meta = [
    day?.dayNumber && day.dayCount ? `Day ${day.dayNumber} of ${day.dayCount}` : null,
    updatedAgo(day?.observedAtMs ?? null, nowMs),
  ].filter(Boolean);
  return (
    <div className="legend-day-card">
      <CardHead
        title={
          <>
            {"Legend day · "}
            <Name>{player.name ?? player.tag}</Name>
          </>
        }
        meta={meta.length ? meta.join(" · ") : null}
      />
      <div className="legend-day-numbers">
        <div>
          <span className="dash-label">Trophies</span>
          <span className="dash-big">
            {trophies === null ? "–" : trophies.toLocaleString("en-US")}
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
        {likely ? (
          <div className="legend-day-likely">
            <span className="dash-label">Likely finish</span>
            <strong>
              {rankText(likely.best)} – {rankText(likely.worst)}
            </strong>
            <span className="dash-muted">
              Where you will most likely be at the Reset, right 9 times in 10. Wide early
              in the day, it narrows as attacks and defenses are used up.
            </span>
          </div>
        ) : null}
      </div>
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
