import { Suspense } from "react";
import { Await, Link } from "react-router";

import { clanProfileLink, playerProfileLink } from "../lib/game-links";
import type { LiveClan } from "../services/live-clan.server";
import { RANKINGS_PAGE_SIZE } from "./LeaderboardShared";

/**
 * The player page's name, then its tag, clan and Crews on one line. The tag,
 * with a small arrow, opens the player's profile in Clash of Clans, so the
 * arrow never reads as part of the name. The saved clan shows first as plain
 * text; the game's
 * answer for the clan right now replaces it, linked to the clan's profile,
 * when it streams in, and leaves it when the game gave none.
 */
export function PlayerHeading({
  name,
  tag,
  clan,
  liveClan,
  children,
}: {
  name: string;
  tag: string;
  clan: string | null;
  liveClan?: Promise<LiveClan | null>;
  /** The player's Crews, at the end of the tag and clan line. */
  children?: React.ReactNode;
}) {
  const saved = <Clan name={clan} tag={null} />;
  return (
    <>
      <h1>
        <bdi>{name}</bdi>
      </h1>
      <p className="player-identity">
        <GameLink
          className="player-tag prominent"
          href={playerProfileLink(tag)}
          name={name}
          text={tag}
        />
        {liveClan === undefined ? (
          saved
        ) : (
          <Suspense fallback={saved}>
            <Await resolve={liveClan} errorElement={saved}>
              {(live) =>
                live === null ? (
                  saved
                ) : (
                  <Clan name={live.clan?.name} tag={live.clan?.tag} />
                )
              }
            </Await>
          </Suspense>
        )}
        {children}
      </p>
    </>
  );
}

function Clan({
  name,
  tag,
}: {
  name: string | null | undefined;
  tag: string | null | undefined;
}) {
  if (!name) return null;
  return (
    <span className="player-clan">
      {/* The word, after the Clan Castle the stylesheet draws, so a Crew
          named like the clan can't pass for it. The castle stays out of the
          markup, so the live clan stays small enough to arrive in place. */}
      <span className="player-clan-label">Clan</span>
      {tag ? <GameLink href={clanProfileLink(tag)} name={name} /> : <bdi>{name}</bdi>}
    </span>
  );
}

/** The player's live place and their place on the last Reset's Daily board,
 *  both from the live board, as Rankings shows them. */
export interface LiveRankRead {
  now: number;
  atReset: number | null;
}

/**
 * The player's place on the live Leaderboard now, and the places they moved
 * since the last Reset's Daily board. It streams in after the page and leaves
 * nothing when the board doesn't hold the player or can't be read. It opens
 * the board at the player.
 */
export function LiveRank({
  rank,
  since,
  tag,
}: {
  rank: Promise<LiveRankRead | null> | undefined;
  /** The place on the last Reset's Daily board when the live board has none. */
  since: number | null;
  tag: string;
}) {
  if (rank === undefined) return null;
  return (
    <Suspense fallback={null}>
      <Await resolve={rank} errorElement={null}>
        {(read) => {
          if (read === null) return null;
          const { now } = read;
          const atReset = read.atReset ?? since;
          const moved = atReset === null ? 0 : atReset - now;
          const places = Math.abs(moved).toLocaleString("en-GB");
          return (
            <Link
              className="player-live-rank"
              to={`/leaderboards/tracked?view=live&page=${Math.ceil(now / RANKINGS_PAGE_SIZE)}&player=${encodeURIComponent(tag)}`}
              aria-label={`Live rank ${now.toLocaleString("en-GB")}${
                moved
                  ? `, ${moved > 0 ? "up" : "down"} ${places} since the last Reset`
                  : ""
              }. Open the Leaderboard`}
            >
              <strong className="rank-mark" data-podium-rank={now <= 3 ? now : undefined}>
                {/* A podium plate shows the bare place, as on Rankings. */}
                {now <= 3 ? now : `#${now.toLocaleString("en-GB")}`}
              </strong>
              {/* One line: the place, its move, and what the move is against. */}
              {moved ? (
                <>
                  <span className={moved > 0 ? "score-positive" : "score-negative"}>
                    {moved > 0 ? "▲" : "▼"}
                    {places}
                  </span>
                  <span className="player-live-rank-since">since Reset</span>
                </>
              ) : null}
            </Link>
          );
        }}
      </Await>
    </Suspense>
  );
}

/** A link into the game, named for what it opens and showing `text`, the
 *  name unless given. */
function GameLink({
  href,
  name,
  text = name,
  className,
}: {
  href: string;
  name: string;
  text?: string;
  className?: string;
}) {
  return (
    <a
      className={className ? `game-link ${className}` : "game-link"}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={`Open ${name} in Clash of Clans`}
      title="Open in Clash of Clans"
    >
      <bdi>{text}</bdi>
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
      </svg>
    </a>
  );
}
