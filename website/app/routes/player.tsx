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
  PlayerPage,
  RefreshError,
  RefreshStatus,
  RefreshWork,
  WebsiteErrorResponse,
} from "../lib/contracts";
import { isRefreshStatusPayload, isWebsiteErrorResponse } from "../lib/validation";

export interface PlayerLoaderData {
  player: PlayerPage | null;
  error: WebsiteErrorResponse | null;
  refreshStatus: RefreshStatus | null;
  refreshError: WebsiteErrorResponse | null;
  noJsIdempotencyKey: string;
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
    };
  }
  const canonicalPath = canonicalPlayerPath(normalizedTag);
  const url = new URL(request.url);
  if (url.pathname !== canonicalPath) {
    throw redirect(`${canonicalPath}${url.search}`, {
      status: 301,
      headers: { "Cache-Control": "no-store" },
    });
  }

  let player: PlayerPage | null = null;
  let error: WebsiteErrorResponse | null = null;
  try {
    const { createPythonClient } = await import("../services/python.server");
    player = await createPythonClient().getPlayer(normalizedTag);
  } catch (cause) {
    error = await safeError(cause);
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
  return { player, error, refreshStatus, refreshError, noJsIdempotencyKey };
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

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

      <section className="data-section" aria-labelledby="recent-days-title">
        <h2 id="recent-days-title">Legend season</h2>
        <div className="table-wrap">
          <table
            className="data-table compact-table responsive-table"
            aria-label="Legend season days"
          >
            <caption className="sr-only">
              Legend season days returned by the service
            </caption>
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col">Offense</th>
                <th scope="col">Defense</th>
                <th scope="col">Trophy change</th>
              </tr>
            </thead>
            <tbody>
              {player.recentDays.map((day) => (
                <tr key={`${day.period}-${day.dayNumber ?? "unknown"}`}>
                  <th scope="row" data-label="Day">
                    {day.dayNumber ?? "Unknown"}
                  </th>
                  <td data-label="Offense">
                    {day.offense.attacks === null
                      ? "Unknown"
                      : `${day.offense.attacks} attacks`}
                  </td>
                  <td data-label="Defense">
                    {day.defense.defenses === null
                      ? "Unknown"
                      : `${day.defense.defenses} defenses`}
                  </td>
                  <td data-label="Trophy change">{formatSigned(day.trophyChange)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </main>
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

function formatFraction(numerator: number | null, denominator: number | null): string {
  return numerator === null || denominator === null
    ? "Unknown"
    : `${numerator} / ${denominator}`;
}

async function safeError(cause: unknown): Promise<WebsiteErrorResponse> {
  const { safeWebsiteError } = await import("../server/errors.server");
  return safeWebsiteError(cause);
}
