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
import { canonicalPlayerPath, normalizePlayerTag } from "../lib/player-tag";
import type {
  HistoricalSeasonDayEntry,
  HistoricalSeasonSummary,
  PlayerPage,
  PlayerLookup,
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
  };
}

function readSeasonParam(value: string | null): string | null {
  if (value === null || value.length === 0 || value.length > 128) return null;
  return value;
}

export function headers() {
  return { "Cache-Control": "no-store" };
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
  const refreshedPlayer =
    visibleStatus && "player" in visibleStatus && visibleStatus.tag === data.requestedTag
      ? (visibleStatus as RefreshStatus).player
      : null;
  const player =
    refreshedPlayer &&
    (data.player === null ||
      Date.parse(refreshedPlayer.profile.freshness.observedAt) >
        Date.parse(data.player.profile.freshness.observedAt))
      ? refreshedPlayer
      : data.player;
  const trackedPlayer = player?.trackingState === "tracking" ? player : null;
  const lookup: PlayerLookup | null = player
    ? { tag: player.tag, state: player.trackingState }
    : data.lookup;
  const history = selectPlayerHistory(player);
  const isChecking =
    lookup?.state === "checking" || (lookup?.state === "tracking" && player === null);
  useEffect(() => {
    if (!isChecking || lookupTimedOut) return;
    const timer = setInterval(() => {
      if (Date.now() - lookupStartedAt.current >= 60_000) setLookupTimedOut(true);
      else if (revalidator.state === "idle") revalidator.revalidate();
    }, 1000);
    return () => clearInterval(timer);
  }, [isChecking, lookupTimedOut, revalidator]);
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
      {
        idempotencyKey: data.noJsIdempotencyKey,
        trigger: isDocumentReload ? "manual" : "automatic",
      },
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
          <p>Try refreshing the page in a moment.</p>
        </main>
      );
    }
    return (
      <main id="main-content" tabIndex={-1} className="page-shell player-page">
        <h1>{data.requestedTag}</h1>
        {lookup ? (
          <LookupNotice lookup={lookup} timedOut={lookupTimedOut} />
        ) : (
          <p className="section-note">
            We could not confirm whether this player is currently tracked. Any saved
            history is still available.
          </p>
        )}
        {data.lookupError ? <ErrorNotice error={data.lookupError} /> : null}
        {data.error ? <ErrorNotice error={data.error} /> : null}
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
                selectedDay={selectedDay}
              />
            ))}
          </section>
        ) : null}
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
              <strong className="player-trophy-count">
                <span className="trophy-mark" aria-hidden="true" />
                {trackedPlayer.profile.trophies.toLocaleString()}
              </strong>
            </div>
            <p className="player-freshness">
              <span>Updated</span>{" "}
              <time
                className="player-updated"
                dateTime={trackedPlayer.profile.freshness.observedAt}
              >
                {formatPlayerTimestamp(trackedPlayer.profile.freshness.observedAt)}
              </time>
            </p>
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
          {trackedPlayer.dataQuality.length > 0 ? (
            <p className="section-note">
              Recent battles from Clash of Clans. Some daily totals are unavailable
              because tracking started partway through the season.
            </p>
          ) : null}
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
                selectedDay={selectedDay}
              />
            ))}
          </div>
        </section>
      )}
    </main>
  );
}

function LookupNotice({ lookup, timedOut }: { lookup: PlayerLookup; timedOut: boolean }) {
  const messages: Record<PlayerLookup["state"], string> = {
    unknown: "Waiting to check this tag with Clash of Clans.",
    checking:
      "Checking this tag with Clash of Clans. Legend I players start tracking automatically.",
    tracking: "Now tracking in Legend I. The first results are being prepared.",
    not_found:
      "Player not found. Clash of Clans did not find this tag. Check the tag and try again.",
    not_in_legend:
      "This player is not in Legend I. We have kept the tag and any saved history.",
    uncertain:
      "This player exists, but we could not confirm their Legend I eligibility. Any saved history is still available.",
    failed:
      "We could not finish checking this tag. This does not mean the player is missing or outside Legend I.",
  };
  return (
    <section aria-label="Player lookup" aria-live="polite">
      <p>
        {timedOut && (lookup.state === "checking" || lookup.state === "tracking")
          ? "The check is taking longer than expected. It may still be running."
          : messages[lookup.state]}
      </p>
      {lookup.state === "checking" || lookup.state === "tracking" ? (
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
          <Metric label="Attacks" value={formatCount(summary.attackCount)} />
          <Metric label="Trophy gain" value={formatSigned(summary.attackGain)} />
        </MetricCard>
        <MetricCard title="Defense">
          <Metric label="Defenses" value={formatCount(summary.defenseCount)} />
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
              <th scope="col">Start</th>
              <th scope="col">Attack</th>
              <th scope="col">Defense</th>
              <th scope="col">Net</th>
              <th scope="col">End</th>
              <th scope="col">Attacks</th>
              <th scope="col">Defenses</th>
              <th scope="col">Adjustment</th>
            </tr>
          </thead>
          <tbody>
            {summary.dailyEntries.map((day) => (
              <tr key={`${day.period}-${day.dayNumber ?? "unknown"}`}>
                <td>{day.dayNumber ?? "Unknown"}</td>
                <td>{formatCount(day.startTrophies)}</td>
                <td>{formatSigned(day.attackGain)}</td>
                <td>
                  {day.defenseLoss === null ? "Unknown" : formatSigned(-day.defenseLoss)}
                </td>
                <td>{formatSigned(day.netChange)}</td>
                <td>{formatCount(day.endTrophies)}</td>
                <td>{formatCount(day.attacks)}</td>
                <td>{formatCount(day.defenses)}</td>
                <td>{formatAdjustment(day)}</td>
              </tr>
            ))}
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

function LegendDay({
  day,
  inSeason,
  selectedDay,
}: {
  day: RankedDaySummary;
  inSeason: boolean;
  selectedDay: string | null;
}) {
  const dayKey = legendDayKey(day.period);
  const dayLabel = legendDayDate(day.period);
  return (
    <details
      className="legend-day"
      id={`legend-day-${dayKey}`}
      open={selectedDay ? selectedDay === dayKey : day.state === "Live"}
    >
      <summary>
        <span className="legend-day-date">
          <strong>{dayLabel}</strong>
          <span className="legend-day-meta">
            <small>{inSeason ? <>Day {day.dayNumber ?? "—"}</> : "Date only"}</small>
            {day.state === "Live" ? <LiveBadge /> : null}
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
          <span>{formatCount(day.offense.attacks)} used</span>
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
          <span>{formatCount(day.defense.defenses)} taken</span>
        </span>
        <span className="legend-day-stat legend-day-net">
          <small>Net</small>
          <strong className={valueTone(day.trophyChange ?? battleTrophyChange(day))}>
            {formatSigned(day.trophyChange ?? battleTrophyChange(day))}
          </strong>
        </span>
      </summary>
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
    <span className="legend-day-live" aria-label="Today's live Legend log">
      <span className="legend-live-dot" ref={dot} aria-hidden="true" />
      Live
    </span>
  );
}

function battleTrophyChange(day: RankedDaySummary): number | null {
  return day.offense.trophyGain === null || day.defense.trophyLoss === null
    ? null
    : day.offense.trophyGain - day.defense.trophyLoss;
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
        {Array.from({ length: 8 }, (_, index) => {
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
              aria-label={`Empty ${title.toLowerCase().slice(0, -1)} slot ${index + 1}`}
              className="battle-slot battle-slot-empty"
              key={`empty-${index}`}
            />
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
