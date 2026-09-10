import { useEffect, useState } from "react";
import {
  redirect,
  useFetcher,
  useLoaderData,
  useRevalidator,
  type LoaderFunctionArgs,
} from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { formatTimestamp } from "../components/Provenance";
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
        <main className="page-shell player-page">
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
            Current profile data is unavailable; showing the compact historical summary
            only. We did not invent a name or trophy count.
          </p>
          <SeasonNav
            tag={data.historical.tag}
            seasons={data.seasons}
            selectedSeason={data.selectedSeason}
          />
          <HistoricalSeasonPanel summary={data.historical} />
        </main>
      );
    }
    return (
      <main className="page-shell narrow-page">
        <h1>Player data unavailable</h1>
        {data.error ? <ErrorNotice error={data.error} /> : null}
        <p>We did not replace saved data with an invented result.</p>
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
    <main className="page-shell player-page">
      <header className="player-header">
        <div>
          <h1>{player.profile.name}</h1>
          <p className="player-identity">
            <span className="player-tag prominent">{player.tag}</span>
            <span>{player.profile.clan}</span>
          </p>
        </div>
        <div className="player-summary">
          <div className="player-trophy-card">
            <span className="metric-label">Trophies</span>
            <strong>{player.profile.trophies.toLocaleString()}</strong>
            <span className="metric-label">Last updated</span>
            <time
              className="player-updated"
              dateTime={player.profile.freshness.observedAt}
            >
              {formatTimestamp(player.profile.freshness.observedAt)}
            </time>
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
              Season {data.selectedSeason} is not available as a compact summary. We did
              not substitute live detail.
            </p>
          </section>
        )
      ) : null}

      {data.selectedSeason !== null && data.historical !== null ? null : (
        <>
          {player.currentDay === null ? (
            <section className="data-section" aria-labelledby="current-day-title">
              <div className="section-heading">
                <h2 id="current-day-title">Current Legend day</h2>
              </div>
              <p className="section-note">Current Legend day data is not available.</p>
            </section>
          ) : (
            <section className="data-section" aria-labelledby="current-day-title">
              <div className="section-heading">
                <h2 id="current-day-title">Current Legend day</h2>
              </div>
              <p className="section-note">
                Ranked day {player.currentDay!.dayNumber ?? "Unknown"} ·{" "}
                {player.currentDay!.period}
              </p>
              <div className="metric-grid">
                <MetricCard title="Offense">
                  <Metric
                    label="Attacks observed"
                    value={formatCount(player.currentDay!.offense.attacks)}
                  />
                  <Metric
                    label="Three-stars"
                    value={formatFraction(
                      player.currentDay!.offense.threeStars,
                      player.currentDay!.offense.attacks,
                    )}
                  />
                  <Metric
                    label="Trophy gain"
                    value={formatSigned(player.currentDay!.offense.trophyGain)}
                  />
                </MetricCard>
                <MetricCard title="Defense">
                  <Metric
                    label="Defenses observed"
                    value={formatCount(player.currentDay!.defense.defenses)}
                  />
                  <Metric
                    label="Three-stars against"
                    value={formatCount(player.currentDay!.defense.threeStarsAgainst)}
                  />
                  <Metric
                    label="Trophy loss"
                    value={
                      player.currentDay!.defense.trophyLoss === null
                        ? "Unknown"
                        : formatSigned(-player.currentDay!.defense.trophyLoss)
                    }
                  />
                </MetricCard>
                <MetricCard title="Trophy change">
                  <Metric
                    label="Net change"
                    value={formatSigned(player.currentDay!.trophyChange)}
                  />
                  <Metric
                    label="Season day"
                    value={
                      player.season
                        ? `${player.season.currentDayNumber} / ${player.season.dayCount}`
                        : "Unknown"
                    }
                  />
                </MetricCard>
              </div>
            </section>
          )}

          <section className="data-section" aria-labelledby="season-days-title">
            <h2 id="season-days-title">Legend season</h2>
            <div className="legend-days">
              {player.seasonDays.map((day) => (
                <LegendDay
                  key={`${day.period}-${day.dayNumber ?? "unknown"}`}
                  day={day}
                />
              ))}
            </div>
          </section>
        </>
      )}
    </main>
  );
}

function SeasonNav({
  tag,
  seasons,
  selectedSeason,
}: {
  tag: string;
  seasons: SummarizedSeasonRef[];
  selectedSeason: string | null;
}) {
  if (seasons.length === 0) return null;
  return (
    <nav className="data-section" aria-label="Historical seasons">
      <div className="section-heading">
        <h2>Historical seasons</h2>
      </div>
      <ul className="season-list">
        <li key="current">
          {selectedSeason === null ? (
            <strong aria-current="page">Current season</strong>
          ) : (
            <a href={canonicalPlayerPath(tag)}>Current season</a>
          )}
        </li>
        {seasons.map((season) => (
          <li key={season.seasonId}>
            {selectedSeason === season.seasonId ? (
              <strong aria-current="page">
                Season {season.seasonId} · {season.coverageState}
              </strong>
            ) : (
              <a
                href={`${canonicalPlayerPath(tag)}?season=${encodeURIComponent(season.seasonId)}`}
              >
                Season {season.seasonId} · {season.coverageState}
              </a>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}

function HistoricalSeasonPanel({ summary }: { summary: HistoricalSeasonSummary }) {
  return (
    <section className="data-section" aria-labelledby="historical-season-title">
      <div className="section-heading">
        <h2 id="historical-season-title">
          Season {summary.seasonId} · {summary.coverageState}
        </h2>
      </div>
      <p className="section-note">
        Compact historical summary · {summary.daysObserved} of 28 days
        {summary.daysMissing.length > 0
          ? ` · missing days ${summary.daysMissing.join(", ")}`
          : ""}
      </p>
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
      {summary.unresolvedFlags.length > 0 ? (
        <p className="section-note">Unresolved: {summary.unresolvedFlags.join("; ")}</p>
      ) : null}
      <table aria-label="Daily trophy totals">
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
            <th scope="col">State</th>
            <th scope="col">Coverage</th>
            <th scope="col">Flags</th>
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
              <td>{day.state}</td>
              <td>{day.coverage}</td>
              <td>{day.flags.length > 0 ? day.flags.join("; ") : "—"}</td>
              <td>{formatAdjustment(day)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function LegendDay({ day }: { day: RankedDaySummary }) {
  return (
    <details className="legend-day" open={day.state === "Live"}>
      <summary>
        <strong>Day {day.dayNumber ?? "Unknown"}</strong>
        <span>{day.state}</span>
        <span>{formatCount(day.offense.attacks)} attacks</span>
        <span>{formatCount(day.defense.defenses)} defenses</span>
        <span>{formatSigned(day.trophyChange)} trophies</span>
      </summary>
      <div className="battle-columns">
        <BattleColumn title="Attacks" events={day.offenseEvents} />
        <BattleColumn title="Defenses" events={day.defenseEvents} />
      </div>
    </details>
  );
}

function BattleColumn({
  title,
  events,
}: {
  title: "Attacks" | "Defenses";
  events: RankedBattleEvent[];
}) {
  return (
    <section className="battle-column" aria-label={title}>
      <h3>{title}</h3>
      <ol className="battle-slots">
        {Array.from({ length: 8 }, (_, index) => {
          const event = events[index];
          return event ? (
            <li className="battle-slot" key={event.battleId}>
              <div className="battle-opponent">
                <strong>{event.opponent.name ?? event.opponent.tag}</strong>
                <span className="player-tag">{event.opponent.tag}</span>
              </div>
              <time dateTime={event.battleTimestamp}>
                {formatTimestamp(event.battleTimestamp)}
              </time>
              <span>{event.stars} ★</span>
              <span>{event.destructionPercentage}%</span>
              <strong>{formatSigned(event.trophyChange)}</strong>
              {event.perspectiveDisagreement ? (
                <span className="battle-disagreement">Perspective disagreement</span>
              ) : null}
              {event.army ? <BattleArmyDetails army={event.army} /> : null}
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

function BattleArmyDetails({ army }: { army: RankedBattleEvent["army"] }) {
  if (!army) return null;
  return (
    <details className="battle-army">
      <summary>Attacking army · {army.state}</summary>
      {army.failureReason ? <p>{army.failureReason}</p> : null}
      <ul>
        {army.components.map((component, index) => (
          <li key={`${component.typedId}-${component.origin}-${index}`}>
            {component.name} ×{component.quantity} <small>({component.origin})</small>
          </li>
        ))}
        {army.unknownComponents.map((component, index) => (
          <li key={`unknown-${component.section}-${component.numericId}-${index}`}>
            Unknown ID {component.numericId} ×{component.quantity}{" "}
            <small>
              ({component.section}, {component.origin})
            </small>
          </li>
        ))}
      </ul>
    </details>
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

function formatCount(value: number | null): string {
  return value === null ? "Unknown" : String(value);
}

function formatAdjustment(day: HistoricalSeasonDayEntry): string {
  if (!day.hasAdjustment) return "No";
  if (day.adjustmentTotal === null) return "Yes · unknown amount";
  return `Yes · ${formatSigned(day.adjustmentTotal)}`;
}

function formatFraction(numerator: number | null, denominator: number | null): string {
  return numerator === null || denominator === null
    ? "Unknown"
    : `${numerator} / ${denominator}`;
}

async function safeError(cause: unknown): Promise<WebsiteErrorResponse> {
  const { safeWebsiteError } = await import("../server/errors.server");
  return safeWebsiteError(cause);
}
