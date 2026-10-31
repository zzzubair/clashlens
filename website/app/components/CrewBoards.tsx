import { Link, type To } from "react-router";

import {
  BOARDS,
  boardDescription,
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
import { TrophyIcon } from "./GameIcons";

/**
 * A board's name on the crew pages. "Top players" beside "Live leaderboard"
 * read as a second ranking of the same thing; this board is the standings held
 * at the last Reset, so its name says that.
 */
export function boardTitle(board: BoardKey): string {
  return board === "top" ? "At last Reset" : BOARDS[board].title;
}

/** The line under a board's name; At last Reset's name already says when. */
export function boardCaption(board: BoardKey, period: CrewPeriod): string {
  return board === "top"
    ? "Trophies when this Legend day began"
    : boardDescription(board, period);
}

/** Owner and Admin chips; members have none. */
export function RoleChip({ role }: { role: CrewRole }) {
  if (role === "member") return null;
  return (
    <span className={`crew-chip crew-chip-${role}`}>
      {role === "owner" ? "Owner" : "Admin"}
    </span>
  );
}

/** A crew's accounts and the Clashers who own them, as every crew page
 *  words it: "4 accounts · 2 Clashers". Without the Clashers when they
 *  aren't shown to this visitor. */
export function crewCount(used: number, clashers: number | null): string {
  const accounts = `${used} ${used === 1 ? "account" : "accounts"}`;
  return clashers === null
    ? accounts
    : `${accounts} · ${clashers} ${clashers === 1 ? "Clasher" : "Clashers"}`;
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
 * signed-in clasher's own accounts are marked. Every board is one list in one
 * row style: a rank (the top three on podium plates), the name and the
 * number in the same columns, so boards side by side line up row for row.
 * On Live and At last Reset every
 * account below first says under its name how far it trails, and on Live
 * `today` adds under its trophies what it has won or lost since the last
 * Reset (CrewDay.tsx's sinceReset). Tags stay for
 * screen readers, to tell same-named players apart. The numbers carry the
 * ranking alone: in a crew they often sit a few apart, which bars from zero
 * would draw as the same length.
 */
export function BoardRows({
  rows,
  board,
  period,
  backLabel,
  limit,
  today,
}: {
  rows: BoardRow[];
  board: BoardKey;
  period: CrewPeriod;
  backLabel: string;
  limit?: number;
  today?: Map<string, number>;
}) {
  const backState = useBackState(backLabel);
  const shown = rows.slice(0, limit);
  const lead = rows[0]?.kind === "trophies" ? rows[0].trophies : null;
  return (
    <ol className="crew-rows">
      {shown.map((row, index) => (
        <li key={row.tag} className={row.you ? "crew-row-you" : undefined}>
          <Link className="crew-row" to={canonicalPlayerPath(row.tag)} state={backState}>
            {/* Every board's top three take the site's podium plates, as
                every ranked board on the site does; the rest are plain numbers.
                Worst defenders' first is no award, so its three plates stay
                plain, in the same shape. */}
            <span
              className={index < 3 ? "crew-rank rank-mark" : "crew-rank"}
              data-podium-rank={
                index < 3 && board !== "worst_defenders" ? index + 1 : undefined
              }
            >
              {index + 1}
            </span>
            <span className="crew-who">
              <b>
                {row.name ?? row.tag}
                {row.you ? <span className="you-badge">You</span> : null}
              </b>
              {row.name ? <span className="sr-only">{row.tag}</span> : null}
              {lead !== null && row.kind === "trophies" && lead > row.trophies ? (
                <span className="crew-behind">
                  {(lead - row.trophies).toLocaleString("en-US")} behind
                  <span className="sr-only"> first</span>
                </span>
              ) : null}
            </span>
            <RowValue
              row={row}
              board={board}
              period={period}
              today={today?.get(row.tag)}
            />
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
  today,
}: {
  row: BoardRow;
  board: BoardKey;
  period: CrewPeriod;
  today?: number;
}) {
  if (row.kind === "trophies") {
    return (
      <span className="crew-value">
        <TrophyIcon />
        {row.trophies.toLocaleString("en-US")}
        {today === undefined ? null : (
          <small
            className={
              today > 0 ? "score-positive" : today < 0 ? "score-negative" : undefined
            }
          >
            {today > 0 ? "+" : today < 0 ? "−" : ""}
            {Math.abs(today).toLocaleString("en-US")} today
          </small>
        )}
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
