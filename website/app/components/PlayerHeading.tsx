import { Suspense } from "react";
import { Await } from "react-router";

import { clanProfileLink, playerProfileLink } from "../lib/game-links";
import type { LiveClan } from "../services/live-clan.server";

/**
 * The player page's name, tag and clan. The name and the clan open their
 * profiles in Clash of Clans; a clan without a saved tag stays plain text.
 * The saved clan shows first; the game's answer for the clan right now
 * replaces it when it streams in, and leaves it when the game gave none.
 */
export function PlayerHeading({
  name,
  tag,
  clan,
  clanTag,
  liveClan,
}: {
  name: string;
  tag: string;
  clan: string | null;
  clanTag: string | null | undefined;
  liveClan?: Promise<LiveClan | null>;
}) {
  const saved = <Clan name={clan} tag={clanTag} />;
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
