import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import {
  data,
  Form,
  Link,
  redirect,
  useLoaderData,
  useLocation,
  useNavigate,
  useNavigationType,
  useSearchParams,
  useNavigation,
  useSubmit,
  type LoaderFunctionArgs,
  type ShouldRevalidateFunctionArgs,
} from "react-router";

import { UnitArt } from "../components/ArmyPortraits";
import { ErrorNotice } from "../components/ErrorNotice";
import { LocalTimestamp } from "../components/Provenance";
import { ArmyDetail } from "../components/VariantExamples";
import {
  battles,
  formatRate,
  starCount,
  starRate,
  type ArmyRow,
} from "../lib/army-stats";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { publicPageMeta } from "../lib/blog";
import { seasonDates } from "../lib/season-end";
import type { Route } from "./+types/army-analytics";
import analyticsStyles from "../analytics.css?url";

export function links() {
  return [{ rel: "stylesheet", href: analyticsStyles }];
}

// Filters change the view, not the page, so every view shares one address.
export function meta({ matches }: Route.MetaArgs) {
  return publicPageMeta(matches, {
    path: "/analytics/armies",
    title: "Legend League army analytics",
    description: "Compare usage and battle results across tracked Legend League armies.",
  });
}

// Opening or closing an army's sheet stays at the same address and reads nothing again.
export function shouldRevalidate({
  currentUrl,
  nextUrl,
  formMethod,
  defaultShouldRevalidate,
}: ShouldRevalidateFunctionArgs) {
  if (!formMethod && currentUrl.href === nextUrl.href) return false;
  return defaultShouldRevalidate;
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

const sortColumns = {
  name: { label: "Name", value: (row: ArmyRow) => row.label },
  quantity: { label: "Quantity", value: (row: ArmyRow) => row.quantity ?? 0 },
  "usage-rate": { label: "Usage", value: (row: ArmyRow) => row.usageRate },
  "three-star-rate": { label: "3-star", value: (row: ArmyRow) => starRate(row, 3) },
  "average-trophies": {
    label: "Avg. trophies",
    value: (row: ArmyRow) => row.averageTrophies ?? 0,
  },
  "two-star-rate": { label: "2-star", value: (row: ArmyRow) => starRate(row, 2) },
  "one-star-rate": { label: "1-star", value: (row: ArmyRow) => starRate(row, 1) },
  "average-stars": {
    label: "Avg. stars",
    value: (row: ArmyRow) => row.averageStars ?? 0,
  },
  "average-destruction": {
    label: "Avg. destruction",
    value: (row: ArmyRow) => row.averageDestruction ?? 0,
  },
  "usage-count": { label: "Battles", value: (row: ArmyRow) => row.usageCount },
} as const;
type SortColumn = keyof typeof sortColumns;
type TableSort = { column: SortColumn; direction: "ascending" | "descending" };
// The phone's Sort menu, most asked-for first.
const menuOrder: SortColumn[] = [
  "usage-rate",
  "three-star-rate",
  "average-trophies",
  "usage-count",
  "name",
  "quantity",
  "two-star-rate",
  "one-star-rate",
  "average-stars",
  "average-destruction",
];
const nameOrder = new Intl.Collator("en", { sensitivity: "base", numeric: true });
// A long list shows its first rows, then Show all.
const FIRST_ROWS = 20;

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

/** The sort a tap on a column's heading, or a pick in the Sort menu, asks for. */
function nextSort(column: SortColumn, sort: TableSort): TableSort {
  const direction =
    sort.column === column
      ? sort.direction === "ascending"
        ? "descending"
        : "ascending"
      : column === "name"
        ? "ascending"
        : "descending";
  return { column, direction };
}

function sortName({ column, direction }: TableSort) {
  const order =
    column === "name"
      ? direction === "ascending"
        ? "A to Z"
        : "Z to A"
      : direction === "ascending"
        ? "lowest first"
        : "highest first";
  return `Sort by ${sortColumns[column].label}, ${order}`;
}

function SortButton({
  column,
  sort,
  onSort,
}: {
  column: SortColumn;
  sort: TableSort;
  onSort: (sort: TableSort) => void;
}) {
  const next = nextSort(column, sort);
  const active = sort.column === column;
  return (
    <button
      type="button"
      className="analytics-sort-button"
      data-column={column}
      aria-label={sortName(next)}
      title={sortName(next)}
      onClick={() => onSort(next)}
    >
      {sortColumns[column].label}
      {active ? (
        <span aria-hidden="true">{sort.direction === "ascending" ? " ↑" : " ↓"}</span>
      ) : null}
    </button>
  );
}

function namedVariantCount(row: ArmyRow) {
  return row.variants?.filter((variant) => variant.variantId !== 0).length ?? 0;
}

function ResultCell({
  row,
  column,
  sorted,
  topSorted,
}: {
  row: ArmyRow;
  column: Exclude<SortColumn, "name">;
  sorted: boolean;
  /** The sorted column's highest value. */
  topSorted: number;
}) {
  const stars =
    column === "three-star-rate"
      ? 3
      : column === "two-star-rate"
        ? 2
        : column === "one-star-rate"
          ? 1
          : null;
  const [value, note] = stars
    ? [formatRate(starRate(row, stars)), battles(starCount(row, stars))]
    : column === "usage-rate"
      ? [formatRate(row.usageRate), null]
      : column === "average-trophies"
        ? [
            row.averageTrophies?.toFixed(1) ?? "–",
            row.averageMissTrophies == null
              ? "all 3 stars"
              : `${row.averageMissTrophies.toFixed(1)} below 3 stars`,
          ]
        : column === "average-stars"
          ? [row.averageStars?.toFixed(2) ?? "–", null]
          : column === "average-destruction"
            ? [
                row.averageDestruction == null
                  ? "–"
                  : `${row.averageDestruction.toFixed(1)}%`,
                null,
              ]
            : column === "quantity"
              ? [row.quantity?.toLocaleString() ?? "–", null]
              : [
                  row.usageCount.toLocaleString(),
                  `of ${row.usageDenominator.toLocaleString()}`,
                ];
  // Usage's bar, and the sorted number's on a phone. A rate fills a 0 to 100%
  // track, as in the army's sheet; any other number is beside the row with the most.
  const bar =
    column === "usage-rate"
      ? row.usageRate
      : !sorted
        ? null
        : column.endsWith("-rate")
          ? Number(sortColumns[column].value(row))
          : topSorted
            ? Number(sortColumns[column].value(row)) / topSorted
            : null;
  return (
    <td
      role="cell"
      data-column={column}
      data-label={sortColumns[column].label}
      data-sorted={sorted || undefined}
    >
      <strong className="analytics-value">{value}</strong>
      {bar !== null ? (
        <span className="meter" aria-hidden="true">
          <span style={{ "--value": `${bar * 100}%` } as CSSProperties} />
        </span>
      ) : null}
      {note ? <span className="analytics-note-value"> {note}</span> : null}
    </td>
  );
}

// An army with variants opens on a tap anywhere on its row, into a sheet with
// its variants and their most-used armies.
function ArmyResultRow({
  row,
  columns,
  sort,
  topSorted,
  more,
  open,
  onOpen,
}: {
  row: ArmyRow;
  columns: SortColumn[];
  sort: SortColumn;
  topSorted: number;
  more: boolean;
  open: boolean;
  onOpen: (key: string) => void;
}) {
  const named = namedVariantCount(row);
  return (
    <tr
      role="row"
      className={more ? "analytics-row analytics-row-more" : "analytics-row"}
      data-opens={named > 0 || undefined}
      onClick={named ? () => onOpen(row.key) : undefined}
    >
      <th scope="row" role="rowheader" data-sorted={sort === "name" || undefined}>
        <div className="analytics-name-cell">
          <span className="analytics-rank" aria-hidden="true" />
          <UnitArt unitKey={row.key} />
          {named ? (
            <button
              type="button"
              className="analytics-row-open"
              aria-haspopup="dialog"
              aria-expanded={open}
              title={`Show ${row.label} variants`}
            >
              <span className="analytics-name">
                {row.label}
                <small>
                  {" "}
                  {named} {named === 1 ? "variant" : "variants"}
                  {row.variants!.length > named ? " + Other" : null}
                </small>
              </span>
              <span className="analytics-chevron" aria-hidden="true" />
            </button>
          ) : (
            <span className="analytics-name">{row.label}</span>
          )}
        </div>
      </th>
      {columns.map((column) =>
        column === "name" ? null : (
          <ResultCell
            key={column}
            row={row}
            column={column}
            sorted={sort === column}
            topSorted={topSorted}
          />
        ),
      )}
    </tr>
  );
}

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
  const unitTypeRow = useRef<HTMLElement>(null);
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
  // Another unit type keeps the players, Season and sort.
  const unitTypeQuery = (unitType: string) => {
    const query = new URLSearchParams(params);
    for (const key of ["cc", "offset", "sample"]) query.delete(key);
    query.set("category", unitType);
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
  const navigationType = useNavigationType();
  const navigate = useNavigate();
  const [chosenSort, setChosenSort] = useState<TableSort | null>(null);
  const [shownAll, setShownAll] = useState<string | null>(null);
  const [defaultVisit, setDefaultVisit] = useState(location.key);
  // An open army's sheet is its own step in history, so Back closes it and
  // keeps the filters, sort and scroll.
  const sheet = location.state as { army?: unknown } | null;
  const openArmy = typeof sheet?.army === "string" ? sheet.army : null;
  // The top-bar link opens the defaults again, sorted by Usage. Opening an
  // army, or Back from it, is not a new visit.
  if (
    !location.search &&
    !openArmy &&
    navigationType === "PUSH" &&
    defaultVisit !== location.key
  ) {
    setDefaultVisit(location.key);
    setChosenSort(null);
    setShownAll(null);
  }
  const openSheet = useCallback(
    (army: string) =>
      navigate(`${location.pathname}${location.search}`, {
        state: { army },
        preventScrollReset: true,
      }),
    [navigate, location.pathname, location.search],
  );
  const closeArmy = useCallback(() => navigate(-1), [navigate]);
  const defaultColumn =
    selected?.sort && Object.hasOwn(sortColumns, selected.sort)
      ? (selected.sort as SortColumn)
      : "usage-rate";
  const columns: SortColumn[] = [
    "name",
    ...(isHistorical ? ["quantity" as const] : []),
    "usage-rate",
    "three-star-rate",
    ...(armyTypes ? ["average-trophies" as const] : []),
    "two-star-rate",
    "one-star-rate",
    ...(!isHistorical ? ["average-stars" as const, "average-destruction" as const] : []),
    "usage-count",
  ];
  const tableSort: TableSort =
    chosenSort && columns.includes(chosenSort.column)
      ? chosenSort
      : { column: defaultColumn, direction: "descending" };
  const rows = analytics ? sortedRows(analytics.rows, tableSort) : [];
  const topSorted =
    tableSort.column === "name"
      ? 0
      : Math.max(
          0,
          ...rows.map((row) => Number(sortColumns[tableSort.column].value(row))),
        );
  const listKey = `${category} ${population} ${requestedSeason}`;
  const collapsed = rows.length > FIRST_ROWS + 5 && shownAll !== listKey;
  const opened = rows.find((row) => row.key === openArmy && namedVariantCount(row));

  useEffect(() => {
    if (!Number.isFinite(currentSeasonEnd) || seasonEnded) return;
    const timer = setTimeout(
      () => setClock((tick) => tick + 1),
      Math.min(currentSeasonEnd - Date.now(), 2 ** 31 - 1),
    );
    return () => clearTimeout(timer);
  }, [currentSeasonEnd, seasonEnded, clock]);

  // The chosen unit type scrolls into view in its sideways row.
  useEffect(() => {
    const row = unitTypeRow.current;
    const chosen = row?.querySelector<HTMLElement>("[aria-current]");
    if (row && chosen)
      row.scrollLeft +=
        chosen.getBoundingClientRect().left -
        row.getBoundingClientRect().left -
        (row.clientWidth - chosen.offsetWidth) / 2;
  }, [category]);

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
      <div className="page-head analytics-head">
        <h1 id="army-analytics-title">Analytics</h1>
        <nav
          aria-label="Armies view"
          className="leaderboard-view-switch analytics-view-switch"
        >
          {(
            [
              ["army-types", "Armies"],
              ["troops", "Unit usage"],
            ] as const
          ).map(([view, title]) => (
            <Link
              key={view}
              className="button"
              to={viewQuery(view)}
              aria-current={(view === "army-types") === armyTypes ? "page" : undefined}
              preventScrollReset
            >
              {title}
            </Link>
          ))}
        </nav>
      </div>
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
        className="analytics-controls"
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
        <input type="hidden" name="category" value={category} />
        {snapshot ? <input type="hidden" name="recent" value="1" /> : null}
        {params.get("saved") === "1" ? (
          <input type="hidden" name="saved" value="1" />
        ) : null}
        {armyTypes ? null : (
          <nav
            ref={unitTypeRow}
            aria-label="Unit type"
            className="tab-row analytics-unit-types"
          >
            {[
              ...unitTypes
                // Past Seasons keep the first six, plus the one in the link.
                .filter(
                  (value) =>
                    !isHistorical || unitTypes.indexOf(value) < 6 || value === category,
                ),
              ...(category === "cc-composition" ? ["cc-composition"] : []),
            ].map((value) => (
              <Link
                key={value}
                to={unitTypeQuery(value)}
                aria-current={value === category ? "page" : undefined}
                replace
                preventScrollReset
              >
                {filterLabels[value]}
              </Link>
            ))}
          </nav>
        )}
        <div className="analytics-filter-row">
          <label className="analytics-field">
            <span>Players</span>
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
          {snapshot ? null : (
            <label className="analytics-field analytics-season-field">
              <span>Season</span>
              <select name="season" defaultValue={requestedSeason}>
                <option value="current">This Season</option>
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
          <span className="analytics-status" role="status">
            {navigation.state !== "idle"
              ? "Updating…"
              : snapshot
                ? "Recent real battles"
                : null}
          </span>
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
          <div className="analytics-results-head">
            <h2>{filterLabels[analytics.selection.category]}</h2>
            <span className="analytics-days">
              {snapshot
                ? `${shortDate(snapshot.battleFrom)} – ${shortDate(snapshot.battleTo)} (UTC)`
                : coveredDays.length
                  ? `Days ${dayList(coveredDays)} of 28`
                  : `${analytics.selection.endDay - analytics.selection.startDay + 1} of 28 days`}
            </span>
          </div>
          <p className="analytics-note" id="army-selection-help">
            <span className="sr-only">
              {usageLabels[category]} usage in attacks by{" "}
              {populationDescription(population)}.
            </span>
            {!isHistorical && !snapshot && untrackedDays.length > 0
              ? `Days not tracked: ${dayList(untrackedDays)}.`
              : null}
          </p>
          <div className="analytics-coverage" role="group" aria-label="Battle coverage">
            <span>
              Battle records <strong>{recordedBattleRecords.toLocaleString()}</strong>
            </span>
            <span>
              Included <strong>{includedBattleRecords.toLocaleString()}</strong>
            </span>
            <span>
              Excluded <strong>{excludedBattleRecords.toLocaleString()}</strong>
            </span>
          </div>
          {/* Phones and iPads: one Sort menu and its direction. */}
          <div className="analytics-sort-menu">
            <label className="analytics-field">
              <span>Sort</span>
              <select
                value={tableSort.column}
                onChange={(event) =>
                  setChosenSort(
                    nextSort(event.currentTarget.value as SortColumn, tableSort),
                  )
                }
              >
                {menuOrder
                  .filter((column) => columns.includes(column))
                  .map((column) => (
                    <option key={column} value={column}>
                      {sortColumns[column].label}
                    </option>
                  ))}
              </select>
            </label>
            <button
              type="button"
              className="analytics-sort-button analytics-sort-direction"
              data-column={tableSort.column}
              aria-label={sortName(nextSort(tableSort.column, tableSort))}
              title={sortName(nextSort(tableSort.column, tableSort))}
              onClick={() => setChosenSort(nextSort(tableSort.column, tableSort))}
            >
              <span aria-hidden="true">
                {tableSort.direction === "ascending" ? "↑" : "↓"}
              </span>
            </button>
          </div>
          <div
            className="analytics-table-wrap"
            tabIndex={0}
            role="region"
            aria-label="Army results table"
          >
            <table
              className="analytics-results-table"
              role="table"
              aria-label="Army analytics results"
              aria-describedby="army-selection-help"
              data-view={armyTypes ? "armies" : "units"}
              data-sort={tableSort.column}
              // Ranks and the top three show for a highest-first number sort.
              data-ranked={
                (tableSort.column !== "name" && tableSort.direction === "descending") ||
                undefined
              }
            >
              <thead role="rowgroup">
                <tr role="row">
                  {columns.map((column) => (
                    <th
                      key={column}
                      scope="col"
                      role="columnheader"
                      data-column={column}
                      aria-sort={
                        tableSort.column === column ? tableSort.direction : undefined
                      }
                    >
                      <SortButton
                        column={column}
                        sort={tableSort}
                        onSort={setChosenSort}
                      />
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody role="rowgroup" data-collapsed={collapsed || undefined}>
                {analytics.rows.length === 0 ? (
                  <tr role="row" className="analytics-row-empty">
                    <td role="cell" colSpan={columns.length}>
                      No recognized components in this selection.
                    </td>
                  </tr>
                ) : null}
                {rows.map((row, index) => (
                  <ArmyResultRow
                    key={row.key}
                    row={row}
                    columns={columns}
                    sort={tableSort.column}
                    topSorted={topSorted}
                    more={index >= FIRST_ROWS}
                    open={opened?.key === row.key}
                    onOpen={openSheet}
                  />
                ))}
              </tbody>
            </table>
          </div>
          {collapsed ? (
            <>
              <button
                type="button"
                className="button button-secondary analytics-show-all"
                onClick={() => setShownAll(listKey)}
              >
                Show all {rows.length.toLocaleString()}
              </button>
              {/* Without JavaScript every row shows. */}
              <noscript>
                <style>
                  {
                    ".analytics-results-table tbody[data-collapsed] > .analytics-row-more{display:var(--row-display)}.analytics-show-all{display:none}"
                  }
                </style>
              </noscript>
            </>
          ) : null}
          <p className="analytics-footnote">
            {armyTypes
              ? "Avg. trophies counts every attack. Every attack is included, even if its army could not be read. "
              : excludedBattleRecords > 0
                ? "Excluded: opponent or army details missing. "
                : null}
            {partialArmies > 0
              ? `${partialArmies.toLocaleString()} of the included armies ${partialArmies === 1 ? "is" : "are"} partly readable. Known troops, spells and equipment count; unknown items do not.`
              : null}
          </p>
          {analytics.pagination ? (
            <div className="analytics-pages">
              <span>
                Showing {analytics.rows.length.toLocaleString()} of{" "}
                {analytics.pagination.totalRows.toLocaleString()}{" "}
                {analytics.pagination.totalRows === 1 ? "result" : "results"}
                {analytics.pagination.totalRows > analytics.rows.length
                  ? ". Sorting applies to this page."
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
          {opened ? (
            <ArmyDetail
              key={opened.key}
              row={opened}
              variants={sortedRows(opened.variants ?? [], tableSort)
                // A type's Other stays last.
                .sort((a, b) => Number(a.variantId === 0) - Number(b.variantId === 0))}
              audience={populationDescription(population)}
              examplesUrl={(variant) =>
                `/resources/analytics/armies/examples?${new URLSearchParams({
                  population,
                  season: analytics.selection.season,
                  days: coveredDays.join(","),
                  type: String(opened.typeId),
                  variant: String(variant.variantId),
                })}`
              }
              onClose={closeArmy}
            />
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
