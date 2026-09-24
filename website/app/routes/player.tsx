import { useEffect, useRef, useState } from "react";
import {
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
}

export async function loader({
  request,
  params,
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

  let player: PlayerPage | null = null;
  let error: WebsiteErrorResponse | null = null;
  let seasons: SummarizedSeasonRef[];
  let historical: HistoricalSeasonSummary | null = null;
  let historicalError: WebsiteErrorResponse | null = null;
  const selectedSeason = readSeasonParam(url.searchParams.get("season"));
  try {
    const { createPythonClient } = await import("../services/python.server");
    player = await createPythonClient().getPlayer(normalizedTag);
  } catch (cause) {
    error = await safeError(cause);
  }
  try {
    const { createPythonClient } = await import("../services/python.server");
    seasons = await createPythonClient().getPlayerSeasons(normalizedTag);
  } catch {
    seasons = [];
  }
  if (selectedSeason !== null) {
    try {
      const { createPythonClient } = await import("../services/python.server");
      historical = await createPythonClient().getPlayerSeason(
        normalizedTag,
        selectedSeason,
      );
    } catch (cause) {
      historicalError = await safeError(cause);
    }
  }

  let refreshStatus: RefreshStatus | null = null;
  let refreshError: WebsiteErrorResponse | null = null;
  const workId = url.searchParams.get("refresh");
  if (workId) {
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(workId)) {
      refreshError = await safeError({
        status: 400,
        payload: { error: "invalid_input" },
      });
    } else {
      try {
        const { createPythonClient } = await import("../services/python.server");
        refreshStatus = await createPythonClient().getRefreshStatus(
          workId,
          normalizedTag,
        );
      } catch (cause) {
        refreshError = await safeError(cause);
      }
    }
  }
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
  const [searchParams] = useSearchParams();
  const requestedDay = searchParams.get("day");
  const selectedDay =
    requestedDay &&
    /^\d{4}-\d{2}-\d{2}$/.test(requestedDay) &&
    !Number.isNaN(Date.parse(requestedDay))
      ? requestedDay
      : null;
  const refreshFetcher = useFetcher<RefreshWork | RefreshError>();
  const revalidator = useRevalidator();
  const [workId, setWorkId] = useState<string | null>(null);
  const [lastStatus, setLastStatus] = useState<RefreshStatus | RefreshWork | null>(null);
  const [pollingError, setPollingError] = useState<WebsiteErrorResponse | null>(null);

  useEffect(() => {
    const status = data.refreshStatus;
    if (status) {
      setWorkId(status.workId);
      setLastStatus(status);
      setPollingError(null);
    }
  }, [data.refreshStatus]);

  useEffect(() => {
    const refresh =
      refreshFetcher.data && "workId" in refreshFetcher.data ? refreshFetcher.data : null;
    if (refresh) {
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
  const refreshedPlayer = lastStatus && "player" in lastStatus ? lastStatus.player : null;
  const player = refreshedPlayer ?? data.player;
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
    if (documentReloadHandled) return;
    documentReloadHandled = true;
    const navigation = performance.getEntriesByType?.("navigation")[0] as
      PerformanceNavigationTiming | undefined;
    if (
      navigation?.type !== "reload" ||
      new URL(navigation.name).pathname !== window.location.pathname ||
      data.player === null
    )
      return;
    refreshFetcher.submit(
      { idempotencyKey: data.noJsIdempotencyKey },
      {
        method: "post",
        action: `/resources/players/${encodeURIComponent(data.player.tag)}/refresh`,
      },
    );
  }, [data.player, data.noJsIdempotencyKey, refreshFetcher]);

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
        if (payload.state === "complete") revalidator.revalidate();
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
  }, [player?.tag, refreshResourcePath, revalidator, terminalState, workId]);

  if (player === null) {
    if (data.selectedSeason !== null && data.historical !== null) {
      return (
        <main id="main-content" tabIndex={-1} className="page-shell player-page">
          <header className="player-header">
            <div>
              <h1>{data.historical.tag}</h1>
              <p className="player-identity">
                <span className="player-tag prominent">{data.historical.tag}</span>
              </p>
            </div>
          </header>
          {data.error ? <ErrorNotice error={data.error} /> : null}
          <p className="section-note">
            Showing saved season results. The current profile is unavailable.
          </p>
          <SeasonNav
            tag={data.historical.tag}
            seasons={data.seasons}
            selectedSeason={data.selectedSeason}
            currentAvailable={false}
          />
          <HistoricalSeasonPanel summary={data.historical} />
        </main>
      );
    }
    if (data.requestedTag !== null && data.seasons.length > 0) {
      return (
        <main id="main-content" tabIndex={-1} className="page-shell player-page">
          <h1>Player history</h1>
          {data.error ? <ErrorNotice error={data.error} /> : null}
          <p className="section-note">
            Current profile data is unavailable. Saved historical seasons remain
            available.
          </p>
          <SeasonNav
            tag={data.requestedTag}
            seasons={data.seasons}
            selectedSeason={data.selectedSeason}
            currentAvailable={false}
          />
          {data.historicalError ? <ErrorNotice error={data.historicalError} /> : null}
        </main>
      );
    }
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-page">
        <h1>Player data unavailable</h1>
        {data.error ? <ErrorNotice error={data.error} /> : null}
        <p>Try refreshing the page in a moment.</p>
      </main>
    );
  }

  const actionError =
    refreshFetcher.data && "error" in refreshFetcher.data ? refreshFetcher.data : null;
  const refreshError = data.refreshError;
  const visibleRefreshError = actionError ?? pollingError ?? refreshError;
  const visibleStatus = lastStatus ?? data.refreshStatus;
  const refreshActionPath = `/resources/players/${encodeURIComponent(player.tag)}/refresh`;

  return (
    <main id="main-content" tabIndex={-1} className="page-shell player-page">
      <header className="player-header">
        <div className="player-profile">
          <h1>{player.profile.name}</h1>
          <p className="player-identity">
            <span className="player-tag prominent">{player.tag}</span>
            <span className="player-clan">{player.profile.clan}</span>
          </p>
        </div>
        <div className="player-summary">
          <div className="player-trophy-card">
            <div>
              <span className="metric-label">Current trophies</span>
              <strong className="player-trophy-count">
                <span className="trophy-mark" aria-hidden="true" />
                {player.profile.trophies.toLocaleString()}
              </strong>
            </div>
            <p className="player-freshness">
              <span>Updated</span>{" "}
              <time
                className="player-updated"
                dateTime={player.profile.freshness.observedAt}
              >
                {formatPlayerTimestamp(player.profile.freshness.observedAt)}
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
      {visibleStatus ? <RefreshProgress status={visibleStatus} /> : null}
      <SeasonNav
        tag={player.tag}
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
          !player.seasonDays.some((day) => legendDayKey(day.period) === selectedDay) ? (
            <p className="section-note" role="status">
              No saved Legend log for {legendDayDate(selectedDay)}.
            </p>
          ) : null}
          {player.dataQuality.length > 0 ? (
            <p className="section-note">
              Recent battles from Clash of Clans. Some daily totals are unavailable
              because tracking started partway through the season.
            </p>
          ) : null}
          {player.seasonDays.some((day) => day.startTrophiesCalculation || day.startTrophies == null) ? (
            <p className="section-note">
              Calculated totals use saved trophies minus recorded changes.
              Unavailable means the saved history is incomplete.
            </p>
          ) : null}
          <div className="legend-days">
            {player.seasonDays.map((day) => (
              <LegendDay
                key={`${day.period}-${day.dayNumber ?? "unknown"}`}
                day={day}
                selectedDay={selectedDay}
              />
            ))}
          </div>
        </section>
      )}
    </main>
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
              <a href={canonicalPlayerPath(tag)}>Current season</a>
            )}
          </li>
        ) : null}
        {seasons.map((season) => (
          <li key={season.seasonId}>
            {selectedSeason === season.seasonId ? (
              <strong aria-current="page">{seasonLabel(season.seasonId)}</strong>
            ) : (
              <a
                href={`${canonicalPlayerPath(tag)}?season=${encodeURIComponent(season.seasonId)}`}
              >
                {seasonLabel(season.seasonId)}
              </a>
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

function formatPlayerDate(date: Date): string {
  return date.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

function legendDayDate(period: string): string {
  const date = new Date(period.split(" – ")[0]);
  return Number.isNaN(date.getTime()) ? "Date unavailable" : formatPlayerDate(date);
}

function formatPlayerTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time unavailable";
  return `${formatPlayerDate(date)}, ${date.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  })} UTC`;
}

function legendDayKey(period: string): string {
  return period.split(" – ")[0].slice(0, 10);
}

function LegendDay({
  day,
  selectedDay,
}: {
  day: RankedDaySummary;
  selectedDay: string | null;
}) {
  const dayKey = legendDayKey(day.period);
  return (
    <details
      className="legend-day"
      id={`legend-day-${dayKey}`}
      open={selectedDay ? selectedDay === dayKey : day.state === "Live"}
    >
      <summary>
        <span className="legend-day-date">
          <strong>{legendDayDate(day.period)}</strong>
          <span className="legend-day-meta">
            <small>Day {day.dayNumber ?? "—"}</small>
            {day.state === "Live" ? <LiveBadge /> : null}
          </span>
        </span>
        <span className="legend-day-stat legend-day-start">
          <small>Starting trophies</small>
          <strong
            className={day.startTrophies == null ? "stat-unavailable" : undefined}
            title={day.startTrophiesCalculation
              ? `${day.startTrophiesCalculation.trophies.toLocaleString("en-GB")} − (${formatSigned(day.startTrophiesCalculation.netChange)}) = ${day.startTrophies?.toLocaleString("en-GB")}`
              : undefined}
          >
            {day.startTrophies == null ? "Unavailable" : day.startTrophies.toLocaleString("en-GB")}
          </strong>
          {day.startTrophiesCalculation ? <span className="legend-day-start-source">Calculated</span> : null}
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
        <BattleColumn title="Attacks" events={day.offenseEvents} day={dayKey} />
        <BattleColumn title="Defenses" events={day.defenseEvents} day={dayKey} />
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
}: {
  title: "Attacks" | "Defenses";
  events: RankedBattleEvent[];
  day: string;
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
                  aria-label={`View ${event.opponent.name ?? event.opponent.tag}'s Legend log for ${legendDayDate(day)}`}
                >
                  <strong>{event.opponent.name ?? event.opponent.tag}</strong>
                </a>
                <span className="player-tag">{event.opponent.tag}</span>
                <time dateTime={event.battleTimestamp}>
                  {new Date(event.battleTimestamp).toLocaleTimeString("en-GB", {
                    hour: "2-digit",
                    minute: "2-digit",
                    timeZone: "UTC",
                  })}{" "}
                  UTC
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
