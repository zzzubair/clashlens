import { Suspense } from "react";
import {
  Await,
  Link,
  data,
  redirect,
  useLoaderData,
  useLocation,
  useRouteLoaderData,
  useSearchParams,
} from "react-router";

import { useBackState } from "../components/BackLink";
import { Crest } from "../components/GameIcons";
import { TrophyMark } from "../components/LeaderboardShared";
import { ProviderMark } from "../components/ProviderMark";
import { SavedLine } from "../components/SavedLine";
import { HeatKey } from "../components/SeasonHeatGrid";
import { SeasonHeatMap, heatMapShown } from "../components/SeasonSummary";
import type { LinkedPlayerCard, PublicUser } from "../lib/account-contracts";
import { normalizeUsername } from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import type { SeasonDay } from "../lib/dashboard-cards";
import { LOOKUP_MESSAGES, lookupExplanation } from "../lib/player-lookup-text";
import { canonicalPlayerPath } from "../lib/player-tag";
import { logGaps, shapeOf } from "../lib/season-shape";
import type { PublicUserDays } from "../services/public-user-days.server";
import type { Route } from "./+types/users.$username";
import type { RootLoaderData } from "../root";

import "../user-profile.css";

const NO_STORE = { "Cache-Control": "no-store" };

export interface UserLoaderData {
  user: PublicUser | null;
  notFound: boolean;
  error: WebsiteErrorResponse | null;
  /** Only for the signed-in owner of this profile, null for everyone else. */
  owner: OwnerTools | null;
  /** Each linked player's rank at the last Reset and Season, by tag, from
   *  one read of every linked player, streamed in after their cards; none
   *  when it could not be read. */
  days?: Promise<Record<string, LinkedDay>>;
}

/** What a linked player's card draws from their player page. */
export interface LinkedDay {
  /** The place on the last Reset's Daily board. */
  resetRank: number | null;
  /** The Season heat map, as the player page's Season summary draws it. */
  season: { start: number; today: number; days: SeasonDay[] } | null;
}

/** The status line on each of the owner's tools; null when it could not be read. */
export interface OwnerTools {
  alerts: string | null;
  overlay: string | null;
}

/**
 * GET /users/:username — the public user page from the anonymous Python
 * client. Only the canonical username, display name, and verified player
 * links are shown; Google identity, groups, preferences, and
 * internal IDs never appear. The signed-in owner also gets the status of
 * their Discord alerts and Stream overlay.
 */
export async function loader({ params, request }: Route.LoaderArgs) {
  const rawUsername = params.username ?? "";
  const username = normalizeUsername(rawUsername);
  if (username === null) {
    return data<UserLoaderData>(
      { user: null, notFound: true, error: null, owner: null },
      { status: 404, headers: NO_STORE },
    );
  }
  if (username !== rawUsername) {
    throw redirect(`/users/${encodeURIComponent(username)}`);
  }
  try {
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient();
    const [user, owner] = await Promise.all([
      client.getPublicUser(username),
      ownerTools(request, username),
    ]);
    // Not awaited: the cards show their numbers at once, and their Reset
    // moves and heat maps follow.
    const days =
      user.verifiedPlayers.length === 0
        ? Promise.resolve({})
        : import("../services/public-user-days.server")
            .then((api) => api.getPublicUserDays(username))
            .then((read) => linkedDays(read, Date.now()))
            .catch(() => ({}));
    return data<UserLoaderData>(
      { user, notFound: false, error: null, owner, days },
      { headers: NO_STORE },
    );
  } catch (cause) {
    const { safeWebsiteError } = await import("../server/errors.server");
    const error = safeWebsiteError(cause);
    if (error.error.code === "missing") {
      return data<UserLoaderData>(
        { user: null, notFound: true, error: null, owner: null },
        { status: 404, headers: NO_STORE },
      );
    }
    return data<UserLoaderData>(
      { user: null, notFound: false, error, owner: null },
      { status: 422, headers: NO_STORE },
    );
  }
}

const DAY_MS = 86_400_000;

/**
 * Each linked player's rank at the last Reset and Season heat map, from one
 * read of every linked player's place and saved days this Season. The heat
 * map colours the days the player page's Season summary does: those its log
 * shows, without a partial Season's stretches outside Legend I.
 */
function linkedDays(read: PublicUserDays, now: number): Record<string, LinkedDay> {
  const seasonStart = Date.parse(read.seasonStart);
  const today = Math.min(28, Math.floor((now - seasonStart) / DAY_MS) + 1);
  return Object.fromEntries(
    read.players.map((player) => {
      const saved = new Map(player.days.map((day) => [day.day, day] as const));
      const shape = shapeOf(
        seasonStart,
        today,
        player.seasonWeeks,
        saved,
        (day) => day.unplayed,
      );
      const gaps = shape.partial ? logGaps(shape) : [];
      const days = player.days
        .filter(
          (kept) =>
            kept.listed &&
            !gaps.some((gap) => gap.first <= kept.day && kept.day <= gap.last),
        )
        .map((kept): SeasonDay => ({
          day: kept.day,
          attacks: kept.attacks,
          defenses: kept.defenses,
          offense: null,
          defense: null,
          net: kept.trophyChange,
          triples: null,
          held: null,
          endTrophies: null,
          rank: null,
          hits: null,
        }));
      return [
        player.tag,
        {
          resetRank: player.resetRank,
          season: days.length > 0 ? { start: seasonStart, today, days } : null,
        },
      ];
    }),
  );
}

/** The owner's tool statuses, or null when the visitor is anyone else. */
async function ownerTools(
  request: Request,
  username: string,
): Promise<OwnerTools | null> {
  try {
    const { getWebsiteConfig } = await import("../server/config.server");
    const actions = await import("../server/actions.server");
    const identity = await actions.readLoginIdentity(request, getWebsiteConfig());
    if (identity === null) return null;
    const { createPythonClient } = await import("../services/python.server");
    const account = await createPythonClient(identity).getAccount();
    if (account.username !== username) return null;
    const connected = account.providers.includes("discord");
    const { getAttackAlerts } = await import("../services/discord-alerts.server");
    const { getOverlay } = await import("../services/obs-overlay.server");
    const [alerts, overlay] = await Promise.all([
      connected ? getAttackAlerts(identity).catch(() => null) : null,
      getOverlay(identity).catch(() => null),
    ]);
    const on = alerts?.players.filter((player) => player.on).length;
    const total = alerts?.players.length ?? 0;
    const shown = overlay?.players.find((player) => player.tag === overlay.tag);
    return {
      alerts: !connected
        ? "Discord not connected"
        : alerts === null
          ? null
          : !on
            ? "Off"
            : total === 1
              ? "On"
              : `On for ${on} of ${total} players`,
      overlay:
        overlay === null
          ? null
          : overlay.tag === null
            ? "Not set up"
            : `Showing ${shown?.name ?? overlay.tag}`,
    };
  } catch {
    return null;
  }
}

export function headers() {
  return NO_STORE;
}

export default function UserRoute() {
  const data = useLoaderData<typeof loader>();
  const location = useLocation();
  const navigation = useRouteLoaderData<RootLoaderData>("root");
  const isOwnProfile = Boolean(
    data.user && navigation?.accountUsername === data.user.username,
  );
  // Set by a successful link on /account/verify-player.
  const [searchParams] = useSearchParams();
  const linkedTag = searchParams.get("linked");
  const justLinked = data.user?.verifiedPlayers.find(
    (player) => player.tag === linkedTag,
  );
  if (data.notFound) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell">
        <section className="hero" aria-labelledby="user-not-found-title">
          <h1 id="user-not-found-title">User not found</h1>
          <p>No Clash Lens user exists at this address.</p>
        </section>
      </main>
    );
  }
  if (data.error) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell">
        <h1>User profile</h1>
        <aside className="notice notice-unavailable" role="alert">
          <strong>Profile could not be loaded.</strong>{" "}
          <a href={location.pathname}>Try again</a>
        </aside>
      </main>
    );
  }
  const players = data.user?.verifiedPlayers ?? [];
  // The cards first with their numbers, then each one's Reset move and
  // heat map once the read of them arrives.
  const playerList = (days?: Record<string, LinkedDay>) => (
    <>
      <div className="profile-players-head">
        <h2 id="user-players-title">Clash players</h2>
        {/* One key for every card's Season heat map, while one draws. */}
        {players.some((player) => {
          const season = days?.[player.tag]?.season;
          return season ? heatMapShown(season.today) : false;
        }) ? (
          <HeatKey />
        ) : null}
      </div>
      <ul className="linked-player-cards">
        {players.map((player) => (
          <li key={player.tag}>
            <LinkedPlayer
              player={player}
              day={days?.[player.tag]}
              backLabel={isOwnProfile ? "Your profile" : data.user!.displayName}
            />
          </li>
        ))}
      </ul>
    </>
  );
  return (
    <main id="main-content" tabIndex={-1} className="page-shell">
      {justLinked ? (
        <SavedLine>{justLinked.name ?? justLinked.tag} linked.</SavedLine>
      ) : null}
      <section className="hero profile-hero" aria-labelledby="user-title">
        <div className="profile-id">
          {/* Their first player's Legend League badge, the dashboard's art,
              stands in for a picture; with no player in Legend League, a
              crest with their initial. The name beside it says who. */}
          <span className="profile-avatar">
            {players[0] && inLegend(players[0]) ? (
              <img src="/images/legend-league.webp" alt="" width={151} height={160} />
            ) : (
              <Crest name={data.user?.displayName ?? ""} />
            )}
          </span>
          <div>
            <div className="profile-name">
              <h1 id="user-title">{data.user?.displayName ?? "User"}</h1>
              {isOwnProfile ? (
                <Link
                  className="button button-secondary profile-edit"
                  to="/account/settings"
                  aria-label="Edit your profile in Settings"
                >
                  Edit
                </Link>
              ) : null}
            </div>
            <p className="player-tag">@{data.user?.username ?? ""}</p>
          </div>
        </div>
      </section>

      <div className="profile-body">
        <section className="data-section" aria-labelledby="user-players-title">
          {players.length === 0 ? (
            <div className="profile-players-head">
              <h2 id="user-players-title">Clash players</h2>
            </div>
          ) : data.days === undefined ? (
            playerList()
          ) : (
            <Suspense fallback={playerList()}>
              <Await resolve={data.days} errorElement={playerList()}>
                {(days) => playerList(days)}
              </Await>
            </Suspense>
          )}
          {players.length > 0 ? null : isOwnProfile ? (
            <div className="empty-state">
              <p>Link your Clash player to see your Legend days here.</p>
              <Link className="button button-primary" to="/account/verify-player">
                Link your Clash player
              </Link>
            </div>
          ) : (
            <div className="empty-state">
              <p>This user hasn&apos;t linked a Clash player yet.</p>
            </div>
          )}
        </section>
        {/* The owner's tools after their players, a quiet second area, so a
            phone opens on the players. */}
        {isOwnProfile ? <YourTools tools={data.owner} /> : null}
      </div>
    </main>
  );
}

/** A tool that is not working yet shows the step that starts it instead of its state. */
const NEXT_STEPS: Record<string, string> = {
  // The row says Discord already, so its step is one short word.
  "Discord not connected": "Connect",
  "Not set up": "Set up",
  Off: "Turn on",
};

/** The owner's two tools, one row each: its mark, name and purpose, then
 *  whether it is on or the step that starts it. */
function YourTools({ tools }: { tools: OwnerTools | null }) {
  const rows = [
    {
      to: "/account/discord-alerts",
      title: "Discord alerts",
      text: "A ping when you're attacked",
      status: tools?.alerts ?? null,
      mark: <ProviderMark provider="discord" />,
    },
    {
      to: "/account/obs-overlay",
      title: "Stream overlay",
      text: "Live stats on your stream",
      status: tools?.overlay ?? null,
      mark: (
        // A screen with a live dot, drawn here as no shared icon shows a stream.
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
          focusable="false"
        >
          <rect x="2.5" y="4" width="19" height="13" rx="2" />
          <path d="M8 21h8M12 17v4" />
          <circle cx="12" cy="10.5" r="2.2" fill="currentColor" stroke="none" />
        </svg>
      ),
    },
  ];
  return (
    <section
      className="data-section profile-tools-section"
      aria-labelledby="your-tools-title"
    >
      <h2 id="your-tools-title">Your tools</h2>
      <ul className="profile-tools">
        {rows.map((row) => (
          <li key={row.to}>
            <Link className="profile-tool" to={row.to}>
              <span className="profile-tool-mark">{row.mark}</span>
              <strong>{row.title}</strong>
              {/* What the tool does: one quiet line under the name, and
                  part of the link's name. */}
              <span className="profile-tool-text">{row.text}</span>
              {row.status === null ? null : NEXT_STEPS[row.status] ? (
                <span className="profile-tool-start button-secondary">
                  {NEXT_STEPS[row.status]}
                </span>
              ) : (
                <span
                  className="profile-tool-status"
                  data-on={/^(On|Showing)\b/.test(row.status) ? "" : undefined}
                >
                  {row.status}
                </span>
              )}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * A linked player: a link to their player page, named by the player's name
 * and tag, with their numbers now as its description, and their Season heat
 * map beside them. The whole card opens the player page, where today's
 * battles are.
 */
function LinkedPlayer({
  player,
  day,
  backLabel,
}: {
  player: LinkedPlayerCard;
  day?: LinkedDay;
  backLabel: string;
}) {
  const backState = useBackState(backLabel);
  const id = `linked-player-${player.tag.slice(1)}`;
  // The Legend League badge, as on the dashboard; otherwise a trophy.
  const legend = inLegend(player);
  const note =
    player.state === "tracking" && player.reason === null
      ? null
      : ((player.state === "tracking"
          ? lookupExplanation(player.reason, player.name)
          : null) ?? LOOKUP_MESSAGES[player.state]);
  const counts =
    player.today?.attacks != null && player.today.defenses !== null
      ? { attacks: player.today.attacks, defenses: player.today.defenses }
      : null;
  return (
    <article className="linked-player-card">
      <Link
        className="linked-player-link"
        to={canonicalPlayerPath(player.tag)}
        state={backState}
        aria-labelledby={`${id}-name ${id}-tag`}
        aria-describedby={`${player.clan ? `${id}-clan ` : ""}${id}-details${note === null ? "" : ` ${id}-note`}`}
      >
        <span className="linked-player-identity">
          <strong className="linked-player-name" id={`${id}-name`}>
            {player.name ?? player.tag}
          </strong>
          <span className="player-tag" id={`${id}-tag`}>
            {player.tag}
          </span>
          {player.clan ? (
            <span className="linked-player-clan" id={`${id}-clan`}>
              {player.clan}
            </span>
          ) : null}
        </span>
        <span className="linked-player-stats" id={`${id}-details`}>
          <span className="linked-player-stat">
            <small>Trophies</small>
            {player.trophies === null ? (
              <span className="linked-player-wait">
                {player.seasonResetPending
                  ? "Waiting for this player's Season reset"
                  : "Unknown"}
              </span>
            ) : (
              <strong className="linked-player-trophies">
                {legend ? (
                  <img
                    className="linked-player-badge"
                    src="/images/legend-league.webp"
                    alt=""
                    width={151}
                    height={160}
                  />
                ) : (
                  <TrophyMark />
                )}
                {player.trophies.toLocaleString("en-GB")}
              </strong>
            )}
          </span>
          <span className="linked-player-stat">
            <small>Rank</small>
            {player.rank === null ? (
              <strong className="linked-player-league">
                {player.league ?? "Unranked"}
              </strong>
            ) : (
              <span className="linked-player-rank">
                <strong
                  className="rank-mark"
                  data-podium-rank={player.rank <= 3 ? player.rank : undefined}
                >
                  {/* A podium plate shows the bare place, as on Rankings. */}
                  {player.rank <= 3
                    ? player.rank
                    : `#${player.rank.toLocaleString("en-GB")}`}
                </strong>
                <RankMove rank={player.rank} since={day?.resetRank ?? null} />
              </span>
            )}
          </span>
          <span className="linked-player-stat linked-player-today">
            <small>Today</small>
            {player.today === null ? (
              <span className="linked-player-wait">Not available yet</span>
            ) : (
              <span>
                <strong className={netTone(player.today.net)}>
                  {formatNet(player.today.net)}
                </strong>
                {player.today.net === null ? null : " so far"}
              </span>
            )}
          </span>
          {/* The battles today in words, for the link's description. */}
          {counts ? (
            <span className="sr-only">
              {counts.attacks}/8 attacks · {counts.defenses}/8 defenses
            </span>
          ) : null}
        </span>
        {note === null ? null : (
          <span className="linked-player-note" id={`${id}-note`}>
            {note}
          </span>
        )}
      </Link>
      {day?.season && heatMapShown(day.season.today) ? (
        <div className="linked-player-heat">
          <SeasonHeatMap
            days={day.season.days}
            seasonStart={day.season.start}
            today={day.season.today}
            still
          />
        </div>
      ) : null}
    </article>
  );
}

/** In Legend League, as the player's newest profile or a Leaderboard place says. */
function inLegend(player: LinkedPlayerCard): boolean {
  return player.league === null ? player.rank !== null : player.league === "Legend I";
}

/** Places moved since the last Reset's Daily board; nothing when unknown or none. */
function RankMove({ rank, since }: { rank: number; since: number | null }) {
  const moved = since === null ? 0 : since - rank;
  if (!moved) return null;
  const places = Math.abs(moved).toLocaleString("en-GB");
  return (
    <span className={moved > 0 ? "linked-player-gain" : "linked-player-loss"}>
      <span aria-hidden="true">
        {moved > 0 ? "▲" : "▼"}
        {places}
        <span className="linked-player-since"> since Reset</span>
      </span>
      <span className="sr-only">
        , {moved > 0 ? "up" : "down"} {places} since the last Reset
      </span>
    </span>
  );
}

function formatNet(value: number | null): string {
  if (value === null) return "Unknown";
  return value > 0 ? `+${value}` : String(value);
}

function netTone(value: number | null): string | undefined {
  if (value === null || value === 0) return undefined;
  return value > 0 ? "linked-player-gain" : "linked-player-loss";
}
