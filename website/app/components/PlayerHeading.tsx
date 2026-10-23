import { clanProfileLink, playerProfileLink } from "../lib/game-links";

/**
 * The player page's name, tag and clan. The name and the clan open their
 * profiles in Clash of Clans; a clan without a saved tag stays plain text.
 */
export function PlayerHeading({
  name,
  tag,
  clan,
  clanTag,
}: {
  name: string;
  tag: string;
  clan: string | null;
  clanTag: string | null | undefined;
}) {
  return (
    <>
      <h1>
        <GameLink href={playerProfileLink(tag)} name={name} />
      </h1>
      <p className="player-identity">
        <span className="player-tag prominent">{tag}</span>
        {clan ? (
          <span className="player-clan">
            {clanTag ? (
              <GameLink href={clanProfileLink(clanTag)} name={clan} />
            ) : (
              <bdi>{clan}</bdi>
            )}
          </span>
        ) : null}
      </p>
    </>
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
