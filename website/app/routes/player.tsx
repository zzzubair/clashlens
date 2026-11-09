import { legendDayDate, validDay } from "../lib/legend-log-data";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import {
  redirect,
  useFetcher,
  useLoaderData,
  useLocation,
  useRevalidator,
  useSearchParams,
  type LoaderFunctionArgs,
  type ShouldRevalidateFunctionArgs,
} from "react-router";
import { getTrackedLeaderboard } from "../services/leaderboard.server";
import {
  getPlayer,
  getPlayerSeason,
  getPlayerSeasons,
  getRefreshStatus,
} from "../services/players.server";

import { usePlayerLookupEvent } from "../components/Analytics";
import { BattleStatistics } from "../components/BattleStatistics";
import { ErrorNotice } from "../components/ErrorNotice";
import { TrophyMark } from "../components/LeaderboardShared";
import { DayBattles, LegendLog, LogBarsKey, TodaySlots } from "../components/LegendLog";
import { LinkedBattle } from "../components/LinkedBattle";
import { PastSeasons } from "../components/PastSeasons";
import { PlayerActions } from "../components/PlayerActions";
import { PlayerCrews } from "../components/PlayerCrews";
import { LiveRank, PlayerHeading, type LiveRankRead } from "../components/PlayerHeading";
import { LocalTimestamp, useCurrentTime, useServerTime } from "../components/Provenance";
import { RefreshProgress, refreshInProgress } from "../components/RefreshProgress";
import { SeasonMenu } from "../components/SeasonMenu";
import { useSeasonReread } from "../components/SeasonReread";
import { SeasonStory } from "../components/SeasonStory";
import { SeasonHeatMap, heatDays } from "../components/SeasonSummary";
import { SelectedSeason } from "../components/SelectedSeason";
import { TagSearchAgain } from "../components/TagSearchAgain";
import { TrophyLine } from "../components/TrophyLine";
import detailsStyles from "../details.css?url";
import { pageMeta } from "../lib/blog";
import type {
  HistoricalSeasonSummary,
  PastSeasonFinish,
  PlayerLookup,
  PlayerPage,
  PlayerProfile,
  RefreshError,
  RefreshStatus,
  RefreshWork,
  SummarizedSeasonRef,
  WebsiteErrorResponse,
} from "../lib/contracts";
import type { PlayerCrew } from "../lib/crew-contracts";
import {
  dayEnd,
  dayReasons,
  explainsNoResults,
  legendDayKey,
  liveDayNotice,
  lookupExplanation,
  lookupMessage,
  newestPlayer,
  playerLookupView,
  savedDays,
  seasonForDay,
  selectPlayerHistory,
  showsCurrentSeason,
} from "../lib/player-lookup-text";
import {
  REFRESH_IDLE,
  automaticRefreshFor,
  lookupWaitTick,
  playerRefreshStep,
  pollAnswer,
  pollThrew,
  reloadedHere,
  refreshSettled as settledRefresh,
  shownRefreshStatus,
  type AutomaticRefresh,
  type LookupWait,
} from "../lib/player-refresh";
import { canonicalPlayerPath, normalizePlayerTag } from "../lib/player-tag";
import { gapLabel, inGap, logGaps, seasonDayDate } from "../lib/season-shape";
import type { LiveClan } from "../services/live-clan.server";
import "../player.css";
import "../player-hero.css";
import "../season-tiles.css";
import "../season-summary.css";
import "../legend-log.css";

export function links() {
  return [{ rel: "stylesheet", href: detailsStyles }];
}

export interface PlayerLoaderData {
  requestedTag: string | null;
  player: PlayerPage | null;
  error: WebsiteErrorResponse | null;
  refreshStatus: RefreshStatus | null;
  refreshError: WebsiteErrorResponse | null;
  noJsIdempotencyKey: string;
  seasons: SummarizedSeasonRef[];
  seasonsError?: WebsiteErrorResponse | null;
  selectedSeason: string | null;
  historical: HistoricalSeasonSummary | null;
  historicalError: WebsiteErrorResponse | null;
  lookup: PlayerLookup | null;
  lookupError: WebsiteErrorResponse | null;
  // Streams in after the page; null when the history response is unavailable.
  pastSeasons?: Promise<PastSeasonFinish[] | null>;
  // Streams in after the page; null when the game gave no answer.
  liveClan?: Promise<LiveClan | null>;
  // Null when the crews answer is unavailable or arrives after the other reads.
  crews?: PlayerCrew[] | null;
  // Streams in after the page: the player's place on the live Leaderboard,
  // null when the board doesn't hold them or can't be read.
  liveRank?: Promise<LiveRankRead | null>;
  // The site's public address, for absolute links in link previews.
  origin?: string;
}

export async function loader({
  request,
  params,
  context,
}: LoaderFunctionArgs): Promise<PlayerLoaderData> {
  const noJsIdempotencyKey = globalThis.crypto.randomUUID();
  const rawTag = params.tag ?? "";
  const normalizedTag = normalizePlayerTag(rawTag);
  if (normalizedTag === null) {
    return {
      requestedTag: null,
      player: null,
      error: {
        error: {
          code: "invalid_input",
          message: "The submitted player tag is not valid.",
        },
      },
      refreshStatus: null,
      refreshError: null,
      noJsIdempotencyKey,
      seasons: [],
      seasonsError: null,
      selectedSeason: null,
      historical: null,
      historicalError: null,
      lookup: null,
      lookupError: null,
    };
  }
  const canonicalPath = canonicalPlayerPath(normalizedTag);
  const url = new URL(request.url);
  // React Router keeps its single-fetch suffix in loader request URLs.
  if (url.pathname.replace(/\.data$/, "") !== canonicalPath) {
    throw redirect(`${canonicalPath}${url.search}`, {
      status: 301,
      headers: { "Cache-Control": "no-store" },
    });
  }

  const selectedSeason = readSeasonParam(url.searchParams.get("season"));
  const workId = url.searchParams.get("refresh");
  const validWorkId = workId && /^[A-Za-z0-9_-]{1,128}$/.test(workId);
  const lookupClient = import("../services/player-lookup.server");
  const pastSeasons = import("../services/past-seasons.server")
    .then((api) => api.getPastSeasons(normalizedTag))
    .catch(() => null);
  const liveClan = Promise.all([
    import("../services/live-clan.server"),
    import("../server/client-address.server"),
  ])
    .then(([api, { clientAddressContext }]) =>
      api.getLiveClan(context?.get(clientAddressContext), normalizedTag),
    )
    .catch(() => null);
  // The player's live place and their place on the last Reset's Daily board,
  // as Rankings shows them; nothing when the board can't be read.
  const liveRank = getTrackedLeaderboard(1, "live", 0, undefined, normalizedTag)
    .then((board) => {
      const entry = board.entries.find((row) => row.tag === normalizedTag);
      return entry ? { now: entry.rank, atReset: entry.resetRank } : null;
    })
    .catch(() => null);
  const crews = import("../services/crews.server")
    .then((api) => api.getPlayerCrews(normalizedTag))
    .catch(() => null);
  const origin = import("../server/blog.server").then(({ blogOrigin }) =>
    blogOrigin(request),
  );
  const [playerResult, seasonsResult, historicalResult, refreshResult, lookupResult] =
    await Promise.allSettled([
      getPlayer(normalizedTag),
      getPlayerSeasons(normalizedTag),
      selectedSeason === null
        ? Promise.resolve(null)
        : getPlayerSeason(normalizedTag, selectedSeason),
      validWorkId ? getRefreshStatus(workId, normalizedTag) : Promise.resolve(null),
      lookupClient.then(async (api) => {
        let lookup = await api.getPlayerLookup(normalizedTag);
        let error: WebsiteErrorResponse | null = null;
        if (
          lookup?.state === "unknown" ||
          (url.searchParams.has("retry") &&
            ["failed", "not_found"].includes(lookup?.state ?? ""))
        ) {
          try {
            const { clientAddressContext } =
              await import("../server/client-address.server");
            lookup = await api.startPlayerLookup(
              context?.get(clientAddressContext),
              normalizedTag,
            );
          } catch (cause) {
            error = await safeError(cause);
          }
        }
        return { lookup, error };
      }),
    ]);
  const lookup = lookupResult.status === "fulfilled" ? lookupResult.value.lookup : null;
  const lookupError =
    lookupResult.status === "fulfilled"
      ? lookupResult.value.error
      : await safeError(lookupResult.reason);
  if (url.searchParams.has("retry") && lookupError === null) {
    url.searchParams.delete("retry");
    throw redirect(`${canonicalPath}${url.search}`, {
      headers: { "Cache-Control": "no-store" },
    });
  }
  const player = playerResult.status === "fulfilled" ? playerResult.value : null;
  const error =
    playerResult.status === "rejected" ? await safeError(playerResult.reason) : null;
  const seasons = seasonsResult.status === "fulfilled" ? seasonsResult.value : [];
  const seasonsError =
    seasonsResult.status === "rejected" ? await safeError(seasonsResult.reason) : null;
  // A day link from an ended Season opens that Season at the day. The address
  // stays as linked: a redirect would drop a #battle- part the server never sees.
  const day = url.searchParams.get("day") ?? "";
  const daySeason =
    selectedSeason === null && /^\d{4}-\d{2}-\d{2}$/.test(day)
      ? seasonForDay(seasons, day)
      : null;
  const [seasonResult] =
    daySeason === null
      ? [historicalResult]
      : await Promise.allSettled([getPlayerSeason(normalizedTag, daySeason)]);
  const historical = seasonResult.status === "fulfilled" ? seasonResult.value : null;
  const historicalError =
    seasonResult.status === "rejected" ? await safeError(seasonResult.reason) : null;
  const refreshStatus = refreshResult.status === "fulfilled" ? refreshResult.value : null;
  const refreshError =
    workId && !validWorkId
      ? await safeError({ status: 400, payload: { error: "invalid_input" } })
      : refreshResult.status === "rejected"
        ? await safeError(refreshResult.reason)
        : null;
  return {
    requestedTag: normalizedTag,
    player,
    error,
    refreshStatus,
    refreshError,
    noJsIdempotencyKey,
    seasons,
    seasonsError,
    selectedSeason: selectedSeason ?? daySeason,
    historical,
    historicalError,
    lookup,
    lookupError,
    pastSeasons,
    liveClan,
    liveRank,
    crews: await Promise.race([crews, null]),
    origin: await origin,
  };
}

// Link previews name the player exactly as the served page does, and never
// show trophies or ranks, which go stale the moment the link is shared.
export function meta({ loaderData }: { loaderData?: PlayerLoaderData }) {
  const tag = loaderData?.requestedTag;
  if (!tag || !loaderData.origin) return [{ title: "Player not found · Clash Lens" }];
  const player = newestPlayer(loaderData.refreshStatus, loaderData);
  const { trackedPlayer, lookup } = playerLookupView(
    player?.tag === tag ? player : null,
    loaderData.lookup,
    explainsNoResults(loaderData.lookup),
  );
  const name = (trackedPlayer ?? lookup)?.profile?.name.trim();
  return pageMeta({
    title: name ? `${name} (${tag})` : `Player ${tag}`,
    description: "Legend League days, battles and Season history on Clash Lens.",
    url: `${loaderData.origin}${canonicalPlayerPath(tag)}`,
    origin: loaderData.origin,
    type: "website",
    image: "/images/og-clashlens.png",
    imageAlt: "The Clash Lens logo",
  });
}

function readSeasonParam(value: string | null): string | null {
  if (value === null || value.length === 0 || value.length > 128) return null;
  return value;
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

/** A refused Refresh can land during a Season switch; the switch still reads its Season. */
export function shouldRevalidate({
  currentUrl,
  nextUrl,
  defaultShouldRevalidate,
}: ShouldRevalidateFunctionArgs) {
  return currentUrl.search !== nextUrl.search || defaultShouldRevalidate;
}

// Effects never run during SSR. This client-document guard also prevents a
// later SPA visit/back navigation from replaying the original reload event.
let documentReload: "unread" | "read" = "unread";

export default function PlayerRoute() {
  const data = useLoaderData<typeof loader>();
  const location = useLocation();
  return <PlayerContent key={location.key} data={data} />;
}

function PlayerContent({ data }: { data: PlayerLoaderData }) {
  const [searchParams] = useSearchParams();
  const selectedDay = validDay(searchParams.get("day"));
  const refreshFetcher = useFetcher<RefreshWork | RefreshError | null>();
  const revalidator = useRevalidator();
  const [refresh, dispatchRefresh] = useReducer(playerRefreshStep, REFRESH_IDLE);
  const [lookupWait, setLookupWait] = useState<LookupWait>("waiting");
  const lookupStartedAt = useRef(Date.now());
  const automaticRefresh = useRef<AutomaticRefresh>("undecided");
  useEffect(() => {
    const status = data.refreshStatus;
    if (status && status.tag === data.requestedTag) {
      dispatchRefresh({ type: "loaderStatus", now: Date.now(), status });
    }
  }, [data.refreshStatus]);

  useEffect(() => {
    dispatchRefresh({
      type: "answered",
      now: Date.now(),
      answer: refreshFetcher.data,
      tag: data.requestedTag,
    });
  }, [refreshFetcher.data]);

  const { submit: submitRefreshForm, reset: resetRefresh } = refreshFetcher;
  const startRefresh = useCallback(
    (form: Record<string, string>, action: string) => {
      dispatchRefresh({ type: "submit", now: Date.now() });
      submitRefreshForm(form, { method: "post", action });
    },
    [submitRefreshForm],
  );

  const refreshFailure = refresh.phase === "stopped" ? refresh.failure : null;
  const refreshSettled = settledRefresh(refresh, refreshFetcher.state === "idle");
  const deadline = refresh.phase === "idle" ? null : refresh.deadline;
  const visibleStatus = shownRefreshStatus(
    refresh,
    data.refreshStatus?.tag === data.requestedTag ? data.refreshStatus : null,
  );
  const player = newestPlayer(visibleStatus, data);
  const explained = explainsNoResults(data.lookup);
  const [explainedVisit, setExplainedVisit] = useState(explained);
  if (explained && !explainedVisit) setExplainedVisit(true);
  const { trackedPlayer, lookup, minuteChecks, isChecking } = playerLookupView(
    player,
    data.lookup,
    explainedVisit,
  );
  usePlayerLookupEvent(data.requestedTag, trackedPlayer, lookup);
  const loadedAt = player?.calendar.as_of;
  const now = useServerTime(loadedAt);
  // Python worked the page's facts out for one Legend day, so the page reads
  // them again at its end, as its clock moved them on before; once the
  // trusted clock passes Season Reset they are withheld until that read lands.
  const seasonExpired = useSeasonReread(
    loadedAt,
    minuteChecks || !!trackedPlayer?.profile.seasonResetPending,
    data.selectedSeason === null,
    !!data.lookup && !["checking", "tracking"].includes(data.lookup.state),
    player?.calendar.day_end,
    player?.calendar.season_end,
  );
  const statisticsTime = now;
  const seasonMenu = (currentAvailable: boolean) =>
    data.requestedTag && (
      <SeasonMenu
        tag={data.requestedTag}
        seasons={data.seasons}
        selectedSeason={data.selectedSeason}
        currentAvailable={currentAvailable}
      />
    );
  const allHistory = seasonExpired ? [] : selectPlayerHistory(player);
  // A partial Season's log: one row for each stretch with no Legend I
  // battles, in place of its days.
  const shape = seasonExpired ? null : (player?.seasonShape ?? null);
  const shapeGaps = shape?.partial ? logGaps(shape) : [];
  const history =
    shape && shapeGaps.length > 0
      ? allHistory.filter(({ day }) => !inGap(shapeGaps, shape, day))
      : allHistory;
  const gaps = shape
    ? shapeGaps.map((gap) => ({
        start: gap.lastStart,
        label: gapLabel(shape, gap),
        kind: gap.kind,
      }))
    : [];
  const heatMap = shape ? (
    <SeasonHeatMap
      days={heatDays(history)}
      seasonStart={shape.seasonStart}
      today={shape.today}
    />
  ) : null;
  // A player now outside Legend I shows the total they left on.
  const leftOn =
    player && shape?.outsideNow && shape.lastLegendDay
      ? dayEnd(shape.lastLegendDay).trophies
      : null;
  const droppedRun = shape
    ? [...shape.runs].reverse().find((run) => run.dropped)
    : undefined;
  const todayEnded =
    player?.currentDay != null && Date.parse(player.currentDay.end ?? "") <= now;
  const today = todayEnded ? null : (player?.currentDay ?? null);
  // Today's day starts folded; an ended day with a real problem starts open.
  const openDay =
    selectedDay ??
    (player?.currentDay && todayEnded && !player.currentDay.presentation!.live_routine
      ? legendDayKey(player.currentDay.start)
      : null);
  // A linked day outside the current Season's log, such as an ended Season's,
  // shows its battles from the days the page already has.
  const pastDayKey =
    data.selectedSeason !== null ||
    !history.some(({ day }) => legendDayKey(day.start) === selectedDay)
      ? selectedDay
      : null;
  const pastDay = player
    ? savedDays(player).find((day) => legendDayKey(day.start) === pastDayKey)
    : undefined;
  const dayBattles =
    pastDayKey &&
    (pastDay ||
      data.historical?.dailyEntries.some(
        (day) => legendDayKey(day.start) === pastDayKey,
      )) ? (
      <DayBattles day={pastDayKey} battles={pastDay} units={player?.armyUnits ?? {}} />
    ) : null;
  const linkedBattle = (
    <LinkedBattle
      log={data.selectedSeason === null ? history.map(({ day }) => day) : []}
      day={pastDay}
    />
  );
  useEffect(() => {
    if (!isChecking || lookupWait === "exhausted") return;
    const timer = setInterval(() => {
      const tick = lookupWaitTick(
        lookupStartedAt.current,
        Date.now(),
        revalidator.state !== "idle",
      );
      if (tick === "exhaust") setLookupWait("exhausted");
      else if (tick === "read") revalidator.revalidate();
    }, 1000);
    return () => clearInterval(timer);
  }, [isChecking, lookupWait, revalidator]);
  const { revalidate } = revalidator;
  const refreshResourcePath = player
    ? `/resources/players/${encodeURIComponent(player.tag)}/refresh`
    : null;

  useEffect(() => {
    if (automaticRefresh.current === "decided") return;
    const navigation = performance.getEntriesByType?.("navigation")[0] as
      PerformanceNavigationTiming | undefined;
    const isDocumentReload =
      documentReload === "unread" && reloadedHere(navigation, window.location.pathname);
    documentReload = "read";
    if (trackedPlayer === null) return;
    // Decide once per visit, including when saved data is already recent.
    // Fetcher updates and revalidation must not spend another Refresh allowance.
    automaticRefresh.current = "decided";
    const send = automaticRefreshFor(
      trackedPlayer.profile.freshness.ageSeconds,
      isDocumentReload,
    );
    if (send === null) return;
    startRefresh(
      send.trigger === "reload"
        ? { idempotencyKey: data.noJsIdempotencyKey }
        : { idempotencyKey: data.noJsIdempotencyKey, trigger: "automatic" },
      `/resources/players/${encodeURIComponent(trackedPlayer.tag)}/refresh`,
    );
  }, [trackedPlayer, data.noJsIdempotencyKey, startRefresh]);

  useEffect(() => {
    if (deadline === null || refreshSettled) return;
    const timer = setTimeout(() => {
      resetRefresh();
      dispatchRefresh({ type: "timedOut" });
    }, deadline - Date.now());
    return () => clearTimeout(timer);
  }, [deadline, refreshSettled, resetRefresh]);

  const workId = refresh.phase === "polling" ? refresh.status.workId : null;
  useEffect(() => {
    if (!workId || refreshResourcePath === null || deadline === null) return;
    let cancelled = false;
    let inFlight = false;
    let controller: AbortController | null = null;
    const late = () => cancelled || Date.now() >= deadline;

    const poll = async () => {
      if (late() || inFlight) return;
      inFlight = true;
      controller = new AbortController();
      try {
        const response = await fetch(
          `${refreshResourcePath}?workId=${encodeURIComponent(workId)}`,
          {
            cache: "no-store",
            headers: { Accept: "application/json" },
            signal: controller.signal,
          },
        );
        const payload: unknown = await response.json();
        if (late()) return;
        dispatchRefresh(pollAnswer(response.ok, payload, workId, player?.tag));
      } catch (error) {
        const failed = late() ? null : pollThrew(error);
        if (failed) dispatchRefresh(failed);
      } finally {
        inFlight = false;
        controller = null;
      }
    };

    const timer = setInterval(() => void poll(), 500);
    void poll();
    return () => {
      cancelled = true;
      clearInterval(timer);
      controller?.abort();
    };
  }, [deadline, player?.tag, refreshResourcePath, workId]);

  const completedWorkId =
    refresh.phase !== "idle" && visibleStatus?.state === "complete"
      ? visibleStatus.workId
      : null;
  useEffect(() => {
    if (completedWorkId === null) return;
    const timers = [0, 3_000, 8_000].map((delay) =>
      setTimeout(() => void revalidate(), delay),
    );
    return () => timers.forEach(clearTimeout);
  }, [completedWorkId, revalidate]);

  if (trackedPlayer === null) {
    if (data.requestedTag === null) {
      return (
        <main id="main-content" tabIndex={-1} className="page-shell narrow-page">
          <h1>Player data unavailable</h1>
          {data.lookupError ? <ErrorNotice error={data.lookupError} /> : null}
          {data.error ? <ErrorNotice error={data.error} /> : null}
          <p>Check the tag and try again.</p>
        </main>
      );
    }
    const current = player && showsCurrentSeason(lookup, allHistory) ? player : null;
    return (
      <main id="main-content" tabIndex={-1} className="page-shell player-page">
        {lookup?.profile ? (
          <header className="player-hero">
            <PlayerHeading
              name={lookup.profile.name}
              tag={lookup.tag}
              clan={lookup.profile.clan}
              liveClan={data.liveClan}
            >
              <PlayerCrews crews={data.crews} name={lookup.profile.name} />
            </PlayerHeading>
            <div className="player-score">
              {current && leftOn !== null && shape && droppedRun ? (
                <div className="player-trophies">
                  <span className="metric-label">Last Legend I trophies</span>
                  <strong className="player-trophy-count">
                    <TrophyMark />
                    {leftOn.toLocaleString("en-GB")}
                  </strong>
                  <span className="player-reset-note">
                    Left Legend I at the {seasonDayDate(shape, droppedRun.last + 1)} Reset
                  </span>
                </div>
              ) : (
                <div className="player-trophies">
                  <span className="metric-label">Trophies</span>
                  <strong className="player-trophy-count">
                    <TrophyMark />
                    {lookup.profile.trophies.toLocaleString("en-GB")}
                  </strong>
                </div>
              )}
              <PlayerActions tag={data.requestedTag} />
            </div>
            {current && leftOn !== null && shape && droppedRun ? (
              <TrophyLine
                history={allHistory}
                player={current}
                now={statisticsTime}
                current={leftOn}
                until={shape.seasonStart + droppedRun.last * 86_400_000}
                endLabel="Left"
              />
            ) : null}
          </header>
        ) : (
          <header className="player-hero">
            <h1>{data.requestedTag}</h1>
            <p className="player-identity">
              <PlayerCrews crews={data.crews} name={data.requestedTag} />
            </p>
            <PlayerActions tag={data.requestedTag} />
          </header>
        )}
        {lookup ? (
          <LookupNotice lookup={lookup} timedOut={lookupWait === "exhausted"} />
        ) : (
          <p className="section-note">
            We could not confirm whether this player is currently tracked. Any saved
            history is still available.
          </p>
        )}
        {data.lookupError ? <ErrorNotice error={data.lookupError} /> : null}
        {/* The lookup notice already explains why an untracked player has no data. */}
        {data.error && !(lookup && data.error.error.code === "missing") ? (
          <ErrorNotice error={data.error} />
        ) : null}
        {linkedBattle}
        {data.seasonsError ? (
          <SeasonsError tag={data.requestedTag} selectedSeason={data.selectedSeason} />
        ) : null}
        {data.selectedSeason === null && current && shape ? (
          <SeasonStory shape={shape} />
        ) : null}
        {data.selectedSeason !== null ? (
          <SelectedSeason
            seasonId={data.selectedSeason}
            bounds={
              data.seasons.find((season) => season.seasonId === data.selectedSeason) ??
              null
            }
            listUnavailable={!!data.seasonsError}
            summary={data.historical}
            error={data.historicalError}
            menu={seasonMenu(current !== null)}
          />
        ) : current ? (
          <BattleStatistics
            player={current}
            now={statisticsTime}
            menu={seasonMenu(current !== null)}
            heatMap={heatMap}
          />
        ) : (
          seasonMenu(false)
        )}
        {dayBattles}
        {data.selectedSeason === null && player && allHistory.length > 0 ? (
          <section
            className="data-section legend-log open-board"
            aria-label="Saved Legend history"
          >
            <div className="log-title">
              <h2>Saved Legend history</h2>
              <LogBarsKey />
            </div>
            <LegendLog
              history={history}
              player={player}
              now={statisticsTime}
              today={today}
              openDay={openDay}
              gaps={gaps}
            />
          </section>
        ) : null}
        <PastSeasons finishes={data.pastSeasons} />
      </main>
    );
  }

  const visibleRefreshError = refreshFailure ?? data.refreshError;
  const refreshing = refreshInProgress(visibleStatus, visibleRefreshError !== null);
  const refreshActionPath = `/resources/players/${encodeURIComponent(trackedPlayer.tag)}/refresh`;

  return (
    <main id="main-content" tabIndex={-1} className="page-shell player-page">
      {/* A phone's long page: one row of links to its parts, kept under the
          site header. */}
      {data.selectedSeason === null ? <PlayerJumps /> : null}
      <header className="player-hero" id="player-now">
        <PlayerHeading
          name={trackedPlayer.profile.name}
          tag={trackedPlayer.tag}
          clan={trackedPlayer.profile.clan}
          liveClan={data.liveClan}
        >
          <PlayerCrews crews={data.crews} name={trackedPlayer.profile.name} />
        </PlayerHeading>
        <div className="player-score">
          <div className="player-trophies">
            <span className="metric-label sr-only">Current trophies</span>
            {seasonExpired || trackedPlayer.profile.seasonResetPending ? (
              <>
                <strong className="player-trophy-count player-reset-pending">
                  Waiting for this player&apos;s Season reset
                </strong>
                <span className="player-reset-note">
                  Last saved before the reset:{" "}
                  {trackedPlayer.profile.trophies.toLocaleString("en-GB")}
                </span>
              </>
            ) : (
              <strong className="player-trophy-count">
                {/* The Legend League badge while the profile and this week's
                    league both put the player there; otherwise a trophy. */}
                {trackedPlayer.profile.eligibility === "legend-i" &&
                !shape?.outsideNow ? (
                  <img
                    className="player-league-badge"
                    src="/images/legend-league.webp"
                    alt=""
                    width={151}
                    height={160}
                  />
                ) : (
                  <TrophyMark />
                )}
                {trackedPlayer.profile.trophies.toLocaleString("en-GB")}
              </strong>
            )}
          </div>
          {seasonExpired || trackedPlayer.profile.seasonResetPending ? null : (
            <LiveRank rank={data.liveRank} tag={trackedPlayer.tag} />
          )}
        </div>
        <PlayerFreshness profile={trackedPlayer.profile} />
        <PlayerActions tag={trackedPlayer.tag}>
          <refreshFetcher.Form
            className="player-refresh-form"
            action={refreshActionPath}
            method="post"
          >
            <input
              type="hidden"
              name="idempotencyKey"
              value={data.noJsIdempotencyKey}
              readOnly
            />
            <button
              type="submit"
              className="button secondary"
              disabled={refreshFetcher.state !== "idle" || refreshing}
              name="refresh"
              value="public"
              onClick={(event) => {
                event.preventDefault();
                startRefresh(
                  { idempotencyKey: data.noJsIdempotencyKey },
                  refreshActionPath,
                );
              }}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M20 12a8 8 0 1 1-2.34-5.66M20 4v5h-5" />
              </svg>
              <span className="player-refresh-label">
                {refreshFetcher.state === "submitting" || refreshing
                  ? "Refreshing…"
                  : "Refresh"}
              </span>
            </button>
          </refreshFetcher.Form>
        </PlayerActions>
        {today && !seasonExpired && !trackedPlayer.profile.seasonResetPending ? (
          <TodaySlots day={today} />
        ) : null}
        {seasonExpired || trackedPlayer.profile.seasonResetPending ? null : (
          <TrophyLine
            history={history}
            player={trackedPlayer}
            now={statisticsTime}
            current={trackedPlayer.profile.trophies}
          />
        )}
        {visibleStatus ? (
          <RefreshProgress status={visibleStatus} failed={visibleRefreshError !== null} />
        ) : null}
      </header>
      {visibleRefreshError ? <ErrorNotice error={visibleRefreshError} /> : null}
      {data.lookupError ? <ErrorNotice error={data.lookupError} /> : null}
      {trackedPlayer.profile.notFoundAt ? (
        <p role="status">
          Player not found. Clash of Clans did not find this tag at its latest check.
        </p>
      ) : null}
      {linkedBattle}
      {data.seasonsError ? (
        <SeasonsError tag={trackedPlayer.tag} selectedSeason={data.selectedSeason} />
      ) : null}
      {data.selectedSeason === null && shape ? <SeasonStory shape={shape} /> : null}
      {data.selectedSeason !== null ? (
        <SelectedSeason
          seasonId={data.selectedSeason}
          bounds={
            data.seasons.find((season) => season.seasonId === data.selectedSeason) ?? null
          }
          listUnavailable={!!data.seasonsError}
          summary={data.historical}
          error={data.historicalError}
          menu={seasonMenu(true)}
        />
      ) : seasonExpired ? (
        <>
          {seasonMenu(true)}
          <p className="section-note">No finished Legend days yet this Season</p>
        </>
      ) : (
        <BattleStatistics
          player={trackedPlayer}
          now={statisticsTime}
          menu={seasonMenu(true)}
          heatMap={heatMap}
        />
      )}
      {dayBattles}
      {data.selectedSeason !== null ? null : (
        <section
          className="data-section legend-log open-board"
          aria-labelledby="season-days-title"
        >
          <div className="log-title">
            <h2 id="season-days-title">Daily Legend log</h2>
            {allHistory.length > 0 ? <LogBarsKey /> : null}
          </div>
          {/* Each shown only while a day in the log carries its dot. */}
          <p className="log-key" aria-hidden="true">
            <span className="day-mark log-key-dot" /> Not yet confirmed by a reading from
            the game. Open the day for why.
          </p>
          <p className="log-key log-key-gap" aria-hidden="true">
            <span className="day-mark day-mark-gap log-key-dot" /> Uncertain: open the day
            for why.
          </p>
          {selectedDay &&
          !pastDay &&
          !history.some(({ day }) => legendDayKey(day.start) === selectedDay) ? (
            <p className="section-note" role="status">
              No saved Legend log for {legendDayDate(selectedDay)}.
            </p>
          ) : null}
          {trackedPlayer.dataQuality.map((warning) => {
            const day = trackedPlayer.currentDay;
            const notice =
              day &&
              warning.code === day.completeness.state &&
              warning.detail === day.completeness.reason
                ? liveDayNotice(day, todayEnded, warning.label)
                : undefined;
            if (notice === null) return null;
            const plain = notice
              ? undefined
              : QUALITY_WORDS[`${warning.code}/${warning.label}`];
            if (plain)
              return (
                <p className="section-note" key={`${warning.code}-${warning.label}`}>
                  {plain}
                </p>
              );
            return (
              <p className="section-note" key={`${warning.code}-${warning.label}`}>
                <strong>{notice?.heading ?? warning.label}:</strong>{" "}
                {notice
                  ? notice.text
                  : /^[a-z0-9_:]+(; [a-z0-9_:]+)*$/i.test(warning.detail)
                    ? dayReasons(warning.detail.split("; "), true).join(" ")
                    : warning.detail}
              </p>
            );
          })}
          {allHistory.length === 0 ? (
            <p className="section-note">No Legend days are saved for this player yet.</p>
          ) : null}
          <LegendLog
            history={history}
            player={trackedPlayer}
            now={statisticsTime}
            today={today}
            openDay={openDay}
            gaps={gaps}
          />
        </section>
      )}
      <PastSeasons finishes={data.pastSeasons} />
    </main>
  );
}

function LookupNotice({ lookup, timedOut }: { lookup: PlayerLookup; timedOut: boolean }) {
  const explanation =
    lookup.state === "tracking"
      ? lookupExplanation(lookup.reason, lookup.profile?.name)
      : null;
  return (
    <section aria-label="Player lookup" aria-live="polite">
      <p>
        {explanation ??
          (timedOut && (lookup.state === "checking" || lookup.state === "tracking")
            ? "The check is taking longer than expected. It may still be running."
            : lookupMessage(lookup))}
      </p>
      {explanation ? (
        <a href={canonicalPlayerPath(lookup.tag)}>Check again</a>
      ) : lookup.state === "checking" || lookup.state === "tracking" ? (
        <a href={canonicalPlayerPath(lookup.tag)}>Check progress</a>
      ) : lookup.state === "not_found" ? (
        <TagSearchAgain tag={lookup.tag} />
      ) : lookup.state === "failed" || lookup.state === "unknown" ? (
        <a href={`${canonicalPlayerPath(lookup.tag)}?retry=1`}>Try again</a>
      ) : null}
    </section>
  );
}

function SeasonsError({
  tag,
  selectedSeason,
}: {
  tag: string;
  selectedSeason: string | null;
}) {
  return (
    <aside className="notice notice-unavailable" role="alert">
      <strong>Season history could not be loaded.</strong>{" "}
      <a
        href={`${canonicalPlayerPath(tag)}${selectedSeason === null ? "" : `?season=${encodeURIComponent(selectedSeason)}`}`}
      >
        Try again
      </a>
    </aside>
  );
}

const FRESHNESS_LIMIT_SECONDS = 15 * 60;

// Plain words for the notices a Clasher can't act on, by code and label: the
// API also sends "unavailable" for a player the game no longer finds.
const QUALITY_WORDS: Record<string, string> = {
  "unavailable/Missing current ranked-day data": "Today's Legend day isn't saved yet.",
  "stale/Stale saved profile":
    "This profile hasn't been confirmed recently, so it may be behind.",
};

// When the server read this profile, so the page's clock starts from it.
function profileLoadedAt(profile: PlayerProfile): string | undefined {
  const { observedAt, ageSeconds } = profile.freshness;
  const loadedAt = Date.parse(observedAt) + ageSeconds * 1000;
  return Number.isNaN(loadedAt) ? undefined : new Date(loadedAt).toISOString();
}

// "Updated 15 min ago" by Refresh, from the profile's last check. Tapping
// it shows the exact times of the profile and the battle history, so it
// works on a phone too; an old time also says its date.
function PlayerFreshness({ profile }: { profile: PlayerProfile }) {
  const { observedAt } = profile.freshness;
  const now = useCurrentTime(profileLoadedAt(profile));
  const age = (value: string) =>
    Math.max(0, Math.floor((now - Date.parse(value)) / 1000));
  const time = (value: string, className?: string) => (
    <LocalTimestamp
      className={className}
      value={value}
      timeOnly={age(value) <= FRESHNESS_LIMIT_SECONDS}
    />
  );
  return (
    <details className="player-freshness">
      <summary>
        {/* A phone says only "6 min ago", under the Refresh it belongs to. */}
        <span className="player-updated-word">Updated </span>
        <time className="player-updated" dateTime={observedAt}>
          {ago(age(observedAt))}
        </time>
      </summary>
      <p>
        <span>Profile {time(observedAt)}</span>
        <span>
          Battles{" "}
          {profile.battleHistoryUpdatedAt
            ? time(profile.battleHistoryUpdatedAt, "player-history-updated")
            : "not saved yet"}
        </span>
      </p>
    </details>
  );
}

function ago(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  return `${days.toLocaleString("en-GB")} day${days === 1 ? "" : "s"} ago`;
}

async function safeError(cause: unknown): Promise<WebsiteErrorResponse> {
  const { safeWebsiteError } = await import("../server/errors.server");
  return safeWebsiteError(cause);
}

const JUMPS = [
  ["player-now", "Today"],
  ["season-summary-title", "Season"],
  ["season-days-title", "Log"],
] as const;

/** The phone's jump links, with the part on screen lit: the last one whose
 * start has passed the top third of the screen. Today is lit before scripts
 * run, as the page opens at its top. */
function PlayerJumps() {
  const [current, setCurrent] = useState<string>(JUMPS[0][0]);
  useEffect(() => {
    let frame = 0;
    const update = () => {
      frame = 0;
      let next: string = JUMPS[0][0];
      for (const [id] of JUMPS) {
        const top = document.getElementById(id)?.getBoundingClientRect().top;
        if (top !== undefined && top <= window.innerHeight / 3) next = id;
      }
      setCurrent(next);
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(update);
    };
    update();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, []);
  return (
    <nav className="player-jumps" aria-label="On this page">
      <div className="leaderboard-view-switch">
        {JUMPS.map(([id, label]) => (
          <a
            key={id}
            className="button"
            href={`#${id}`}
            aria-current={current === id ? "location" : undefined}
          >
            {label}
          </a>
        ))}
      </div>
    </nav>
  );
}
