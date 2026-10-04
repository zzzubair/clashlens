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
import { formatAge, useCurrentTime } from "../components/Provenance";
import { pageMeta } from "../lib/blog";
import { LOOKUP_MESSAGES, lookupExplanation } from "../lib/player-lookup-text";
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
  useEffect(() => {
    if (!isChecking || lookupTimedOut) return;
    const timer = setInterval(() => {
      if (Date.now() - lookupStartedAt.current >= 60_000) setLookupTimedOut(true);
      else if (revalidator.state === "idle") revalidator.revalidate();
    }, 1000);
    return () => clearInterval(timer);
  }, [isChecking, lookupTimedOut, revalidator]);
  useEffect(() => {
    if (!minuteChecks) return;
    const timer = setInterval(() => {
      if (!document.hidden && revalidator.state === "idle") revalidator.revalidate();
    }, 60_000);
    return () => clearInterval(timer);
  }, [minuteChecks, revalidator]);
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
  const { revalidate } = revalidator;
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
              <h1>{lookup.profile.name}</h1>
              <p className="player-identity">
                <span className="player-tag prominent">{lookup.tag}</span>
                {lookup.profile.clan ? (
                  <span className="player-clan">{lookup.profile.clan}</span>
                ) : null}
              </p>
            </div>
            <div className="player-summary">
              <div className="player-trophy-card">
                <div>
                  <span className="metric-label">Trophies</span>
                  <strong className="player-trophy-count">
                    <span className="trophy-mark" aria-hidden="true" />
                    {lookup.profile.trophies.toLocaleString()}
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
        {data.historicalError ? <ErrorNotice error={data.historicalError} /> : null}
        {data.historical ? <HistoricalSeasonPanel summary={data.historical} /> : null}
        {data.selectedSeason === null && history.length > 0 ? (
          <section className="data-section" aria-label="Saved Legend history">
            <h2>Saved Legend history</h2>
            {history.map(({ day, inSeason }) => (
              <LegendDay
                key={legendDayKey(day.period)}
                day={day}
                inSeason={inSeason}
                isCurrentDay={isCurrentDay(player, day)}
                selectedDay={selectedDay}
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
          <h1>{trackedPlayer.profile.name}</h1>
          <p className="player-identity">
            <span className="player-tag prominent">{trackedPlayer.tag}</span>
            <span className="player-clan">{trackedPlayer.profile.clan}</span>
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
                    {trackedPlayer.profile.trophies.toLocaleString()}
                  </span>
                </>
              ) : (
                <strong className="player-trophy-count">
                  <span className="trophy-mark" aria-hidden="true" />
                  {trackedPlayer.profile.trophies.toLocaleString()}
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
        data.historical !== null ? (
          <HistoricalSeasonPanel summary={data.historical} />
        ) : (
          <section className="data-section" aria-labelledby="historical-title">
            <div className="section-heading">
              <h2 id="historical-title">Historical season</h2>
            </div>
            {data.historicalError ? <ErrorNotice error={data.historicalError} /> : null}
            <p className="section-note">
              Results for {seasonLabel(data.selectedSeason)} are unavailable.
            </p>
          </section>
        )
      ) : null}

      {data.selectedSeason !== null ? null : (
        <section className="data-section" aria-labelledby="season-days-title">
          <h2 id="season-days-title">Daily Legend log</h2>
          {selectedDay &&
          !history.some(({ day }) => legendDayKey(day.period) === selectedDay) ? (
            <p className="section-note" role="status">
              No saved Legend log for {legendDayDate(selectedDay)}.
            </p>
          ) : null}
          {trackedPlayer.dataQuality.map((warning) => (
            <p className="section-note" key={`${warning.code}-${warning.label}`}>
              <strong>{warning.label}:</strong>{" "}
              {/^[a-z0-9_:]+(; [a-z0-9_:]+)*$/i.test(warning.detail)
                ? dayReasons(warning.detail.split("; "), true).join(" ")
                : warning.detail}
            </p>
          ))}
          {history.some(
            ({ day }) => day.startTrophiesCalculation || day.startTrophies == null,
          ) ? (
            <p className="section-note">
              Calculated totals use saved trophies minus recorded changes. Unavailable
              means the saved history is incomplete.
            </p>
          ) : null}
          <div className="legend-days">
            {history.map(({ day, inSeason }) => (
              <LegendDay
                key={legendDayKey(day.period)}
                day={day}
                inSeason={inSeason}
                isCurrentDay={isCurrentDay(player, day)}
                selectedDay={selectedDay}
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
  if (seasons.length === 0) return null;
  return (
    <nav className="data-section" aria-label="Historical seasons">
      <div className="section-heading">
        <h2>Historical seasons</h2>
      </div>
      <ul className="season-list">
        {currentAvailable ? (
          <li key="current">
            {selectedSeason === null ? (
              <strong aria-current="page">Current season</strong>
            ) : (
              <Link to={canonicalPlayerPath(tag)}>Current season</Link>
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
          Season result from Clash of Clans. Daily battle logs were not recorded for this
          season.
        </p>
        {summary.officialHistory ? (
          <div className="metric-grid">
            <MetricCard title="Season finish">
              <Metric
                label="Final trophies"
                value={formatCount(summary.officialHistory.eodTrophies)}
              />
              <Metric
                label="Final rank"
                value={formatCount(summary.officialHistory.finalPlacement)}
              />
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
      <p className="section-note">{summary.daysObserved} of 28 Legend days recorded</p>
      {summary.officialHistory ? (
        <p className="section-note">
          Final trophies: {formatCount(summary.officialHistory.eodTrophies)} · Final rank:{" "}
          {formatCount(summary.officialHistory.finalPlacement)}
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
          <Metric label="Net change" value={formatSigned(summary.netTrophyChange)} />
          <Metric
            label="Trophies"
            value={
              summary.startTrophies === null || summary.endTrophies === null
                ? "Unknown"
                : `${summary.startTrophies} → ${summary.endTrophies}`
            }
          />
          <Metric
            label="Final rank"
            value={summary.finalRank === null ? "Unknown" : String(summary.finalRank)}
          />
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
      {summary.source === "tracked_summary" && summary.unresolvedFlags.length > 0 ? (
        <p className="section-note">Some daily totals are unavailable.</p>
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
              <th scope="col">Status</th>
              <th scope="col">Start</th>
              <th scope="col">Attack</th>
              <th scope="col">Defense</th>
              <th scope="col">Net</th>
              <th scope="col">Recorded battle net</th>
              <th scope="col">End</th>
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
                  <td>{formatCount(day.endTrophies)}</td>
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
  return Number.isNaN(end.getTime())
    ? "Past season"
    : formatPlayerDate(end).replace("Sept", "Sep");
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
  return playerDateFormatter.format(date);
}

function legendDayDate(period: string): string {
  const date = new Date(period.split(" – ")[0]);
  return Number.isNaN(date.getTime()) ? "Date unavailable" : formatPlayerDate(date);
}

const FRESHNESS_LIMIT_SECONDS = 15 * 60;

function PlayerFreshness({ profile }: { profile: PlayerProfile }) {
  const { observedAt, ageSeconds } = profile.freshness;
  const loadedAt = Date.parse(observedAt) + ageSeconds * 1000;
  const now = useCurrentTime(
    Number.isNaN(loadedAt) ? undefined : new Date(loadedAt).toISOString(),
  );
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

function isCurrentDay(player: PlayerPage | null, day: RankedDaySummary): boolean {
  return (
    player?.currentDay != null &&
    legendDayKey(player.currentDay.period) === legendDayKey(day.period)
  );
}

interface DayEvidence {
  net: number | null;
  state: string;
  coverage: string;
  codes: string[];
  attackGain: number | null;
  defenseLoss: number | null;
  attacks: number | null;
  defenses: number | null;
  battlesComplete?: boolean;
}

// Codes that leave a day's 8 attacks and 8 defenses in doubt, or may hide such a code.
const BATTLE_DOUBT_CODES = new Set([
  "perspective_disagreement",
  "duplicate_contribution_disagreement",
  "trophy_equation_mismatch",
  "ranked_version_mismatch",
  "attack_star_total_mismatch",
  "defense_star_total_mismatch",
  "truncated_reasons",
]);

// Only Python's calendar check makes a day current; a saved "Live" state can
// outlast its day. No saved result proves the Reset settled yet, so a finished
// day with a number is still provisional. A finished day with every battle
// recorded (all 8 of each) has nothing missing from its number. Python marks
// this for recent days; saved Season entries carry only their counts and codes.
function presentDay(day: DayEvidence, isCurrentDay: boolean) {
  const battlesComplete =
    day.battlesComplete ??
    (day.attacks === 8 &&
      day.defenses === 8 &&
      !day.codes.some((code) => BATTLE_DOUBT_CODES.has(code)));
  const status = isCurrentDay
    ? "In progress"
    : day.net === null
      ? "Result unknown"
      : !battlesComplete &&
          (day.state !== "Complete" ||
            day.coverage !== "complete" ||
            day.codes.length > 0)
        ? "Incomplete"
        : "Provisional result";
  const reasons = dayReasons(day.codes, isCurrentDay, day);
  if (reasons.length === 0 && status === "Incomplete")
    reasons.push(
      day.state === "Live"
        ? "Final evidence for this day has not been processed yet."
        : "Some daily evidence is unavailable.",
    );
  const battleNet =
    day.attackGain === null || day.defenseLoss === null
      ? null
      : day.attackGain - day.defenseLoss;
  return { status, reasons, battleNet };
}

const REASON_TEXT: Record<string, string> = {
  missing_start_battle_log_baseline:
    "The battle log was not checked at the start of this day.",
  missing_end_battle_log_baseline: "The battle log was not checked after this day ended.",
  missing_start_baseline: "Trophies at the start of this day were not recorded.",
  start_baseline_incomplete: "The start-of-day trophy reading is incomplete.",
  missing_end_baseline: "Trophies at the end of this day were not recorded.",
  end_baseline_incomplete: "The end-of-day trophy reading is incomplete.",
  battle_log_stale_window:
    "The battle log was not checked often enough to be sure every battle was seen.",
  battle_log_overlap_gap: "Some battles may be missing between two battle log checks.",
  battle_log_row_gap: "Part of a battle log reply could not be read.",
  battle_log_row_count_exceeds_fifty: "Part of a battle log reply could not be read.",
  duplicate_battle_identity_in_observation:
    "Part of a battle log reply could not be read.",
  unclassified_rows: "Some battles in the log could not be identified.",
  perspective_disagreement: "The two players' battle logs disagree about a result.",
  duplicate_contribution_disagreement:
    "The two players' battle logs disagree about a result.",
  trophy_equation_mismatch:
    "Recorded battles do not add up to the change between trophy readings.",
  automatic_defense_basis_unavailable:
    "The automatic defense loss at Reset could not be calculated.",
  season_anchor_conflict: "The Season start date could not be confirmed.",
  player_not_eligible: "The player was not in Legend I for all of this day.",
  shield_sequence_longer_than_two_days:
    "A shield period was longer than expected and could not be explained.",
  malformed_evidence: "Some saved evidence for this day could not be read.",
  malformed_contribution: "Some saved evidence for this day could not be read.",
  "ranked_day_state:Inconsistent": "The evidence for this day conflicts.",
  "ranked_day_state:Malformed": "Some saved evidence for this day could not be read.",
  battle_event_projection_incomplete: "Not every recorded battle is listed for this day.",
  detailed_boundaries_unavailable:
    "Detailed start and end readings for this day were not saved.",
  ranked_version_missing: "Some saved evidence for this day could not be read.",
  malformed_battle_entries: "Some saved evidence for this day could not be read.",
  ranked_version_mismatch: "The evidence for this day conflicts.",
  attack_star_total_mismatch: "Recorded attacks do not match the day's attack count.",
  defense_star_total_mismatch: "Recorded defenses do not match the day's defense count.",
  truncated_reasons: "More reasons were saved than can be shown.",
};
const ENDING_REASONS = new Set([
  "missing_end_battle_log_baseline",
  "missing_end_baseline",
  "end_baseline_incomplete",
]);

// Plain words for Python's reason codes.
function dayReasons(
  codes: string[],
  isCurrentDay: boolean,
  counts: { attacks: number | null; defenses: number | null } = {
    attacks: null,
    defenses: null,
  },
): string[] {
  const reasons = codes.map((code) =>
    code === "attack_count_exceeds_eight"
      ? excessNote(counts.attacks, "attacks")
      : code === "defense_count_exceeds_eight"
        ? excessNote(counts.defenses, "defenses")
        : isCurrentDay && ENDING_REASONS.has(code)
          ? "Ending evidence arrives after Reset."
          : (REASON_TEXT[code] ?? "Some daily evidence is unavailable."),
  );
  return [...new Set(reasons)];
}

function LegendDay({
  day,
  inSeason,
  isCurrentDay,
  selectedDay,
}: {
  day: RankedDaySummary;
  inSeason: boolean;
  isCurrentDay: boolean;
  selectedDay: string | null;
}) {
  const dayKey = legendDayKey(day.period);
  const dayLabel = legendDayDate(day.period);
  const { status, reasons, battleNet } = presentDay(
    {
      net: day.trophyChange,
      state: day.state,
      coverage: day.completeness.state,
      codes: day.uncertainty,
      attackGain: day.offense.trophyGain,
      defenseLoss: day.defense.trophyLoss,
      attacks: day.offenseEvents.length,
      defenses: day.defenseEvents.length,
      battlesComplete: day.battlesComplete,
    },
    isCurrentDay,
  );
  return (
    <details
      className="legend-day"
      id={`legend-day-${dayKey}`}
      open={selectedDay ? selectedDay === dayKey : isCurrentDay}
    >
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
          <span>{formatCount(day.offense.attacks)} recorded</span>
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
          <span>{formatCount(day.defense.defenses)} recorded</span>
        </span>
        <span className="legend-day-stat legend-day-net">
          <small>Net</small>
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
  const dot = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    let frame = 0;
    const breathe = (time: number) => {
      const phase = (1 - Math.cos((time / 2600) * Math.PI * 2)) / 2;
      dot.current?.style.setProperty("--live-pulse", String(phase));
      frame = requestAnimationFrame(breathe);
    };
    frame = requestAnimationFrame(breathe);
    return () => cancelAnimationFrame(frame);
  }, []);
  return (
    <span className="legend-day-live" aria-label="Today's Legend day, in progress">
      <span className="legend-live-dot" ref={dot} aria-hidden="true" />
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
                  <strong>{event.opponent.name ?? event.opponent.tag}</strong>
                </a>
                <span className="player-tag">{event.opponent.tag}</span>
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
  return value > 0 ? `+${value}` : String(value);
}

function valueTone(value: number | null): string {
  if (value === null || value === 0) return "score-neutral";
  return value > 0 ? "score-positive" : "score-negative";
}

function excessNote(count: number | null, kind: "attacks" | "defenses"): string {
  return count === null
    ? `Clash of Clans returned more than the usual 8 ${kind} for this day, so this day is marked partial.`
    : `Clash of Clans returned ${count} ${kind} for this day, more than the usual 8, so this day is marked partial.`;
}

function formatCount(value: number | null): string {
  return value === null ? "Unknown" : String(value);
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
