import { useCallback, useEffect, useRef, useState } from "react";
import {
  Link,
  redirect,
  useFetcher,
  useLoaderData,
  useLocation,
  useRevalidator,
  useSearchParams,
  type LoaderFunctionArgs,
  type ShouldRevalidateFunctionArgs,
} from "react-router";

import { usePlayerLookupEvent } from "../components/Analytics";
import { ArmyPortraits } from "../components/ArmyPortraits";
import { DayMark, DayStatusNote, provisional } from "../components/DayStatus";
import { BattleStatistics } from "../components/BattleStatistics";
import { ErrorNotice } from "../components/ErrorNotice";
import { FoldedBattles } from "../components/FoldedBattles";
import { LinkedBattle } from "../components/LinkedBattle";
import { PlayerActions } from "../components/PlayerActions";
import { PlayerCrews } from "../components/PlayerCrews";
import { PlayerHeading } from "../components/PlayerHeading";
import { RefreshProgress, refreshInProgress } from "../components/RefreshProgress";
import { nextSeasonReset, useSeasonReread } from "../components/SeasonReread";
import { PastSeasons } from "../components/PastSeasons";
import { SeasonMenu } from "../components/SeasonMenu";
import { TagSearchAgain } from "../components/TagSearchAgain";
import { SeasonSummary, per, type SummarySide } from "../components/SeasonSummary";
import {
  LocalTimestamp,
  formatAge,
  useCurrentTime,
  useServerTime,
} from "../components/Provenance";
import type { ArmyUnitNames } from "../lib/army";
import { pageMeta } from "../lib/blog";
import { copyArmyLink } from "../lib/game-links";
import { seasonDates } from "../lib/season-end";
import {
  dayEnd,
  dayEvidence,
  dayReasons,
  explainsNoResults,
  liveDay,
  legendDayKey,
  liveDayNotice,
  lookupExplanation,
  lookupMessage,
  newestPlayer,
  playerLookupView,
  presentDay,
  savedDays,
  seasonForDay,
  selectPlayerHistory,
  showsCurrentSeason,
} from "../lib/player-lookup-text";
import { canonicalPlayerPath, normalizePlayerTag } from "../lib/player-tag";
import type {
  HistoricalSeasonDayEntry,
  HistoricalSeasonSummary,
  PastSeasonFinish,
  PlayerPage,
  PlayerLookup,
  PlayerProfile,
  RankedBattleEvent,
  RankedDaySummary,
  RefreshError,
  RefreshStatus,
  RefreshWork,
  SummarizedSeasonRef,
  WebsiteErrorResponse,
} from "../lib/contracts";
import type { PlayerCrew } from "../lib/crew-contracts";
import type { LiveClan } from "../services/live-clan.server";
import { isRefreshStatusPayload, isWebsiteErrorResponse } from "../lib/validation";
import detailsStyles from "../details.css?url";

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
  const client = import("../services/python.server").then(({ createPythonClient }) =>
    createPythonClient(),
  );
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
  const crews = import("../services/crews.server")
    .then((api) => api.getPlayerCrews(normalizedTag))
    .catch(() => null);
  const origin = import("../server/blog.server").then(({ blogOrigin }) =>
    blogOrigin(request),
  );
  const [playerResult, seasonsResult, historicalResult, refreshResult, lookupResult] =
    await Promise.allSettled([
      client.then((api) => api.getPlayer(normalizedTag)),
      client.then((api) => api.getPlayerSeasons(normalizedTag)),
      selectedSeason === null
        ? Promise.resolve(null)
        : client.then((api) => api.getPlayerSeason(normalizedTag, selectedSeason)),
      validWorkId
        ? client.then((api) => api.getRefreshStatus(workId, normalizedTag))
        : Promise.resolve(null),
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
      : await Promise.allSettled([
          client.then((api) => api.getPlayerSeason(normalizedTag, daySeason)),
        ]);
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
let documentReloadHandled = false;

const REFRESH_TIMED_OUT: WebsiteErrorResponse = {
  error: {
    code: "unavailable",
    message:
      "Couldn't refresh within one minute, so the page stopped checking. Showing saved results.",
  },
};

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
  const [workId, setWorkId] = useState<string | null>(null);
  const [lastStatus, setLastStatus] = useState<RefreshStatus | RefreshWork | null>(null);
  const [refreshFailure, setRefreshFailure] = useState<WebsiteErrorResponse | null>(null);
  const [deadline, setDeadline] = useState<number | null>(null);
  const [lookupTimedOut, setLookupTimedOut] = useState(false);
  const lookupStartedAt = useRef(Date.now());
  const automaticRefreshHandled = useRef(false);
  useEffect(() => {
    const status = data.refreshStatus;
    if (status && status.tag === data.requestedTag) {
      setWorkId(status.workId);
      setLastStatus(status);
      setRefreshFailure(null);
      setDeadline((current) => current ?? Date.now() + 60_000);
    }
  }, [data.refreshStatus]);

  useEffect(() => {
    const result = refreshFetcher.data;
    if (deadline !== null && Date.now() >= deadline) {
      setRefreshFailure(REFRESH_TIMED_OUT);
    } else if (result && "error" in result) {
      setRefreshFailure(result);
    } else if (result && result.tag === data.requestedTag) {
      setWorkId(result.workId);
      setLastStatus(result);
      setRefreshFailure(null);
    }
  }, [refreshFetcher.data]);

  const { submit: submitRefreshForm, reset: resetRefresh } = refreshFetcher;
  const startRefresh = useCallback(
    (form: Record<string, string>, action: string) => {
      setWorkId(null);
      setLastStatus(null);
      setRefreshFailure(null);
      setDeadline(Date.now() + 60_000);
      submitRefreshForm(form, { method: "post", action });
    },
    [submitRefreshForm],
  );

  const terminalState =
    lastStatus?.state === "complete" ||
    lastStatus?.state === "failed" ||
    lastStatus?.state === "unavailable" ||
    refreshFailure !== null;
  const refreshSettled =
    terminalState || (workId === null && refreshFetcher.state === "idle");
  const visibleStatus =
    lastStatus ??
    (deadline === null && data.refreshStatus?.tag === data.requestedTag
      ? data.refreshStatus
      : null);
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
  const loadedAt = player ? profileLoadedAt(player.profile) : undefined;
  const now = useServerTime(loadedAt);
  const seasonExpired = useSeasonReread(
    loadedAt,
    minuteChecks || !!trackedPlayer?.profile.seasonResetPending,
    data.selectedSeason === null,
    !!data.lookup && !["checking", "tracking"].includes(data.lookup.state),
  );
  const statisticsTime =
    seasonExpired && loadedAt ? Math.max(now, nextSeasonReset(loadedAt)) : now;
  const seasonMenu = (currentAvailable: boolean) =>
    data.requestedTag && (
      <SeasonMenu
        tag={data.requestedTag}
        seasons={data.seasons}
        selectedSeason={data.selectedSeason}
        currentAvailable={currentAvailable}
        now={statisticsTime}
      />
    );
  const history = selectPlayerHistory(player, statisticsTime);
  const todayEnded =
    player?.currentDay != null &&
    Date.parse(player.currentDay.period.split(" – ")[1]) <= now;
  const today = todayEnded ? null : (player?.currentDay ?? null);
  // Today's day starts folded; an ended day with a real problem starts open.
  const openDay =
    selectedDay ??
    (player?.currentDay && todayEnded && !liveDay(dayEvidence(player.currentDay)).routine
      ? legendDayKey(player.currentDay.period)
      : null);
  // A linked day outside the current Season's log, such as an ended Season's,
  // shows its battles from the days the page already has.
  const pastDayKey =
    data.selectedSeason !== null ||
    !history.some(({ day }) => legendDayKey(day.period) === selectedDay)
      ? selectedDay
      : null;
  const pastDay = player
    ? savedDays(player).find((day) => legendDayKey(day.period) === pastDayKey)
    : undefined;
  const dayBattles =
    pastDayKey &&
    (pastDay ||
      data.historical?.dailyEntries.some(
        (day) => legendDayKey(day.period) === pastDayKey,
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
    if (!isChecking || lookupTimedOut) return;
    const timer = setInterval(() => {
      if (Date.now() - lookupStartedAt.current >= 60_000) setLookupTimedOut(true);
      else if (revalidator.state === "idle") revalidator.revalidate();
    }, 1000);
    return () => clearInterval(timer);
  }, [isChecking, lookupTimedOut, revalidator]);
  const { revalidate } = revalidator;
  const refreshResourcePath = player
    ? `/resources/players/${encodeURIComponent(player.tag)}/refresh`
    : null;

  useEffect(() => {
    if (automaticRefreshHandled.current) return;
    const navigation = performance.getEntriesByType?.("navigation")[0] as
      PerformanceNavigationTiming | undefined;
    const isDocumentReload =
      !documentReloadHandled &&
      navigation?.type === "reload" &&
      new URL(navigation.name).pathname === window.location.pathname;
    documentReloadHandled = true;
    if (trackedPlayer === null) return;
    // Decide once per visit, including when saved data is already recent.
    // Fetcher updates and revalidation must not spend another Refresh allowance.
    automaticRefreshHandled.current = true;
    if (!isDocumentReload && trackedPlayer.profile.freshness.ageSeconds <= 60) return;
    startRefresh(
      isDocumentReload
        ? { idempotencyKey: data.noJsIdempotencyKey }
        : { idempotencyKey: data.noJsIdempotencyKey, trigger: "automatic" },
      `/resources/players/${encodeURIComponent(trackedPlayer.tag)}/refresh`,
    );
  }, [trackedPlayer, data.noJsIdempotencyKey, startRefresh]);

  useEffect(() => {
    if (deadline === null || refreshSettled) return;
    const timer = setTimeout(() => {
      resetRefresh();
      setRefreshFailure(REFRESH_TIMED_OUT);
    }, deadline - Date.now());
    return () => clearTimeout(timer);
  }, [deadline, refreshSettled, resetRefresh]);

  useEffect(() => {
    if (!workId || terminalState || refreshResourcePath === null || deadline === null) {
      return;
    }
    let cancelled = false;
    let inFlight = false;
    let controller: AbortController | null = null;
    const late = () => cancelled || Date.now() >= deadline;
    const unavailableError: WebsiteErrorResponse = {
      error: {
        code: "unavailable",
        message: "Saved data is still available, but the live service is unavailable.",
      },
    };

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
        if (!response.ok || !isRefreshStatusPayload(payload)) {
          setRefreshFailure(isWebsiteErrorResponse(payload) ? payload : unavailableError);
          return;
        }
        if (payload.workId !== workId || payload.tag !== player?.tag) {
          setRefreshFailure({
            error: {
              code: "conflict",
              message: "The request conflicts with current saved data.",
            },
          });
          return;
        }
        setLastStatus(payload);
      } catch (error) {
        if (!late() && !(error instanceof DOMException && error.name === "AbortError")) {
          setRefreshFailure(unavailableError);
        }
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
  }, [deadline, player?.tag, refreshResourcePath, terminalState, workId]);

  const completedWorkId = lastStatus?.state === "complete" ? lastStatus.workId : null;
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
    const current = player && showsCurrentSeason(lookup, history) ? player : null;
    return (
      <main id="main-content" tabIndex={-1} className="page-shell player-page">
        {lookup?.profile ? (
          <header className="player-header">
            <div className="player-profile">
              <PlayerHeading
                name={lookup.profile.name}
                tag={lookup.tag}
                clan={lookup.profile.clan}
                liveClan={data.liveClan}
              />
              <PlayerCrews crews={data.crews} name={lookup.profile.name} />
            </div>
            <div className="player-summary">
              <div className="player-trophy-card">
                <div>
                  <span className="metric-label">Trophies</span>
                  <strong className="player-trophy-count">
                    <span className="trophy-mark" aria-hidden="true" />
                    {lookup.profile.trophies.toLocaleString("en-GB")}
                  </strong>
                </div>
              </div>
            </div>
          </header>
        ) : (
          <>
            <h1>{data.requestedTag}</h1>
            <PlayerCrews crews={data.crews} name={data.requestedTag} />
          </>
        )}
        <PlayerActions tag={data.requestedTag} />
        {lookup ? (
          <LookupNotice lookup={lookup} timedOut={lookupTimedOut} />
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
        {data.selectedSeason !== null ? (
          <SelectedSeason
            seasonId={data.selectedSeason}
            summary={data.historical}
            error={data.historicalError}
            menu={seasonMenu(current !== null)}
          />
        ) : current ? (
          <BattleStatistics
            player={current}
            now={statisticsTime}
            menu={seasonMenu(current !== null)}
          />
        ) : (
          seasonMenu(false)
        )}
        {dayBattles}
        {data.selectedSeason === null && player && history.length > 0 ? (
          <section className="data-section" aria-label="Saved Legend history">
            <h2>Saved Legend history</h2>
            {history.map(({ day, seasonDay }) => (
              <LegendDay
                key={legendDayKey(day.period)}
                day={day}
                player={player}
                now={statisticsTime}
                seasonDay={seasonDay}
                isCurrentDay={isCurrentDay(today, day)}
                openDay={openDay}
              />
            ))}
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
      <header className="player-header">
        <div className="player-profile">
          <PlayerHeading
            name={trackedPlayer.profile.name}
            tag={trackedPlayer.tag}
            clan={trackedPlayer.profile.clan}
            liveClan={data.liveClan}
          />
          <PlayerCrews crews={data.crews} name={trackedPlayer.profile.name} />
        </div>
        <div className="player-summary">
          <div className="player-trophy-card">
            <div>
              <span className="metric-label">Current trophies</span>
              {seasonExpired || trackedPlayer.profile.seasonResetPending ? (
                <>
                  <strong className="player-trophy-count player-reset-pending">
                    Waiting for this player&apos;s Season reset
                  </strong>
                  <span className="player-update-age">
                    Last saved before the reset:{" "}
                    {trackedPlayer.profile.trophies.toLocaleString("en-GB")}
                  </span>
                </>
              ) : (
                <strong className="player-trophy-count">
                  <span className="trophy-mark" aria-hidden="true" />
                  {trackedPlayer.profile.trophies.toLocaleString("en-GB")}
                </strong>
              )}
            </div>
            <PlayerFreshness profile={trackedPlayer.profile} />
          </div>
        </div>
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
              {refreshFetcher.state === "submitting" || refreshing
                ? "Refreshing…"
                : "Refresh"}
            </button>
          </refreshFetcher.Form>
          {visibleStatus ? (
            <RefreshProgress
              status={visibleStatus}
              failed={visibleRefreshError !== null}
            />
          ) : null}
        </PlayerActions>
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
      {data.selectedSeason !== null ? (
        <SelectedSeason
          seasonId={data.selectedSeason}
          summary={data.historical}
          error={data.historicalError}
          menu={seasonMenu(true)}
        />
      ) : (
        <BattleStatistics
          player={trackedPlayer}
          now={statisticsTime}
          menu={seasonMenu(true)}
        />
      )}
      {dayBattles}
      {data.selectedSeason !== null ? null : (
        <section className="data-section" aria-labelledby="season-days-title">
          <h2 id="season-days-title">Daily Legend log</h2>
          {selectedDay &&
          !pastDay &&
          !history.some(({ day }) => legendDayKey(day.period) === selectedDay) ? (
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
                ? liveDayNotice(dayEvidence(day), todayEnded, warning.label)
                : undefined;
            if (notice === null) return null;
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
          {history.length === 0 ? (
            <p className="section-note">No Legend days are saved for this player yet.</p>
          ) : null}
          <div className="legend-days">
            {history.map(({ day, seasonDay }) => (
              <LegendDay
                key={legendDayKey(day.period)}
                day={day}
                player={trackedPlayer}
                now={statisticsTime}
                seasonDay={seasonDay}
                isCurrentDay={isCurrentDay(today, day)}
                openDay={openDay}
              />
            ))}
          </div>
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

function SelectedSeason({
  seasonId,
  summary,
  error,
  menu,
}: {
  seasonId: string;
  summary: HistoricalSeasonSummary | null;
  error: WebsiteErrorResponse | null;
  menu: React.ReactNode;
}) {
  if (summary !== null) {
    return (
      <>
        <SeasonFinish summary={summary} menu={menu} />
        {summary.source === "tracked_summary" ? (
          <HistoricalSeasonPanel summary={summary} />
        ) : null}
      </>
    );
  }
  return (
    <section className="data-section" aria-labelledby="historical-title">
      <div className="section-heading">
        <h2 id="historical-title">{seasonDates(seasonId)}</h2>
        {menu}
      </div>
      {error ? <ErrorNotice error={error} /> : null}
      <p className="section-note">Results for {seasonDates(seasonId)} are unavailable.</p>
    </section>
  );
}

// A linked day's Attacks and Defenses, or a note when the page no longer has them.
function DayBattles({
  day,
  battles,
  units,
}: {
  day: string;
  battles: RankedDaySummary | undefined;
  units: ArmyUnitNames;
}) {
  return (
    <section className="data-section" aria-labelledby="day-battles-title">
      <h2 id="day-battles-title">Battles on {legendDayDate(day)}</h2>
      {battles ? (
        <div className="battle-columns">
          {(["Attacks", "Defenses"] as const).map((title) => (
            <BattleColumn
              key={title}
              title={title}
              events={title === "Attacks" ? battles.offenseEvents : battles.defenseEvents}
              day={day}
              dayLabel={legendDayDate(day)}
              units={units}
            />
          ))}
        </div>
      ) : (
        <p className="section-note">
          Attacks and defenses from this day are no longer shown here.
        </p>
      )}
    </section>
  );
}

function SeasonFinish({
  summary,
  menu,
}: {
  summary: HistoricalSeasonSummary;
  menu: React.ReactNode;
}) {
  const tracked = summary.source === "tracked_summary";
  const side = (
    count: number | null,
    stars: Record<string, number | null>,
    unknown: number | null,
    trophies: number | null,
    played: "attacks" | "defenses",
  ): SummarySide => ({
    count,
    stars: [0, 1, 2, 3].map((star) => stars[star] ?? null),
    unknown,
    // Days without a battle on this side, such as shielded days, don't count.
    perDay: per(
      trophies,
      summary.daysObserved -
        summary.dailyEntries.filter((day) => day[played] === 0).length,
    ),
    perBattle: per(trophies, count),
  });
  return (
    <SeasonSummary
      title={seasonDates(summary.seasonId)}
      controls={menu}
      {...(tracked &&
        summary.coverageState !== "complete" && {
          meta: `${summary.daysObserved} of 28 days recorded`,
        })}
      rank={["Final rank", finalRank(summary)]}
      finalRank
      trophies={[
        "Final trophies",
        formatCount(summary.officialHistory?.eodTrophies ?? summary.endTrophies),
      ]}
      {...(tracked && {
        attack: side(
          summary.attackCount,
          summary.attackStars,
          summary.attackStarsUnknown,
          summary.attackGain,
          "attacks",
        ),
        defense: side(
          summary.defenseCount,
          summary.defenseStars,
          summary.defenseStarsUnknown,
          summary.defenseLoss,
          "defenses",
        ),
      })}
    />
  );
}

function HistoricalSeasonPanel({ summary }: { summary: HistoricalSeasonSummary }) {
  const selectedDay = validDay(useSearchParams()[0].get("day"));
  const { hash } = useLocation();
  // A day link opens at its row unless the link names a place on the page.
  useEffect(() => {
    if (!hash) document.getElementById(`legend-day-${selectedDay}`)?.scrollIntoView();
  }, [hash, selectedDay]);
  return (
    <section className="data-section" aria-label="Daily trophy totals">
      {selectedDay &&
      !summary.dailyEntries.some((day) => legendDayKey(day.period) === selectedDay) ? (
        <p className="section-note" role="status">
          No saved Legend log for {legendDayDate(selectedDay)}.
        </p>
      ) : null}
      <div
        className="table-wrap top-space"
        tabIndex={0}
        role="region"
        aria-label="Daily trophy totals table"
      >
        <table className="data-table" aria-label="Daily trophy totals">
          <thead>
            <tr>
              <th scope="col">Day</th>
              <th scope="col">Start</th>
              <th scope="col">Attack</th>
              <th scope="col">Defense</th>
              <th scope="col">Trophy change</th>
              <th
                scope="col"
                title="Recorded attacks minus recorded defenses, without the automatic defense loss at Reset."
              >
                Recorded battle net
              </th>
              <th scope="col">End</th>
              <th scope="col">EOD change from previous day</th>
              <th scope="col">Reset rank</th>
              <th scope="col">Attacks recorded</th>
              <th scope="col">Defenses recorded</th>
              <th scope="col">Adjustment</th>
            </tr>
          </thead>
          <tbody>
            {summary.dailyEntries.map((day) => {
              const { status, reasons, battleNet } = presentDay(
                {
                  net: day.netChange,
                  state: day.state,
                  confidence: day.confidence,
                  coverage: day.coverage,
                  codes: day.flags,
                  attackGain: day.attackGain,
                  defenseLoss: day.defenseLoss,
                  attacks: day.attacks,
                  defenses: day.defenses,
                },
                false,
              );
              return (
                <tr
                  key={`${day.period}-${day.dayNumber ?? "unknown"}`}
                  {...(legendDayKey(day.period) === selectedDay && {
                    id: `legend-day-${selectedDay}`,
                    "aria-current": "date",
                  })}
                >
                  <td>
                    <details className="day-status">
                      <summary>
                        {day.dayNumber ?? "Unknown"}
                        <DayMark status={status} />
                      </summary>
                      <DayStatusNote status={status} reasons={reasons} />
                    </details>
                  </td>
                  <td>{formatCount(day.startTrophies)}</td>
                  <td>{formatSigned(day.attackGain)}</td>
                  <td>
                    {day.defenseLoss === null
                      ? "Unknown"
                      : formatSigned(-day.defenseLoss)}
                  </td>
                  <td>{formatSigned(day.netChange)}</td>
                  <td>{formatSigned(battleNet)}</td>
                  <td>{provisional(formatCount(day.endTrophies), day.eodState)}</td>
                  <td>{provisional(formatSigned(day.eodChange), day.eodChangeState)}</td>
                  <td>{formatCount(day.resetRank ?? null)}</td>
                  <td>{formatCount(day.attacks)}</td>
                  <td>{formatCount(day.defenses)}</td>
                  <td>{formatAdjustment(day)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

const playerDateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

function formatPlayerDate(date: Date): string {
  return playerDateFormatter.format(date).replace("Sept", "Sep");
}

function validDay(day: string | null): string | null {
  return day && /^\d{4}-\d{2}-\d{2}$/.test(day) && !Number.isNaN(Date.parse(day))
    ? day
    : null;
}

function legendDayDate(period: string): string {
  const date = new Date(period.split(" – ")[0]);
  return Number.isNaN(date.getTime()) ? "Date unavailable" : formatPlayerDate(date);
}

const FRESHNESS_LIMIT_SECONDS = 15 * 60;

// When the server read this profile, so the page's clock starts from it.
function profileLoadedAt(profile: PlayerProfile): string | undefined {
  const { observedAt, ageSeconds } = profile.freshness;
  const loadedAt = Date.parse(observedAt) + ageSeconds * 1000;
  return Number.isNaN(loadedAt) ? undefined : new Date(loadedAt).toISOString();
}

function PlayerFreshness({ profile }: { profile: PlayerProfile }) {
  const { observedAt } = profile.freshness;
  const now = useCurrentTime(profileLoadedAt(profile));
  const oldAge = (value: string) => {
    const age = Math.floor((now - Date.parse(value)) / 1000);
    return age > FRESHNESS_LIMIT_SECONDS ? ` · ${formatAge(age)} old` : null;
  };
  return (
    <>
      <p className="player-freshness">
        <span>Updated</span>{" "}
        <LocalTimestamp className="player-updated" value={observedAt} />
        {oldAge(observedAt)}
      </p>
      <p className="player-freshness">
        <span>Battle history updated</span>{" "}
        {profile.battleHistoryUpdatedAt ? (
          <>
            <LocalTimestamp
              className="player-history-updated"
              value={profile.battleHistoryUpdatedAt}
            />
            {oldAge(profile.battleHistoryUpdatedAt)}
          </>
        ) : (
          "not yet"
        )}
      </p>
    </>
  );
}

function isCurrentDay(today: RankedDaySummary | null, day: RankedDaySummary): boolean {
  return today != null && legendDayKey(today.period) === legendDayKey(day.period);
}

function LegendDay({
  day,
  player,
  now,
  seasonDay,
  isCurrentDay,
  openDay,
}: {
  day: RankedDaySummary;
  player: PlayerPage;
  now: number;
  seasonDay: string;
  isCurrentDay: boolean;
  openDay: string | null;
}) {
  const dayKey = legendDayKey(day.period);
  const dayLabel = legendDayDate(day.period);
  const shown = presentDay(dayEvidence(day), isCurrentDay);
  const end = dayEnd(day, player, now);
  const { status, reasons } = isCurrentDay ? shown : end;
  const { battleNet } = shown;
  return (
    <details className="legend-day" id={`legend-day-${dayKey}`} open={openDay === dayKey}>
      <summary>
        <span className="legend-day-date">
          <strong>{dayLabel}</strong>
          <span className="legend-day-meta">
            <small>{seasonDay}</small>
            {isCurrentDay ? <LiveBadge /> : <DayMark status={status} />}
          </span>
        </span>
        <span className="legend-day-stat legend-day-start">
          <small>Starting trophies</small>
          <strong
            className={day.startTrophies == null ? "stat-unavailable" : undefined}
            title={
              day.startTrophiesCalculation
                ? `${day.startTrophiesCalculation.trophies.toLocaleString("en-GB")} − (${formatSigned(day.startTrophiesCalculation.netChange)}) = ${day.startTrophies?.toLocaleString("en-GB")}`
                : undefined
            }
          >
            {day.startTrophies == null
              ? "Unavailable"
              : day.startTrophies.toLocaleString("en-GB")}
          </strong>
          {day.startTrophiesSource ? (
            <span
              className="legend-day-start-source"
              title={
                day.startTrophiesSource === "Season rule"
                  ? "Every Legend I player starts a Season on 5,000"
                  : day.startTrophiesCalculation
                    ? undefined
                    : "The previous day's calculated end. No reading from the game has confirmed it yet."
              }
            >
              {day.startTrophiesSource}
            </span>
          ) : null}
        </span>
        <span className="legend-day-stat legend-day-offense">
          <small>Attacks</small>
          <strong className={valueTone(day.offense.trophyGain)}>
            {formatSigned(day.offense.trophyGain)}
          </strong>
          <span>{recordedCount(day.offense.attacks)}</span>
        </span>
        <span className="legend-day-stat legend-day-defense">
          <small>Defenses</small>
          <strong
            className={valueTone(
              day.defense.trophyLoss === null ? null : -day.defense.trophyLoss,
            )}
          >
            {formatSigned(
              day.defense.trophyLoss === null ? null : -day.defense.trophyLoss,
            )}
          </strong>
          <span>{recordedCount(day.defense.defenses)}</span>
          {day.automaticDefenseLoss ? (
            <span title="Taken by the game at Reset for defenses not played">
              {formatSigned(-day.automaticDefenseLoss)} automatic loss
            </span>
          ) : null}
        </span>
        <span className="legend-day-stat legend-day-net">
          <small>Trophy change</small>
          {isCurrentDay && day.trophyChange === null && day.battlesComplete ? (
            <>
              <strong className={valueTone(battleNet)}>{formatSigned(battleNet)}</strong>
              <span>so far</span>
            </>
          ) : (
            <>
              <strong className={valueTone(day.trophyChange)}>
                {formatSigned(day.trophyChange)}
              </strong>
              {day.trophyChange === null && battleNet !== null ? (
                <span>{formatSigned(battleNet)} from battles</span>
              ) : null}
              {day.resetAdjustment ? (
                <span>
                  {formatSigned(day.resetAdjustment.amount)} {day.resetAdjustment.kind}{" "}
                  reset
                </span>
              ) : null}
            </>
          )}
        </span>
        <span className="legend-day-stat legend-day-end">
          <small>End of day</small>
          <strong className={end.trophies === null ? "stat-unavailable" : undefined}>
            {isCurrentDay
              ? "After Reset"
              : end.trophies === null
                ? "Unavailable"
                : end.trophies.toLocaleString("en-GB")}
            {isCurrentDay || end.trophies === null ? null : <DayMark status={status} />}
          </strong>
          {!isCurrentDay && end.conflict ? (
            <span className="legend-day-end-conflict">{end.conflict}</span>
          ) : null}
        </span>
        <span className="legend-day-stat legend-day-rank">
          <small>Reset rank</small>
          <strong className={day.resetRank == null ? "stat-unavailable" : undefined}>
            {isCurrentDay ? "After Reset" : formatCount(day.resetRank ?? null)}
          </strong>
        </span>
      </summary>
      {isCurrentDay ? (
        reasons.map((reason) => (
          <p className="section-note" key={reason}>
            {reason}
          </p>
        ))
      ) : (
        <DayStatusNote status={status} reasons={reasons} />
      )}
      <FoldedBattles open={openDay === dayKey} day={day}>
        <BattleColumn
          title="Attacks"
          events={day.offenseEvents}
          day={dayKey}
          dayLabel={dayLabel}
          units={player.armyUnits ?? {}}
          live={isCurrentDay}
        />
        <BattleColumn
          title="Defenses"
          events={day.defenseEvents}
          day={dayKey}
          dayLabel={dayLabel}
          units={player.armyUnits ?? {}}
          live={isCurrentDay}
        />
      </FoldedBattles>
    </details>
  );
}

function LiveBadge() {
  return (
    <span className="legend-day-live" aria-label="Today's Legend day, in progress">
      <span className="legend-live-dot" aria-hidden="true" />
      In progress
    </span>
  );
}

function BattleColumn({
  title,
  events,
  day,
  dayLabel,
  units,
  live = false,
}: {
  title: "Attacks" | "Defenses";
  events: RankedBattleEvent[];
  day: string;
  dayLabel: string;
  units: ArmyUnitNames;
  /** Today's empty slots are not recorded yet, not missing. */
  live?: boolean;
}) {
  // Each army stays one tap away, so the battle's result leads the card.
  const [shownArmies, setShownArmies] = useState<ReadonlySet<string>>(new Set());
  const toggleArmy = (battleId: string) =>
    setShownArmies((shown) => {
      const next = new Set(shown);
      if (!next.delete(battleId)) next.add(battleId);
      return next;
    });
  return (
    <section className="battle-column" aria-label={title}>
      <h3>{title}</h3>
      <ol className="battle-slots">
        {/* Show every saved battle; the game sometimes returns more than eight. */}
        {Array.from({ length: Math.max(8, events.length) }, (_, index) => {
          const event = events[index];
          return event ? (
            <li
              className={`battle-slot battle-slot-${title === "Attacks" ? "attack" : "defense"}`}
              id={`battle-${event.battleId}`}
              key={event.battleId}
            >
              <span className="battle-number" aria-hidden="true">
                {index + 1}
              </span>
              <div className="battle-opponent">
                <Link
                  className="battle-profile-link"
                  to={`${canonicalPlayerPath(event.opponent.tag)}?day=${day}#battle-${encodeURIComponent(event.battleId)}`}
                  aria-label={`View ${event.opponent.name ?? event.opponent.tag}'s Legend log for ${dayLabel}`}
                >
                  <strong>
                    <bdi>{event.opponent.name ?? event.opponent.tag}</bdi>
                  </strong>
                </Link>
                {event.opponent.name ? (
                  <span className="player-tag">{event.opponent.tag}</span>
                ) : null}
                <LocalTimestamp value={event.battleTimestamp} timeOnly />
              </div>
              <div
                className="battle-score"
                role="group"
                aria-label={`${event.stars} stars, ${event.destructionPercentage}% destruction, ${formatSigned(event.trophyChange)} trophies`}
              >
                <span aria-hidden="true">{event.stars}★</span>
                <span aria-hidden="true">{event.destructionPercentage}%</span>
                <strong className={valueTone(event.trophyChange)} aria-hidden="true">
                  {formatSigned(event.trophyChange)}
                </strong>
              </div>
              {event.perspectiveDisagreement ? (
                <span className="battle-disagreement">Result awaiting confirmation</span>
              ) : null}
              {event.armyShareCode ? (
                <span className="battle-actions">
                  <button
                    type="button"
                    className="battle-army"
                    aria-expanded={shownArmies.has(event.battleId)}
                    aria-controls={`army-${event.battleId}`}
                    onClick={() => toggleArmy(event.battleId)}
                  >
                    {shownArmies.has(event.battleId) ? "Hide army" : "Show army"}
                  </button>
                  <a
                    className="battle-army"
                    href={copyArmyLink(event.armyShareCode)}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={`Copy army from the battle against ${event.opponent.name ?? event.opponent.tag}, opens in a new tab`}
                  >
                    Copy army
                  </a>
                </span>
              ) : null}
              {event.armyShareCode && shownArmies.has(event.battleId) ? (
                <ArmyPortraits
                  id={`army-${event.battleId}`}
                  code={event.armyShareCode}
                  names={units}
                />
              ) : null}
            </li>
          ) : (
            <li
              aria-label={`${title.slice(0, -1)} ${index + 1} ${live ? "not recorded yet" : "not recorded"}`}
              className="battle-slot battle-slot-empty"
              key={`empty-${index}`}
            >
              <span className="battle-number" aria-hidden="true">
                {index + 1}
              </span>
              <span aria-hidden="true">{live ? "—" : "Not recorded"}</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function formatSigned(value: number | null): string {
  if (value === null) return "Unknown";
  return value > 0 ? `+${formatCount(value)}` : formatCount(value);
}

function valueTone(value: number | null): string {
  if (value === null || value === 0) return "score-neutral";
  return value > 0 ? "score-positive" : "score-negative";
}

// The official in-game placement, never Clash Lens's own leaderboard position.
function finalRank(summary: HistoricalSeasonSummary): string {
  const placement = summary.officialHistory?.finalPlacement ?? null;
  return placement === null ? "Not published yet" : `#${formatCount(placement)}`;
}

function formatCount(value: number | null): string {
  return value === null ? "Unknown" : (value || 0).toLocaleString("en-GB");
}

function recordedCount(value: number | null): string {
  return value === null ? "Count unknown" : `${formatCount(value)} recorded`;
}

function formatAdjustment(day: HistoricalSeasonDayEntry): string {
  if (!day.hasAdjustment) return "No";
  if (day.adjustmentTotal === null) return "Yes · unknown amount";
  return `Yes · ${formatSigned(day.adjustmentTotal)}`;
}

async function safeError(cause: unknown): Promise<WebsiteErrorResponse> {
  const { safeWebsiteError } = await import("../server/errors.server");
  return safeWebsiteError(cause);
}
