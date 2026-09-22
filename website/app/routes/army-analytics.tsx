import {
  data,
  Form,
  Link,
  useLoaderData,
  useSearchParams,
  useNavigation,
  type LoaderFunctionArgs,
} from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import type { WebsiteErrorResponse } from "../lib/contracts";

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

export async function loader({ request }: LoaderFunctionArgs) {
  const source = new URL(request.url).searchParams;
  const season = source.get("season") ?? "current";
  const lens = source.get("lens") ?? "offense";
  const category = source.get("category") ?? "troops";
  const sort = source.get("sort") ?? "usage-rate";
  const python = await import("../services/python.server");
  try {
    if (season !== "current") {
      const summaryQuery = new URLSearchParams({ lens, category, sort });
      if (source.has("offset")) summaryQuery.set("offset", source.get("offset")!);
      return {
        analytics: await python
          .createPythonClient()
          .getArmySeasonSummary(season, summaryQuery),
        error: null,
        seasonEmpty: null,
        historicalSummary: true,
        requestedSeason: season,
      };
    }
    const query = new URLSearchParams({
      season,
      lens,
      start_day: source.get("start_day") ?? "1",
      end_day: source.get("end_day") ?? "28",
      population: source.get("population") ?? "top-100",
      category,
      sort,
    });
    return {
      analytics: await python.createPythonClient().getArmyAnalytics(query),
      error: null,
      seasonEmpty: null,
      historicalSummary: false,
      requestedSeason: season,
    };
  } catch (cause) {
    if (cause instanceof python.NoCompletedLegendDaysError) {
      // The current season has no completed Legend day; show the agreed empty
      // state and link to the previous season instead of serving its data.
      return data(
        {
          analytics: null,
          error: null,
          seasonEmpty: { previousSeasonId: cause.previousSeasonId },
          historicalSummary: false,
          requestedSeason: season,
        },
        { status: 404 },
      );
    }
    const { safeWebsiteError } = await import("../server/errors.server");
    const pythonError = cause as { status?: unknown; payload?: unknown };
    if (
      (pythonError.status === 404 || pythonError.status === 422) &&
      typeof pythonError.status === "number"
    ) {
      const payload = pythonError.payload;
      const payloadError =
        typeof payload === "object" && payload !== null
          ? (payload as { error?: unknown }).error
          : undefined;
      const error: WebsiteErrorResponse =
        payloadError === "invalid_army_analytics_selection" ||
        payloadError === "invalid_request"
          ? {
              error: {
                code: "invalid_input",
                message: "Check the army analytics selection.",
              },
            }
          : payloadError === "army_analytics_unavailable"
            ? {
                error: {
                  code: "unavailable",
                  message: "Army analytics are unavailable for the selected Legend days.",
                },
              }
            : safeWebsiteError(cause);
      if (
        typeof payload === "object" &&
        payload !== null &&
        Array.isArray((payload as { affected_days?: unknown }).affected_days) &&
        (payload as { affected_days: unknown[] }).affected_days.every((day) =>
          Number.isSafeInteger(day),
        )
      ) {
        error.error.affectedDays = (payload as { affected_days: number[] }).affected_days;
      }
      return data(
        {
          analytics: null,
          error,
          selectionUnavailable: payloadError === "army_analytics_unavailable",
          seasonEmpty: null,
          historicalSummary: season !== "current",
          requestedSeason: season,
        },
        { status: pythonError.status },
      );
    }
    return {
      analytics: null,
      error: safeWebsiteError(cause),
      seasonEmpty: null,
      historicalSummary: false,
      requestedSeason: season,
    };
  }
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

const filterLabels: Record<string, string> = {
  troops: "Troops",
  spells: "Spells",
  siege: "Siege machines",
  heroes: "Heroes",
  pets: "Hero pets",
  equipment: "Hero equipment",
  "equipment-for-hero": "Equipment by hero",
  "cc-troops": "Clan Castle troops",
  "hero-pet": "Hero and pet",
  "hero-equipment": "Hero and equipment",
  "cc-composition": "Clan Castle army",
  "usage-rate": "Most used",
  "usage-count": "Total uses",
  "three-star-rate": "Three-star rate",
  "average-stars": "Average stars",
  "average-destruction": "Average destruction",
};
const topPlayers = [5, 10, 20, 50, 100, 200, 500, 1000];

export default function ArmyAnalyticsRoute() {
  const result = useLoaderData<typeof loader>();
  const { analytics, error, seasonEmpty, historicalSummary, requestedSeason } = result;
  const [params] = useSearchParams();
  const navigation = useNavigation();
  const selected = analytics?.selection;
  const isHistorical = historicalSummary === true;
  const lens = selected?.lens ?? params.get("lens") ?? "offense";
  const population = selected?.population ?? params.get("population") ?? "top-100";
  const unavailable = "selectionUnavailable" in result && result.selectionUnavailable;
  return (
    <main id="main-content" tabIndex={-1} className="page-shell analytics-page">
      <section className="hero" aria-labelledby="army-analytics-title">
        <h1 id="army-analytics-title">Army analytics</h1>
        <p className="hero-copy">
          Compare usage and battle results across tracked Legend League armies.
        </p>
      </section>
      <Form
        method="get"
        className="search-panel analytics-filters"
        aria-label="Army analytics filters"
        key={params.toString()}
      >
        <div className="filter-heading">
          <fieldset className="segmented-control">
            <legend className="sr-only">Battle perspective</legend>
            {allowed.lens.map((value) => (
              <label key={value}>
                <input
                  className="sr-only"
                  type="radio"
                  name="lens"
                  value={value}
                  defaultChecked={lens === value}
                />
                <span>{value === "offense" ? "Attacks" : "Defenses"}</span>
              </label>
            ))}
          </fieldset>
          <span className="analytics-season">
            {isHistorical ? "Past season" : "Current season"}
          </span>
        </div>
        <div className="filter-grid">
          <label className="filter-field">
            Show
            <select
              name="category"
              defaultValue={selected?.category ?? params.get("category") ?? "troops"}
            >
              {allowed.category
                .filter(
                  (value) =>
                    !isHistorical ||
                    ["troops", "spells", "siege", "heroes", "pets", "equipment"].includes(
                      value,
                    ),
                )
                .map((value) => (
                  <option key={value} value={value}>
                    {filterLabels[value]}
                  </option>
                ))}
            </select>
          </label>
          <label className="filter-field">
            Players
            <select
              name="population"
              defaultValue={isHistorical ? "all" : population}
              disabled={isHistorical}
            >
              {isHistorical ? <option value="all">All players</option> : null}
              {!isHistorical &&
              !topPlayers.some(
                (count) =>
                  population === `top-${count}` || population === `streak-top-${count}`,
              ) ? (
                <option value={population}>Selected player group</option>
              ) : null}
              <optgroup label="Leaderboard position">
                {topPlayers.map((count) => (
                  <option key={count} value={`top-${count}`}>
                    Top {count.toLocaleString()}
                  </option>
                ))}
              </optgroup>
              <optgroup label="Top players on every selected day">
                {topPlayers.map((count) => (
                  <option key={count} value={`streak-top-${count}`}>
                    Consistent top {count.toLocaleString()}
                  </option>
                ))}
              </optgroup>
            </select>
          </label>
          <label className="filter-field">
            Sort by
            <select
              name="sort"
              defaultValue={selected?.sort ?? params.get("sort") ?? "usage-rate"}
            >
              {allowed.sort
                .filter(
                  (value) =>
                    !isHistorical || ["usage-rate", "usage-count"].includes(value),
                )
                .map((value) => (
                  <option key={value} value={value}>
                    {filterLabels[value]}
                  </option>
                ))}
            </select>
          </label>
        </div>
        <div className="filter-footer">
          <details className="filter-details">
            <summary>Season & day range</summary>
            <div className="filter-grid">
              <label className="filter-field">
                Season
                <select name="season" defaultValue={requestedSeason}>
                  <option value="current">Current season</option>
                  {requestedSeason !== "current" ? (
                    <option value={requestedSeason}>{seasonName(requestedSeason)}</option>
                  ) : null}
                  {seasonEmpty?.previousSeasonId &&
                  seasonEmpty.previousSeasonId !== requestedSeason ? (
                    <option value={seasonEmpty.previousSeasonId}>
                      {seasonName(seasonEmpty.previousSeasonId)}
                    </option>
                  ) : null}
                </select>
              </label>
              <label className="filter-field">
                From Legend day
                <input
                  name="start_day"
                  type="number"
                  min="1"
                  max="28"
                  defaultValue={selected?.startDay ?? params.get("start_day") ?? 1}
                  disabled={isHistorical}
                />
              </label>
              <label className="filter-field">
                To Legend day
                <input
                  name="end_day"
                  type="number"
                  min="1"
                  max="28"
                  defaultValue={selected?.endDay ?? params.get("end_day") ?? 28}
                  disabled={isHistorical}
                />
              </label>
            </div>
            <p className="form-help">
              {isHistorical
                ? "Past seasons include all players across all 28 Legend days."
                : "Only completed Legend days are included. Each day starts at 05:00 UTC."}
            </p>
          </details>
          <button
            className="button button-primary"
            type="submit"
            disabled={navigation.state !== "idle"}
          >
            {navigation.state !== "idle" ? "Updating…" : "Apply filters"}
          </button>
        </div>
      </Form>
      {error && !unavailable ? <ErrorNotice error={error} /> : null}
      {seasonEmpty || unavailable || (!analytics && !error) ? (
        <section className="analytics-empty" aria-live="polite">
          <h2>
            {seasonEmpty
              ? "A new season is underway"
              : "No army stats for these days yet"}
          </h2>
          <p>
            {seasonEmpty
              ? "Stats will appear after the first Legend day is complete."
              : "We don’t have the complete daily records needed for this selection. Try a different day range or player group."}
          </p>
          {seasonEmpty?.previousSeasonId ? (
            <Link
              className="button button-secondary"
              to={`?season=${encodeURIComponent(seasonEmpty.previousSeasonId)}&lens=offense&category=troops&sort=usage-rate`}
            >
              View previous season
            </Link>
          ) : null}
        </section>
      ) : null}
      {analytics ? (
        <section className="analytics-results" aria-label="Army statistics">
          <div className="analytics-kpis" aria-label="Selected sample">
            <article className="analytics-kpi analytics-kpi-primary">
              <span>Attacks in sample</span>
              <strong>{analytics.totalAttacks.toLocaleString()}</strong>
            </article>
            <article className="analytics-kpi">
              <span>Armies analyzed</span>
              <strong>{analytics.usableArmySample.toLocaleString()}</strong>
            </article>
            <article className="analytics-kpi">
              <span>Details unavailable</span>
              <strong>{analytics.unknownAffectedAttacks.toLocaleString()}</strong>
            </article>
          </div>
          <div className="section-heading">
            <h2>{filterLabels[analytics.selection.category]}</h2>
            <span className="section-note">
              Days {analytics.selection.startDay}–{analytics.selection.endDay} ·{" "}
              {analytics.selection.lens === "offense" ? "Attacks" : "Defenses"}
            </span>
          </div>
          <div className="table-wrap analytics-table-wrap">
            <table className="data-table analytics-table" aria-label="Army analytics results">
              <thead>
                <tr>
                  <th>{isHistorical ? "Unit" : "Army component"}</th>
                  {isHistorical ? (
                    <>
                      <th>Quantity</th>
                      <th>1★</th>
                      <th>2★</th>
                      <th>3★</th>
                    </>
                  ) : null}
                  <th>Uses / sample</th>
                  <th>Usage</th>
                  {!isHistorical ? (
                    <>
                      <th>3-star rate</th>
                      <th>Avg. stars</th>
                      <th>Avg. destruction</th>
                    </>
                  ) : null}
                </tr>
              </thead>
              <tbody>
                {analytics.rows.map((row) => (
                  <tr key={row.key}>
                    <th scope="row">{row.label}</th>
                    {isHistorical ? (
                      <>
                        <td>{row.quantity}</td>
                        <td>{row.oneStarCount}</td>
                        <td>{row.twoStarCount}</td>
                        <td>{row.threeStarCount}</td>
                      </>
                    ) : null}
                    <td>
                      {row.usageCount} / {row.usageDenominator}
                    </td>
                    <td>{formatRate(row.usageRate)}</td>
                    {!isHistorical ? (
                      <>
                        <td>{formatRate(row.threeStarRate!)}</td>
                        <td>{row.averageStars!.toFixed(2)}</td>
                        <td>{row.averageDestruction!.toFixed(1)}%</td>
                      </>
                    ) : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!isHistorical ? (
            <details className="analytics-breakdown">
              <summary>Star breakdown & sample coverage</summary>
              <p className="section-note">
                {analytics.collectionCoverage.completedDays} completed days.{" "}
                {analytics.perspectiveDisagreementCount} battles have conflicting reports.{" "}
                {analytics.unknownAffectedAttacks} attacks have incomplete army details.
              </p>
              <div className="table-wrap analytics-table-wrap">
                <table className="data-table analytics-table" aria-label="Army star breakdown">
                  <thead>
                    <tr>
                      <th>Army component</th>
                      <th>0★</th>
                      <th>1★</th>
                      <th>2★</th>
                      <th>3★</th>
                      <th>Excluded attacks</th>
                    </tr>
                  </thead>
                  <tbody>
                    {analytics.rows.map((row) => (
                      <tr key={row.key}>
                        <th scope="row">{row.label}</th>
                        {row.starCounts?.map((count, index) => (
                          <td key={index}>
                            {count} ({formatRate(row.starRates![index])})
                          </td>
                        ))}
                        <td>{row.unknownExcludedAttacks}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          ) : null}
          {analytics.pagination ? (
            <div className="hero-actions">
              <span>
                Showing {analytics.rows.length} of {analytics.pagination.totalRows}{" "}
                results
              </span>
              {analytics.pagination.nextOffset !== null ? (
                <Link
                  className="button button-secondary"
                  to={`?${new URLSearchParams({ season: requestedSeason, lens: analytics.selection.lens, category: analytics.selection.category, sort: analytics.selection.sort, offset: String(analytics.pagination.nextOffset) })}`}
                >
                  Next results →
                </Link>
              ) : null}
            </div>
          ) : null}
        </section>
      ) : null}
    </main>
  );
}

function seasonName(seasonId: string) {
  const end = new Date((Number(seasonId) + 28 * 86400) * 1000);
  return Number.isNaN(end.getTime())
    ? "Past season"
    : end.toLocaleDateString("en-GB", {
        day: "numeric",
        month: "short",
        year: "numeric",
        timeZone: "UTC",
      });
}

function formatRate(value: number) {
  return `${(value * 100).toFixed(1)}%`;
}
