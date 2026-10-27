import { Suspense } from "react";
import { Await } from "react-router";

import { clanProfileLink, playerProfileLink } from "../lib/game-links";
import type { LiveClan } from "../services/live-clan.server";

/**
 * The player page's name, then its tag, clan and Crews on one line. The name opens the player's profile
 * in Clash of Clans. The saved clan shows first as plain text; the game's
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
        <GameLink href={playerProfileLink(tag)} name={name} />
      </h1>
      <p className="player-identity">
        <span className="player-tag prominent">{tag}</span>
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
      {tag ? <GameLink href={clanProfileLink(tag)} name={name} /> : <bdi>{name}</bdi>}
    </span>
  );
}

function GameLink({ href, name }: { href: string; name: string }) {
  return (
    <a
      className="game-link"
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={`Open ${name} in Clash of Clans`}
    >
      <bdi>{name}</bdi>
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
      </svg>
    </a>
  );
}
