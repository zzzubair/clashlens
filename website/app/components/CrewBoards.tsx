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

const ROLE_CHIP: Record<CrewRole, string | null> = {
  owner: "Owner",
  admin: "Admin",
  uploader: "Uploader",
  member: null,
};

/** Owner, Admin and Uploader chips; members have none. */
export function RoleChip({ role }: { role: CrewRole }) {
  const label = ROLE_CHIP[role];
  return label ? <span className={`crew-chip crew-chip-${role}`}>{label}</span> : null;
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
    <span
      className={`crew-value ${board === "attackers" ? "score-positive" : "score-negative"}`}
    >
      {formatAverage(row, board, period)}
      <small>
        {period === "today"
          ? battles
          : `${row.days} ${row.days === 1 ? "day" : "days"} · ${battles}`}
      </small>
    </span>
  );
}
