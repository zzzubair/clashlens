import { Link, type To } from "react-router";

import {
  BOARDS,
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

/** How full a crew is, as a bar; the words beside it say the numbers. */
export function PlacesMeter({ used, size }: { used: number; size: number }) {
  const value = `${Math.min(100, (used / size) * 100)}%`;
  return (
    <span className="meter" aria-hidden="true">
      <span style={{ "--value": value } as React.CSSProperties} />
    </span>
  );
}

/**
 * The crew page's address for a board and period. Live and Season are the
 * defaults, so they stay out of it; a board's own page passes no board.
 */
export function crewSearch(period: CrewPeriod, board?: BoardKey): string {
  const params = new URLSearchParams();
  if (board !== undefined && board !== "live") params.set("board", BOARDS[board].slug);
  if (period !== "season") params.set("period", period);
  const search = params.toString();
  return search === "" ? "" : `?${search}`;
}

/**
 * Today / Last 7 days / Season, as links so it works without JavaScript. On
 * the crew page `board` keeps the board picked on a phone.
 */
export function PeriodSwitch({
  period,
  board,
}: {
  period: CrewPeriod;
  board?: BoardKey;
}) {
  return (
    <nav aria-label="Period" className="leaderboard-view-switch crew-period">
      {PERIODS.map((value) => (
        <Link
          key={value}
          className="button secondary"
          aria-current={value === period ? "page" : undefined}
          to={{ search: crewSearch(value, board) }}
          preventScrollReset
        >
          {PERIOD_LABELS[value]}
        </Link>
      ))}
    </nav>
  );
}

export const DEFENSE_BOARDS = ["best_defenders", "worst_defenders"] as const;

/**
 * Best and Worst defenders are the same accounts in opposite orders, so they
 * share one card with this switch between them.
 */
export function DefenseOrder({
  board,
  to,
}: {
  board: BoardKey;
  to: (board: BoardKey) => To;
}) {
  return (
    <nav aria-label="Defense order" className="leaderboard-view-switch crew-order">
      {DEFENSE_BOARDS.map((key) => (
        <Link
          key={key}
          className="button secondary"
          aria-current={key === board ? "page" : undefined}
          to={to(key)}
          replace
          preventScrollReset
        >
          {key === "best_defenders" ? "Best" : "Worst"}
          <span className="sr-only"> defenders</span>
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
  const battles = board === "attackers" || board === "streaks" ? "attacks" : "defenses";
  if (period === "today") return `No ${battles} yet today`;
  if (dayNumber === 1 && board !== "streaks") return "No finished day yet";
  return `No Legend ${battles} in this period`;
}

/**
 * Ranked rows; each opens the player's page, whose Back returns here. The
 * signed-in clasher's own accounts are marked. Every board is one list with
 * rank, name and number in the same columns; the top three wear their medal,
 * and on Live every account below first says under its name how far it
 * trails. Tags stay for screen readers, to tell same-named players apart.
 * Averages and streaks add a slim bar under the name, scaled to the board's
 * biggest number, so the spread shows at a glance.
 */
export function BoardRows({
  rows,
  board,
  period,
  backLabel,
  limit,
}: {
  rows: BoardRow[];
  board: BoardKey;
  period: CrewPeriod;
  backLabel: string;
  limit?: number;
}) {
  const backState = useBackState(backLabel);
  const shown = rows.slice(0, limit);
  const lead = rows[0]?.kind === "trophies" ? rows[0].trophies : null;
  const most = Math.max(0, ...rows.map(rowNumber));
  return (
    <ol className="crew-rows">
      {shown.map((row, index) => (
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
              {row.name ? <span className="sr-only">{row.tag}</span> : null}
              {row.kind !== "trophies" && most > 0 ? (
                <span
                  className="crew-bar"
                  aria-hidden="true"
                  style={
                    {
                      "--value": `${(rowNumber(row) / most) * 100}%`,
                    } as React.CSSProperties
                  }
                />
              ) : null}
              {board === "live" &&
              lead !== null &&
              row.kind === "trophies" &&
              lead > row.trophies ? (
                <span className="crew-behind">
                  {(lead - row.trophies).toLocaleString("en-US")} behind
                  <span className="sr-only"> first</span>
                </span>
              ) : null}
            </span>
            <RowValue row={row} board={board} period={period} />
          </Link>
        </li>
      ))}
    </ol>
  );
}

/** The number a row is ranked on, without its sign: an average a day, or a streak. */
function rowNumber(row: BoardRow): number {
  if (row.kind === "trophies") return row.trophies;
  if (row.kind === "streak") return row.best;
  return row.total / row.days;
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
    <span className="crew-value">
      {formatAverage(row, board, period)}
      <small>
        {period === "today"
          ? battles
          : `${row.days} ${row.days === 1 ? "day" : "days"} · ${battles}`}
      </small>
    </span>
  );
}
