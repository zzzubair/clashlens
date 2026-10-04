import { data, redirect, useLoaderData, useLocation } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { formatAge } from "../components/Provenance";
import type { WebsiteErrorResponse } from "../lib/contracts";
import {
  COMPARISON_DAYS,
  type ComparedPlayer,
  type ComparisonDays,
  type DayResult,
  type GroupComparison,
  type MemberStatus,
} from "../lib/group-comparison";
import { canonicalPlayerPath } from "../lib/player-tag";
import { isCanonicalUuid } from "../lib/validation";
import type { Route } from "./+types/account.groups.$groupId";
import "../group-compare.css";

const NO_STORE = { "Cache-Control": "no-store" };
const SORTS = {
  trophies: "Trophies",
  net: "Trophies a day",
  attack: "Attack",
  defense: "Defense",
} as const;
type SortKey = keyof typeof SORTS;

export interface GroupCompareLoaderData {
  comparison: GroupComparison | null;
  days: ComparisonDays;
  sort: SortKey;
  notFound: boolean;
  tooLarge: string | null;
  error: WebsiteErrorResponse | null;
}

/**
 * GET /account/groups/:groupId — one private group side by side over the
 * same ended Legend days, read for the signed-in account only.
 */
export async function loader({ request, params }: Route.LoaderArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const url = new URL(request.url);
  const requestedDays = Number(url.searchParams.get("days") ?? "7");
  const days = COMPARISON_DAYS.find((value) => value === requestedDays) ?? 7;
  const requestedSort = url.searchParams.get("sort") ?? "trophies";
  const sort = (
    Object.keys(SORTS).includes(requestedSort) ? requestedSort : "trophies"
  ) as SortKey;
  const empty = {
    comparison: null,
    days,
    sort,
    notFound: false,
    tooLarge: null,
    error: null,
  };
  const groupId = params.groupId ?? "";
  if (!isCanonicalUuid(groupId)) {
    return data<GroupCompareLoaderData>(
      { ...empty, notFound: true },
      { status: 404, headers: NO_STORE },
    );
  }
  try {
    const { getGroupComparison } = await import("../services/group-comparison.server");
    const comparison = await getGroupComparison(identity, groupId, days);
    return data<GroupCompareLoaderData>({ ...empty, comparison }, { headers: NO_STORE });
  } catch (cause) {
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(cause)) {
      const { accountSetupPath } = await import("../server/return-path.server");
      throw redirect(accountSetupPath(url.pathname, url));
    }
    const failure = cause as { status?: number; payload?: unknown };
    const payload = (
      typeof failure.payload === "object" && failure.payload !== null
        ? failure.payload
        : {}
    ) as Record<string, unknown>;
    if (failure.status === 404 && payload.error === "group_not_found") {
      return data<GroupCompareLoaderData>(
        { ...empty, notFound: true },
        { status: 404, headers: NO_STORE },
      );
    }
    if (failure.status === 422 && payload.error === "group_too_large") {
      const detail = typeof payload.detail === "string" ? payload.detail : "";
      return data<GroupCompareLoaderData>(
        { ...empty, tooLarge: detail },
        { status: 422, headers: NO_STORE },
      );
    }
    const { safeWebsiteError } = await import("../server/errors.server");
    return data<GroupCompareLoaderData>(
      { ...empty, error: safeWebsiteError(cause) },
      { status: 503, headers: NO_STORE },
    );
  }
}

export function headers() {
  return NO_STORE;
}

export default function GroupCompareRoute() {
  const { comparison, days, sort, notFound, tooLarge, error } =
    useLoaderData<typeof loader>();
  const location = useLocation();
  if (comparison === null) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
        <p className="eyebrow">
          <a href="/account/groups">Private groups</a>
        </p>
        <h1>{notFound ? "Group not found" : "Comparison unavailable"}</h1>
        {notFound ? (
          <p>This group does not exist or belongs to another account.</p>
        ) : null}
        {tooLarge !== null ? (
          <p>
            Comparisons show up to 20 players at once ({tooLarge}). Edit the group or make
            a smaller one for the players you want side by side.
          </p>
        ) : null}
        {error ? <ErrorNotice error={error} /> : null}
        {error ? (
          <p>
            <a href={`${location.pathname}${location.search}`}>Try again</a>
          </p>
        ) : null}
      </main>
    );
  }
  const players = sortPlayers(comparison.players, sort);
  const waiting = comparison.players.filter((player) => player.status !== "tracking");
  const retiredDays = comparison.dayStarts.filter((_, index) =>
    comparison.players.some((player) => player.days[index].state === "retired"),
  ).length;
  const hasYou = comparison.players.some((player) => player.you);
  const scale = Math.max(
    1,
    ...comparison.players.flatMap((player) =>
      player.days.map((day) => Math.abs(day.net ?? 0)),
    ),
  );
  const query = (next: { days?: number; sort?: SortKey }) =>
    `?days=${next.days ?? days}&sort=${next.sort ?? sort}`;

  return (
    <main id="main-content" tabIndex={-1} className="page-shell compare-page">
      <section className="compare-head" aria-labelledby="compare-title">
        <p className="eyebrow">
          <a href="/account/groups">Private groups</a> ·{" "}
          <a href={`/account/groups#group-${comparison.groupId}`}>
            Add or remove players
          </a>
        </p>
        <h1 id="compare-title">{comparison.name}</h1>
        <p className="lede">
          Every player over the same {days} ended Legend days,{" "}
          {formatDay(comparison.dayStarts[0])} to{" "}
          {formatDay(comparison.dayStarts[comparison.dayStarts.length - 1])}. A Legend day
          runs from 05:00 to 05:00 UTC.
          {retiredDays > 0
            ? ` For ${retiredDays} of these days, history is no longer kept for some players; those days are marked and never counted as zero.`
            : null}
        </p>
        <div className="compare-controls">
          <nav aria-label="Days compared" className="leaderboard-view-switch">
            {COMPARISON_DAYS.map((value) => (
              <a
                key={value}
                className="button secondary"
                href={query({ days: value })}
                aria-current={value === days ? "page" : undefined}
              >
                {value} days
              </a>
            ))}
          </nav>
          <nav aria-label="Sort by" className="leaderboard-view-switch">
            {(Object.keys(SORTS) as SortKey[]).map((key) => (
              <a
                key={key}
                className="button secondary"
                href={query({ sort: key })}
                aria-current={key === sort ? "page" : undefined}
              >
                {SORTS[key]}
              </a>
            ))}
          </nav>
        </div>
      </section>

      {waiting.length > 0 ? (
        <p className="compare-notice" role="status">
          {waiting.length === 1 ? "1 player is" : `${waiting.length} players are`} not
          being tracked right now; the reason is under each name. Results already recorded
          for them are still shown, and nothing missing is filled in.
        </p>
      ) : null}
      {!hasYou ? (
        <p className="compare-notice">
          <a href="/account/verify-player">Link your own player</a> to see yourself next
          to this group.
        </p>
      ) : null}

      {comparison.players.length > 0 ? (
        <>
          <p className="section-note">
            Last {days} days adds up each player&apos;s trophy change on counted days
            only. Won vs lost adds up trophies won in attacks and lost in defenses across
            every battle recorded in these days, incomplete days included, so the two can
            differ.
          </p>

          <div className="compare-board">
            <table className="compare-table">
              <caption className="sr-only">
                {comparison.name}: {days}-day comparison sorted by {SORTS[sort]}
              </caption>
              <thead>
                <tr>
                  <th scope="col">Player</th>
                  <th scope="col">Trophies now</th>
                  <th scope="col">Today so far</th>
                  <th scope="col">Last {days} days</th>
                  <th scope="col">Won vs lost</th>
                  <th scope="col">Vs the group</th>
                  <th scope="col">Attack</th>
                  <th scope="col">Defense</th>
                </tr>
              </thead>
              <tbody>
                {players.map((player) => (
                  <PlayerRow key={player.tag} player={player} scale={scale} days={days} />
                ))}
              </tbody>
            </table>
          </div>

          <dl className="compare-key">
            <div>
              <dt>
                <span className="day-bar day-complete" aria-hidden="true" /> Counted
              </dt>
              <dd>
                A complete day. Bars go up for trophies won and down for trophies lost.
              </dd>
            </div>
            <div>
              <dt>
                <span className="day-bar day-correcting" aria-hidden="true" /> May still
                change
              </dt>
              <dd>
                The day that ended at the last Reset. It is counted, but battles reported
                late can still change it.
              </dd>
            </div>
            <div>
              <dt>
                <span className="day-bar day-partial" aria-hidden="true" /> Incomplete
              </dt>
              <dd>
                Some battles are missing. Shown, but left out of the Last {days} days
                total.
              </dd>
            </div>
            <div>
              <dt>
                <span className="day-bar day-missing" aria-hidden="true" /> No result
              </dt>
              <dd>Nothing recorded. Never counted as zero.</dd>
            </div>
            <div>
              <dt>
                <span className="day-bar day-retired" aria-hidden="true" /> History no
                longer kept
              </dt>
              <dd>
                The day belongs to a finished season whose daily detail has been cleaned
                up. Never counted as zero.
              </dd>
            </div>
          </dl>
          <p className="section-note">
            Attack and defense also use every battle recorded in these days; the number of
            battles is shown with each. Attack success is the average stars and average
            destruction per attack. Attacks a day count only counted days, out of the 8
            attacks a Legend day allows. Vs the group compares a player with each other
            group member on the days both have counted results, then averages across those
            members; your own players outside the group are never part of it.
          </p>
        </>
      ) : (
        <div className="empty-state compare-empty-group">
          <h2>No players in this group yet</h2>
          <p>
            <a href={`/account/groups#group-${comparison.groupId}`}>Add players</a> to
            compare them side by side.
          </p>
        </div>
      )}
    </main>
  );
}

function PlayerRow({
  player,
  scale,
  days,
}: {
  player: ComparedPlayer;
  scale: number;
  days: number;
}) {
  const { attack, defense } = player;
  return (
    <tr className={player.you ? "compare-you" : undefined}>
      <th scope="row" data-label="Player">
        <a className="compare-name" href={canonicalPlayerPath(player.tag)}>
          {player.name ?? player.tag}
        </a>
        <span className="compare-tag">
          {player.tag}
          {player.you ? <span className="compare-badge">You</span> : null}
          {player.you && !player.inGroup ? (
            <span className="compare-badge-note">not in this group</span>
          ) : null}
        </span>
        {player.status !== "tracking" ? (
          <span className="compare-status">{STATUS_LABELS[player.status]}</span>
        ) : null}
      </th>
      <td data-label="Trophies now">
        {player.seasonResetPending ? (
          <span className="compare-sub">Waiting for this player&apos;s Season reset</span>
        ) : player.trophies === null ? (
          <Empty />
        ) : (
          <>
            <strong className="compare-number">
              {player.trophies.toLocaleString("en")}
            </strong>
            {player.ageSeconds !== null ? (
              <span
                className={`compare-sub${player.freshness === "stale" ? " compare-stale" : ""}`}
              >
                {player.freshness === "stale" ? "Out of date: " : ""}
                {formatAge(player.ageSeconds)} ago
              </span>
            ) : null}
          </>
        )}
      </td>
      <td data-label="Today so far">
        {player.today === null ? (
          <Empty />
        ) : (
          <>
            {player.today.net === null ? (
              <span>Not yet proven</span>
            ) : (
              <Signed value={player.today.net} />
            )}
            {player.today.gained !== null && player.today.lost !== null ? (
              <span className="compare-sub">
                Recorded: +{player.today.gained} won · −{player.today.lost} lost
              </span>
            ) : null}
            {player.today.attacks !== null && player.today.defenses !== null ? (
              <span className="compare-sub">
                Recorded: {plural(player.today.attacks, "attack")},{" "}
                {plural(player.today.defenses, "defense")}
              </span>
            ) : null}
          </>
        )}
      </td>
      <td data-label={`Last ${days} days`}>
        {player.net === null ? <Empty /> : <Signed value={player.net} />}
        <span className="compare-sub">
          {player.countedDays} of {days} days counted
        </span>
        {player.netPerDay !== null ? (
          <span className="compare-sub">
            Own average {signed(player.netPerDay, 1)} a day
          </span>
        ) : null}
        <Trend days={player.days} scale={scale} />
      </td>
      <td data-label="Won vs lost">
        {attack.count + defense.count === 0 ? (
          <Empty label="No battles recorded" />
        ) : (
          <>
            <Signed value={attack.trophies - defense.trophies} />
            <span className="compare-sub">
              +{attack.trophies.toLocaleString("en")} won · −
              {defense.trophies.toLocaleString("en")} lost
            </span>
          </>
        )}
      </td>
      <td data-label="Vs the group">
        {player.vsGroup === null ? (
          <Empty />
        ) : (
          <Signed value={player.vsGroup} decimals={1} />
        )}
      </td>
      <td data-label="Attack">
        {attack.count === 0 ? (
          <Empty label="No attacks recorded" />
        ) : (
          <>
            <strong className="compare-number">
              {(attack.stars / attack.count).toFixed(2)}★
            </strong>
            <span className="compare-sub">
              {Math.round(attack.destruction / attack.count)}% ·{" "}
              {Math.round((attack.threeStars / attack.count) * 100)}% triples
            </span>
            <span className="compare-sub">{plural(attack.count, "attack")}</span>
          </>
        )}
        {player.countedDays > 0 ? (
          <span className="compare-sub">
            {(player.countedAttacks / player.countedDays).toFixed(1)} of 8 attacks a day
          </span>
        ) : null}
      </td>
      <td data-label="Defense">
        {defense.count === 0 ? (
          <Empty label="No defenses recorded" />
        ) : (
          <>
            <strong className="compare-number">
              {(defense.stars / defense.count).toFixed(2)}★
            </strong>
            <span className="compare-sub">
              {Math.round(defense.destruction / defense.count)}% lost · −
              {(defense.trophies / defense.count).toFixed(1)} each
            </span>
            <span className="compare-sub">
              {plural(defense.count, "defense")}:{" "}
              <span className="star-split">
                {defense.starCounts.map((count, stars) => (
                  <span
                    key={stars}
                    title={`${plural(count, "defense")} gave up ${plural(stars, "star")}`}
                  >
                    {stars}★ {count}
                    {stars < 3 ? <span className="sr-only">, </span> : null}
                  </span>
                ))}
              </span>
            </span>
          </>
        )}
      </td>
    </tr>
  );
}

function Trend({ days, scale }: { days: DayResult[]; scale: number }) {
  const summary = days
    .map(
      (day) =>
        `${formatDay(day.start)} ${DAY_LABELS[day.state]}${day.net === null ? "" : ` ${signed(day.net)}`}`,
    )
    .join("; ");
  return (
    <span className="trend" role="img" aria-label={`Daily results: ${summary}`}>
      {days.map((day) => {
        const direction =
          day.net === null || day.net === 0
            ? "trend-flat"
            : day.net > 0
              ? "trend-up"
              : "trend-down";
        const height =
          day.net === null || day.net === 0
            ? undefined
            : { height: `${Math.max(8, (Math.abs(day.net) / scale) * 50)}%` };
        return (
          <span
            key={day.start}
            className={`trend-day ${direction}`}
            title={`${formatDay(day.start)}: ${DAY_LABELS[day.state]}${day.net === null ? "" : `, ${signed(day.net)}`}`}
          >
            <span
              className={`day-bar day-${day.state === "uncertain" ? "partial" : day.state}`}
              style={height}
            />
          </span>
        );
      })}
    </span>
  );
}

function Signed({ value, decimals = 0 }: { value: number; decimals?: number }) {
  const tone = value > 0 ? "compare-up" : value < 0 ? "compare-down" : "";
  return <strong className={`compare-number ${tone}`}>{signed(value, decimals)}</strong>;
}

function Empty({ label = "No result" }: { label?: string }) {
  return <span className="compare-empty">{label}</span>;
}

const STATUS_LABELS: Record<MemberStatus, string> = {
  tracking: "",
  checking: "Looking up this player…",
  unknown: "Not looked up yet",
  not_found: "Tag not found in Clash of Clans",
  not_in_legend: "Not in Legend League",
  uncertain: "Not confirmed in Legend League",
  failed: "Lookup failed; open the player to retry",
};

const DAY_LABELS: Record<DayResult["state"], string> = {
  complete: "counted",
  correcting: "may still change",
  partial: "incomplete",
  uncertain: "incomplete",
  missing: "no result",
  retired: "history no longer kept",
};

function sortPlayers(players: ComparedPlayer[], sort: SortKey): ComparedPlayer[] {
  const value = (player: ComparedPlayer): number | null => {
    if (sort === "trophies") return player.trophies;
    if (sort === "net") return player.netPerDay;
    if (sort === "attack") {
      return player.attack.count === 0 ? null : player.attack.stars / player.attack.count;
    }
    // Fewer stars given up is the better defense.
    return player.defense.count === 0
      ? null
      : -player.defense.stars / player.defense.count;
  };
  return [...players].sort((left, right) => {
    const a = value(left);
    const b = value(right);
    if (a === null || b === null) {
      return a === b ? left.tag.localeCompare(right.tag) : a === null ? 1 : -1;
    }
    return b - a || left.tag.localeCompare(right.tag);
  });
}

function signed(value: number, decimals = 0): string {
  const text = Math.abs(value).toLocaleString("en", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  return value > 0 ? `+${text}` : value < 0 ? `−${text}` : text;
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function formatDay(value: string | undefined): string {
  if (value === undefined) return "";
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  }).format(new Date(value));
}
