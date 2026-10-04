import { useEffect, useRef, useState } from "react";
import {
  Link,
  redirect,
  useFetcher,
  useLoaderData,
  useLocation,
  useRevalidator,
  useSearchParams,
  type LoaderFunctionArgs,
} from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { PastSeasons } from "../components/PastSeasons";
import { formatAge, useCurrentTime, useServerTime } from "../components/Provenance";
import { pageMeta } from "../lib/blog";
import {
  LOOKUP_MESSAGES,
  dayEvidence,
  dayReasons,
  liveDay,
  liveDayNotice,
  lookupExplanation,
  presentDay,
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
import { isRefreshStatusPayload, isWebsiteErrorResponse } from "../lib/validation";

export interface PlayerLoaderData {
  requestedTag: string | null;
  player: PlayerPage | null;
  error: WebsiteErrorResponse | null;
  refreshStatus: RefreshStatus | null;
  refreshError: WebsiteErrorResponse | null;
  noJsIdempotencyKey: string;
  seasons: SummarizedSeasonRef[];
  selectedSeason: string | null;
  historical: HistoricalSeasonSummary | null;
  historicalError: WebsiteErrorResponse | null;
  lookup: PlayerLookup | null;
  lookupError: WebsiteErrorResponse | null;
  // Streams in after the page; null when ClashKing finishes are unavailable.
  pastSeasons?: Promise<PastSeasonFinish[] | null>;
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
  const historical =
    historicalResult.status === "fulfilled" ? historicalResult.value : null;
  const historicalError =
    historicalResult.status === "rejected"
      ? await safeError(historicalResult.reason)
      : null;
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
    selectedSeason,
    historical,
    historicalError,
    lookup,
    lookupError,
    pastSeasons,
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
    image: "/images/legend-league.webp",
    imageAlt: "The Legend League badge",
  });
}

function readSeasonParam(value: string | null): string | null {
  if (value === null || value.length === 0 || value.length > 128) return null;
  return value;
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

// The saved player, or a finished refresh's player when that is newer.
function newestPlayer(
  status: RefreshStatus | RefreshWork | null,
  data: PlayerLoaderData,
): PlayerPage | null {
  const refreshed =
    status && "player" in status && status.tag === data.requestedTag
      ? status.player
      : null;
  return refreshed &&
    (data.player === null ||
      Date.parse(refreshed.profile.freshness.observedAt) >
        Date.parse(data.player.profile.freshness.observedAt))
    ? refreshed
    : data.player;
}

// A newest profile we cannot use is explained, even over saved results.
function explainsNoResults(lookup: PlayerLookup | null): boolean {
  return lookup?.state === "tracking" && (lookup.reason ?? "pending") !== "pending";
}

// What a visit shows and how often it rereads saved data. A successful lookup
// alone decides the state; saved results show as current only when it says
// tracking too. Once explained, the visit rereads once a minute, through failed
// or partial reads, until a successful lookup gives a normal page or a final
// answer.
export function playerLookupView(
  player: PlayerPage | null,
  fetched: PlayerLookup | null,
  explainedVisit: boolean,
) {
  const current =
    fetched === null
      ? !explainedVisit
      : fetched.state === "tracking" && !explainsNoResults(fetched);
  const trackedPlayer = current && player?.trackingState === "tracking" ? player : null;
  const lookup: PlayerLookup | null =
    fetched ??
    (player && !explainedVisit ? { tag: player.tag, state: player.trackingState } : null);
  const minuteChecks =
    explainedVisit &&
    trackedPlayer === null &&
    (lookup === null || lookup.state === "checking" || lookup.state === "tracking");
  const isChecking =
    !explainedVisit &&
    (lookup?.state === "checking" ||
      (lookup?.state === "tracking" && trackedPlayer === null));
  return { trackedPlayer, lookup, minuteChecks, isChecking };
}

// Effects never run during SSR. This client-document guard also prevents a
// later SPA visit/back navigation from replaying the original reload event.
let documentReloadHandled = false;

export default function PlayerRoute() {
  const data = useLoaderData<typeof loader>();
  const location = useLocation();
  return <PlayerContent key={location.key} data={data} />;
}

function PlayerContent({ data }: { data: PlayerLoaderData }) {
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const requestedDay = searchParams.get("day");
  const selectedDay =
    requestedDay &&
    /^\d{4}-\d{2}-\d{2}$/.test(requestedDay) &&
    !Number.isNaN(Date.parse(requestedDay))
      ? requestedDay
      : null;
  const refreshFetcher = useFetcher<RefreshWork | RefreshError | null>();
  const revalidator = useRevalidator();
  const [workId, setWorkId] = useState<string | null>(null);
  const [lastStatus, setLastStatus] = useState<RefreshStatus | RefreshWork | null>(null);
  const [pollingError, setPollingError] = useState<WebsiteErrorResponse | null>(null);
  const [lookupTimedOut, setLookupTimedOut] = useState(false);
  const lookupStartedAt = useRef(Date.now());
  const automaticRefreshHandled = useRef(false);
  useEffect(() => {
    const status = data.refreshStatus;
    if (status && status.tag === data.requestedTag) {
      setWorkId(status.workId);
      setLastStatus(status);
      setPollingError(null);
    }
  }, [data.refreshStatus]);

  useEffect(() => {
    const refresh =
      refreshFetcher.data && "workId" in refreshFetcher.data ? refreshFetcher.data : null;
    if (refresh && refresh.tag === data.requestedTag) {
      setWorkId(refresh.workId);
      setLastStatus(refresh);
      setPollingError(null);
    }
  }, [refreshFetcher.data]);

  const terminalState =
    lastStatus?.state === "complete" ||
    lastStatus?.state === "failed" ||
    lastStatus?.state === "unavailable" ||
    pollingError !== null;
  const visibleStatus =
    lastStatus ??
    (data.refreshStatus?.tag === data.requestedTag ? data.refreshStatus : null);
  const player = newestPlayer(visibleStatus, data);
  const explained = explainsNoResults(data.lookup);
  const [explainedVisit, setExplainedVisit] = useState(explained);
  if (explained && !explainedVisit) setExplainedVisit(true);
  const { trackedPlayer, lookup, minuteChecks, isChecking } = playerLookupView(
    player,
    data.lookup,
    explainedVisit,
  );
  const history = selectPlayerHistory(player);
  const now = useServerTime(player ? profileLoadedAt(player.profile) : undefined);
  const todayEnded =
    player?.currentDay != null &&
    Date.parse(player.currentDay.period.split(" – ")[1]) <= now;
  const today = todayEnded ? null : (player?.currentDay ?? null);
  const openDay =
    selectedDay ??
    (player?.currentDay &&
    (!todayEnded || !liveDay(dayEvidence(player.currentDay)).routine)
      ? legendDayKey(player.currentDay.period)
      : null);
  useEffect(() => {
    if (!isChecking || lookupTimedOut) return;
    const timer = setInterval(() => {
      if (Date.now() - lookupStartedAt.current >= 60_000) setLookupTimedOut(true);
      else if (revalidator.state === "idle") revalidator.revalidate();
    }, 1000);
    return () => clearInterval(timer);
  }, [isChecking, lookupTimedOut, revalidator]);
  // Only the stable revalidate function, so a reread starting or finishing never
  // restarts the minute.
  const { revalidate } = revalidator;
  useEffect(() => {
    if (!minuteChecks) return;
    const timer = setInterval(() => {
      if (!document.hidden) void revalidate();
    }, 60_000);
    return () => clearInterval(timer);
  }, [minuteChecks, revalidate]);
  const refreshResourcePath = player
    ? `/resources/players/${encodeURIComponent(player.tag)}/refresh`
    : null;

  useEffect(() => {
    if (!location.hash.startsWith("#battle-")) return;
    let battleId: string;
    try {
      battleId = decodeURIComponent(location.hash.slice(1));
    } catch {
      return;
    }
    const battle = document.getElementById(battleId);
    if (!battle?.matches(".battle-slot")) return;
    let frame = 0;
    let highlightTimer: ReturnType<typeof setTimeout> | undefined;
    const highlight = () => {
      if (document.hidden) return;
      cancelAnimationFrame(frame);
      clearTimeout(highlightTimer);
      battle.classList.remove("battle-arrival");
      const day = battle.closest("details");
      if (day) day.open = true;
      battle.scrollIntoView({ block: "center", behavior: "instant" });
      // Let scrolling and layout finish before the two-second highlight starts.
      frame = requestAnimationFrame(() => {
        frame = requestAnimationFrame(() => {
          battle.classList.add("battle-arrival");
          highlightTimer = setTimeout(
            () => battle.classList.remove("battle-arrival"),
            2000,
          );
        });
      });
    };
    if (document.readyState === "complete") highlight();
    else window.addEventListener("load", highlight, { once: true });
    window.addEventListener("pageshow", highlight);
    document.addEventListener("visibilitychange", highlight);
    return () => {
      cancelAnimationFrame(frame);
      clearTimeout(highlightTimer);
      window.removeEventListener("load", highlight);
      window.removeEventListener("pageshow", highlight);
      document.removeEventListener("visibilitychange", highlight);
      battle.classList.remove("battle-arrival");
    };
  }, [location.hash, location.key, player?.tag]);

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
    refreshFetcher.submit(
      isDocumentReload
        ? { idempotencyKey: data.noJsIdempotencyKey }
        : { idempotencyKey: data.noJsIdempotencyKey, trigger: "automatic" },
      {
        method: "post",
        action: `/resources/players/${encodeURIComponent(trackedPlayer.tag)}/refresh`,
      },
    );
  }, [trackedPlayer, data.noJsIdempotencyKey, refreshFetcher]);

  useEffect(() => {
    if (!workId || terminalState || refreshResourcePath === null) return;
    let cancelled = false;
    let inFlight = false;
    let controller: AbortController | null = null;
    const deadline = Date.now() + 60_000;
    const unavailableError: WebsiteErrorResponse = {
      error: {
        code: "unavailable",
        message: "Saved data is still available, but the live service is unavailable.",
      },
    };

    const poll = async () => {
      if (cancelled || inFlight) return;
      if (Date.now() >= deadline) {
        setPollingError(unavailableError);
        return;
      }
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
        if (cancelled) return;
        if (!response.ok || !isRefreshStatusPayload(payload)) {
          setPollingError(isWebsiteErrorResponse(payload) ? payload : unavailableError);
          return;
        }
        if (payload.workId !== workId || payload.tag !== player?.tag) {
          setPollingError({
            error: {
              code: "conflict",
              message: "The request conflicts with current saved data.",
            },
          });
          return;
        }
        setPollingError(null);
        setLastStatus(payload);
      } catch (error) {
        if (
          !cancelled &&
          !(error instanceof DOMException && error.name === "AbortError")
        ) {
          setPollingError(unavailableError);
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
  }, [player?.tag, refreshResourcePath, terminalState, workId]);

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
    return (
      <main id="main-content" tabIndex={-1} className="page-shell player-page">
        {lookup?.profile ? (
          <header className="player-header">
            <div className="player-profile">
              <h1>
                <bdi>{lookup.profile.name}</bdi>
              </h1>
              <p className="player-identity">
                <span className="player-tag prominent">{lookup.tag}</span>
                {lookup.profile.clan ? (
                  <span className="player-clan">
                    <bdi>{lookup.profile.clan}</bdi>
                  </span>
                ) : null}
              </p>
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
          <h1>{data.requestedTag}</h1>
        )}
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
        <SeasonNav
          tag={data.requestedTag}
          seasons={data.seasons}
          selectedSeason={data.selectedSeason}
          currentAvailable={player !== null}
        />
        {data.selectedSeason !== null ? (
          <SelectedSeason
            seasonId={data.selectedSeason}
            summary={data.historical}
            error={data.historicalError}
          />
        ) : null}
        {data.selectedSeason === null && history.length > 0 ? (
          <section className="data-section" aria-label="Saved Legend history">
            <h2>Saved Legend history</h2>
            <p className="section-note">{LEGEND_DAY_NOTE}</p>
            {history.map(({ day, inSeason }) => (
              <LegendDay
                key={legendDayKey(day.period)}
                day={day}
                inSeason={inSeason}
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

  const actionError =
    refreshFetcher.data && "error" in refreshFetcher.data ? refreshFetcher.data : null;
  const refreshError = data.refreshError;
  const visibleRefreshError = actionError ?? pollingError ?? refreshError;
  const refreshActionPath = `/resources/players/${encodeURIComponent(trackedPlayer.tag)}/refresh`;

  return (
    <main id="main-content" tabIndex={-1} className="page-shell player-page">
      <header className="player-header">
        <div className="player-profile">
          <h1>
            <bdi>{trackedPlayer.profile.name}</bdi>
          </h1>
          <p className="player-identity">
            <span className="player-tag prominent">{trackedPlayer.tag}</span>
            {trackedPlayer.profile.clan ? (
              <span className="player-clan">
                <bdi>{trackedPlayer.profile.clan}</bdi>
              </span>
            ) : null}
          </p>
        </div>
        <div className="player-summary">
          <div className="player-trophy-card">
            <div>
              <span className="metric-label">Current trophies</span>
              {trackedPlayer.profile.seasonResetPending ? (
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
              disabled={refreshFetcher.state !== "idle"}
              name="refresh"
              value="public"
              onClick={(event) => {
                event.preventDefault();
                refreshFetcher.submit(
                  { idempotencyKey: data.noJsIdempotencyKey },
                  { method: "post", action: refreshActionPath },
                );
              }}
            >
              {refreshFetcher.state === "submitting" ? "Refreshing…" : "Refresh"}
            </button>
          </refreshFetcher.Form>
        </div>
      </header>

      {visibleRefreshError ? <ErrorNotice error={visibleRefreshError} /> : null}
      {data.lookupError ? <ErrorNotice error={data.lookupError} /> : null}
      {visibleStatus ? <RefreshProgress status={visibleStatus} /> : null}
      <p role="status">Now tracking in Legend I.</p>
      <SeasonNav
        tag={trackedPlayer.tag}
        seasons={data.seasons}
        selectedSeason={data.selectedSeason}
      />

      {data.selectedSeason !== null ? (
        <SelectedSeason
          seasonId={data.selectedSeason}
          summary={data.historical}
          error={data.historicalError}
        />
      ) : null}

      {data.selectedSeason !== null ? null : (
        <section className="data-section" aria-labelledby="season-days-title">
          <h2 id="season-days-title">Daily Legend log</h2>
          <p className="section-note">{LEGEND_DAY_NOTE}</p>
          {selectedDay &&
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
                : null;
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
          {history.some(
            ({ day }) => day.startTrophiesCalculation || day.startTrophies == null,
          ) ? (
            <p className="section-note">
              Calculated totals use saved trophies minus recorded changes. Unavailable
              means the saved history is incomplete.
            </p>
          ) : null}
          {history.length === 0 ? (
            <p className="section-note">No Legend days are saved for this player yet.</p>
          ) : null}
          <div className="legend-days">
            {history.map(({ day, inSeason }) => (
              <LegendDay
                key={legendDayKey(day.period)}
                day={day}
                inSeason={inSeason}
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
            : LOOKUP_MESSAGES[lookup.state])}
      </p>
      {explanation && lookup.reason === "no_legend_battles" ? (
        <p className="section-note">
          Taking part in Legend League battles is optional. This page updates as soon as
          they play.
        </p>
      ) : null}
      {explanation ? (
        <a href={canonicalPlayerPath(lookup.tag)}>Check again</a>
      ) : lookup.state === "checking" || lookup.state === "tracking" ? (
        <a href={canonicalPlayerPath(lookup.tag)}>Check progress</a>
      ) : lookup.state === "failed" ||
        lookup.state === "not_found" ||
        lookup.state === "unknown" ? (
        <a href={`${canonicalPlayerPath(lookup.tag)}?retry=1`}>Try again</a>
      ) : null}
    </section>
  );
}

function SeasonNav({
  tag,
  seasons,
  selectedSeason,
  currentAvailable = true,
}: {
  tag: string;
  seasons: SummarizedSeasonRef[];
  selectedSeason: string | null;
  currentAvailable?: boolean;
}) {
  // A selected past Season always keeps its way back, even if the list failed.
  if (seasons.length === 0 && selectedSeason === null) return null;
  return (
    <nav className="data-section" aria-label="Historical seasons">
      <div className="section-heading">
        <h2>Historical seasons</h2>
      </div>
      <ul className="season-list">
        {currentAvailable || selectedSeason !== null ? (
          <li key="current">
            {selectedSeason === null ? (
              <strong aria-current="page">Current Season</strong>
            ) : (
              <Link to={canonicalPlayerPath(tag)}>Current Season</Link>
            )}
          </li>
        ) : null}
        {seasons.map((season) => (
          <li key={season.seasonId}>
            {selectedSeason === season.seasonId ? (
              <strong aria-current="page">{seasonLabel(season.seasonId)}</strong>
            ) : (
              <Link
                to={`${canonicalPlayerPath(tag)}?season=${encodeURIComponent(season.seasonId)}`}
              >
                {seasonLabel(season.seasonId)}
              </Link>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}

function SelectedSeason({
  seasonId,
  summary,
  error,
}: {
  seasonId: string;
  summary: HistoricalSeasonSummary | null;
  error: WebsiteErrorResponse | null;
}) {
  if (summary !== null) return <HistoricalSeasonPanel summary={summary} />;
  return (
    <section className="data-section" aria-labelledby="historical-title">
      <div className="section-heading">
        <h2 id="historical-title">Historical season</h2>
      </div>
      {error ? <ErrorNotice error={error} /> : null}
      <p className="section-note">Results for {seasonLabel(seasonId)} are unavailable.</p>
    </section>
  );
}

function HistoricalSeasonPanel({ summary }: { summary: HistoricalSeasonSummary }) {
  if (summary.source === "official_league_history") {
    return (
      <section className="data-section" aria-labelledby="historical-season-title">
        <div className="section-heading">
          <h2 id="historical-season-title">
            {seasonLabel(summary.seasonId, summary.seasonEnd)}
          </h2>
        </div>
        <p className="section-note">
          Season result from Clash of Clans. A Clash Lens daily summary is not available
          for this Season.
        </p>
        {summary.officialHistory ? (
          <div className="metric-grid">
            <MetricCard title="Season finish">
              <Metric
                label="Final trophies"
                value={formatCount(summary.officialHistory.eodTrophies)}
              />
              <Metric label="Final rank" value={formatCount(summary.finalRank)} />
            </MetricCard>
          </div>
        ) : null}
      </section>
    );
  }
  return (
    <section className="data-section" aria-labelledby="historical-season-title">
      <div className="section-heading">
        <h2 id="historical-season-title">
          {seasonLabel(summary.seasonId, summary.seasonEnd)}
        </h2>
      </div>
      <p className="section-note">
        {summary.coverageState === "complete"
          ? "Records cover all 28 Legend days."
          : `Partial Season history: records cover ${summary.daysObserved} of 28 Legend days, and some of those may be incomplete. Totals below cover the recorded days only.`}
      </p>
      {summary.officialHistory ? (
        <p className="section-note">
          Final trophies: {formatCount(summary.officialHistory.eodTrophies)}
        </p>
      ) : null}
      <div className="metric-grid">
        <MetricCard title="Offense">
          <Metric label="Attacks recorded" value={formatCount(summary.attackCount)} />
          <Metric label="Trophy gain" value={formatSigned(summary.attackGain)} />
        </MetricCard>
        <MetricCard title="Defense">
          <Metric label="Defenses recorded" value={formatCount(summary.defenseCount)} />
          <Metric
            label="Trophy loss"
            value={
              summary.defenseLoss === null
                ? "Unknown"
                : formatSigned(-summary.defenseLoss)
            }
          />
        </MetricCard>
        <MetricCard title="Season">
          <Metric
            label={`Sum of daily trophy changes (${summary.daysObserved} of 28 days)`}
            value={formatSigned(summary.netTrophyChange)}
          />
          <Metric
            label="Trophies"
            value={
              summary.endTrophies === null
                ? "Unknown"
                : `${formatCount(summary.startTrophies)} → ${formatCount(summary.endTrophies)}`
            }
          />
          <Metric label="Final rank" value={formatCount(summary.finalRank)} />
        </MetricCard>
        <MetricCard title="Attack stars">
          <Metric label="Three-star" value={formatCount(summary.attackStars["3"])} />
          <Metric label="Two-star" value={formatCount(summary.attackStars["2"])} />
          <Metric label="One-star" value={formatCount(summary.attackStars["1"])} />
          <Metric label="No-star" value={formatCount(summary.attackStars["0"])} />
          <Metric label="Unknown" value={formatCount(summary.attackStarsUnknown)} />
        </MetricCard>
        <MetricCard title="Defense stars">
          <Metric label="Three-star" value={formatCount(summary.defenseStars["3"])} />
          <Metric label="Two-star" value={formatCount(summary.defenseStars["2"])} />
          <Metric label="One-star" value={formatCount(summary.defenseStars["1"])} />
          <Metric label="No-star" value={formatCount(summary.defenseStars["0"])} />
          <Metric label="Unknown" value={formatCount(summary.defenseStarsUnknown)} />
        </MetricCard>
      </div>
      <p className="section-note">
        The sum of daily trophy changes includes automatic defense losses at Reset; it
        shows Unknown if any recorded day&apos;s change is unknown. Recorded battle net
        leaves those losses out.
      </p>
      {[
        summary.attackCount,
        summary.attackGain,
        summary.defenseCount,
        summary.defenseLoss,
        summary.netTrophyChange,
      ].includes(null) ? (
        <p className="section-note">Some daily totals are unavailable.</p>
      ) : null}
      <p className="section-note">{LEGEND_DAY_NOTE}</p>
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
              <th scope="col">Status</th>
              <th scope="col">Start</th>
              <th scope="col">Attack</th>
              <th scope="col">Defense</th>
              <th scope="col">Trophy change</th>
              <th scope="col">Recorded battle net</th>
              <th scope="col">End</th>
              <th scope="col">EOD change from previous day</th>
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
                <tr key={`${day.period}-${day.dayNumber ?? "unknown"}`}>
                  <td>{day.dayNumber ?? "Unknown"}</td>
                  <td>
                    {status}
                    {reasons.map((reason) => (
                      <small className="table-note" key={reason}>
                        {reason}
                      </small>
                    ))}
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

function seasonLabel(seasonId: string, seasonEnd?: string | null): string {
  const end = seasonEnd
    ? new Date(seasonEnd)
    : new Date((Number(seasonId) + 28 * 24 * 60 * 60) * 1000);
  return Number.isNaN(end.getTime()) ? "Past season" : formatPlayerDate(end);
}

const playerDateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});
const playerTimeFormatter = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
});

function formatPlayerDate(date: Date): string {
  return playerDateFormatter.format(date).replace("Sept", "Sep");
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
        <time className="player-updated" dateTime={observedAt}>
          {formatPlayerTimestamp(observedAt)}
        </time>
        {oldAge(observedAt)}
      </p>
      <p className="player-freshness">
        <span>Battle history updated</span>{" "}
        {profile.battleHistoryUpdatedAt ? (
          <>
            <time
              className="player-history-updated"
              dateTime={profile.battleHistoryUpdatedAt}
            >
              {formatPlayerTimestamp(profile.battleHistoryUpdatedAt)}
            </time>
            {oldAge(profile.battleHistoryUpdatedAt)}
          </>
        ) : (
          "not yet"
        )}
      </p>
    </>
  );
}

function formatPlayerTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time unavailable";
  return `${formatPlayerDate(date)}, ${playerTimeFormatter.format(date)} UTC`;
}

function legendDayKey(period: string): string {
  return period.split(" – ")[0].slice(0, 10);
}

function selectPlayerHistory(player: PlayerPage | null) {
  const seasonDays = player?.seasonDays ?? [];
  const days = [
    ...seasonDays,
    ...(player?.currentDay ? [player.currentDay] : []),
    ...(player?.recentDays ?? []),
  ].filter(
    (day) =>
      !day.uncertainty.includes("player_not_eligible") ||
      day.offenseEvents.length > 0 ||
      day.defenseEvents.length > 0,
  );
  return days
    .filter(
      (day, index) =>
        days.findIndex(
          (saved) => legendDayKey(saved.period) === legendDayKey(day.period),
        ) === index,
    )
    .sort((a, b) => legendDayKey(b.period).localeCompare(legendDayKey(a.period)))
    .map((day) => ({
      day,
      inSeason: seasonDays.some(
        (saved) => legendDayKey(saved.period) === legendDayKey(day.period),
      ),
    }));
}

const LEGEND_DAY_NOTE =
  "A Legend day runs from 05:00 to 05:00 UTC. Ended days show “Provisional result” until Clash Lens can prove their trophy change includes the automatic defense loss at Reset.";

function isCurrentDay(today: RankedDaySummary | null, day: RankedDaySummary): boolean {
  return today != null && legendDayKey(today.period) === legendDayKey(day.period);
}

function LegendDay({
  day,
  inSeason,
  isCurrentDay,
  openDay,
}: {
  day: RankedDaySummary;
  inSeason: boolean;
  isCurrentDay: boolean;
  openDay: string | null;
}) {
  const dayKey = legendDayKey(day.period);
  const dayLabel = legendDayDate(day.period);
  const { status, reasons, battleNet } = presentDay(dayEvidence(day), isCurrentDay);
  return (
    <details className="legend-day" id={`legend-day-${dayKey}`} open={openDay === dayKey}>
      <summary>
        <span className="legend-day-date">
          <strong>{dayLabel}</strong>
          <span className="legend-day-meta">
            <small>{inSeason ? <>Day {day.dayNumber ?? "—"}</> : "Date only"}</small>
            {isCurrentDay ? (
              <LiveBadge />
            ) : (
              <span className="legend-day-live legend-day-status">{status}</span>
            )}
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
          {day.startTrophiesCalculation ? (
            <span className="legend-day-start-source">Calculated</span>
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
        </span>
        <span className="legend-day-stat legend-day-net">
          <small>Trophy change</small>
          {isCurrentDay && day.trophyChange === null && day.battlesComplete ? (
            <>
              <strong className={valueTone(battleNet)}>{formatSigned(battleNet)}</strong>
              <span>so far</span>
            </>
          ) : (
            <strong className={valueTone(day.trophyChange)}>
              {formatSigned(day.trophyChange)}
            </strong>
          )}
        </span>
      </summary>
      {reasons.map((reason) => (
        <p className="section-note" key={reason}>
          {reason}
        </p>
      ))}
      <p className="section-note">
        {`Recorded battle net ${formatSigned(battleNet)}: recorded attacks minus recorded defenses, without the automatic defense loss at Reset.`}
      </p>
      <div className="battle-columns">
        <BattleColumn
          title="Attacks"
          events={day.offenseEvents}
          day={dayKey}
          dayLabel={dayLabel}
        />
        <BattleColumn
          title="Defenses"
          events={day.defenseEvents}
          day={dayKey}
          dayLabel={dayLabel}
        />
      </div>
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
}: {
  title: "Attacks" | "Defenses";
  events: RankedBattleEvent[];
  day: string;
  dayLabel: string;
}) {
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
                <a
                  className="battle-profile-link"
                  href={`${canonicalPlayerPath(event.opponent.tag)}?day=${day}#battle-${encodeURIComponent(event.battleId)}`}
                  aria-label={`View ${event.opponent.name ?? event.opponent.tag}'s Legend log for ${dayLabel}`}
                >
                  <strong>
                    <bdi>{event.opponent.name ?? event.opponent.tag}</bdi>
                  </strong>
                </a>
                {event.opponent.name ? (
                  <span className="player-tag">{event.opponent.tag}</span>
                ) : null}
                <time dateTime={event.battleTimestamp}>
                  {playerTimeFormatter.format(new Date(event.battleTimestamp))} UTC
                </time>
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
                <a
                  className="battle-army"
                  href={`https://link.clashofclans.com/en?action=CopyArmy&army=${encodeURIComponent(event.armyShareCode)}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={`Copy army from the battle against ${event.opponent.name ?? event.opponent.tag}, opens in a new tab`}
                >
                  Copy army
                </a>
              ) : null}
            </li>
          ) : (
            <li
              aria-label={`${title.slice(0, -1)} ${index + 1} not recorded`}
              className="battle-slot battle-slot-empty"
              key={`empty-${index}`}
            >
              <span className="battle-number" aria-hidden="true">
                {index + 1}
              </span>
              <span aria-hidden="true">Not recorded</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function RefreshProgress({ status }: { status: RefreshStatus | RefreshWork }) {
  const inProgress = status.state === "queued" || status.state === "running";
  return (
    <section className="refresh-panel" aria-live="polite" aria-label="Player refresh">
      <p role="status">{inProgress ? "Refreshing…" : "Updated."}</p>
      {inProgress ? (
        <progress aria-label="Refresh progress" value={status.progressPercent} max="100">
          {status.progressPercent}%
        </progress>
      ) : null}
      <span className="sr-only" data-testid="refresh-work-id">
        Work ID: {status.workId}
      </span>
    </section>
  );
}

function MetricCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <article className="metric-card">
      <h3>{title}</h3>
      <dl>{children}</dl>
    </article>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
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

function provisional(value: string, state: string | null): string {
  return value === "Unknown" || state === "accepted" ? value : `${value} (provisional)`;
}

function formatCount(value: number | null): string {
  return value === null ? "Unknown" : value.toLocaleString("en-GB");
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
