import { memo, useEffect, useRef, useState } from "react";
import {
  data,
  Form,
  Link,
  redirect,
  useLoaderData,
  useSearchParams,
  useNavigation,
  useSubmit,
  type LoaderFunctionArgs,
} from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { LocalTimestamp } from "../components/Provenance";
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
} as const;

export async function loader({ request }: LoaderFunctionArgs) {
  const source = new URL(request.url).searchParams;
  if ((source.get("category") ?? "troops") === "troops" && source.get("cc") === "1") {
    source.set("category", "cc-troops");
  }
  const recentAvailable =
    import.meta.env.DEV || process.env.CLASHLENS_ARMY_PREVIEW === "true";
  if (
    recentAvailable &&
    source.get("saved") !== "1" &&
    (source.get("recent") === "1" || source.has("sample") || !source.has("season"))
  ) {
    if (source.get("sample") === "1") {
      for (const key of ["sample", "season", "start_day", "end_day"]) source.delete(key);
      source.set("recent", "1");
      return redirect(`?${source}`);
    }
    const { recentArmyAnalytics } = await import("../services/army-preview.server");
    const recent = source.has("sample") ? null : recentArmyAnalytics(source);
    if (recent === null) {
      return data(
        {
          recentAvailable,
          analytics: null,
          error: {
            error: {
              code: "invalid_input",
              message: "Check the army analytics selection.",
            },
          } satisfies WebsiteErrorResponse,
          seasonEmpty: null,
          historicalSummary: false,
          requestedSeason: "current",
        },
        { status: 422 },
      );
    }
    return {
      ...recent,
      recentAvailable,
      error: null,
      seasonEmpty: null,
      historicalSummary: false,
      requestedSeason: "current",
    };
  }
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
        recentAvailable,
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
      recentAvailable,
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
          recentAvailable,
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
          recentAvailable,
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
      recentAvailable,
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
};
const topPlayers = [5, 10, 20, 50, 100, 200, 500, 1000];

type ArmyRow = ArmyAnalytics["rows"][number];
const sortColumns = {
  name: { label: "Name", value: (row: ArmyRow) => row.label },
  quantity: { label: "Quantity", value: (row: ArmyRow) => row.quantity ?? 0 },
  "usage-count": { label: "Battles / included", value: (row: ArmyRow) => row.usageCount },
  "usage-rate": { label: "Usage", value: (row: ArmyRow) => row.usageRate },
  "one-star-rate": { label: "1-star", value: (row: ArmyRow) => starRate(row, 1) },
  "two-star-rate": { label: "2-star", value: (row: ArmyRow) => starRate(row, 2) },
  "three-star-rate": { label: "3-star", value: (row: ArmyRow) => starRate(row, 3) },
  "average-stars": {
    label: "Avg. stars",
    value: (row: ArmyRow) => row.averageStars ?? 0,
  },
  "average-destruction": {
    label: "Avg. destruction",
    value: (row: ArmyRow) => row.averageDestruction ?? 0,
  },
  "zero-star-rate": { label: "0-star", value: (row: ArmyRow) => starRate(row, 0) },
  exclusions: {
    label: "Additional exclusions",
    value: (row: ArmyRow) => row.unknownExcludedAttacks ?? 0,
  },
} as const;
type SortColumn = keyof typeof sortColumns;
type TableSort = { column: SortColumn; direction: "ascending" | "descending" };
const nameOrder = new Intl.Collator("en", { sensitivity: "base", numeric: true });

function starRate(row: ArmyRow, stars: number) {
  const count =
    row.starCounts?.[stars] ??
    [0, row.oneStarCount, row.twoStarCount, row.threeStarCount][stars] ??
    0;
  return row.usageCount ? count / row.usageCount : 0;
}

function sortedRows(rows: ArmyRow[], sort: TableSort) {
  const value = sortColumns[sort.column].value;
  return [...rows].sort((a, b) => {
    const left = value(a);
    const right = value(b);
    const comparison =
      typeof left === "string" && typeof right === "string"
        ? nameOrder.compare(left, right)
        : Number(left) - Number(right);
    return (
      comparison * (sort.direction === "ascending" ? 1 : -1) ||
      nameOrder.compare(a.label, b.label) ||
      a.key.localeCompare(b.key)
    );
  });
}

function SortHeading({
  column,
  sort,
  onSort,
}: {
  column: SortColumn;
  sort: TableSort;
  onSort: (sort: TableSort) => void;
}) {
  const active = sort.column === column;
  const direction = active
    ? sort.direction === "ascending"
      ? "descending"
      : "ascending"
    : column === "name"
      ? "ascending"
      : "descending";
  const label = sortColumns[column].label;
  const order =
    column === "name"
      ? direction === "ascending"
        ? "A to Z"
        : "Z to A"
      : direction === "ascending"
        ? "lowest first"
        : "highest first";
  return (
    <th
      scope="col"
      aria-sort={active ? sort.direction : undefined}
      className="analytics-sort-heading"
    >
      <button
        type="button"
        className="analytics-sort-button"
        aria-label={`Sort by ${label}, ${order}`}
        title={`Sort by ${label}, ${order}`}
        onClick={() => onSort({ column, direction })}
      >
        {label}
        <span aria-hidden="true">
          {active ? (sort.direction === "ascending" ? "↑" : "↓") : "↕"}
        </span>
      </button>
    </th>
  );
}

const ArmyResultRow = memo(function ArmyResultRow({
  row,
  isHistorical,
}: {
  row: ArmyRow;
  isHistorical: boolean;
}) {
  return (
    <tr>
      <th scope="row">{row.label}</th>
      {isHistorical ? <td>{row.quantity}</td> : null}
      <td>
        {row.usageCount.toLocaleString()} / {row.usageDenominator.toLocaleString()}
      </td>
      <td>{formatRate(row.usageRate)}</td>
      {[1, 2, 3].map((stars) => {
        const count =
          row.starCounts?.[stars] ??
          [row.oneStarCount, row.twoStarCount, row.threeStarCount][stars - 1] ??
          0;
        return (
          <td key={stars}>
            <strong className="analytics-star-rate">
              {formatRate(row.usageCount ? count / row.usageCount : 0)}
            </strong>
            <span className="analytics-star-count">
              {count.toLocaleString()} {count === 1 ? "battle" : "battles"}
            </span>
          </td>
        );
      })}
      {!isHistorical ? (
        <>
          <td>{row.averageStars!.toFixed(2)}</td>
          <td>{row.averageDestruction!.toFixed(1)}%</td>
        </>
      ) : null}
    </tr>
  );
});

const ArmyBreakdownRow = memo(function ArmyBreakdownRow({ row }: { row: ArmyRow }) {
  return (
    <tr>
      <th scope="row">{row.label}</th>
      {row.starCounts?.map((count, index) => (
        <td key={index}>
          {count} ({formatRate(row.starRates![index])})
        </td>
      ))}
      <td>{row.unknownExcludedAttacks}</td>
    </tr>
  );
});

export default function ArmyAnalyticsRoute() {
  const result = useLoaderData<typeof loader>();
  const { analytics, error, seasonEmpty, historicalSummary, requestedSeason } = result;
  const [params] = useSearchParams();
  const navigation = useNavigation();
  const submit = useSubmit();
  const filterForm = useRef<HTMLFormElement>(null);
  const pendingChange = useRef<ReturnType<typeof setTimeout> | null>(null);
  const selected = analytics?.selection;
  const isHistorical = historicalSummary === true;
  const lens = selected?.lens ?? params.get("lens") ?? "offense";
  const population = selected?.population ?? params.get("population") ?? "top-100";
  const category =
    selected?.category ??
    ((params.get("category") ?? "troops") === "troops" && params.get("cc") === "1"
      ? "cc-troops"
      : params.get("category")) ??
    "troops";
  const showCategory = category === "cc-troops" ? "troops" : category;
  const clanCastle = category === "cc-troops";
  const unavailable = "selectionUnavailable" in result && result.selectionUnavailable;
  const snapshot = "snapshot" in result ? result.snapshot : null;
  const unreadableArmyRecords = analytics
    ? analytics.totalAttacks - analytics.usableArmySample
    : 0;
  const recordedBattleRecords = analytics
    ? analytics.totalAttacks + (snapshot?.invalidBattleRows ?? 0)
    : 0;
  const excludedBattleRecords = analytics
    ? recordedBattleRecords - analytics.usableArmySample
    : 0;
  const partialArmies = analytics?.armyStates.partial ?? 0;
  const [chosenSort, setChosenSort] = useState<TableSort | null>(null);
  const [breakdownSort, setBreakdownSort] = useState<TableSort | null>(null);
  const defaultColumn =
    selected?.sort && Object.hasOwn(sortColumns, selected.sort)
      ? (selected.sort as SortColumn)
      : "usage-rate";
  const columns: SortColumn[] = [
    "name",
    ...(isHistorical ? ["quantity" as const] : []),
    "usage-count",
    "usage-rate",
    "one-star-rate",
    "two-star-rate",
    "three-star-rate",
    ...(!isHistorical ? ["average-stars" as const, "average-destruction" as const] : []),
  ];
  const tableSort: TableSort =
    chosenSort && columns.includes(chosenSort.column)
      ? chosenSort
      : { column: defaultColumn, direction: "descending" };
  const rows = analytics ? sortedRows(analytics.rows, tableSort) : [];

  useEffect(() => {
    return () => {
      if (pendingChange.current !== null) clearTimeout(pendingChange.current);
    };
  }, []);

  // Leaving through a link must cancel a day edit that is still waiting.
  useEffect(() => {
    if (
      navigation.state !== "idle" &&
      !navigation.formData &&
      pendingChange.current !== null
    ) {
      clearTimeout(pendingChange.current);
      pendingChange.current = null;
    }
  }, [navigation.state, navigation.formData]);

  // Update existing controls after URL navigation without replacing the form,
  // which would lose keyboard focus and close the day-range disclosure.
  useEffect(() => {
    if (
      !filterForm.current ||
      navigation.state !== "idle" ||
      pendingChange.current !== null
    )
      return;
    const values = {
      lens,
      population: isHistorical ? "all" : population,
      category: showCategory,
      sort: selected?.sort ?? params.get("sort") ?? "usage-rate",
      season: requestedSeason,
      start_day: String(selected?.startDay ?? params.get("start_day") ?? 1),
      end_day: String(selected?.endDay ?? params.get("end_day") ?? 28),
    };
    for (const [name, value] of Object.entries(values)) {
      const control = filterForm.current.elements.namedItem(name);
      if (
        control instanceof RadioNodeList ||
        control instanceof HTMLInputElement ||
        control instanceof HTMLSelectElement
      ) {
        control.value = value;
      }
    }
    const ccToggle = filterForm.current.elements.namedItem("cc");
    if (ccToggle instanceof HTMLInputElement) ccToggle.checked = clanCastle;
  }, [
    params,
    selected,
    lens,
    population,
    showCategory,
    clanCastle,
    isHistorical,
    requestedSeason,
    navigation.state,
  ]);

  return (
    <main id="main-content" tabIndex={-1} className="page-shell analytics-page">
      <section className="hero" aria-labelledby="army-analytics-title">
        <h1 id="army-analytics-title">Army analytics</h1>
        <p className="hero-copy">
          Compare usage and battle results across tracked Legend League armies.
        </p>
      </section>
      {snapshot ? (
        <aside className="notice sample-data-notice" aria-label="Real battle data">
          <div>
            <strong>Real battle data.</strong> Recent Legend battles from the top{" "}
            {snapshot.playerCount} players.
            <br />
            Fetched <LocalTimestamp value={snapshot.fetchedAt} />. This is a saved
            snapshot; daily coverage is incomplete.
          </div>
          <Link to="?saved=1">Completed-day stats</Link>
        </aside>
      ) : null}
      <Form
        method="get"
        ref={filterForm}
        replace
        preventScrollReset
        className="search-panel analytics-filters"
        aria-label="Army analytics filters"
        onChange={(event) => {
          if (pendingChange.current !== null) clearTimeout(pendingChange.current);
          const form = event.currentTarget;
          if (
            event.target instanceof HTMLSelectElement &&
            event.target.name === "category" &&
            event.target.value !== "troops"
          ) {
            const ccToggle = form.elements.namedItem("cc");
            if (ccToggle instanceof HTMLInputElement) ccToggle.checked = false;
          }
          const apply = () => {
            pendingChange.current = null;
            if (!form.checkValidity()) return;
            const values = new FormData(form);
            if (Number(values.get("start_day")) > Number(values.get("end_day"))) return;
            void submit(form, { replace: true, preventScrollReset: true });
          };
          if (
            event.target instanceof HTMLInputElement &&
            event.target.type === "number"
          ) {
            pendingChange.current = setTimeout(apply, 350);
          } else {
            apply();
          }
        }}
        onSubmit={() => {
          if (pendingChange.current !== null) clearTimeout(pendingChange.current);
          pendingChange.current = null;
        }}
      >
        <input
          type="hidden"
          name="sort"
          value={selected?.sort ?? params.get("sort") ?? "usage-rate"}
        />
        {snapshot ? <input type="hidden" name="recent" value="1" /> : null}
        {params.get("saved") === "1" ? (
          <input type="hidden" name="saved" value="1" />
        ) : null}
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
          <span className="analytics-season" role="status">
            {navigation.state !== "idle"
              ? "Updating…"
              : snapshot
                ? "Recent real battles"
                : isHistorical
                  ? "Past season"
                  : "Current season"}
          </span>
        </div>
        <div className="filter-grid analytics-main-filters">
          <div className="analytics-show-filter">
            <label className="filter-field">
              Show
              <select name="category" defaultValue={showCategory}>
                {allowed.category
                  .filter(
                    (value) =>
                      value !== "cc-troops" &&
                      value !== "cc-composition" &&
                      (!isHistorical ||
                        [
                          "troops",
                          "spells",
                          "siege",
                          "heroes",
                          "pets",
                          "equipment",
                        ].includes(value)),
                  )
                  .map((value) => (
                    <option key={value} value={value}>
                      {filterLabels[value]}
                    </option>
                  ))}
                {category === "cc-composition" ? (
                  <option value="cc-composition" hidden>
                    Clan Castle army
                  </option>
                ) : null}
              </select>
            </label>
            {showCategory === "troops" ? (
              <>
                <label className="analytics-cc-toggle">
                  <input
                    type="checkbox"
                    name="cc"
                    value="1"
                    defaultChecked={clanCastle}
                  />
                  Clan Castle troops
                </label>
                {isHistorical ? (
                  <p className="form-help">
                    Clan Castle troop stats are unavailable for past seasons.
                  </p>
                ) : null}
              </>
            ) : null}
          </div>
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
                {topPlayers
                  .filter((count) => !snapshot || count <= snapshot.playerCount)
                  .map((count) => (
                    <option key={count} value={`top-${count}`}>
                      Top {count.toLocaleString()}
                    </option>
                  ))}
              </optgroup>
              {!snapshot ? (
                <optgroup label="Top players on every selected day">
                  {topPlayers.map((count) => (
                    <option key={count} value={`streak-top-${count}`}>
                      Consistent top {count.toLocaleString()}
                    </option>
                  ))}
                </optgroup>
              ) : null}
            </select>
          </label>
        </div>
        <div className="filter-footer">
          {snapshot ? (
            <p className="form-help">
              Player groups use the leaderboard at collection time. Includes the battle
              records saved in this snapshot, not every battle from a complete Legend day.
            </p>
          ) : (
            <details className="filter-details">
              <summary>Season & day range</summary>
              <div className="filter-grid">
                <label className="filter-field">
                  Season
                  <select name="season" defaultValue={requestedSeason}>
                    <option value="current">Current season</option>
                    {requestedSeason !== "current" ? (
                      <option value={requestedSeason}>
                        {seasonName(requestedSeason)}
                      </option>
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
                    required
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
                    required
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
          )}
        </div>
        <noscript>
          <button type="submit" className="button button-secondary">
            Apply filters
          </button>
        </noscript>
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
              : (error?.error.message ??
                "We don’t have the complete daily records needed for this selection. Try a different day range or player group.")}
          </p>
          {seasonEmpty?.previousSeasonId ? (
            <Link
              className="button button-secondary"
              to={`?season=${encodeURIComponent(seasonEmpty.previousSeasonId)}&lens=offense&category=troops&sort=usage-rate`}
            >
              View previous season
            </Link>
          ) : null}
          {result.recentAvailable ? (
            <Link className="button button-secondary" to="?recent=1">
              View recent real battles
            </Link>
          ) : null}
        </section>
      ) : null}
      {analytics ? (
        <section
          className="analytics-results"
          aria-label="Army statistics"
          aria-busy={navigation.state !== "idle"}
        >
          <div className="analytics-kpis" aria-label="Battle coverage">
            <article className="analytics-kpi analytics-kpi-primary">
              <span>Battle records</span>
              <strong>{recordedBattleRecords.toLocaleString()}</strong>
              <small>Recorded in this selection</small>
            </article>
            <article className="analytics-kpi">
              <span>Records included</span>
              <strong>{analytics.usableArmySample.toLocaleString()}</strong>
              <small>Enough army details to use in these stats</small>
            </article>
            <article className="analytics-kpi">
              <span>Records excluded</span>
              <strong>{excludedBattleRecords.toLocaleString()}</strong>
              <small>Opponent or army details missing</small>
            </article>
          </div>
          {partialArmies > 0 ? (
            <p className="section-note analytics-coverage-note">
              {partialArmies.toLocaleString()} of the included armies are partly readable.
              Known troops, spells and equipment count; unknown items do not.
            </p>
          ) : null}
          <div className="section-heading">
            <h2>{filterLabels[analytics.selection.category]}</h2>
            <span className="section-note">
              {snapshot
                ? `${shortDate(snapshot.battleFrom)} – ${shortDate(snapshot.battleTo)} (UTC)`
                : `Days ${analytics.selection.startDay}–${analytics.selection.endDay}`}{" "}
              · {analytics.selection.lens === "offense" ? "Attacks" : "Defenses"}
            </span>
          </div>
          <p className="section-note analytics-coverage-note" id="army-rate-help">
            {lens === "defense"
              ? "Results are the attacking army’s stars against the selected players. "
              : ""}
            Star rates show how often battles using each component ended with that result.
            Each battle counts once per component.
          </p>
          <div
            className="table-wrap analytics-table-wrap"
            tabIndex={0}
            role="region"
            aria-label="Army results table"
          >
            <table
              className="data-table analytics-table"
              aria-label="Army analytics results"
              aria-describedby="army-rate-help"
            >
              <thead>
                <tr>
                  {columns.map((column) => (
                    <SortHeading
                      key={column}
                      column={column}
                      sort={tableSort}
                      onSort={setChosenSort}
                    />
                  ))}
                </tr>
              </thead>
              <tbody>
                {analytics.rows.length === 0 ? (
                  <tr>
                    <td colSpan={isHistorical ? 7 : 8}>
                      No recognized components in this selection.
                    </td>
                  </tr>
                ) : null}
                {rows.map((row) => (
                  <ArmyResultRow key={row.key} row={row} isHistorical={isHistorical} />
                ))}
              </tbody>
            </table>
          </div>
          {!isHistorical ? (
            <details className="analytics-breakdown">
              <summary>Full star breakdown & coverage</summary>
              <p className="section-note">
                {snapshot
                  ? "Recent battle logs can omit older battles and include unfinished days."
                  : `${analytics.collectionCoverage.completedDays} completed Legend days.`}{" "}
                {analytics.perspectiveDisagreementCount} battles have conflicting reports.{" "}
                {unreadableArmyRecords.toLocaleString()} battle records had missing or
                unreadable army details and are excluded from every row.{" "}
                {snapshot?.invalidBattleRows
                  ? `${snapshot.invalidBattleRows} other battle ${snapshot.invalidBattleRows === 1 ? "record had" : "records had"} no opponent and ${snapshot.invalidBattleRows === 1 ? "was" : "were"} excluded. `
                  : ""}
                Additional exclusions below apply when unknown details prevent a
                particular combination from being identified.
              </p>
              <div
                className="table-wrap analytics-table-wrap"
                tabIndex={0}
                role="region"
                aria-label="Star breakdown table"
              >
                <table
                  className="data-table analytics-table"
                  aria-label="Army star breakdown"
                >
                  <thead>
                    <tr>
                      {(
                        [
                          "name",
                          "zero-star-rate",
                          "one-star-rate",
                          "two-star-rate",
                          "three-star-rate",
                          "exclusions",
                        ] as const
                      ).map((column) => (
                        <SortHeading
                          key={column}
                          column={column}
                          sort={breakdownSort ?? tableSort}
                          onSort={setBreakdownSort}
                        />
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {sortedRows(analytics.rows, breakdownSort ?? tableSort).map((row) => (
                      <ArmyBreakdownRow key={row.key} row={row} />
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
                {analytics.pagination.totalRows > analytics.rows.length
                  ? ". Heading sorts apply to this page."
                  : ""}
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

function shortDate(value: string) {
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  }).format(new Date(value));
}

function formatRate(value: number) {
  return `${(value * 100).toFixed(1)}%`;
}
