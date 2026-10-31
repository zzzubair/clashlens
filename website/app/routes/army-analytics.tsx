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

import { ArmyMap } from "../components/ArmyMap";
import { UnitArt } from "../components/ArmyPortraits";
import { ErrorNotice } from "../components/ErrorNotice";
import { TrophyIcon } from "../components/GameIcons";
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
import analyticsSheetStyles from "../analytics-sheet.css?url";
import analyticsStyles from "../analytics.css?url";

export function links() {
  return [
    { rel: "stylesheet", href: analyticsStyles },
    { rel: "stylesheet", href: analyticsSheetStyles },
  ];
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
  "two-star-rate": { label: "2-star", value: (row: ArmyRow) => starRate(row, 2) },
  "one-star-rate": { label: "1-star", value: (row: ArmyRow) => starRate(row, 1) },
  "average-trophies": {
    label: "Trophies per attack",
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
// Armies lead with Usage, 3-star, Trophies per attack and Battles; these wait behind
// More columns, except the one the table is sorted by.
const extraColumns: SortColumn[] = [
  "two-star-rate",
  "one-star-rate",
  "average-stars",
  "average-destruction",
];
const nameOrder = new Intl.Collator("en", { sensitivity: "base", numeric: true });
// A long list shows its first rows, then Show all.
const FIRST_ROWS = 20;

/** Other and unidentified armies are no army type of their own: last, with no rank. */
function isLeftover(row: ArmyRow) {
  return row.key === "army-type:1" || row.key === "army-type:2";
}

/** The armies the decoder couldn't read, named for what a Clasher sees. */
function shownRow(row: ArmyRow): ArmyRow {
  return row.key === "army-type:2" ? { ...row, label: "Army not identified" } : row;
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
      Number(isLeftover(a)) - Number(isLeftover(b)) ||
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
      {/* Headings stay short; the trophy says what "Per attack" counts. */}
      {column === "average-trophies" ? (
        <>
          <TrophyIcon /> Per attack
        </>
      ) : (
        sortColumns[column].label
      )}
      {active ? (
        <span aria-hidden="true">{sort.direction === "ascending" ? " ↑" : " ↓"}</span>
      ) : null}
    </button>
  );
}

function namedVariantCount(row: ArmyRow) {
  return row.variants?.filter((variant) => variant.variantId !== 0).length ?? 0;
}

/** The usage bars' full track: the top share rounded up to a clean number,
 * such as 25% for 21.4%, so every row reads on one stated scale. */
function usageScaleFor(top: number) {
  const step = top <= 0.1 ? 0.01 : top <= 0.5 ? 0.05 : 0.1;
  return Math.min(1, Math.ceil(top / step - 1e-9) * step);
}

// Each bar's scale, under its heading: 0 where the bar starts, its full value
// where the track ends. Usage and 3-star differ, so neither bar is read
// against the other.
function BarScale({ end }: { end: number }) {
  return (
    <span className="analytics-scale" aria-hidden="true">
      <span>0</span>
      <span>{Math.round(end * 100)}%</span>
    </span>
  );
}

/**
 * The rates over every attack that the 3-star rate and trophies per attack
 * are read against: each army shows its own number, then one signed
 * difference from these, named once in the column's heading.
 */
interface Averages {
  threeStar: number;
  trophies: number | null;
}

/** A difference is clearly better or worse, and coloured, once it is 5% of
 * the Legend average it is read against: one rule for both columns, judged
 * on the number shown, stated once behind the ⓘ with what it comes to. */
function clearDifferences(averages: Averages) {
  const share = (average: number) => Math.round(average * 0.05 * 10) / 10;
  return {
    "three-star-rate": share(averages.threeStar * 100),
    "average-trophies": averages.trophies === null ? null : share(averages.trophies),
  };
}
function clearWords(averages: Averages) {
  const clear = clearDifferences(averages);
  const trophies = clear["average-trophies"];
  const perAttack =
    averages.trophies === null
      ? ""
      : `, ${averages.trophies.toFixed(1)} trophies per attack`;
  return `Differences are against the Legend average: ${formatRate(averages.threeStar)} 3-star${perAttack}. Green or red when 5% or more from it: ${clear["three-star-rate"].toFixed(1)}+ pts for 3-star${trophies === null ? "" : `, ${trophies.toFixed(1)}+ for trophies per attack`}.`;
}

function ResultCell({
  row,
  column,
  sorted,
  averages,
  usageScale,
}: {
  row: ArmyRow;
  column: Exclude<SortColumn, "name">;
  sorted: boolean;
  averages: Averages | null;
  usageScale: number;
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
  // Usage is a bar on the stated usage scale, so close shares look apart. The
  // 3-star rate and trophies per attack each show one signed difference from
  // the rate over every attack; units have no average, so their 3-star rate
  // is a plain bar from 0 to 100%.
  const bar =
    column === "usage-rate"
      ? usageScale && row.usageRate / usageScale
      : column === "three-star-rate" && averages === null
        ? starRate(row, 3)
        : null;
  const difference =
    averages === null
      ? null
      : column === "three-star-rate"
        ? (starRate(row, 3) - averages.threeStar) * 100
        : column === "average-trophies" &&
            averages.trophies !== null &&
            row.averageTrophies != null
          ? row.averageTrophies - averages.trophies
          : null;
  // Only a clear difference is coloured, judged on the number shown, so small
  // ones don't read as good or bad; every row follows the same rule.
  const rounded = difference === null ? 0 : Math.round(difference * 10) / 10;
  const clear =
    averages !== null && (column === "three-star-rate" || column === "average-trophies")
      ? (clearDifferences(averages)[column] ?? Infinity)
      : Infinity;
  const compared =
    Math.abs(rounded) < clear ? undefined : rounded > 0 ? "above" : "below";
  const points = column === "three-star-rate";
  return (
    <td
      role="cell"
      data-column={column}
      data-label={sortColumns[column].label}
      data-sorted={sorted || undefined}
    >
      <strong className="analytics-value">{value}</strong>
      {difference !== null ? (
        <span className="analytics-difference" data-compared={compared}>
          {rounded > 0 ? "+" : rounded < 0 ? "−" : "±"}
          {Math.abs(rounded).toFixed(1)}
          {points ? <span aria-hidden="true"> pts</span> : null}
          <span className="sr-only">{points ? " points" : ""} against the average</span>
        </span>
      ) : null}
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
// its variants and their most-used armies. Armies end in one arrow column,
// the arrow on each row that opens, at the same place on every row.
function ArmyResultRow({
  row,
  columns,
  sort,
  averages,
  usageScale,
  more,
  open,
  onOpen,
  armies,
}: {
  row: ArmyRow;
  columns: SortColumn[];
  sort: SortColumn;
  averages: Averages | null;
  usageScale: number;
  more: boolean;
  open: boolean;
  onOpen: (key: string) => void;
  armies: boolean;
}) {
  const named = namedVariantCount(row);
  return (
    <tr
      role="row"
      className={more ? "analytics-row analytics-row-more" : "analytics-row"}
      data-leftover={isLeftover(row) || undefined}
      data-opens={named > 0 || undefined}
      onClick={named ? () => onOpen(row.key) : undefined}
    >
      <th scope="row" role="rowheader" data-sorted={sort === "name" || undefined}>
        <div className="analytics-name-cell">
          <span className="analytics-rank" aria-hidden="true" />
          {/* Other and Unreadable have no troops to show; a plain tile keeps
              their names in line with the armies'. */}
          {isLeftover(row) ? (
            <span
              className="unit-art analytics-leftover-art"
              aria-hidden="true"
              data-kind={row.key === "army-type:1" ? "other" : "unreadable"}
            />
          ) : (
            <UnitArt unitKey={row.key} />
          )}
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
                {/* Only a count of two or more, a pill on every screen; one
                    variant says nothing the arrow doesn't. The sheet names them. */}
                {named > 1 ? <small> {named} variants</small> : null}
              </span>
            </button>
          ) : (
            <span className="analytics-name">{row.label}</span>
          )}
          {/* A phone drops the Battles column; the count sits under the name. */}
          {armies ? (
            <small className="analytics-name-battles" aria-hidden="true">
              {row.usageCount.toLocaleString("en")} battles
            </small>
          ) : null}
        </div>
      </th>
      {columns.map((column) =>
        column === "name" ? null : (
          <ResultCell
            key={column}
            row={row}
            column={column}
            sorted={sort === column}
            averages={averages}
            usageScale={usageScale}
          />
        ),
      )}
      {armies ? (
        <td className="analytics-open-cell" aria-hidden="true">
          {named ? <span className="analytics-chevron" /> : null}
        </td>
      ) : null}
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
  const [moreColumns, setMoreColumns] = useState(false);
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
  // Armies keep their four main numbers first, so More columns adds to the
  // right without moving them.
  const columns: SortColumn[] = armyTypes
    ? [
        "name",
        "usage-rate",
        "three-star-rate",
        "average-trophies",
        "usage-count",
        ...extraColumns,
      ]
    : [
        "name",
        ...(isHistorical ? ["quantity" as const] : []),
        "usage-rate",
        "three-star-rate",
        "two-star-rate",
        "one-star-rate",
        ...(!isHistorical
          ? ["average-stars" as const, "average-destruction" as const]
          : []),
        "usage-count",
      ];
  const tableSort: TableSort =
    chosenSort && columns.includes(chosenSort.column)
      ? chosenSort
      : { column: defaultColumn, direction: "descending" };
  const shownColumns =
    armyTypes && !moreColumns
      ? columns.filter(
          (column) => !extraColumns.includes(column) || column === tableSort.column,
        )
      : columns;
  const rows = analytics ? sortedRows(analytics.rows.map(shownRow), tableSort) : [];
  // Every attack has one army type, so the rows add up to every attack.
  const attackCount = rows.reduce((sum, row) => sum + row.usageCount, 0);
  const threeStarAverage =
    armyTypes && attackCount
      ? rows.reduce((sum, row) => sum + starCount(row, 3), 0) / attackCount
      : null;
  // Trophies per attack over every attack: each army's own, weighted by its attacks.
  const trophyAttacks = rows.filter((row) => row.averageTrophies != null);
  const trophyCount = trophyAttacks.reduce((sum, row) => sum + row.usageCount, 0);
  const averages: Averages | null =
    threeStarAverage === null
      ? null
      : {
          threeStar: threeStarAverage,
          trophies: trophyCount
            ? trophyAttacks.reduce(
                (sum, row) => sum + (row.averageTrophies ?? 0) * row.usageCount,
                0,
              ) / trophyCount
            : null,
        };
  const usageScale = usageScaleFor(Math.max(0, ...rows.map((row) => row.usageRate)));
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
      <header className="page-hero analytics-head">
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
      </header>
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
          className="analytics-results open-board"
          aria-label="Army statistics"
          aria-busy={navigation.state !== "idle"}
        >
          <div className="analytics-results-head">
            <h2>{filterLabels[analytics.selection.category]}</h2>
            <div className="analytics-days">
              {snapshot ? (
                `${shortDate(snapshot.battleFrom)} – ${shortDate(snapshot.battleTo)} (UTC)`
              ) : coveredDays.length ? (
                // A phone drops "of 28" so the range fits beside Sort.
                <span>
                  Days {dayList(coveredDays)}
                  <span className="analytics-days-of"> of 28</span>
                </span>
              ) : (
                `${analytics.selection.endDay - analytics.selection.startDay + 1} of 28 days`
              )}
              {/* What the differences are against, said once for the table;
                  the averages and the one colouring rule a tap away. */}
              {averages !== null ? (
                <span className="analytics-average-key">
                  vs Legend avg
                  <details className="analytics-rule">
                    <summary title={clearWords(averages)}>
                      <span aria-hidden="true">ⓘ</span>
                      <span className="sr-only">
                        The averages, and how differences are coloured
                      </span>
                    </summary>
                    <p>{clearWords(averages)}</p>
                  </details>
                </span>
              ) : null}
            </div>
            {armyTypes ? (
              <button
                type="button"
                className="analytics-more-columns"
                aria-pressed={moreColumns}
                aria-controls="army-results-table"
                title="2-star, 1-star, Avg. stars and Avg. destruction"
                onClick={() => setMoreColumns(!moreColumns)}
              >
                <span className="analytics-more-icon" aria-hidden="true" />
                More columns
              </button>
            ) : null}
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
          {armyTypes && averages !== null ? (
            <ArmyMap
              rows={rows.filter((row) => !isLeftover(row))}
              usageEnd={usageScale}
              threeStar={averages.threeStar}
            />
          ) : null}
          {/* Phones and iPads: one Sort menu with every choice, and its direction
              beside it; wide screens sort by the table's headings. */}
          <div className="analytics-sort-menu" role="group" aria-label="Sort">
            <label className="analytics-sort-field">
              <span>Sort</span>
              <select
                value={tableSort.column}
                onChange={(event) =>
                  setChosenSort(nextSort(event.target.value as SortColumn, tableSort))
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
              title={sortName(nextSort(tableSort.column, tableSort))}
              onClick={() => setChosenSort(nextSort(tableSort.column, tableSort))}
            >
              <span aria-hidden="true">
                {tableSort.direction === "ascending" ? "↑" : "↓"}
              </span>
              <span className="sr-only">
                {tableSort.direction === "ascending"
                  ? tableSort.column === "name"
                    ? "A to Z"
                    : "Lowest first"
                  : tableSort.column === "name"
                    ? "Z to A"
                    : "Highest first"}
              </span>
            </button>
          </div>
          {/* Phones: one strip names the cards' numbers and the bars' scales,
              so each card shows only values. Screen readers hear each
              card's own labels. */}
          <div
            className="analytics-phone-key"
            aria-hidden="true"
            data-view={armyTypes ? "armies" : "units"}
            data-sort={tableSort.column}
          >
            {shownColumns.map((column) =>
              column === "name" ? null : (
                <span
                  key={column}
                  data-column={column}
                  data-sorted={tableSort.column === column || undefined}
                >
                  {/* The trophy says what Per attack counts; the rows don't repeat it. */}
                  {column === "average-trophies" ? (
                    <>
                      <TrophyIcon /> Per attack
                    </>
                  ) : (
                    sortColumns[column].label
                  )}
                  {column === "usage-rate" && usageScale ? (
                    <small>0–{Math.round(usageScale * 100)}%</small>
                  ) : null}
                </span>
              ),
            )}
          </div>
          <div
            className="analytics-table-wrap"
            tabIndex={0}
            role="region"
            aria-label="Army results table"
          >
            <table
              id="army-results-table"
              className="analytics-results-table"
              role="table"
              aria-label="Army analytics results"
              aria-describedby="army-selection-help"
              data-view={armyTypes ? "armies" : "units"}
              data-sort={tableSort.column}
              data-more={(armyTypes && moreColumns) || undefined}
              // Ranks show for a highest-first number sort; armies are always
              // numbered, as the chart over them numbers them on a phone.
              data-ranked={
                armyTypes ||
                (tableSort.column !== "name" && tableSort.direction === "descending") ||
                undefined
              }
            >
              <thead role="rowgroup">
                <tr role="row">
                  {shownColumns.map((column) => (
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
                      {column === "usage-rate" && usageScale ? (
                        <BarScale end={usageScale} />
                      ) : averages === null && column === "three-star-rate" ? (
                        <BarScale end={1} />
                      ) : null}
                    </th>
                  ))}
                  {armyTypes ? (
                    <td className="analytics-open-cell" aria-hidden="true" />
                  ) : null}
                </tr>
              </thead>
              <tbody role="rowgroup" data-collapsed={collapsed || undefined}>
                {analytics.rows.length === 0 ? (
                  <tr role="row" className="analytics-row-empty">
                    <td role="cell" colSpan={shownColumns.length + (armyTypes ? 1 : 0)}>
                      No recognized components in this selection.
                    </td>
                  </tr>
                ) : null}
                {rows.map((row, index) => (
                  <ArmyResultRow
                    key={row.key}
                    row={row}
                    columns={shownColumns}
                    sort={tableSort.column}
                    averages={averages}
                    usageScale={usageScale}
                    more={index >= FIRST_ROWS}
                    open={opened?.key === row.key}
                    onOpen={openSheet}
                    armies={armyTypes}
                  />
                ))}
              </tbody>
            </table>
          </div>
          {collapsed ? (
            <>
              <button
                type="button"
                className="show-all analytics-show-all"
                onClick={() => setShownAll(listKey)}
              >
                Show all {rows.length.toLocaleString()}
              </button>
              {/* Without JavaScript every row shows. */}
              <noscript>
                <style>
                  {
                    ".analytics-results-table tbody[data-collapsed] > .analytics-row-more{display:var(--row-display)}:root button.analytics-show-all{display:none}"
                  }
                </style>
              </noscript>
            </>
          ) : null}
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
          {/* What was counted, and how, a tap away under the list. */}
          <details className="analytics-footnote">
            <summary>How these are counted</summary>
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
            {armyTypes || excludedBattleRecords > 0 || partialArmies > 0 ? (
              <p>
                {armyTypes
                  ? "Trophies per attack counts every attack. Every attack is included, even if its army could not be read. The average beside 3-star and trophies per attack is over every attack in this selection. "
                  : excludedBattleRecords > 0
                    ? "Excluded: opponent or army details missing. "
                    : null}
                {partialArmies > 0
                  ? `${partialArmies.toLocaleString()} of the included armies ${partialArmies === 1 ? "is" : "are"} partly readable. Known troops, spells and equipment count; unknown items do not.`
                  : null}
              </p>
            ) : null}
          </details>
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
