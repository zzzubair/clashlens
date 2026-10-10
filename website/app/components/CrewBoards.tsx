import { Link } from "react-router";

import {
  formatAverage,
  PERIOD_LABELS,
  PERIODS,
  type BoardKey,
  type BoardRow,
  type CrewPeriod,
  type CrewRole,
} from "../lib/crew-contracts";
import { canonicalPlayerPath } from "../lib/player-tag";
import { useBackState } from "./BackLink";
import { TrophyMark } from "./LeaderboardShared";

/** Owner and Admin chips; members have none. */
export function RoleChip({ role }: { role: CrewRole }) {
  if (role === "member") return null;
  return (
    <span className={`crew-chip crew-chip-${role}`}>
      {role === "owner" ? "Owner" : "Admin"}
    </span>
  );
}

/** Today / Last 7 days / Season, as links so it works without JavaScript. */
export function PeriodSwitch({ period }: { period: CrewPeriod }) {
  return (
    <nav aria-label="Period" className="leaderboard-view-switch crew-period">
      {PERIODS.map((value) => (
        <Link
          key={value}
          className="button secondary"
          aria-current={value === period ? "page" : undefined}
          to={{ search: value === "season" ? "" : `?period=${value}` }}
          preventScrollReset
        >
          {PERIOD_LABELS[value]}
        </Link>
      ))}
    </nav>
  );
}

/** What an empty board says. */
export function emptyBoardText(board: BoardKey, period: CrewPeriod, dayNumber: number) {
  if (board === "live") return "Nobody on the Live leaderboard yet";
  if (board === "top")
    return dayNumber === 1 ? "No finished day yet" : "No Reset reading yet";
  if (period === "today") return "No battles yet today";
  if (dayNumber === 1 && board !== "streaks") return "No finished day yet";
  return "No Legend battles in this period";
}

/**
 * Ranked rows; each opens the player's page, whose Back returns here. The
 * signed-in clasher's own accounts are marked.
 */
export function BoardRows({
  rows,
  board,
  period,
  backLabel,
}: {
  rows: BoardRow[];
  board: BoardKey;
  period: CrewPeriod;
  backLabel: string;
}) {
  const backState = useBackState(backLabel);
  return (
    <ol className="crew-rows">
      {rows.map((row, index) => (
        <li key={row.tag} className={row.you ? "crew-row-you" : undefined}>
          <Link className="crew-row" to={canonicalPlayerPath(row.tag)} state={backState}>
            <span
              className="crew-rank"
              data-podium-rank={index < 3 ? index + 1 : undefined}
            >
              {index + 1}
            </span>
            <span className="crew-who">
              <b>
                {row.name ?? row.tag}
                {row.you ? <span className="crew-chip crew-chip-you">You</span> : null}
              </b>
              <span>{row.tag}</span>
            </span>
            <RowValue row={row} board={board} period={period} />
          </Link>
        </li>
      ))}
    </ol>
  );
}

function RowValue({
  row,
  board,
  period,
}: {
  row: BoardRow;
  board: BoardKey;
  period: CrewPeriod;
}) {
  if (row.kind === "trophies") {
    return (
      <span className="crew-value">
        <TrophyMark />
        {row.trophies.toLocaleString("en-US")}
      </span>
    );
  }
  if (row.kind === "streak") {
    return (
      <span className="crew-value">
        {row.best}
        {row.going ? <small className="crew-going">Still going</small> : null}
      </span>
    );
  }
  const battles = `${row.battles} ${board === "attackers" ? "attack" : "defense"}${row.battles === 1 ? "" : "s"}`;
  return (
    <span className={`crew-value ${board === "attackers" ? "crew-gain" : "crew-loss"}`}>
      {formatAverage(row, board, period)}
      <small>
        {period === "today"
          ? battles
          : `${row.days} ${row.days === 1 ? "day" : "days"} · ${battles}`}
      </small>
    </span>
  );
}
