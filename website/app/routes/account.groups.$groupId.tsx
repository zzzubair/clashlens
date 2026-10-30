import { useEffect, useRef, useState } from "react";
import { data, Link, redirect, useLoaderData, useLocation } from "react-router";

import { DashboardIcon } from "../components/DashboardIcon";
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
import { leftLine } from "../lib/left-legend";
import { canonicalPlayerPath } from "../lib/player-tag";
import { expireSeasonTrophies, useSeasonEnded } from "../lib/season-end";
import { isCanonicalUuid } from "../lib/validation";
import { type BackHandle, useBackState } from "../components/BackLink";
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
/** What a phone row shows beside the name: there is room for only one of these. */
const METRICS = { change: "Change", attack: "Attack", defense: "Defense" } as const;
type Metric = keyof typeof METRICS;

export const handle: BackHandle = {
  back: { to: "/account/watchlists", label: "Your watchlists" },
};

export interface GroupCompareLoaderData {
  comparison: GroupComparison | null;
  days: ComparisonDays;
  sort: SortKey;
  notFound: boolean;
  tooLarge: string | null;
  error: WebsiteErrorResponse | null;
}

/**
 * GET /account/watchlists/:groupId — one private group side by side over the
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
  const seasonEnded = useSeasonEnded(comparison?.season ?? null);
  // A phone row shows the measure the list is sorted by until the Clasher picks
  // another; sorting again shows the new order's measure.
  const [picked, setPicked] = useState<{ sort: SortKey; metric: Metric } | null>(null);
  const metric: Metric =
    picked?.sort === sort
      ? picked.metric
      : sort === "attack" || sort === "defense"
        ? sort
        : "change";
  if (comparison === null) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
        <p className="eyebrow">
          <a href="/account/watchlists">Your watchlists</a>
        </p>
        <h1>{notFound ? "Watchlist not found" : "Comparison unavailable"}</h1>
        {notFound ? (
          <p>This watchlist does not exist or belongs to another account.</p>
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
  const players = sortPlayers(
    seasonEnded ? comparison.players.map(expireSeasonTrophies) : comparison.players,
    sort,
  );
  const waiting = comparison.players.filter((player) => player.status !== "tracking");
  const retiredDays = comparison.dayStarts.filter((_, index) =>
    comparison.players.some((player) => player.days[index].state === "retired"),
  ).length;
  const hasYou = comparison.players.some((player) => player.you);
  // Your own players outside the list are explained once, under the board.
  const outside = comparison.players.filter((player) => player.you && !player.inGroup);
  // Every player's bars share this one scale, so equal heights mean equal trophies.
  // The zero line sits where the board's biggest gain and biggest loss both fit,
  // with a little room below it even when nobody lost a day.
  const nets = comparison.players.flatMap((player) =>
    player.days.map((day) => day.net ?? 0),
  );
  const up = Math.max(1, ...nets);
  const scale: BarScale = {
    up,
    down: Math.max(up * 0.15, ...nets.map((net) => -net)),
    largest: Math.max(1, ...nets.map(Math.abs)),
  };
  // Switching views replaces this history entry, so the browser's Back skips past them.
  const query = (next: { days?: number; sort?: SortKey }) =>
    `?days=${next.days ?? days}&sort=${next.sort ?? sort}`;

  const showVsGroup = comparison.players.some((player) => player.vsGroup !== null);
  const firstDay = formatDay(comparison.dayStarts[0]);
  const lastDay = formatDay(comparison.dayStarts[comparison.dayStarts.length - 1]);
  const change = `${days}-day change`;
  // A heading sorts by its column; the arrow sits outside the link's name.
  // An aside shares the link's line, as the bars' scale does.
  const heading = (key: SortKey, label: React.ReactNode, aside?: React.ReactNode) => (
    <th
      scope="col"
      data-col={key}
      className={key === sort ? "compare-sorted" : undefined}
      aria-sort={
        key === sort ? (key === "defense" ? "ascending" : "descending") : undefined
      }
    >
      {aside === undefined ? (
        <Link to={query({ sort: key })} preventScrollReset replace>
          {label}
        </Link>
      ) : (
        <span className="trend-head">
          <Link to={query({ sort: key })} preventScrollReset replace>
            {label}
          </Link>
          {aside}
        </span>
      )}
    </th>
  );

  return (
    <main id="main-content" tabIndex={-1} className="page-shell compare-page">
      <section className="compare-head" aria-labelledby="compare-title">
        <div className="compare-title-row">
          <div>
            <h1 id="compare-title">{comparison.name}</h1>
            <p className="compare-span">
              {firstDay} – {lastDay} · {days} finished{" "}
              <span className="compare-wide">Legend </span>
              days
            </p>
          </div>
          <a
            className="button secondary"
            href={`/account/watchlists#group-${comparison.groupId}`}
          >
            Edit players
          </a>
        </div>
        {retiredDays > 0 ? (
          <p className="compare-span">
            For {retiredDays} of these days, history is no longer kept for some players;
            those days are marked and never counted as zero.
          </p>
        ) : null}
        <div className="compare-controls">
          <nav aria-label="Days compared" className="leaderboard-view-switch">
            {COMPARISON_DAYS.map((value) => (
              <Link
                key={value}
                className="button secondary"
                to={query({ days: value })}
                aria-current={value === days ? "page" : undefined}
                preventScrollReset
                replace
              >
                {value} days
              </Link>
            ))}
          </nav>
          <SortMenu key={sort} sort={sort} query={query} />
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
          to this watchlist.
        </p>
      ) : null}

      {comparison.players.length > 0 ? (
        <div className="compare-board" data-metric={metric}>
          {/* A phone has no heading row and room for one measure a row: this
              picks it for every row, and over the bars puts their scale and
              dates; the table says the same for screen readers. */}
          <div className="compare-key">
            <div className="compare-metric" role="group" aria-label="Show on each row">
              {(Object.keys(METRICS) as Metric[]).map((key) => (
                <button
                  key={key}
                  type="button"
                  aria-pressed={key === metric}
                  onClick={() => setPicked({ sort, metric: key })}
                >
                  {METRICS[key]}
                </button>
              ))}
            </div>
            {metric === "change" ? (
              <span className="trend-head" aria-hidden="true">
                <TrendKey scale={scale} firstDay={firstDay} lastDay={lastDay} />
              </span>
            ) : null}
          </div>
          <table className="compare-table">
            <caption className="sr-only">
              {comparison.name}: {days}-day comparison sorted by {SORTS[sort]}
            </caption>
            <thead>
              <tr>
                <th scope="col" data-col="player">
                  Player
                </th>
                {heading("trophies", "Trophies")}
                <th scope="col" data-col="today" className="compare-extra">
                  Today
                </th>
                {heading(
                  "net",
                  change,
                  // The total keeps this heading; the bars have their own name,
                  // and the shared scale is spelt out in full for screen readers.
                  <>
                    <TrendKey scale={scale} firstDay={firstDay} lastDay={lastDay} />
                    <span className="sr-only">
                      One bar a day from {firstDay} to {lastDay}, on the same scale for
                      every player: the tallest is {scale.largest.toLocaleString("en")}{" "}
                      trophies, and the dashed line is the best day, {signed(scale.up)}.
                    </span>
                  </>,
                )}
                {showVsGroup ? (
                  <th scope="col" data-col="vs" className="compare-extra">
                    Vs the watchlist
                  </th>
                ) : null}
                {heading("attack", "Attack")}
                {heading("defense", "Defense")}
                <th scope="col" data-col="toggle">
                  <span className="sr-only">Details</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {players.map((player) => (
                <PlayerRow
                  key={player.tag}
                  group={comparison.name}
                  player={player}
                  scale={scale}
                  days={days}
                  change={change}
                  sort={sort}
                  showVsGroup={showVsGroup}
                />
              ))}
            </tbody>
          </table>
          {outside.length > 0 ? (
            <p className="compare-foot">
              <span className="compare-badge" aria-hidden="true">
                You
              </span>{" "}
              {outside.length === comparison.players.filter((player) => player.you).length
                ? "Your own players, shown for comparison; they aren't in this watchlist."
                : `Your own players; ${outside.map((player) => player.name ?? player.tag).join(", ")} ${outside.length === 1 ? "is" : "are"} shown for comparison and not in this watchlist.`}
            </p>
          ) : null}
        </div>
      ) : (
        <div className="empty-state compare-empty-group">
          <h2>No players in this watchlist yet</h2>
          <p>
            <a href={`/account/watchlists#group-${comparison.groupId}`}>Add players</a> to
            compare them side by side.
          </p>
        </div>
      )}
    </main>
  );
}

/** "Sort: Trophies", opening every sort in a small list; it closes like a menu. */
function SortMenu({
  sort,
  query,
}: {
  sort: SortKey;
  query: (next: { sort: SortKey }) => string;
}) {
  const menu = useRef<HTMLDetailsElement>(null);
  const toggle = useRef<HTMLElement>(null);
  useEffect(() => {
    function handlePointerDown(event: PointerEvent) {
      if (menu.current?.open && !menu.current.contains(event.target as Node)) {
        menu.current.open = false;
      }
    }
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, []);
  return (
    <details
      ref={menu}
      className="compare-sort"
      onKeyDown={(event) => {
        if (event.currentTarget.open && event.key === "Escape") {
          event.preventDefault();
          event.currentTarget.open = false;
          toggle.current?.focus();
        }
      }}
      onBlur={(event) => {
        if (
          event.relatedTarget !== null &&
          !event.currentTarget.contains(event.relatedTarget)
        ) {
          event.currentTarget.open = false;
        }
      }}
    >
      <summary ref={toggle}>
        <span className="compare-sort-label">Sort:</span> {SORTS[sort]}
      </summary>
      <nav aria-label="Sort by">
        {(Object.keys(SORTS) as SortKey[]).map((key) => (
          <Link
            key={key}
            to={query({ sort: key })}
            aria-current={key === sort ? "page" : undefined}
            preventScrollReset
            replace
          >
            {SORTS[key]}
          </Link>
        ))}
      </nav>
    </details>
  );
}

function PlayerRow({
  group,
  player,
  scale,
  days,
  change,
  sort,
  showVsGroup,
}: {
  group: string;
  player: ComparedPlayer;
  scale: BarScale;
  days: number;
  change: string;
  sort: SortKey;
  showVsGroup: boolean;
}) {
  const { attack, defense } = player;
  const backState = useBackState(group);
  const [open, setOpen] = useState(false);
  const sorted = (key: SortKey) => (key === sort ? "compare-sorted" : undefined);
  const name = player.name ?? player.tag;
  const detailId = `compare-detail-${player.tag.slice(1)}`;
  const tripleShare = attack.count ? attack.threeStars / attack.count : 0;
  // Defenses that kept the third star: the defense side's answer to triples.
  const heldShare = defense.count ? 1 - defense.starCounts[3] / defense.count : 0;
  return (
    <>
      <tr
        className={`compare-row${player.you ? " compare-you" : ""}`}
        // The whole row opens its details for a pointer; keyboards use the button.
        onClick={(event) => {
          if (!(event.target as Element).closest("a, button")) setOpen(!open);
        }}
      >
        <th scope="row" data-label="Player">
          <span className="compare-who">
            <Link
              className="compare-name"
              to={canonicalPlayerPath(player.tag)}
              state={backState}
            >
              {name}
            </Link>
            {player.you ? (
              <span
                className="compare-badge"
                aria-hidden="true"
                title={
                  player.inGroup ? "Your player" : "Your player, not in this watchlist"
                }
              >
                You
              </span>
            ) : null}
          </span>
          {/* The name tells players apart on sight; the tag, and whose player
              this is, stay for screen readers. */}
          <span className="sr-only">
            {player.tag}
            {player.you
              ? player.inGroup
                ? ", you"
                : ", you, not in this watchlist"
              : ""}
          </span>
          {player.status !== "tracking" ? (
            <span className="compare-status">
              {player.leftLegendI
                ? leftLine(player.leftLegendI)
                : STATUS_LABELS[player.status]}
            </span>
          ) : null}
        </th>
        <td data-label="Trophies now" className={sorted("trophies")}>
          {player.seasonResetPending ? (
            <span className="compare-sub">
              Waiting
              <span className="compare-wide"> for this player&apos;s Season reset</span>
            </span>
          ) : player.trophies === null ? (
            <Empty />
          ) : (
            <>
              <strong
                className={`compare-number compare-trophies${player.freshness === "stale" ? " compare-trophies-stale" : ""}`}
              >
                {player.trophies.toLocaleString("en")}
              </strong>
              {player.ageSeconds !== null && player.freshness === "stale" ? (
                <span className="compare-sub compare-stale">
                  Out of date: {formatAge(player.ageSeconds)} ago
                </span>
              ) : null}
            </>
          )}
          {/* A phone drops the Today column, so today's result rides under trophies. */}
          {player.today?.net == null ? null : (
            <span className="compare-today-short">
              today{" "}
              <span className={tone(player.today.net)}>{signed(player.today.net)}</span>
            </span>
          )}
        </td>
        <td data-label="Today" className="compare-extra">
          {player.today === null ? (
            <Empty />
          ) : (
            <>
              {player.today.net === null ? (
                <span className="compare-sub">Not yet proven</span>
              ) : (
                <Signed value={player.today.net} />
              )}
              {player.today.attacks !== null && player.today.defenses !== null ? (
                <span
                  className="compare-battles"
                  aria-label={`${plural(player.today.attacks, "attack")} and ${plural(player.today.defenses, "defense")} recorded`}
                >
                  <DashboardIcon name="swords" />
                  {player.today.attacks}
                  <DashboardIcon name="shield" />
                  {player.today.defenses}
                </span>
              ) : null}
            </>
          )}
        </td>
        <td data-label={change} className={sorted("net")}>
          <span className="compare-change">
            <span className="compare-net">
              {player.net === null ? <Empty /> : <Signed value={player.net} />}
              {player.netPerDay !== null ? (
                <span className="compare-sub compare-sub-day">
                  {signed(player.netPerDay, 1)} a day
                </span>
              ) : null}
              {player.countedDays < days ? (
                <span className="compare-sub">
                  {player.countedDays} of {days} days counted
                </span>
              ) : null}
            </span>
            <Trend days={player.days} scale={scale} />
          </span>
        </td>
        {showVsGroup ? (
          <td data-label="Vs the watchlist" className="compare-extra">
            {player.vsGroup === null ? (
              <Empty />
            ) : (
              <Signed value={player.vsGroup} decimals={1} />
            )}
          </td>
        ) : null}
        <td data-label="Avg attack" className={sorted("attack")}>
          {attack.count === 0 ? (
            <Empty label="No attacks" />
          ) : (
            <Rate
              share={tripleShare}
              word="triples"
              average={attack.stars / attack.count}
              averageWords="stars an attack"
            />
          )}
        </td>
        <td data-label="Avg defense" className={sorted("defense")}>
          {defense.count === 0 ? (
            <Empty label="No defenses" />
          ) : (
            <Rate
              share={heldShare}
              word="held"
              average={defense.stars / defense.count}
              averageWords="stars given up a defense"
              allowed
            />
          )}
        </td>
        <td className="compare-toggle-cell">
          <button
            type="button"
            className="compare-toggle"
            aria-expanded={open}
            aria-controls={detailId}
            aria-label={`Details for ${name}`}
            title={open ? "Hide details" : "Show details"}
            onClick={() => setOpen(!open)}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="m6 9 6 6 6-6" />
            </svg>
          </button>
        </td>
      </tr>
      <tr id={detailId} className="compare-detail" hidden={!open}>
        <td colSpan={showVsGroup ? 8 : 7}>
          <div className="compare-detail-grid">
            <DayChart days={player.days} scale={scale} />
            <dl className="compare-facts">
              <div>
                <dt>Today</dt>
                {player.today === null ? (
                  <dd>No result</dd>
                ) : (
                  <>
                    <dd>
                      {player.today.net === null ? (
                        "Not yet proven"
                      ) : (
                        <Signed value={player.today.net} />
                      )}
                    </dd>
                    {player.today.gained !== null && player.today.lost !== null ? (
                      <dd>
                        +{player.today.gained} won · −{player.today.lost} lost
                      </dd>
                    ) : null}
                    {player.today.attacks !== null && player.today.defenses !== null ? (
                      <dd>
                        {plural(player.today.attacks, "attack")},{" "}
                        {plural(player.today.defenses, "defense")}
                      </dd>
                    ) : null}
                  </>
                )}
                {player.ageSeconds !== null ? (
                  <dd>Trophies read {formatAge(player.ageSeconds)} ago</dd>
                ) : null}
                {player.seasonResetPending ? (
                  <dd>Waiting for this player&apos;s Season reset</dd>
                ) : null}
              </div>
              <div>
                <dt>Last {days} days</dt>
                {player.netPerDay !== null ? (
                  <dd>{signed(player.netPerDay, 1)} a day</dd>
                ) : null}
                {attack.count + defense.count > 0 ? (
                  <dd>
                    +{attack.trophies.toLocaleString("en")} won · −
                    {defense.trophies.toLocaleString("en")} lost
                  </dd>
                ) : null}
                <dd>
                  {player.countedDays} of {days} days counted
                </dd>
                {player.vsGroup !== null ? (
                  <dd>{signed(player.vsGroup, 1)} vs the watchlist</dd>
                ) : null}
              </div>
              <div>
                <dt>Attack</dt>
                {attack.count === 0 ? (
                  <dd>No attacks</dd>
                ) : (
                  <>
                    <dd>
                      {Math.round(tripleShare * 100)}% triples ·{" "}
                      {Math.round(attack.destruction / attack.count)}% destruction
                    </dd>
                    <dd>{(attack.stars / attack.count).toFixed(2)} stars each</dd>
                    <dd>
                      {plural(attack.count, "attack")}
                      {player.countedDays > 0
                        ? ` · ${(player.countedAttacks / player.countedDays).toFixed(1)} of 8 a day`
                        : ""}
                    </dd>
                  </>
                )}
              </div>
              <div>
                <dt>Defense</dt>
                {defense.count === 0 ? (
                  <dd>No defenses</dd>
                ) : (
                  <>
                    <dd>
                      {Math.round(heldShare * 100)}% held · −
                      {(defense.trophies / defense.count).toFixed(1)} each
                    </dd>
                    <dd>
                      {(defense.stars / defense.count).toFixed(2)} stars given up each
                    </dd>
                    <dd>
                      {plural(defense.count, "defense")} ·{" "}
                      {Math.round(defense.destruction / defense.count)}% destruction
                    </dd>
                    <dd className="compare-star-split">
                      {defense.starCounts.map((count, stars) => (
                        <span key={stars}>
                          <span aria-hidden="true">
                            {stars}★ {count}
                          </span>
                          <span className="sr-only">
                            {plural(count, "defense")} gave up {plural(stars, "star")}
                            {stars < 3 ? ", " : ""}
                          </span>
                        </span>
                      ))}
                    </dd>
                  </>
                )}
              </div>
            </dl>
          </div>
        </td>
      </tr>
    </>
  );
}

/**
 * Attack or defense led by its success share (triples, or defenses held), the
 * one where higher is better on both sides, over a bar of that share; the
 * average stars follow as the smaller figure.
 */
function Rate({
  share,
  word,
  average,
  averageWords,
  allowed = false,
}: {
  share: number;
  word: string;
  average: number;
  averageWords: string;
  allowed?: boolean;
}) {
  return (
    <span className="compare-rate">
      <span className="compare-rate-head">
        <strong className="compare-number">{Math.round(share * 100)}%</strong>
        <span className="compare-rate-word">{word}</span>
      </span>
      <span className="meter" aria-hidden="true">
        <span style={{ "--value": `${share * 100}%` } as React.CSSProperties} />
      </span>
      <span className="compare-sub compare-rate-average">
        {average.toFixed(2)}
        <span
          className={`compare-star${allowed ? " compare-star-allowed" : ""}`}
          aria-hidden="true"
        >
          ★
        </span>
        <span className="sr-only"> {averageWords}</span>
        <span aria-hidden="true"> avg</span>
      </span>
    </span>
  );
}

/**
 * The bars' name, scale and dates, over their column: the dashed line at the
 * top of every player's bars is the board's best single day, and the bars run
 * from the first day on the left to the last on the right.
 */
function TrendKey({
  scale,
  firstDay,
  lastDay,
}: {
  scale: BarScale;
  firstDay: string;
  lastDay: string;
}) {
  return (
    <>
      <span className="trend-title" aria-hidden="true">
        Daily change
      </span>
      <span className="trend-scale" aria-hidden="true">
        <i className="trend-scale-line" />
        best day {signed(scale.up)}
      </span>
      <span className="trend-dates" aria-hidden="true">
        <span>{firstDay}</span>
        <span>{lastDay}</span>
      </span>
    </>
  );
}

/**
 * A player's days as small bars. Pointing at a bar, or stepping through them
 * with the arrow keys once the bars have focus, shows that day's date and result.
 */
function Trend({ days, scale }: { days: DayResult[]; scale: BarScale }) {
  const [active, setActive] = useState<number | null>(null);
  const summary = days
    .map(
      (day) =>
        `${formatDay(day.start)} ${DAY_LABELS[day.state]}${day.net === null ? "" : ` ${signed(day.net)}`}`,
    )
    .join("; ");
  const shown = active === null ? undefined : days[active];
  return (
    <span
      className="trend"
      role="img"
      aria-label={`Daily results: ${summary}`}
      tabIndex={0}
      style={zeroLine(scale)}
      onFocus={() => setActive((index) => index ?? days.length - 1)}
      onBlur={() => setActive(null)}
      onMouseLeave={() => setActive(null)}
      onKeyDown={(event) => {
        const step = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
        if (step === undefined) return;
        event.preventDefault();
        setActive((index) =>
          Math.min(days.length - 1, Math.max(0, (index ?? days.length - 1) + step)),
        );
      }}
    >
      {days.map((day, index) => (
        <span
          key={day.start}
          className={`trend-day ${direction(day)}${index === active ? " trend-active" : ""}`}
          onMouseEnter={() => setActive(index)}
        >
          <DayBar day={day} scale={scale} />
        </span>
      ))}
      {shown && active !== null ? (
        <span
          className="trend-readout"
          aria-hidden="true"
          data-edge={
            active === 0 ? "start" : active === days.length - 1 ? "end" : undefined
          }
          style={
            { "--at": `${((active + 0.5) / days.length) * 100}%` } as React.CSSProperties
          }
        >
          {formatDay(shown.start)}{" "}
          <strong className={tone(shown.net ?? 0)}>
            {shown.net === null ? DAY_LABELS[shown.state] : signed(shown.net)}
          </strong>
          {shown.net !== null && shown.state !== "complete"
            ? ` ${DAY_LABELS[shown.state]}`
            : null}
        </span>
      ) : null}
    </span>
  );
}

/** One player's days at full size: each bar with its exact result and date. */
function DayChart({ days, scale }: { days: DayResult[]; scale: BarScale }) {
  return (
    <ol
      className={`day-chart${days.length > 7 ? " day-chart-dense" : ""}`}
      aria-label="Each Legend day"
      style={zeroLine(scale)}
    >
      {days.map((day) => (
        <li key={day.start} className={direction(day)}>
          <span className="day-chart-value" aria-hidden="true">
            {day.net === null ? "–" : signed(day.net)}
          </span>
          <span className="trend-day" aria-hidden="true">
            <DayBar day={day} scale={scale} />
          </span>
          <span className="day-chart-date" aria-hidden="true">
            {new Date(day.start).getUTCDate()}
          </span>
          <span className="sr-only">
            {formatDay(day.start)}: {DAY_LABELS[day.state]}
            {day.net === null ? "" : `, ${signed(day.net)}`}
          </span>
        </li>
      ))}
    </ol>
  );
}

function direction(day: DayResult): string {
  if (day.net === null || day.net === 0) return "trend-flat";
  return day.net > 0 ? "trend-up" : "trend-down";
}

/** The board's one scale: its biggest gain and loss, and the largest of the two. */
interface BarScale {
  up: number;
  down: number;
  largest: number;
}

/** Where the zero line sits, from the top, so the biggest gain and loss both fit. */
function zeroLine(scale: BarScale): React.CSSProperties {
  return {
    "--zero": `${(scale.up / (scale.up + scale.down)) * 100}%`,
  } as React.CSSProperties;
}

/** A bar up or down from the zero line, its height on the shared scale. */
function DayBar({ day, scale }: { day: DayResult; scale: BarScale }) {
  const height =
    day.net === null || day.net === 0
      ? undefined
      : {
          height: `max(3px, ${(Math.abs(day.net) / (scale.up + scale.down)) * 100}%)`,
        };
  return (
    <span
      className={`day-bar day-${day.state === "uncertain" ? "partial" : day.state}`}
      style={height}
    />
  );
}

function Signed({ value, decimals = 0 }: { value: number; decimals?: number }) {
  return (
    <strong className={`compare-number ${tone(value)}`}>{signed(value, decimals)}</strong>
  );
}

function tone(value: number): string {
  return value > 0 ? "compare-up" : value < 0 ? "compare-down" : "";
}

/** A missing value; a phone's narrow column shows a dash and keeps the words for screen readers. */
function Empty({ label = "No result" }: { label?: string }) {
  return (
    <span className="compare-empty">
      <span className="compare-empty-dash" aria-hidden="true">
        –
      </span>
      <span className="compare-empty-text">{label}</span>
    </span>
  );
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
