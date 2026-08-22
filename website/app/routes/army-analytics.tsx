import { Form, Link, useLoaderData, type LoaderFunctionArgs } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import type { ArmyAnalytics, WebsiteErrorResponse } from "../lib/contracts";

const allowed = {
  lens: ["offense", "defense"],
  category: [
    "troops",
    "spells",
    "siege",
    "heroes",
    "pets",
    "equipment",
    "equipment-for-hero",
    "cc-troops",
    "hero-pet",
    "hero-equipment",
    "cc-composition",
  ],
  sort: [
    "usage-rate",
    "usage-count",
    "three-star-rate",
    "average-stars",
    "average-destruction",
  ],
} as const;

export async function loader({ request }: LoaderFunctionArgs): Promise<{
  analytics: ArmyAnalytics | null;
  error: WebsiteErrorResponse | null;
  seasonEmpty: { previousSeasonId: string | null } | null;
}> {
  const source = new URL(request.url).searchParams;
  const query = new URLSearchParams({
    season: source.get("season") ?? "current",
    lens: source.get("lens") ?? "offense",
    start_day: source.get("start_day") ?? "1",
    end_day: source.get("end_day") ?? "28",
    population: source.get("population") ?? "top-100",
    category: source.get("category") ?? "troops",
    sort: source.get("sort") ?? "usage-rate",
  });
  const python = await import("../services/python.server");
  try {
    return {
      analytics: await python.createPythonClient().getArmyAnalytics(query),
      error: null,
      seasonEmpty: null,
    };
  } catch (cause) {
    if (cause instanceof python.NoCompletedLegendDaysError) {
      // The current season has no completed Legend day; show the agreed empty
      // state and link to the previous season instead of serving its data.
      return {
        analytics: null,
        error: null,
        seasonEmpty: { previousSeasonId: cause.previousSeasonId },
      };
    }
    const { safeWebsiteError } = await import("../server/errors.server");
    return { analytics: null, error: safeWebsiteError(cause), seasonEmpty: null };
  }
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

export default function ArmyAnalyticsRoute() {
  const { analytics, error, seasonEmpty } = useLoaderData<typeof loader>();
  const selected = analytics?.selection;
  return (
    <main className="page-shell">
      <section className="hero" aria-labelledby="army-analytics-title">
        <h1 id="army-analytics-title">Army analytics</h1>
        <p>
          Completed Legend-day attacking-army evidence. Offense and defense remain
          separate.
        </p>
      </section>
      <Form method="get" className="search-panel" aria-label="Army analytics filters">
        <label>
          Lens
          <select name="lens" defaultValue={selected?.lens ?? "offense"}>
            {allowed.lens.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
        <label>
          Season
          <input name="season" defaultValue={selected?.season ?? "current"} required />
        </label>
        <label>
          Start Legend day
          <input
            name="start_day"
            type="number"
            min="1"
            max="28"
            defaultValue={selected?.startDay ?? 1}
          />
        </label>
        <label>
          End Legend day
          <input
            name="end_day"
            type="number"
            min="1"
            max="28"
            defaultValue={selected?.endDay ?? 28}
          />
        </label>
        <label>
          Population
          <input name="population" defaultValue={selected?.population ?? "top-100"} />
        </label>
        <label>
          Category
          <select name="category" defaultValue={selected?.category ?? "troops"}>
            {allowed.category.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
        <label>
          Sort
          <select name="sort" defaultValue={selected?.sort ?? "usage-rate"}>
            {allowed.sort.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
        <button type="submit">Apply</button>
      </Form>
      {error ? <ErrorNotice error={error} /> : null}
      {seasonEmpty ? (
        <section aria-live="polite">
          <p>No completed Legend days this season</p>
          {seasonEmpty.previousSeasonId ? (
            <Link
              to={`/analytics/armies?season=${encodeURIComponent(
                seasonEmpty.previousSeasonId,
              )}&lens=offense&start_day=1&end_day=28&population=top-100&category=troops&sort=usage-rate`}
            >
              View the previous season
            </Link>
          ) : null}
        </section>
      ) : null}
      {!analytics && !seasonEmpty ? (
        <p>No completed Legend-day army publication is available for this selection.</p>
      ) : null}
      {analytics ? (
        <>
          <section className="metric-grid" aria-label="Army evidence coverage">
            <article className="metric-card">
              <h2>Total attacks</h2>
              <strong>{analytics.totalAttacks}</strong>
            </article>
            <article className="metric-card">
              <h2>Usable army sample</h2>
              <strong>{analytics.usableArmySample}</strong>
            </article>
            <article className="metric-card">
              <h2>Unknown-affected attacks</h2>
              <strong>{analytics.unknownAffectedAttacks}</strong>
            </article>
          </section>
          <p>
            Legend Days {analytics.selection.startDay}–{analytics.selection.endDay} ·{" "}
            {analytics.selection.lens} · {analytics.selection.population}
          </p>
          <p>
            Army states reconcile:{" "}
            {Object.entries(analytics.armyStates)
              .map(([state, count]) => `${state} ${count}`)
              .join(", ")}
            . Perspective disagreements: {analytics.perspectiveDisagreementCount}. Unknown
            component occurrences: {analytics.unknownComponentOccurrences}.
          </p>
          <p>
            Collection coverage {analytics.collectionCoverage.state} ({""}
            {analytics.collectionCoverage.completedDays} days) · freshness{" "}
            {analytics.freshness.state} · attacks missing battle-time trophy evidence:{" "}
            {analytics.missingTrophyMembershipEvidence} · stale or uncertain cohort
            members: {analytics.cohortEvidence.staleOrUncertainCohortMembers}
            {analytics.cohortEvidence.streakExcludedPlayers > 0
              ? ` · streak-excluded players: ${analytics.cohortEvidence.streakExcludedPlayers}`
              : null}
            {analytics.cohortEvidence.shieldedPlayerDays > 0
              ? ` · shielded member-days: ${analytics.cohortEvidence.shieldedPlayerDays}`
              : null}
            . Snapshot versions:{" "}
            {analytics.reproducibility.snapshotVersions.join(", ") || "none"}.
          </p>
          <div className="table-scroll">
            <table className="data-table" aria-label="Army analytics results">
              <thead>
                <tr>
                  <th>Item or combination</th>
                  <th>Uses / denominator</th>
                  <th>Usage rate</th>
                  <th>0★</th>
                  <th>1★</th>
                  <th>2★</th>
                  <th>3★</th>
                  <th>Three-star rate</th>
                  <th>Average stars</th>
                  <th>Average destruction</th>
                </tr>
              </thead>
              <tbody>
                {analytics.rows.map((row) => (
                  <tr key={row.key}>
                    <th scope="row">{row.label}</th>
                    <td>
                      {row.usageCount} / {row.usageDenominator}
                    </td>
                    <td>{formatRate(row.usageRate)}</td>
                    {row.starCounts.map((count, index) => (
                      <td key={index}>
                        {count} ({formatRate(row.starRates[index])})
                      </td>
                    ))}
                    <td>{formatRate(row.threeStarRate)}</td>
                    <td>{row.averageStars.toFixed(2)}</td>
                    <td>{row.averageDestruction.toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p>
            <small>
              Publication {analytics.publicationIdentity} · decoder{" "}
              {analytics.versions.decoder} · catalog {analytics.versions.catalog} · rules{" "}
              {analytics.versions.analytics}
            </small>
          </p>
        </>
      ) : null}
      <Link to="/">Back to home</Link>
    </main>
  );
}

function formatRate(value: number) {
  return `${(value * 100).toFixed(1)}%`;
}
