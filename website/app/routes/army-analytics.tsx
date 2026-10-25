import { memo, useCallback, useEffect, useRef, useState } from "react";
import {
  data,
  Form,
  Link,
  redirect,
  useLoaderData,
  useLocation,
  useSearchParams,
  useNavigation,
  useSubmit,
  type LoaderFunctionArgs,
} from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { LocalTimestamp } from "../components/Provenance";
import { seasonStartAt } from "../components/SeasonReread";
import { VariantExamples } from "../components/VariantExamples";
import type { ArmyAnalytics, WebsiteErrorResponse } from "../lib/contracts";
import { publicPageMeta } from "../lib/blog";
import { seasonDates } from "../lib/season-end";
import type { Route } from "./+types/army-analytics";
import detailsStyles from "../details.css?url";

export function links() {
  return [{ rel: "stylesheet", href: detailsStyles }];
}

// Filters change the view, not the page, so every view shares one address.
export function meta({ matches }: Route.MetaArgs) {
  return publicPageMeta(matches, {
    path: "/analytics/armies",
    title: "Legend League army analytics",
    description: "Compare usage and battle results across tracked Legend League armies.",
  });
}

export async function loader({ request }: LoaderFunctionArgs) {
  const source = new URL(request.url).searchParams;
  const recentAvailable =
    import.meta.env.DEV || process.env.CLASHLENS_ARMY_PREVIEW === "true";
  if ((source.get("category") ?? "troops") === "troops" && source.get("cc") === "1") {
    source.set("category", "cc-troops");
  }
  const season = source.get("season") ?? "current";
  // Armies show first, attacks only.
  const category = source.get("category") ?? "army-types";
  // Players are a leaderboard top only, Top 1,000 unless the link names another.
  const population = topPlayers.some(
    (count) => source.get("population") === `top-${count}`,
  )
    ? source.get("population")!
    : "top-1000";
  if (
    recentAvailable &&
    source.get("saved") !== "1" &&
    source.get("category") !== "army-types" &&
    (source.get("recent") === "1" ||
      source.has("sample") ||
      (!source.has("season") && category !== "army-types"))
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
          pastSeasons: [] as string[],
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
      pastSeasons: [] as string[],
    };
  }
  // Attacks only, over the whole Season: the API's defaults are the attack
  // view and every finished Legend day from Day 1.
  const sort = source.get("sort") ?? "usage-rate";
  const python = await import("../services/python.server");
  let pastSeasonsRead: Promise<string[]> = Promise.resolve([]);
  try {
    const client = python.createPythonClient();
    pastSeasonsRead = armySeasons(client);
    // Army types are counted from the current Season on.
    if (season !== "current" && category === "army-types") {
      return data(
        {
          recentAvailable,
          analytics: null,
          error: {
            error: {
              code: "unavailable",
              message: "Army types are counted from the current season on.",
            },
          } satisfies WebsiteErrorResponse,
          selectionUnavailable: true,
          armiesNotAvailable: true,
          seasonEmpty: null,
          historicalSummary: true,
          requestedSeason: season,
          pastSeasons: await pastSeasonsRead,
        },
        { status: 404 },
      );
    }
    if (season !== "current") {
      const summaryQuery = new URLSearchParams({ category, sort });
      if (source.has("offset")) summaryQuery.set("offset", source.get("offset")!);
      return {
        recentAvailable,
        analytics: await client.getArmySeasonSummary(season, summaryQuery),
        error: null,
        seasonEmpty: null,
        historicalSummary: true,
        requestedSeason: season,
        pastSeasons: await pastSeasonsRead,
      };
    }
    const query = new URLSearchParams({
      season,
      population,
      category,
      sort,
    });
    return {
      recentAvailable,
      analytics: await client.getArmyAnalytics(query),
      error: null,
      seasonEmpty: null,
      historicalSummary: false,
      requestedSeason: season,
      pastSeasons: await pastSeasonsRead,
    };
  } catch (cause) {
    const pastSeasons = await pastSeasonsRead;
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
          pastSeasons,
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
          : payloadError === "army_types_preparing"
            ? {
                error: {
                  code: "unavailable",
                  message:
                    "We’re sorting every saved army into its type. Back in a few minutes; Unit usage works now.",
                },
              }
            : payloadError === "army_analytics_unavailable"
              ? {
                  error: {
                    code: "unavailable",
                    message:
                      "Army analytics are unavailable for the selected Legend days.",
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
          selectionUnavailable:
            payloadError === "army_analytics_unavailable" ||
            payloadError === "army_types_preparing",
          armyTypesPreparing: payloadError === "army_types_preparing",
          seasonEmpty: null,
          historicalSummary: season !== "current",
          requestedSeason: season,
          pastSeasons,
        },
        { status: pythonError.status },
      );
    }
    const timedOut =
      season === "current" &&
      cause instanceof python.PythonApiError &&
      (cause.payload as { error?: unknown } | null)?.error === "timeout";
    return {
      recentAvailable,
      analytics: null,
      error: timedOut
        ? ({
            error: {
              code: "unavailable",
              message:
                "This player group took too long to load right now. Try a smaller group.",
            },
          } satisfies WebsiteErrorResponse)
        : safeWebsiteError(cause),
      seasonEmpty: null,
      historicalSummary: false,
      requestedSeason: season,
      pastSeasons,
    };
  }
}

// The list only adds finished Seasons to the picker, so the page still works
// without it.
async function armySeasons(
  client: ReturnType<typeof import("../services/python.server").createPythonClient>,
) {
  try {
    return await client.getArmySeasons();
  } catch {
    return [];
  }
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

const filterLabels: Record<string, string> = {
  "army-types": "Armies",
  troops: "Troops",
  spells: "Spells",
  siege: "Siege Machines",
  heroes: "Heroes",
  pets: "Pets",
  equipment: "Equipments",
  "equipment-for-hero": "Hero + Equipment",
  "hero-pet": "Hero + Pet",
  "hero-equipment": "Hero + Both Equipments",
  "cc-troops": "Clan Castle Troops",
  "cc-composition": "Clan Castle army",
};
// Unit types in the order the owner listed them.
const unitTypes = [
  "troops",
  "spells",
  "siege",
  "heroes",
  "pets",
  "equipment",
  "equipment-for-hero",
  "hero-pet",
  "hero-equipment",
  "cc-troops",
];
const topPlayers = [5, 10, 20, 50, 100, 200, 300, 500, 1000, 2000, 5000, 10000];
const usageLabels: Record<string, string> = {
  "army-types": "Army",
  troops: "Troop",
  spells: "Spell",
  siege: "Siege machine",
  heroes: "Hero",
  pets: "Hero pet",
  equipment: "Hero equipment",
  "equipment-for-hero": "Equipment",
  "cc-troops": "Clan Castle troop",
  "hero-pet": "Hero and pet",
  "hero-equipment": "Hero and both equipments",
  "cc-composition": "Clan Castle army",
};

function populationDescription(population: string) {
  if (population === "all") return "all players";
  if (population.startsWith("top-"))
    return `top-${Number(population.slice(4)).toLocaleString()} players`;
  return "the selected players";
}

type ArmyRow = ArmyAnalytics["rows"][number];
const sortColumns = {
  name: { label: "Name", value: (row: ArmyRow) => row.label },
  quantity: { label: "Quantity", value: (row: ArmyRow) => row.quantity ?? 0 },
  "usage-count": { label: "Battles / included", value: (row: ArmyRow) => row.usageCount },
  "usage-rate": { label: "Usage", value: (row: ArmyRow) => row.usageRate },
  "one-star-rate": { label: "1-star", value: (row: ArmyRow) => starRate(row, 1) },
  "two-star-rate": { label: "2-star", value: (row: ArmyRow) => starRate(row, 2) },
  "three-star-rate": { label: "3-star", value: (row: ArmyRow) => starRate(row, 3) },
  "average-trophies": {
    label: "Avg. trophies",
    value: (row: ArmyRow) => row.averageTrophies ?? 0,
  },
  "average-stars": {
    label: "Avg. stars",
    value: (row: ArmyRow) => row.averageStars ?? 0,
  },
  "average-destruction": {
    label: "Avg. destruction",
    value: (row: ArmyRow) => row.averageDestruction ?? 0,
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
      role="columnheader"
      data-column={column}
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

function namedVariantCount(row: ArmyRow) {
  return row.variants?.filter((variant) => variant.variantId !== 0).length ?? 0;
}

// A type with variants opens on a tap anywhere on its row; its variant rows
// follow it, each with its share of the type, and open on a tap to their
// most-used armies.
const ArmyResultRow = memo(function ArmyResultRow({
  row,
  isHistorical,
  trophies,
  open = false,
  onToggle,
  typeLabel,
}: {
  row: ArmyRow;
  isHistorical: boolean;
  trophies: boolean;
  open?: boolean;
  onToggle?: (key: string) => void;
  typeLabel?: string;
}) {
  const named = namedVariantCount(row);
  const variants = named ? row.variants : null;
  const opens = variants !== null || (typeLabel !== undefined && onToggle !== undefined);
  return (
    <tr
      role="row"
      className={
        variants ? "analytics-type-row" : typeLabel ? "analytics-variant-row" : undefined
      }
      data-open={opens ? open : undefined}
      onClick={opens ? () => onToggle?.(row.key) : undefined}
    >
      <th scope="row" role="rowheader">
        {opens ? (
          <button
            type="button"
            className="analytics-type-toggle"
            aria-expanded={open}
            title={`${open ? "Hide" : "Show"} ${row.label} ${variants ? "variants" : "armies"}`}
          >
            <span className="analytics-type-chevron" aria-hidden="true" />
            <span>
              {row.label}
              {variants ? (
                <small>
                  {named} {named === 1 ? "variant" : "variants"}
                  {variants.length > named ? " + Other" : null}
                </small>
              ) : null}
            </span>
          </button>
        ) : (
          row.label
        )}
      </th>
      {isHistorical ? <td role="cell">{row.quantity?.toLocaleString()}</td> : null}
      <td role="cell" data-column="usage-count">
        {row.usageCount.toLocaleString()} / {row.usageDenominator.toLocaleString()}
      </td>
      <td role="cell" data-column="usage-rate">
        {typeLabel ? (
          <>
            <strong className="analytics-star-rate">{formatRate(row.usageRate)}</strong>
            <span className="analytics-star-count">of {typeLabel}</span>
            <span className="analytics-share-bar" aria-hidden="true">
              <span style={{ width: formatRate(row.usageRate) }} />
            </span>
          </>
        ) : (
          formatRate(row.usageRate)
        )}
      </td>
      {[1, 2, 3].map((stars) => {
        const count =
          row.starCounts?.[stars] ??
          [row.oneStarCount, row.twoStarCount, row.threeStarCount][stars - 1] ??
          0;
        return (
          <td
            key={stars}
            role="cell"
            data-column={stars === 3 ? "three-star-rate" : undefined}
          >
            <strong className="analytics-star-rate">
              {formatRate(row.usageCount ? count / row.usageCount : 0)}
            </strong>
            <span className="analytics-star-count">
              {count.toLocaleString()} {count === 1 ? "battle" : "battles"}
            </span>
          </td>
        );
      })}
      {trophies ? (
        <td role="cell" data-column="average-trophies">
          <strong className="analytics-star-rate">
            {row.averageTrophies?.toFixed(1) ?? "–"}
          </strong>
          <span className="analytics-star-count">
            {row.averageMissTrophies == null
              ? "no misses"
              : `misses ${row.averageMissTrophies.toFixed(1)}`}
          </span>
        </td>
      ) : null}
      {!isHistorical ? (
        <>
          <td role="cell">{row.averageStars!.toFixed(2)}</td>
          <td role="cell">{row.averageDestruction!.toFixed(1)}%</td>
        </>
      ) : null}
    </tr>
  );
});

export default function ArmyAnalyticsRoute() {
  const result = useLoaderData<typeof loader>();
  const { error, historicalSummary, requestedSeason, pastSeasons } = result;
  const [clock, setClock] = useState(0);
  // A current-season result stops being current when its 28 days end, even
  // if the page stays open or the reply arrives after the Reset.
  const currentSeasonEnd =
    requestedSeason === "current" &&
    historicalSummary !== true &&
    !("snapshot" in result) &&
    result.analytics
      ? (Number(result.analytics.selection.season) + 28 * 86400) * 1000
      : NaN;
  const seasonEnded = Date.now() >= currentSeasonEnd;
  const analytics = seasonEnded ? null : result.analytics;
  const seasonEmpty = seasonEnded
    ? { previousSeasonId: result.analytics!.selection.season }
    : result.seasonEmpty;
  const [params] = useSearchParams();
  const navigation = useNavigation();
  const submit = useSubmit();
  const filterForm = useRef<HTMLFormElement>(null);
  const selected = analytics?.selection;
  const isHistorical = historicalSummary === true;
  const coveredDays = analytics?.collectionCoverage.coveredDays ?? [];
  const untrackedDays = coveredDays.length
    ? Array.from(
        { length: coveredDays[coveredDays.length - 1] },
        (_, index) => index + 1,
      ).filter((day) => !coveredDays.includes(day))
    : [];
  const population = selected?.population ?? params.get("population") ?? "top-1000";
  const category =
    selected?.category ??
    ((params.get("category") ?? "troops") === "troops" && params.get("cc") === "1"
      ? "cc-troops"
      : params.get("category")) ??
    "army-types";
  const armyTypes = category === "army-types";
  const viewQuery = (view: string) => {
    const query = new URLSearchParams(params);
    // Switching views starts again from Top 1,000.
    for (const key of ["category", "cc", "offset", "recent", "sample", "population"])
      query.delete(key);
    if (view !== "army-types" && query.get("sort") === "average-trophies")
      query.delete("sort");
    query.set("category", view);
    return `?${query}`;
  };
  const unavailable = "selectionUnavailable" in result && result.selectionUnavailable;
  const preparing = "armyTypesPreparing" in result && result.armyTypesPreparing;
  const armiesNotAvailable = "armiesNotAvailable" in result && result.armiesNotAvailable;
  const snapshot = "snapshot" in result ? result.snapshot : null;
  const recordedBattleRecords = analytics
    ? analytics.totalAttacks + (snapshot?.invalidBattleRows ?? 0)
    : 0;
  // Armies keep unreadable attacks as their own row.
  const includedBattleRecords = analytics
    ? armyTypes
      ? analytics.totalAttacks
      : analytics.usableArmySample
    : 0;
  const excludedBattleRecords = recordedBattleRecords - includedBattleRecords;
  const partialArmies = analytics?.armyStates.partial ?? 0;
  const location = useLocation();
  const [chosenSort, setChosenSort] = useState<TableSort | null>(null);
  const [openTypes, setOpenTypes] = useState<ReadonlySet<string>>(new Set());
  const toggleType = useCallback(
    (key: string) =>
      setOpenTypes((open) => {
        const next = new Set(open);
        if (!next.delete(key)) next.add(key);
        return next;
      }),
    [],
  );
  const [openVariants, setOpenVariants] = useState<ReadonlySet<string>>(new Set());
  const toggleVariant = useCallback(
    (key: string) =>
      setOpenVariants((open) => {
        const next = new Set(open);
        if (!next.delete(key)) next.add(key);
        return next;
      }),
    [],
  );
  const [defaultVisit, setDefaultVisit] = useState(location.key);
  // The top-bar link opens the defaults again, sorted by Usage.
  if (!location.search && defaultVisit !== location.key) {
    setDefaultVisit(location.key);
    setChosenSort(null);
  }
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
    ...(armyTypes ? ["average-trophies" as const] : []),
    ...(!isHistorical ? ["average-stars" as const, "average-destruction" as const] : []),
  ];
  const tableSort: TableSort =
    chosenSort && columns.includes(chosenSort.column)
      ? chosenSort
      : { column: defaultColumn, direction: "descending" };
  const rows = analytics ? sortedRows(analytics.rows, tableSort) : [];

  useEffect(() => {
    if (!Number.isFinite(currentSeasonEnd) || seasonEnded) return;
    const timer = setTimeout(
      () => setClock((tick) => tick + 1),
      Math.min(currentSeasonEnd - Date.now(), 2 ** 31 - 1),
    );
    return () => clearTimeout(timer);
  }, [currentSeasonEnd, seasonEnded, clock]);

  // Update existing controls after URL navigation without replacing the form,
  // which would lose keyboard focus.
  useEffect(() => {
    if (!filterForm.current || navigation.state !== "idle") return;
    const values = {
      population: isHistorical ? "all" : population,
      category,
      sort: selected?.sort ?? params.get("sort") ?? "usage-rate",
      season: requestedSeason,
    };
    for (const [name, value] of Object.entries(values)) {
      const control = filterForm.current.elements.namedItem(name);
      if (control instanceof HTMLInputElement || control instanceof HTMLSelectElement) {
        control.value = value;
      }
    }
  }, [
    params,
    selected,
    population,
    category,
    isHistorical,
    requestedSeason,
    navigation.state,
  ]);

  return (
    <main id="main-content" tabIndex={-1} className="page-shell analytics-page">
      <section className="hero" aria-labelledby="army-analytics-title">
        <h1 id="army-analytics-title">Analytics</h1>
        <p className="hero-copy">
          Compare usage and battle results across tracked Legend League armies.
        </p>
      </section>
      <nav aria-label="Armies view" className="army-view-picker">
        {(
          [
            ["army-types", "Armies", "The army each attack used"],
            ["troops", "Unit usage", "Troops, spells, heroes and more"],
          ] as const
        ).map(([view, title, note]) => (
          <Link
            key={view}
            className="army-view-choice"
            to={viewQuery(view)}
            aria-current={(view === "army-types") === armyTypes ? "page" : undefined}
            preventScrollReset
          >
            <strong>{title}</strong>
            <small>{note}</small>
          </Link>
        ))}
      </nav>
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
          void submit(new FormData(event.currentTarget), {
            method: "get",
            replace: true,
            preventScrollReset: true,
          });
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
        <div className="analytics-filter-row">
          <label className="filter-field" hidden={armyTypes}>
            Unit Type
            <select name="category" defaultValue={category}>
              {armyTypes ? <option value="army-types">Armies</option> : null}
              {unitTypes
                // Past Seasons keep the first six, plus the one in the link.
                .filter(
                  (value) =>
                    !isHistorical || unitTypes.indexOf(value) < 6 || value === category,
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
          <label className="filter-field">
            Players
            <select
              name="population"
              defaultValue={isHistorical ? "all" : population}
              disabled={isHistorical}
            >
              {isHistorical ? (
                <option value="all">All players</option>
              ) : (
                topPlayers
                  .filter((count) => !snapshot || count <= snapshot.playerCount)
                  .map((count) => (
                    <option key={count} value={`top-${count}`}>
                      Top {count.toLocaleString()}
                    </option>
                  ))
              )}
            </select>
          </label>
          <span className="analytics-season" role="status">
            {navigation.state !== "idle"
              ? "Updating…"
              : snapshot
                ? "Recent real battles"
                : null}
          </span>
          {snapshot ? null : (
            <label className="filter-field analytics-season-picker">
              Season
              <select name="season" defaultValue={requestedSeason}>
                <option value="current">
                  {`This Season · ${seasonDates(seasonStartAt(Date.now()) / 1000)}`}
                </option>
                {[
                  ...pastSeasons,
                  ...(requestedSeason !== "current" &&
                  !pastSeasons.includes(requestedSeason)
                    ? [requestedSeason]
                    : []),
                ].map((season) => (
                  <option key={season} value={season}>
                    {seasonDates(season)}
                  </option>
                ))}
              </select>
            </label>
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
              : preparing
                ? "Army types are being prepared"
                : armiesNotAvailable
                  ? "Armies are not available for this season"
                  : "No army stats for this season yet"}
          </h2>
          <p>
            {seasonEmpty
              ? "Stats will appear after the first Legend day is complete."
              : (error?.error.message ??
                "We don’t have the complete daily records needed for this selection. Try a different player group.")}
          </p>
          {seasonEmpty?.previousSeasonId &&
          pastSeasons.includes(seasonEmpty.previousSeasonId) ? (
            <Link
              className="button button-secondary"
              to={`?season=${encodeURIComponent(seasonEmpty.previousSeasonId)}&category=troops&sort=usage-rate`}
            >
              View previous season
            </Link>
          ) : null}
          {preparing ? (
            <Link className="button button-secondary" to={viewQuery("troops")}>
              See Unit usage
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
          <p className="section-note analytics-coverage-note" id="army-selection-help">
            {usageLabels[category]} usage in attacks <strong>by</strong>{" "}
            {populationDescription(population)}.
          </p>
          <div className="analytics-kpis" aria-label="Battle coverage">
            <article className="analytics-kpi analytics-kpi-primary">
              <span>Battle records</span>
              <strong>{recordedBattleRecords.toLocaleString()}</strong>
              <small>Recorded in this selection</small>
            </article>
            <article className="analytics-kpi">
              <span>Records included</span>
              <strong>{includedBattleRecords.toLocaleString()}</strong>
              <small>
                {armyTypes
                  ? "Every attack, including ones whose army could not be read"
                  : "Enough army details to use in these stats"}
              </small>
            </article>
            <article className="analytics-kpi">
              <span>Records excluded</span>
              <strong>{excludedBattleRecords.toLocaleString()}</strong>
              <small>Opponent or army details missing</small>
            </article>
          </div>
          {partialArmies > 0 ? (
            <p className="section-note analytics-coverage-note">
              {partialArmies.toLocaleString()} of the included armies{" "}
              {partialArmies === 1 ? "is" : "are"} partly readable. Known troops, spells
              and equipment count; unknown items do not.
            </p>
          ) : null}
          <div className="section-heading">
            <h2>{filterLabels[analytics.selection.category]}</h2>
            <span className="section-note">
              {snapshot
                ? `${shortDate(snapshot.battleFrom)} – ${shortDate(snapshot.battleTo)} (UTC)`
                : `${analytics.selection.endDay - analytics.selection.startDay + 1} of 28 days`}
            </span>
          </div>
          {!isHistorical && !snapshot && untrackedDays.length > 0 ? (
            <p className="section-note analytics-coverage-note">
              Days {dayList(coveredDays)} of 28; days not tracked:{" "}
              {dayList(untrackedDays)}.
            </p>
          ) : null}
          {armyTypes ? (
            <p className="section-note analytics-coverage-note">
              {analytics.rows.some((row) => namedVariantCount(row) > 0)
                ? "Tap an army for its variants, then a variant for its top armies. "
                : null}
              Avg. trophies counts every attack.
            </p>
          ) : null}
          <div
            className="table-wrap analytics-table-wrap"
            tabIndex={0}
            role="region"
            aria-label="Army results table"
          >
            <table
              className="data-table analytics-table analytics-results-table"
              role="table"
              aria-label="Army analytics results"
              aria-describedby="army-selection-help"
            >
              <thead role="rowgroup">
                <tr role="row">
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
              <tbody role="rowgroup">
                {analytics.rows.length === 0 ? (
                  <tr role="row">
                    <td role="cell" colSpan={columns.length}>
                      No recognized components in this selection.
                    </td>
                  </tr>
                ) : null}
                {rows.flatMap((row) => [
                  <ArmyResultRow
                    key={row.key}
                    row={row}
                    isHistorical={isHistorical}
                    trophies={armyTypes}
                    open={openTypes.has(row.key)}
                    onToggle={toggleType}
                  />,
                  ...(row.variants && namedVariantCount(row) > 0 && openTypes.has(row.key)
                    ? sortedRows(row.variants, tableSort)
                        // A type's Other stays last.
                        .sort(
                          (a, b) => Number(a.variantId === 0) - Number(b.variantId === 0),
                        )
                        .flatMap((variant) => [
                          <ArmyResultRow
                            key={variant.key}
                            row={variant}
                            isHistorical={isHistorical}
                            trophies={armyTypes}
                            typeLabel={row.label}
                            open={openVariants.has(variant.key)}
                            onToggle={toggleVariant}
                          />,
                          ...(openVariants.has(variant.key)
                            ? [
                                <VariantExamples
                                  key={`${variant.key}:examples`}
                                  id={variant.key}
                                  label={variant.label}
                                  attacks={variant.usageCount}
                                  url={`/resources/analytics/armies/examples?${new URLSearchParams(
                                    {
                                      population,
                                      season: analytics.selection.season,
                                      days: coveredDays.join(","),
                                      type: String(row.typeId),
                                      variant: String(variant.variantId),
                                    },
                                  )}`}
                                  columns={columns.length}
                                />,
                              ]
                            : []),
                        ])
                    : []),
                ])}
              </tbody>
            </table>
          </div>
          {analytics.pagination ? (
            <div className="hero-actions">
              <span>
                Showing {analytics.rows.length.toLocaleString()} of{" "}
                {analytics.pagination.totalRows.toLocaleString()}{" "}
                {analytics.pagination.totalRows === 1 ? "result" : "results"}
                {analytics.pagination.totalRows > analytics.rows.length
                  ? ". Heading sorts apply to this page."
                  : ""}
              </span>
              {analytics.pagination.nextOffset !== null ? (
                <Link
                  className="button button-secondary"
                  to={`?${new URLSearchParams({ season: requestedSeason, category: analytics.selection.category, sort: analytics.selection.sort, offset: String(analytics.pagination.nextOffset) })}`}
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

function dayList(days: number[]) {
  const runs: string[] = [];
  for (let first = 0; first < days.length;) {
    let last = first;
    while (days[last + 1] === days[last] + 1) last += 1;
    runs.push(last === first ? String(days[first]) : `${days[first]}–${days[last]}`);
    first = last + 1;
  }
  return runs.join(", ");
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
