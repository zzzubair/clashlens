import type { ReactNode } from "react";
import { Link } from "react-router";
import type { TrackedPlayerEntry } from "../lib/contracts";
import { canonicalPlayerPath } from "../lib/player-tag";
import type { BackTarget } from "./BackLink";
import { TrophyMark } from "./LeaderboardShared";
import { RankMove, RecentDays, TodayChange } from "./ResetChange";

/** Profiles without a known clan use the same wording on both boards. */
function clanName(clan: string) {
  return clan === "Unknown" ? (
    <span className="no-clan">No clan</span>
  ) : (
    <bdi>{clan}</bdi>
  );
}

export function StandingsCells({
  entry,
  compared,
  showsDays,
  showsToday,
  backState,
  rankings = false,
  you = false,
  mobileUpdate,
  beforeTrophies,
}: {
  entry: TrackedPlayerEntry;
  compared: boolean;
  showsDays: boolean;
  showsToday: boolean;
  backState: { back: BackTarget };
  rankings?: boolean;
  you?: boolean;
  mobileUpdate?: ReactNode;
  beforeTrophies?: ReactNode;
}) {
  return (
    <>
      <td className="rank-cell" data-label="Rank">
        <span className="rank-mark">
          {rankings ? entry.rank.toLocaleString() : entry.rank}
        </span>
      </td>
      {compared ? (
        <td className="move-cell">
          <RankMove entry={entry} />
        </td>
      ) : null}
      <th scope="row" className="player-cell" data-label="Player">
        <span className="player-id">
          <Link
            className="player-name"
            to={canonicalPlayerPath(entry.tag)}
            state={backState}
          >
            <bdi>{entry.name}</bdi>
          </Link>
          {you ? <span className="you-badge">You</span> : null}
          {rankings ? <span className="player-tag">{entry.tag}</span> : null}
        </span>
        {/* A phone puts the clan on a smaller line under the name, as
                    on Rankings; wider screens give it the Clan column. */}
        <span className="player-sub">
          <span className="player-clan" data-label={rankings ? "Clan" : undefined}>
            {clanName(entry.clan)}
          </span>
          {mobileUpdate}
        </span>
        {/* Left to screen readers, to tell two players of one name apart. */}
        {!rankings ? <span className="sr-only"> {entry.tag}</span> : null}
      </th>
      <td className="clan-cell">
        {clanName(entry.clan)}
        {rankings ? (
          <span className="clan-tag" aria-hidden="true">
            {entry.tag}
          </span>
        ) : null}
      </td>
      {showsDays ? (
        <td className="recent-cell">
          <RecentDays entry={entry} />
        </td>
      ) : null}
      {showsToday ? (
        <td className="today-cell">
          <TodayChange entry={entry} />
        </td>
      ) : null}
      {beforeTrophies}
      <td className="trophy-cell" data-label="Trophies">
        <TrophyMark />
        <strong>{entry.trophies.toLocaleString()}</strong>
      </td>
    </>
  );
}
