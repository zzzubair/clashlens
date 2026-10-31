import { useState } from "react";
import {
  data,
  Link,
  redirect,
  useLoaderData,
  useLocation,
  useNavigate,
} from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { GainLines, pointed, type Light } from "../components/GainLines";
import { ShieldIcon, Stars, SwordIcon } from "../components/GameIcons";
import { dayBar, dayLabel, dayRange, rowScale } from "../components/DayScale";
import { LastDays } from "../components/LastDays";
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
/** Each measure's own order: its heading sorts by it. */
const MEASURE_SORT = { change: "net", attack: "attack", defense: "defense" } as const;

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
  // The days compared are what the page is for, so it opens best gain first;
  // the Trophies heading still sorts by trophies now.
  const requestedSort = url.searchParams.get("sort") ?? "net";
  const sort = (
    Object.keys(SORTS).includes(requestedSort) ? requestedSort : "net"
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
  const navigate = useNavigate();
  // A phone row shows the measure the list is sorted by, so the order is always
  // the one on screen; sorted by trophies, it keeps the measure last picked.
  const [picked, setPicked] = useState<Metric>("change");
  // The player pointed at, or picked with a click or tap, lit up on the
  // chart and in the table; a pick stays until it is clicked again.
  const [hovered, setHovered] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const lit = hovered ?? pinned;
  const light: Light = {
    hover: setHovered,
    pin: (tag) => setPinned((current) => (current === tag ? null : tag)),
  };
  const metric: Metric = sort === "trophies" ? picked : sort === "net" ? "change" : sort;
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
  // Said by the title: who the list holds, and your own players shown beside it.
  const watched = comparison.players.filter((player) => player.inGroup).length;
  const yours = comparison.players.length - watched;
  // The best of each measure, the player first when sorted by it.
  const leads = {
    net: leaders(players, "net"),
    attack: leaders(players, "attack"),
    defense: leaders(players, "defense"),
  };
  // Switching views replaces this history entry, so the browser's Back skips past them.
  const query = (next: { days?: number; sort?: SortKey }) =>
    `?days=${next.days ?? days}&sort=${next.sort ?? sort}`;

  const showVsGroup = comparison.players.some((player) => player.vsGroup !== null);
  const change = `${days}-day gain`;
  // A heading sorts by its column; the arrow sits outside the link's name.
  // An aside shares the link's line: what a cell's two numbers are.
  const heading = (key: SortKey, label: React.ReactNode, aside?: React.ReactNode) => (
    <th
      scope="col"
      data-col={key}
      className={key === sort ? "compare-sorted" : undefined}
      aria-sort={key === sort ? "descending" : undefined}
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

  // A phone's two headings sort the same way; the sorted one carries the
  // arrow. Sorting by trophies keeps the measure on screen.
  const keySort = (key: SortKey, label: string, aside?: React.ReactNode) => (
    <Link
      className="compare-key-sort"
      to={query({ sort: key })}
      aria-current={key === sort ? "true" : undefined}
      onClick={() => setPicked(metric)}
      preventScrollReset
      replace
    >
      <span>{label}</span>
      {aside}
      {key === sort ? <span className="sr-only">, sorted</span> : null}
    </Link>
  );

  return (
    <main id="main-content" tabIndex={-1} className="page-shell compare-page">
      <section className="compare-head" aria-labelledby="compare-title">
        <div className="compare-title-row">
          <div>
            <h1 id="compare-title">{comparison.name}</h1>
            <p className="compare-span">
              {watched} watched{yours > 0 ? ` + ${yours} yours` : ""}
              {/* A phone's days menu already says how many. */}
              <span className="compare-span-days"> · {days} finished days</span>
              {/* The crown on each measure's best number, keyed once up here;
                  screen readers hear it on the number itself. */}
              {leads.net.size + leads.attack.size + leads.defense.size > 0 ? (
                <span className="compare-lead-key" aria-hidden="true">
                  {" · "}
                  <span className="compare-crown" />
                  best in this watchlist
                </span>
              ) : null}
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
        <>
          <GainLines
            lines={comparison.players.flatMap((player) =>
              player.net === null
                ? []
                : [
                    {
                      tag: player.tag,
                      name: player.name ?? player.tag,
                      you: player.you,
                      days: player.days,
                    },
                  ],
            )}
            dayStarts={comparison.dayStarts}
            lit={lit}
            light={light}
          />
          <div className="compare-board open-board" data-metric={metric}>
            {/* A phone has no heading row and room for one measure a row: the
              tabs pick it for every row, and the two headings under them sort
              by trophies or by that measure. */}
            <div className="compare-key">
              <div
                className="leaderboard-view-switch compare-metric"
                role="group"
                aria-label="Show on each row"
              >
                {(Object.keys(METRICS) as Metric[]).map((key) => (
                  <button
                    key={key}
                    type="button"
                    aria-pressed={key === metric}
                    onClick={() => {
                      setPicked(key);
                      // Picking a measure sorts by it, best first; the
                      // Trophies heading still sorts by trophies.
                      void navigate(query({ sort: MEASURE_SORT[key] }), {
                        replace: true,
                        preventScrollReset: true,
                      });
                    }}
                  >
                    {METRICS[key]}
                  </button>
                ))}
              </div>
              {/* A phone picks the days here, beside the measures, instead of a
                row of its own above the list. */}
              <label className="compare-days">
                <span className="sr-only">Days compared</span>
                <select
                  value={days}
                  onChange={(event) =>
                    void navigate(query({ days: Number(event.target.value) }), {
                      replace: true,
                      preventScrollReset: true,
                    })
                  }
                >
                  {COMPARISON_DAYS.map((value) => (
                    <option key={value} value={value}>
                      {value} days
                    </option>
                  ))}
                </select>
              </label>
              {keySort("trophies", "Trophies")}
              {keySort(
                MEASURE_SORT[metric],
                metric === "change" ? "Gain" : metric === "attack" ? "Triples" : "Held",
                metric === "change" ? undefined : (
                  <small className="compare-key-range">and avg ★</small>
                ),
              )}
            </div>
            <table className="compare-table">
              <caption className="sr-only">
                {comparison.name}: {days}-day comparison sorted by {SORTS[sort]}
              </caption>
              <thead>
                {/* Wide screens group the columns: where each player is now, and
                  the finished days compared. */}
                <tr className="compare-groups">
                  <td />
                  <th scope="colgroup" colSpan={2}>
                    <span>Now</span>
                  </th>
                  <th scope="colgroup" colSpan={showVsGroup ? 4 : 3}>
                    {/* The days compared, named once here, their dates behind the ⓘ. */}
                    <span>
                      <LastDays count={days} range={dayRange(comparison.dayStarts)} />
                    </span>
                  </th>
                  <td />
                </tr>
                <tr>
                  <th scope="col" data-col="player">
                    Player
                  </th>
                  {heading("trophies", "Trophies")}
                  <th scope="col" data-col="today" className="compare-extra">
                    Today
                  </th>
                  {/* The gain and its daily bars share one short heading. */}
                  {heading("net", "Gain")}
                  {showVsGroup ? (
                    <th scope="col" data-col="vs" className="compare-extra">
                      Vs the watchlist
                    </th>
                  ) : null}
                  {/* What each cell's two numbers are, said once here. */}
                  {heading(
                    "attack",
                    "Attack",
                    <small className="compare-head-sub">Triples · avg ★</small>,
                  )}
                  {heading(
                    "defense",
                    "Defense",
                    <small className="compare-head-sub">Held · avg ★</small>,
                  )}
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
                    days={days}
                    change={change}
                    sort={sort}
                    lead={{
                      net: leads.net.has(player.tag),
                      attack: leads.attack.has(player.tag),
                      defense: leads.defense.has(player.tag),
                    }}
                    showVsGroup={showVsGroup}
                    lit={player.tag === lit}
                    light={light}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </>
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

function PlayerRow({
  group,
  player,
  days,
  change,
  sort,
  lead,
  showVsGroup,
  lit,
  light,
}: {
  group: string;
  player: ComparedPlayer;
  days: number;
  change: string;
  sort: SortKey;
  /** Whether this player leads each measure, marked on its number. */
  lead: Record<"net" | "attack" | "defense", boolean>;
  showVsGroup: boolean;
  /** Lit up here and on the chart, from either. */
  lit: boolean;
  light: Light;
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
  // The bars' own scale: this player's biggest day fills the band.
  const scale = rowScale(player.days.map((day) => day.net));
  return (
    <>
      <tr
        className={`compare-row${player.you ? " compare-you" : ""}`}
        data-lit={lit || undefined}
        {...pointed(light, player.tag)}
        // The whole row opens its details for a pointer, and picks its line;
        // keyboards use the button.
        onClick={(event) => {
          light.pin(player.tag);
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
                className="you-badge"
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
          {/* A phone drops the Today column, so today's result rides under
              trophies, except while they wait for the Season reset. */}
          {player.seasonResetPending || player.today?.net == null ? null : (
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
                  <SwordIcon />
                  {player.today.attacks}
                  <ShieldIcon />
                  {player.today.defenses}
                </span>
              ) : null}
            </>
          )}
        </td>
        <td data-label={change} className={sorted("net")}>
          <span className="compare-change">
            <span className="compare-net">
              {player.net === null ? (
                <Empty />
              ) : (
                <Signed value={player.net} lead={lead.net} />
              )}
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
              shareWords="of attacks tripled"
              average={attack.stars / attack.count}
              averageWords="stars an attack"
              lead={lead.attack}
            />
          )}
        </td>
        <td data-label="Avg defense" className={sorted("defense")}>
          {defense.count === 0 ? (
            <Empty label="No defenses" />
          ) : (
            <Rate
              share={heldShare}
              shareWords="of defenses held"
              average={defense.stars / defense.count}
              averageWords="stars given up a defense"
              lead={lead.defense}
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
                            <Stars count={stars} /> {count}
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
 * average stars follow as the quieter figure. The heading names both once;
 * screen readers hear the words on each.
 */
function Rate({
  share,
  shareWords,
  average,
  averageWords,
  lead = false,
}: {
  share: number;
  shareWords: string;
  average: number;
  averageWords: string;
  lead?: boolean;
}) {
  return (
    <span className="compare-rate">
      <span className="compare-rate-head">
        <strong
          className={`compare-number${lead ? " compare-lead" : ""}`}
          title={lead ? LEAD_TITLE : undefined}
        >
          {`${Math.round(share * 100)}%`}
          <span className="sr-only">{` ${shareWords}`}</span>
          <LeadWords lead={lead} />
        </strong>
        <span className="compare-sub compare-rate-average">
          {average.toFixed(2)}
          <span aria-hidden="true">★</span>
          <span className="sr-only">{` ${averageWords}`}</span>
        </span>
      </span>
      <span className="meter" aria-hidden="true">
        <span style={{ "--value": `${share * 100}%` } as React.CSSProperties} />
      </span>
    </span>
  );
}

/**
 * A player's days as thin bars. Pointing at a bar, or stepping through them
 * with the arrow keys once the bars have focus, shows that day's date and result.
 */
function Trend({ days, scale }: { days: DayResult[]; scale: number }) {
  const [active, setActive] = useState<number | null>(null);
  const summary = days
    .map(
      (day) =>
        `${dayLabel(day.start)} ${DAY_LABELS[day.state]}${day.net === null ? "" : ` ${signed(day.net)}`}`,
    )
    .join("; ");
  const shown = active === null ? undefined : days[active];
  return (
    <span
      className="trend"
      role="img"
      aria-label={`Daily results: ${summary}`}
      tabIndex={0}
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
          {dayLabel(shown.start)}{" "}
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
function DayChart({ days, scale }: { days: DayResult[]; scale: number }) {
  return (
    <ol
      className={`day-chart${days.length > 7 ? " day-chart-dense" : ""}`}
      aria-label="Each Legend day"
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
            {dayLabel(day.start)}: {DAY_LABELS[day.state]}
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

/** A bar up or down from the zero line, its height on the player's own scale. */
function DayBar({ day, scale }: { day: DayResult; scale: number }) {
  return (
    <span
      className={`day-bar day-${day.state === "uncertain" ? "partial" : day.state}`}
      style={
        day.net === null || day.net === 0
          ? undefined
          : { height: `max(4px, ${dayBar(day.net, scale)})` }
      }
    />
  );
}

function Signed({
  value,
  decimals = 0,
  lead = false,
}: {
  value: number;
  decimals?: number;
  lead?: boolean;
}) {
  return (
    <strong
      className={`compare-number ${tone(value)}${lead ? " compare-lead" : ""}`}
      title={lead ? LEAD_TITLE : undefined}
    >
      {signed(value, decimals)}
      <LeadWords lead={lead} />
    </strong>
  );
}

const LEAD_TITLE = "The best in this watchlist";

/** The leading number's mark, said for screen readers. */
function LeadWords({ lead }: { lead: boolean }) {
  return lead ? <span className="sr-only">, the best in this watchlist</span> : null;
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

/**
 * What an order ranks by, bigger first: the number its column shows big (a
 * gain a day, so a missed day costs nothing; the share of triples; the share
 * of defenses held), then average stars to break a tie. Null has nothing to
 * rank.
 */
function rankBy(player: ComparedPlayer, sort: SortKey): [number, number] | null {
  const { attack, defense } = player;
  if (sort === "trophies") return player.trophies === null ? null : [player.trophies, 0];
  // The total gain shown big in its column, the number its crown sits on.
  if (sort === "net") return player.net === null ? null : [player.net, 0];
  if (sort === "attack") {
    return attack.count === 0
      ? null
      : [attack.threeStars / attack.count, attack.stars / attack.count];
  }
  // Fewer stars given up is the better defense.
  return defense.count === 0
    ? null
    : [1 - defense.starCounts[3] / defense.count, -defense.stars / defense.count];
}

function byRank(a: [number, number], b: [number, number]): number {
  return b[0] - a[0] || b[1] - a[1];
}

function sortPlayers(players: ComparedPlayer[], sort: SortKey): ComparedPlayer[] {
  return [...players].sort((left, right) => {
    const a = rankBy(left, sort);
    const b = rankBy(right, sort);
    if (a === null || b === null) {
      return a === b ? left.tag.localeCompare(right.tag) : a === null ? 1 : -1;
    }
    return byRank(a, b) || left.tag.localeCompare(right.tag);
  });
}

/** Who leads a measure, ties included: no one when fewer than two have a
 *  result or every one of them ties. */
function leaders(players: ComparedPlayer[], sort: SortKey): Set<string> {
  const ranked = players.flatMap((player) => {
    const rank = rankBy(player, sort);
    return rank === null ? [] : [{ tag: player.tag, rank }];
  });
  const best = ranked.reduce<[number, number] | null>(
    (top, entry) => (top === null || byRank(entry.rank, top) < 0 ? entry.rank : top),
    null,
  );
  const top = ranked.filter((entry) => best !== null && byRank(entry.rank, best) === 0);
  return new Set(top.length === ranked.length ? [] : top.map((entry) => entry.tag));
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
