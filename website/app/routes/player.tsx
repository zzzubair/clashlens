import { useEffect, useState } from "react";
import {
  Link,
  redirect,
  useFetcher,
  useLoaderData,
  useRevalidator,
  type LoaderFunctionArgs,
} from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { FreshnessText, Provenance, formatTimestamp } from "../components/Provenance";
import { StateBadge } from "../components/StateBadge";
import type { StateValue } from "../components/StateBadge";
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
        <Link className="back-link" to="/">
          ← Back to tracked players
        </Link>
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
      <Link className="back-link" to="/">
        ← Back to tracked players
      </Link>
      <header className="player-header">
        <div>
          <p className="eyebrow">Public player page</p>
          <h1>{player.profile.name}</h1>
          <p className="player-identity">
            <span className="player-tag prominent">{player.tag}</span>
            <span>{player.profile.clan}</span>
          </p>
        </div>
        <div className="player-trophy-card">
          <span className="metric-label">Saved trophies</span>
          <strong>{player.profile.trophies.toLocaleString()}</strong>
          <StateBadge state={player.profile.freshness.state} />
        </div>
      </header>

      <section className="saved-data-panel" aria-labelledby="saved-data-title">
        <div>
          <h2 id="saved-data-title">Saved profile data</h2>
          <p>
            Last saved observation:{" "}
            <time dateTime={player.profile.freshness.observedAt}>
              {formatTimestamp(player.profile.freshness.observedAt)}
            </time>{" "}
            (<FreshnessText freshness={player.profile.freshness} />
            ).
          </p>
        </div>
        <refreshFetcher.Form action={refreshActionPath} method="post">
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
                { idempotencyKey: globalThis.crypto.randomUUID() },
                { method: "post", action: refreshActionPath },
              );
            }}
          >
            {refreshFetcher.state === "submitting"
              ? "Requesting refresh…"
              : "Refresh public data"}
          </button>
        </refreshFetcher.Form>
      </section>

      {visibleRefreshError ? <ErrorNotice error={visibleRefreshError} /> : null}
      {visibleStatus ? <RefreshProgress status={visibleStatus} /> : null}

      {player.currentDay === null ? (
        <section className="data-section" aria-labelledby="current-day-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Ranked-day data quality</p>
              <h2 id="current-day-title">Current Legend day</h2>
            </div>
            <StateBadge state="unavailable" />
          </div>
          <p className="section-note">
            No ranked-day publication is available for this accepted profile. Clash Lens
            does not fabricate a day.
          </p>
        </section>
      ) : (
        <section className="data-section" aria-labelledby="current-day-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Season {player.season?.id ?? "Unknown"}</p>
              <h2 id="current-day-title">Current Legend day</h2>
            </div>
            <StateBadge state={dayStateBadge(player.currentDay!.state)} />
          </div>
          <p className="section-note">
            Ranked day {player.currentDay!.dayNumber ?? "Unknown"} ·{" "}
            {player.currentDay!.period} · day state is{" "}
            <strong>{player.currentDay!.state}</strong> until it can be reconciled.
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
                label="Completeness"
                value={capitalize(player.currentDay!.completeness.state)}
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
          <div className="quality-panel">
            <h3>Completeness and uncertainty</h3>
            <p>
              Completeness: <StateBadge state={player.currentDay!.completeness.state} />{" "}
              {player.currentDay!.completeness.reason}
            </p>
            <ul>
              {player.currentDay!.uncertainty.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        </section>
      )}

      <section className="data-section" aria-labelledby="quality-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Trust and provenance</p>
            <h2 id="quality-title">Data quality</h2>
          </div>
        </div>
        <ul className="quality-list">
          {player.dataQuality.map((item) => (
            <li key={item.code}>
              <StateBadge state={item.code === "partial" ? "partial" : item.code} />
              <div>
                <strong>{item.label}</strong>
                <p>{item.detail}</p>
              </div>
            </li>
          ))}
        </ul>
        <Provenance provenance={player.provenance} />
      </section>

      <section className="data-section" aria-labelledby="recent-days-title">
        <h2 id="recent-days-title">Recent Legend days</h2>
        <div className="table-wrap">
          <table className="data-table compact-table">
            <caption className="sr-only">Recent ranked day summaries</caption>
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col">State</th>
                <th scope="col">Offense</th>
                <th scope="col">Defense</th>
                <th scope="col">Trophy change</th>
              </tr>
            </thead>
            <tbody>
              {player.recentDays.map((day) => (
                <tr key={`${day.period}-${day.dayNumber ?? "unknown"}`}>
                  <th scope="row">{day.dayNumber ?? "Unknown"}</th>
                  <td>
                    <StateBadge state={dayStateBadge(day.state)} />
                  </td>
                  <td>
                    {day.offense.attacks === null
                      ? "Unknown"
                      : `${day.offense.attacks} attacks`}
                  </td>
                  <td>
                    {day.defense.defenses === null
                      ? "Unknown"
                      : `${day.defense.defenses} defenses`}
                  </td>
                  <td>{formatSigned(day.trophyChange)}</td>
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
  return (
    <section className="refresh-panel" aria-live="polite" aria-labelledby="refresh-title">
      <h2 id="refresh-title">Public refresh</h2>
      <p role="status">
        Refresh status: <strong>{status.state}</strong>
      </p>
      <progress aria-label="Refresh progress" value={status.progressPercent} max="100">
        {status.progressPercent}%
      </progress>
      <p data-testid="refresh-work-id">Work ID: {status.workId}</p>
      <p>{status.message}</p>
      {status.state === "queued" || status.state === "running" ? (
        <p>Saved data remains visible while refresh runs.</p>
      ) : null}
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

function capitalize(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function dayStateBadge(
  value: NonNullable<PlayerPage["currentDay"]>["state"],
): StateValue {
  switch (value) {
    case "Live":
      return "live";
    case "Complete":
      return "complete";
    case "Partial":
      return "partial";
    case "Uncertain":
      return "uncertain";
  }
}

async function safeError(cause: unknown): Promise<WebsiteErrorResponse> {
  const { safeWebsiteError } = await import("../server/errors.server");
  return safeWebsiteError(cause);
}
