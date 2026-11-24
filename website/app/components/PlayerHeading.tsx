import { rankMovement } from "../lib/rank-movement";
import { Suspense } from "react";
import { Await, Link } from "react-router";

import { clanProfileLink, playerProfileLink } from "../lib/game-links";
import type { LiveClan } from "../services/live-clan.server";
import { RANKINGS_PAGE_SIZE } from "./LeaderboardShared";

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
      <span className="player-clan-label">Clan</span>
      {tag ? <GameLink href={clanProfileLink(tag)} name={name} /> : <bdi>{name}</bdi>}
    </span>
  );
}

export interface LiveRankRead {
  now: number;
  atReset: number | null;
}

export function LiveRank({
  rank,
  tag,
}: {
  rank: Promise<LiveRankRead | null> | undefined;
  tag: string;
}) {
  if (rank === undefined) return null;
  return (
    <Suspense fallback={null}>
      <Await resolve={rank} errorElement={null}>
        {(read) => {
          if (read === null) return null;
          const { now, atReset } = read;
          const moved = rankMovement(now, atReset) ?? 0;
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
                {now <= 3 ? now : `#${now.toLocaleString("en-GB")}`}
              </strong>
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
