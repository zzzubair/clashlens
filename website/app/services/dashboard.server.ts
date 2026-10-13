import type { LinkedPlayerCard, ListedGroup } from "../lib/account-contracts";
import type {
  PlayerPage,
  RankedDaySummary,
  TrackedLeaderboard,
  TrackedPlayerEntry,
} from "../lib/contracts";
import type {
  CardId,
  DashboardSettings,
  LegendsHeld,
  OpponentRow,
  PlayerDay,
  RankRange,
} from "../lib/dashboard";
import { nextResetMs } from "../lib/dashboard";
import {
  DAILY_BATTLES,
  GOAL_PACE_DAYS,
  SEASON_START_TROPHIES,
  rate,
  type CardDataMap,
  type CardReads,
  type ClockData,
  type CutoffsData,
  type GoalData,
  type GroupRow,
  type GroupWatchData,
  type RivalRow,
  type RivalsData,
  type SeasonData,
  type SeasonDay,
} from "../lib/dashboard-cards";
import type {
  ArmyHits,
  ArmyLens,
  ArmyOutcome,
  PlayerArmies,
} from "../lib/dashboard-armies";
import { ARMY_DEFAULT_DAYS, isArmyDays } from "../lib/dashboard-armies";
import type { ComparedPlayer } from "../lib/group-comparison";
import { getGroupComparison } from "./group-comparison.server";
import {
  requestJson,
  type GoogleAccountIdentity,
  type PythonClient,
} from "./python.server";
import {
  PythonApiError,
  isCanonicalPlayerTag,
  isInteger,
  isRecord,
  isString,
} from "./python-response.server";

/** One player's dashboard numbers for today, from GET /v1/players/{tag}/today. */
export interface PlayerToday {
  rankRange: RankRange | null;
  openDefenses: number | null;
  automaticDefenseEach: number | null;
  /** Null until the player's battles for today are saved. */
  opponents: OpponentRow[] | null;
  /** When the board behind the range and the bases' comparisons was read; null without one. */
  boardComputedAtMs: number | null;
}

function malformed(): never {
  throw new PythonApiError(502, { error: "malformed" });
}

function count(value: unknown, max = Number.MAX_SAFE_INTEGER): number {
  if (!isInteger(value) || value < 0 || value > max) malformed();
  return value;
}

function nullable<T>(value: unknown, read: (value: unknown) => T): T | null {
  return value === null ? null : read(value);
}

function time(value: unknown): number {
  const parsed = isString(value) ? Date.parse(value) : Number.NaN;
  if (!Number.isFinite(parsed)) malformed();
  return parsed;
}

function opponent(value: unknown): OpponentRow {
  if (
    !isRecord(value) ||
    !isCanonicalPlayerTag(value.tag) ||
    !(value.name === null || isString(value.name)) ||
    !isRecord(value.hit) ||
    !Array.isArray(value.defenses)
  )
    malformed();
  const hit = value.hit;
  if (!isInteger(hit.trophy_change)) malformed();
  return {
    tag: value.tag,
    name: value.name,
    resetTrophies: nullable(value.reset_trophies, (item) => count(item)),
    hit: {
      stars: count(hit.stars, 3),
      destruction: count(hit.destruction_percentage, 100),
      trophyChange: hit.trophy_change,
      at: time(hit.battle_timestamp),
    },
    defenses: value.defenses.map((defense) => {
      if (
        !isRecord(defense) ||
        typeof defense.yours !== "boolean" ||
        !isInteger(defense.trophy_change) ||
        defense.trophy_change > 0
      )
        malformed();
      return {
        stars: count(defense.stars, 3),
        trophyChange: defense.trophy_change,
        yours: defense.yours,
      };
    }),
    observedAtMs: nullable(value.observed_at, time),
    legends: nullable(value.legends_held, legendsHeld),
  };
}

function legendsHeld(value: unknown): LegendsHeld {
  if (!isRecord(value)) malformed();
  const defenses = count(value.defenses);
  return { held: count(value.held, defenses), defenses };
}

export async function getPlayerToday(tag: string): Promise<PlayerToday> {
  if (!isCanonicalPlayerTag(tag)) throw new PythonApiError(422, { error: "invalid_tag" });
  const payload = await requestJson<unknown>(
    `/v1/players/${encodeURIComponent(tag)}/today`,
    "GET",
    undefined,
    undefined,
  );
  if (!isRecord(payload) || payload.tag !== tag) malformed();
  const range = payload.rank_range;
  return {
    rankRange: nullable(range, (item) => {
      if (!isRecord(item)) malformed();
      const best = count(item.best);
      const worst = count(item.worst);
      if (best < 1 || worst < best) malformed();
      return { best, worst };
    }),
    openDefenses: nullable(payload.open_defenses, (item) => count(item, 8)),
    automaticDefenseEach: nullable(payload.automatic_defense_each, (item) => count(item)),
    opponents: nullable(payload.opponents, (items) => {
      if (!Array.isArray(items)) malformed();
      return items.slice(0, 8).map(opponent);
    }),
    boardComputedAtMs: nullable(payload.board_computed_at, time),
  };
}

/** What the card reads need to know about the signed-in user. */
export interface CardContext {
  client: PythonClient;
  /** The account the cards follow; always in Legend I. */
  selected: LinkedPlayerCard;
  settings: DashboardSettings;
}

const DAY_MS = 86_400_000;
const SEASON_SECONDS = 28 * 86_400;
const RIVALS_ABOVE = 3;
const RIVALS_BELOW = 3;
const RIVAL_ROWS = RIVALS_ABOVE + RIVALS_BELOW + 1;
const BOARD_PAGE = 10;

/** Player pages read once per load, however many cards use them. */
type PageReader = (tag: string) => Promise<PlayerPage | null>;

function periodStart(day: RankedDaySummary): number {
  return Date.parse(day.period.split(" – ")[0] ?? "");
}

/**
 * The Season's finished days for one player. A day's end is the next day's
 * Reset reading when that is saved, otherwise its start plus its change and
 * any weekly or Season reset at its closing Reset. Python leaves a total unknown when battles may be missing, and so does
 * this; the battles' sum stands in only when Python found every battle.
 */
export function seasonData(page: PlayerPage): SeasonData {
  const season = page.season;
  if (season === null) return { today: null, days: [] };
  const anchor = Date.parse(season.anchor);
  const saved = new Map<number, RankedDaySummary>();
  for (const day of [
    ...page.recentDays,
    ...page.seasonDays,
    ...(page.currentDay ? [page.currentDay] : []),
  ]) {
    const number = Math.floor((periodStart(day) - anchor) / DAY_MS) + 1;
    if (number >= 1 && number <= season.currentDayNumber && !saved.has(number))
      saved.set(number, day);
  }
  const days: SeasonDay[] = [];
  for (let number = 1; number < season.currentDayNumber; number++) {
    const day = saved.get(number);
    if (!day) continue;
    const next = saved.get(number + 1);
    const gain = day.offense.trophyGain;
    const loss = day.defense.trophyLoss;
    const net =
      day.trophyChange ??
      (day.battlesComplete && gain !== null && loss !== null ? gain - loss : null);
    const attacks = day.offense.attacks;
    const defenses = day.defense.defenses;
    const tripledOn = day.defense.threeStarsAgainst;
    const everyAttack =
      attacks === day.offenseEvents.length &&
      gain === day.offenseEvents.reduce((sum, event) => sum + event.trophyChange, 0) &&
      !day.offenseEvents.some((event) => event.perspectiveDisagreement) &&
      day.offenseEvents.length <= DAILY_BATTLES &&
      (attacks === DAILY_BATTLES || day.logCoverage === "complete");
    days.push({
      day: number,
      attacks,
      defenses,
      offense: gain,
      defense: loss === null ? null : -loss,
      net,
      triples: day.offense.threeStars,
      held: defenses === null || tripledOn === null ? null : defenses - tripledOn,
      endTrophies:
        next?.startTrophies ??
        (day.startTrophies != null && net !== null
          ? day.startTrophies + net + (day.resetAdjustment?.amount ?? 0)
          : null),
      rank: day.resetRank ?? null,
      hits: everyAttack
        ? [...day.offenseEvents]
            .sort((a, b) => Date.parse(a.battleTimestamp) - Date.parse(b.battleTimestamp))
            .map((event) => [event.stars, event.trophyChange])
        : null,
    });
  }
  return { today: season.currentDayNumber, days };
}

/**
 * The selected account's Legend day for the Legend day card: the player
 * page's battles and totals, and the automatic defense loss from today's
 * read.
 */
function legendDay(page: PlayerPage, today: PlayerToday | null): PlayerDay {
  const day = page.currentDay;
  const complete = day?.battlesComplete === true;
  const gain = day?.offense.trophyGain ?? null;
  const loss = day?.defense.trophyLoss ?? null;
  // The finished day that ended at the Reset that started today.
  const dayStartMs = nextResetMs(Date.now()) - DAY_MS;
  const previous = [...page.recentDays, ...page.seasonDays].find(
    (finished) => Date.parse(finished.period.split(" – ")[1] ?? "") === dayStartMs,
  );
  const [profileAt, battlesAt] = [
    page.profile.freshness.observedAt,
    page.currentDayPublishedAt,
  ].map((value) => (value ? Date.parse(value) : Number.NaN));
  const observed = [profileAt, battlesAt].filter(Number.isFinite);
  // A range shown is as old as the board it came from.
  if (today?.rankRange && today.boardComputedAtMs !== null)
    observed.push(today.boardComputedAtMs);
  return {
    dayNumber: page.season?.currentDayNumber ?? null,
    dayCount: page.season?.dayCount ?? null,
    battles: clockData(page).battles,
    complete,
    net:
      day?.trophyChange ??
      (complete && gain !== null && loss !== null ? gain - loss : null),
    attacks: day?.offense.attacks ?? null,
    defenses: day?.defense.defenses ?? null,
    lastResetRank: previous?.resetRank ?? null,
    trophies: page.profile.seasonResetPending ? null : page.profile.trophies,
    // The oldest of the trophy, battle and range reads, so the card never looks newer than it is.
    observedAtMs: observed.length ? Math.min(...observed) : null,
    battlesObservedAtMs: Number.isFinite(battlesAt) ? battlesAt : null,
    openDefenses: today?.openDefenses ?? null,
    autoDefenseEach: today?.automaticDefenseEach ?? null,
  };
}

function clockData(page: PlayerPage): ClockData {
  const day = page.currentDay;
  const battles = day
    ? [
        ...day.offenseEvents.map((event) => ({ event, kind: "attack" as const })),
        ...day.defenseEvents.map((event) => ({ event, kind: "defense" as const })),
      ]
        .map(({ event, kind }) => ({
          at: Date.parse(event.battleTimestamp),
          kind,
          stars: event.stars,
          destruction: event.destructionPercentage,
          trophyChange: event.trophyChange,
          opponent: event.opponent.name,
        }))
        .filter((battle) => Number.isFinite(battle.at))
    : [];
  const published = page.currentDayPublishedAt
    ? Date.parse(page.currentDayPublishedAt)
    : NaN;
  return { battles, observedAtMs: Number.isFinite(published) ? published : null };
}

/** The selected account's Season, with the board selector for its last Reset. */
async function lastReset(ctx: CardContext, pages: PageReader) {
  const page = await pages(ctx.selected.tag);
  if (page === null) return null;
  const season = seasonData(page);
  const finished = season.today === null ? 0 : season.today - 1;
  return {
    page,
    season,
    finished,
    seasonId: page.season?.id ?? null,
    you: season.days.find((day) => day.day === finished) ?? null,
  };
}

/** What `read` returns, or null when Python says it is not there. */
function unlessMissing<T>(read: Promise<T>): Promise<T | null> {
  return read.catch((cause: unknown) => {
    if (cause instanceof PythonApiError && cause.status === 404) return null;
    throw cause;
  });
}

/** The trophies at one rank of a board; null when the board or the rank is missing. */
async function trophiesAt(
  ctx: CardContext,
  rank: number,
  selector?: { officialSeasonId: string; dayNumber: number },
) {
  const board = await unlessMissing(
    ctx.client.getTrackedLeaderboard(1, selector ? "daily" : "live", rank - 1, selector),
  );
  const entry = board?.entries[0];
  return entry && entry.rank === rank ? entry : null;
}

/**
 * Your trophies on one day's saved Reset board, taken before the automatic
 * defense loss like every line on it; null when it does not hold you at your
 * saved rank.
 */
async function boardTrophies(
  ctx: CardContext,
  seasonId: string | null,
  day: SeasonDay | null | undefined,
) {
  const rank = day?.rank ?? null;
  if (seasonId === null || !day || rank === null) return null;
  const entry = await trophiesAt(ctx, rank, {
    officialSeasonId: seasonId,
    dayNumber: day.day,
  });
  return entry?.tag === ctx.selected.tag ? entry.trophies : null;
}

/** The oldest of the readings a card shows; null when any of them has no time. */
function observedMs(times: (string | null | undefined)[]): number | null {
  const parsed = times.map((time) => Date.parse(time ?? ""));
  return parsed.length && parsed.every(Number.isFinite) ? Math.min(...parsed) : null;
}

/**
 * The places from 3 above to 3 below `rank`, filled from the other side at
 * either end of a board of `total`.
 */
function rivalPlaces(rank: number, total: number): number[] {
  const start = Math.max(
    1,
    Math.min(rank - RIVALS_ABOVE, total - RIVALS_ABOVE - RIVALS_BELOW),
  );
  const stop = Math.min(total, start + RIVALS_ABOVE + RIVALS_BELOW);
  return Array.from({ length: stop - start + 1 }, (_, index) => start + index);
}

/** The rows at `places` in `entries`, each player once. */
function rowsAt(entries: TrackedPlayerEntry[], places: number[]): TrackedPlayerEntry[] {
  const tags = new Set<string>();
  return places.flatMap((place) => {
    const entry = entries.find((row) => row.rank === place);
    if (entry === undefined || tags.has(entry.tag)) return [];
    tags.add(entry.tag);
    return [entry];
  });
}

/** The rows `first` holds around `rank`, plus any page of 10 holding the rest. */
async function rivalEntries(
  first: TrackedLeaderboard,
  rank: number,
  read: (offset: number) => Promise<TrackedLeaderboard>,
): Promise<TrackedPlayerEntry[]> {
  const places = rivalPlaces(rank, first.totalEntries);
  const held = new Set(first.entries.map((entry) => entry.rank));
  const offsets = new Set(
    places
      .filter((place) => !held.has(place))
      .map((place) => Math.floor((place - 1) / BOARD_PAGE) * BOARD_PAGE),
  );
  const others = await Promise.all([...offsets].map(read));
  return rowsAt(
    [first, ...others].flatMap((board) => board.entries),
    places,
  );
}

/** The rows around `tag` that one `board` reply holds, or null when `tag` is off it. */
function rivalsIn(board: TrackedLeaderboard, tag: string): TrackedPlayerEntry[] | null {
  const rank = board.entries.find((entry) => entry.tag === tag)?.rank;
  return rank === undefined
    ? null
    : rowsAt(board.entries, rivalPlaces(rank, board.totalEntries));
}

async function goalData(ctx: CardContext, pages: PageReader): Promise<GoalData | null> {
  const reset = await lastReset(ctx, pages);
  if (reset === null) return null;
  const { finished, season, seasonId } = reset;
  const window = Math.min(GOAL_PACE_DAYS, finished);
  const firstDay = finished - window;
  const end = (day: number) =>
    day === 0
      ? SEASON_START_TROPHIES
      : (season.days.find((saved) => saved.day === day)?.endTrophies ?? null);
  const lineOn = async (day: number) => {
    if (day === 0) return SEASON_START_TROPHIES;
    if (seasonId === null) return null;
    const entry = await trophiesAt(ctx, ctx.settings.goal, {
      officialSeasonId: seasonId,
      dayNumber: day,
    });
    return entry?.trophies ?? null;
  };
  const onBoard = (day: number) =>
    day === 0
      ? SEASON_START_TROPHIES
      : boardTrophies(
          ctx,
          seasonId,
          season.days.find((saved) => saved.day === day),
        );
  const [line, lineThen, history] =
    finished === 0
      ? [null, null, []]
      : await Promise.all([
          lineOn(finished),
          lineOn(firstDay),
          Promise.all(
            Array.from({ length: window + 1 }, (_, index) => onBoard(firstDay + index)),
          ),
        ]);
  return {
    rank: ctx.settings.goal,
    name: ctx.selected.name ?? ctx.selected.tag,
    finished,
    window,
    you: finished === 0 ? null : end(finished),
    youThen: finished === 0 ? null : end(firstDay),
    line,
    lineThen,
    history,
  };
}

async function cutoffsData(
  ctx: CardContext,
  pages: PageReader,
): Promise<CutoffsData | null> {
  const reset = await lastReset(ctx, pages);
  const lines = ctx.settings.cutoffs;
  const live = await Promise.all(lines.map((top) => trophiesAt(ctx, top)));
  const selector =
    reset && reset.finished > 0 && reset.seasonId
      ? { officialSeasonId: reset.seasonId, dayNumber: reset.finished }
      : null;
  const [saved, youSaved] = selector
    ? await Promise.all([
        Promise.all(lines.map((top) => trophiesAt(ctx, top, selector))),
        boardTrophies(ctx, selector.officialSeasonId, reset?.you),
      ])
    : [null, null];
  const profile = reset?.page.profile ?? null;
  const you = profile === null || profile.seasonResetPending ? null : profile.trophies;
  return {
    name: ctx.selected.name ?? ctx.selected.tag,
    live: {
      lines: lines.map((top, index) => ({
        top,
        trophies: live[index]?.trophies ?? null,
      })),
      you,
      observedAtMs: observedMs([
        ...live.flatMap((entry) => (entry ? [entry.freshness.observedAt] : [])),
        ...(you === null ? [] : [profile?.freshness.observedAt]),
      ]),
    },
    reset: saved
      ? {
          lines: lines.map((top, index) => ({
            top,
            trophies: saved[index]?.trophies ?? null,
          })),
          you: youSaved,
        }
      : null,
  };
}

/** Today's totals, or the Season's averages a day, for one rival. */
function rivalNumbers(page: PlayerPage | null, mode: "live" | "season") {
  if (page === null) return { hitRate: null, offense: null, defense: null };
  if (mode === "live") {
    const day = page.currentDay;
    const attacks = day?.offense.attacks ?? null;
    const triples = day?.offense.threeStars ?? null;
    const loss = day?.defense.trophyLoss ?? null;
    return {
      hitRate: attacks && triples !== null ? triples / attacks : null,
      offense: day?.offense.trophyGain ?? null,
      defense: loss === null ? null : -loss,
    };
  }
  const days = seasonData(page).days;
  return {
    hitRate: rate(
      days,
      (day) => day.triples,
      (day) => day.attacks,
    ),
    offense: rate(days, (day) => day.offense),
    defense: rate(days, (day) => day.defense),
  };
}

async function rivalsData(
  ctx: CardContext,
  pages: PageReader,
): Promise<RivalsData | null> {
  const tag = ctx.selected.tag;
  const reset = await lastReset(ctx, pages);
  const lastRank = (page: PlayerPage | null) => {
    if (page === null) return null;
    const season = seasonData(page);
    return season.today === null
      ? null
      : (season.days.find((day) => day.day === season.today! - 1)?.rank ?? null);
  };
  const liveRows = async () => {
    // A read focused on one player holds their page of 7 and 5 places either
    // side, so all 7 rows everywhere but the board's last place. There, a read
    // focused on the fourth row it holds is used only if it alone holds all 7.
    const focused = (on: string) =>
      unlessMissing(
        ctx.client.getTrackedLeaderboard(RIVAL_ROWS, "live", 0, undefined, on),
      );
    const whole = (board: TrackedLeaderboard | null) => {
      if (board === null) return null;
      const rows = rivalsIn(board, tag);
      return rows?.length === Math.min(board.totalEntries, RIVAL_ROWS) ? rows : null;
    };
    const first = await focused(tag);
    const near = first && rivalsIn(first, tag);
    if (!near) return null;
    const middle = near[RIVALS_ABOVE];
    const shown = whole(first) ?? (middle && whole(await focused(middle.tag))) ?? near;
    const read = await Promise.all(
      shown.map(async (entry) => [entry, await pages(entry.tag)] as const),
    );
    const rows = read.map(([entry, page]): RivalRow => {
      const before = lastRank(page);
      return {
        tag: entry.tag,
        name: entry.name,
        rank: entry.rank,
        trophies: entry.trophies,
        you: entry.tag === tag,
        moved: before === null ? null : before - entry.rank,
        ...rivalNumbers(page, "live"),
      };
    });
    // Trophies come from the board; today's numbers from each player's battles.
    const times = [
      ...shown.map((entry) => entry.freshness.observedAt),
      ...read.flatMap(([, page]) =>
        page?.currentDay ? [page.currentDayPublishedAt] : [],
      ),
    ];
    return { rows, observedAtMs: observedMs(times) };
  };
  const seasonRows = async () => {
    const rank = reset?.you?.rank ?? null;
    if (reset === null || rank === null || reset.seasonId === null) return null;
    const selector = { officialSeasonId: reset.seasonId, dayNumber: reset.finished };
    const read = (offset: number) =>
      ctx.client.getTrackedLeaderboard(BOARD_PAGE, "daily", offset, selector);
    const first = await unlessMissing(
      read(Math.floor((rank - 1) / BOARD_PAGE) * BOARD_PAGE),
    );
    if (first === null) return null;
    const entries = await rivalEntries(first, rank, read);
    const rows = await Promise.all(
      entries.map(async (entry): Promise<RivalRow> => ({
        tag: entry.tag,
        name: entry.name,
        rank: entry.rank,
        trophies: entry.trophies,
        you: entry.tag === tag,
        moved: null,
        ...rivalNumbers(await pages(entry.tag), "season"),
      })),
    );
    return { rows, days: reset.finished };
  };
  const [live, season] = await Promise.all([liveRows(), seasonRows()]);
  return { live, season };
}

function comparedRow(player: ComparedPlayer, name: string | null): GroupRow {
  // The battle totals cover every saved day, not only the days whose result
  // counts, so a day's averages divide by the saved days.
  const days = player.days.filter(
    (day) => day.state !== "missing" && day.state !== "retired",
  ).length;
  return {
    tag: player.tag,
    name: name ?? player.name ?? player.tag,
    you: player.you,
    trophies: player.trophies,
    gain: player.netPerDay,
    hitRate: player.attack.count ? player.attack.threeStars / player.attack.count : null,
    offense: days ? player.attack.trophies / days : null,
    defense: days ? -player.defense.trophies / days : null,
  };
}

async function groupWatchData(
  ctx: CardContext,
  pages: PageReader,
  identity: GoogleAccountIdentity,
): Promise<GroupWatchData | null> {
  const { groups } = await ctx.client.listGroups();
  // Oldest first; a group without a creation time sorts last, by name.
  const made = (item: ListedGroup) =>
    item.createdAt ? Date.parse(item.createdAt) : Number.POSITIVE_INFINITY;
  const ordered = [...groups].sort(
    (a, b) => made(a) - made(b) || a.name.localeCompare(b.name),
  );
  const group =
    ordered.find((item) => item.groupId === ctx.settings.group) ?? ordered[0] ?? null;
  const window = ctx.settings.groupWindow;
  const listed = ordered.map((item) => ({
    id: item.groupId,
    name: item.name,
    members: item.tags.length,
  }));
  if (group === null) return { groups: listed, groupId: null, window, days: 0, rows: [] };
  const selected = ctx.selected.tag;
  if (window === "7") {
    const comparison = await getGroupComparison(identity, group.groupId, 7);
    // The group's members plus the selected account; never another linked account
    // unless it is a member, and never an account outside Legend I.
    const rows = comparison.players
      .filter(
        (player) =>
          (player.inGroup || player.tag === selected) &&
          player.status !== "not_in_legend",
      )
      .map((player) => comparedRow(player, null))
      .map((row) => ({ ...row, you: row.tag === selected }));
    return {
      groups: listed,
      groupId: group.groupId,
      window,
      days: 7,
      rows: byGain(rows),
    };
  }
  const tags = [...new Set([...group.tags, selected])];
  const read = await Promise.all(
    tags.map(async (tag) => [tag, await pages(tag)] as const),
  );
  let days = 0;
  const rows = read.flatMap(([tag, page]): GroupRow[] => {
    if (page === null || page.trackingState === "not_in_legend") return [];
    const season = seasonData(page);
    days = Math.max(days, season.today === null ? 0 : season.today - 1);
    return [
      {
        tag,
        name: page.profile.name || tag,
        you: tag === selected,
        trophies: page.profile.seasonResetPending ? null : page.profile.trophies,
        gain: rate(season.days, (day) => day.net),
        hitRate: rate(
          season.days,
          (day) => day.triples,
          (day) => day.attacks,
        ),
        offense: rate(season.days, (day) => day.offense),
        defense: rate(season.days, (day) => day.defense),
      },
    ];
  });
  return { groups: listed, groupId: group.groupId, window, days, rows: byGain(rows) };
}

function byGain(rows: GroupRow[]): GroupRow[] {
  return rows.sort(
    (a, b) =>
      (b.gain ?? Number.NEGATIVE_INFINITY) - (a.gain ?? Number.NEGATIVE_INFINITY) ||
      a.name.localeCompare(b.name),
  );
}

async function lastSeasonFinish(ctx: CardContext, page: PlayerPage) {
  if (page.season === null) return null;
  const previous = String(Date.parse(page.season.anchor) / 1000 - SEASON_SECONDS);
  const seasons = await ctx.client.getPlayerSeasons(ctx.selected.tag);
  const history = seasons.find((season) => season.seasonId === previous)?.officialHistory;
  return history ? { rank: history.finalPlacement, trophies: history.eodTrophies } : null;
}

/**
 * Start the reads for each built card on the page, without waiting for
 * them: each card shows as soon as its own data arrives. Cards share one
 * read of each player page; a card whose reads fail gets null and says so,
 * without taking the other cards down.
 */
export function readCards(
  cards: readonly CardId[],
  ctx: CardContext,
  identity: GoogleAccountIdentity,
): CardReads {
  const read = new Map<string, Promise<PlayerPage | null>>();
  const pages: PageReader = (tag) => {
    if (!read.has(tag)) read.set(tag, unlessMissing(ctx.client.getPlayer(tag)));
    return read.get(tag)!;
  };
  const season = async () => {
    const page = await pages(ctx.selected.tag);
    return page === null ? null : seasonData(page);
  };
  // Legend day and Bases you attacked share one read of today's numbers.
  let todayRead: Promise<PlayerToday | null> | undefined;
  const today = () => (todayRead ??= getPlayerToday(ctx.selected.tag).catch(() => null));
  const armies = async (lens: ArmyLens) => ({
    tag: ctx.selected.tag,
    armies: await getPlayerArmies(ctx.selected.tag, lens, ARMY_DEFAULT_DAYS),
  });
  const readers: {
    [Card in keyof CardDataMap]: () => Promise<CardDataMap[Card] | null>;
  } = {
    "legend-day": async () => {
      // Without the page the card still shows the account's live trophies and rank.
      const [page, read] = await Promise.all([
        pages(ctx.selected.tag).catch(() => null),
        today(),
      ]);
      return {
        player: ctx.selected,
        day: page === null ? null : legendDay(page, read),
        range: read?.rankRange ?? null,
      };
    },
    bases: async () => {
      const read = await today();
      return read === null
        ? null
        : { rows: read.opponents, comparedAtMs: read.boardComputedAtMs };
    },
    "armies-defense": () => armies("defense"),
    "armies-offense": () => armies("offense"),
    "legend-clock": async () => {
      const page = await pages(ctx.selected.tag);
      return page === null ? null : clockData(page);
    },
    goal: () => goalData(ctx, pages),
    "rivals-watch": () => rivalsData(ctx, pages),
    "heat-map": season,
    cutoffs: () => cutoffsData(ctx, pages),
    "season-graph": async () => {
      const page = await pages(ctx.selected.tag);
      if (page === null) return null;
      const lastSeason = await lastSeasonFinish(ctx, page).catch(() => null);
      return { ...seasonData(page), lastSeason };
    },
    "recent-form": season,
    "offense-consistency": season,
    "group-watch": () => groupWatchData(ctx, pages, identity),
  };
  return Object.fromEntries(
    cards
      .filter((card): card is keyof CardDataMap => Object.hasOwn(readers, card))
      .map((card) => [card, readers[card]().catch(() => null)]),
  ) as CardReads;
}

/**
 * Wait up to `ms` for the card reads. Those done by then go out with the
 * page; the rest stream in later. React holds a streamed card back for
 * 300 ms after it showed "Loading…", so a quick card is faster this way.
 */
export async function settleWithin(reads: CardReads, ms: number): Promise<CardReads> {
  const settled: Record<string, unknown> = {};
  const done = Object.entries(reads).map(async ([card, read]) => {
    settled[card] = await read;
  });
  let timer: ReturnType<typeof setTimeout> | undefined;
  await Promise.race([
    Promise.all(done),
    new Promise((resolve) => (timer = setTimeout(resolve, ms))),
  ]);
  clearTimeout(timer);
  return Object.fromEntries(
    Object.entries(reads).map(([card, read]) => [
      card,
      Object.hasOwn(settled, card) ? settled[card] : read,
    ]),
  ) as CardReads;
}

function hits(value: unknown): ArmyHits {
  if (!isRecord(value)) malformed();
  const battles = count(value.battles);
  return { battles, threeStars: count(value.three_stars, battles) };
}

function outcome(value: unknown): ArmyOutcome {
  if (!isRecord(value)) malformed();
  return { ...hits(value), trophies: count(value.trophies) };
}

/** One player's attacks or defenses per army type, from GET /v1/players/{tag}/armies. */
export async function getPlayerArmies(
  tag: string,
  lens: ArmyLens,
  days: number,
): Promise<PlayerArmies> {
  if (!isCanonicalPlayerTag(tag) || !isArmyDays(days, lens))
    throw new PythonApiError(422, { error: "invalid_request" });
  const payload = await requestJson<unknown>(
    `/v1/players/${encodeURIComponent(tag)}/armies?lens=${lens}&days=${days}`,
    "GET",
    undefined,
    undefined,
  );
  if (
    !isRecord(payload) ||
    payload.tag !== tag ||
    payload.lens !== lens ||
    payload.days !== days ||
    !Array.isArray(payload.covered_days) ||
    !Array.isArray(payload.rows) ||
    !isRecord(payload.total)
  )
    malformed();
  return {
    lens,
    days,
    coveredDays: payload.covered_days.slice(0, days).map(time),
    rows: payload.rows.slice(0, 64).map((row) => {
      if (!isRecord(row) || !isInteger(row.type_id) || !isString(row.name)) malformed();
      return {
        typeId: row.type_id,
        name: row.name,
        ...outcome(row),
        legends:
          lens === "offense"
            ? hits(row.legends)
            : row.legends === null
              ? null
              : outcome(row.legends),
      };
    }),
    total: outcome(payload.total),
    legendsTotal:
      lens === "defense" && payload.legends_total !== null
        ? outcome(payload.legends_total)
        : null,
  };
}
