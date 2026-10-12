import type { LinkedPlayerCard, ListedGroup } from "../lib/account-contracts";
import type { PlayerPage, RankedDaySummary } from "../lib/contracts";
import type {
  CardId,
  DashboardSettings,
  LegendsHeld,
  OpponentRow,
  RankRange,
} from "../lib/dashboard";
import {
  GOAL_PACE_DAYS,
  SEASON_START_TROPHIES,
  around,
  type CardDataMap,
  type CardDataSet,
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
  legendsHeld: LegendsHeld | null;
  openDefenses: number | null;
  automaticDefenseEach: number | null;
  opponents: OpponentRow[];
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
      if (!isRecord(defense) || typeof defense.yours !== "boolean") malformed();
      return { stars: count(defense.stars, 3), yours: defense.yours };
    }),
    observedAtMs: nullable(value.observed_at, time),
  };
}

export async function getPlayerToday(tag: string): Promise<PlayerToday> {
  if (!isCanonicalPlayerTag(tag)) throw new PythonApiError(422, { error: "invalid_tag" });
  const payload = await requestJson<unknown>(
    `/v1/players/${encodeURIComponent(tag)}/today`,
    "GET",
    undefined,
    undefined,
  );
  if (!isRecord(payload) || payload.tag !== tag || !Array.isArray(payload.opponents))
    malformed();
  const range = payload.rank_range;
  const held = payload.legends_held;
  return {
    rankRange: nullable(range, (item) => {
      if (!isRecord(item)) malformed();
      const best = count(item.best);
      const worst = count(item.worst);
      if (best < 1 || worst < best) malformed();
      return { best, worst };
    }),
    legendsHeld: nullable(held, (item) => {
      if (!isRecord(item)) malformed();
      const defenses = count(item.defenses);
      const heldCount = count(item.held, defenses);
      return { held: heldCount, defenses };
    }),
    openDefenses: nullable(payload.open_defenses, (item) => count(item, 8)),
    automaticDefenseEach: nullable(payload.automatic_defense_each, (item) => count(item)),
    opponents: payload.opponents.slice(0, 8).map(opponent),
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
const BOARD_PAGE = 10;

/** Player pages read once per load, however many cards use them. */
type PageReader = (tag: string) => Promise<PlayerPage | null>;

function periodStart(day: RankedDaySummary): number {
  return Date.parse(day.period.split(" – ")[0] ?? "");
}

/**
 * The Season's finished days for one player. A day's end is the next day's
 * Reset reading when that is saved, otherwise its start plus its change.
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
    const net = day.trophyChange ?? (gain !== null && loss !== null ? gain - loss : null);
    const attacks =
      day.offense.attacks ?? (day.battlesComplete ? day.offenseEvents.length : null);
    const defenses =
      day.defense.defenses ?? (day.battlesComplete ? day.defenseEvents.length : null);
    const tripledOn = day.defense.threeStarsAgainst;
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
        (day.startTrophies != null && net !== null ? day.startTrophies + net : null),
      rank: day.resetRank ?? null,
      hits: [...day.offenseEvents]
        .sort((a, b) => Date.parse(a.battleTimestamp) - Date.parse(b.battleTimestamp))
        .map((event) => [event.stars, event.trophyChange]),
    });
  }
  return { today: season.currentDayNumber, days };
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

/** The trophies at one rank of a board; null when the board or the rank is missing. */
async function trophiesAt(
  ctx: CardContext,
  rank: number,
  selector?: { officialSeasonId: string; dayNumber: number },
) {
  const board = await ctx.client
    .getTrackedLeaderboard(1, selector ? "daily" : "live", rank - 1, selector)
    .catch(() => null);
  const entry = board?.entries[0];
  return entry && entry.rank === rank ? entry : null;
}

function observedMs(entries: { freshness: { observedAt: string } }[]): number | null {
  const times = entries.map((entry) => Date.parse(entry.freshness.observedAt));
  return times.length && times.every(Number.isFinite) ? Math.min(...times) : null;
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
  const [line, lineThen] =
    finished === 0
      ? [null, null]
      : await Promise.all([lineOn(finished), lineOn(firstDay)]);
  return {
    rank: ctx.settings.goal,
    name: ctx.selected.name ?? ctx.selected.tag,
    finished,
    window,
    you: finished === 0 ? null : end(finished),
    youThen: finished === 0 ? null : end(firstDay),
    line,
    lineThen,
    history: Array.from({ length: finished === 0 ? 0 : window + 1 }, (_, index) =>
      end(firstDay + index),
    ),
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
  const saved = selector
    ? await Promise.all(lines.map((top) => trophiesAt(ctx, top, selector)))
    : null;
  return {
    name: ctx.selected.name ?? ctx.selected.tag,
    live: {
      lines: lines.map((top, index) => ({
        top,
        trophies: live[index]?.trophies ?? null,
      })),
      you: ctx.selected.seasonResetPending ? null : ctx.selected.trophies,
      observedAtMs: observedMs(live.filter((entry) => entry !== null)),
    },
    reset: saved
      ? {
          lines: lines.map((top, index) => ({
            top,
            trophies: saved[index]?.trophies ?? null,
          })),
          you: reset?.you?.endTrophies ?? null,
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
  const total = (pick: (day: SeasonDay) => number | null) =>
    days.reduce((sum, day) => sum + (pick(day) ?? 0), 0);
  const attacks = total((day) => day.attacks);
  return {
    hitRate: attacks ? total((day) => day.triples) / attacks : null,
    offense: days.length ? total((day) => day.offense) / days.length : null,
    defense: days.length ? total((day) => day.defense) / days.length : null,
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
    const board = await ctx.client.getTrackedLeaderboard(1, "live", 0, undefined, tag);
    const index = board.entries.findIndex((entry) => entry.tag === tag);
    const shown = around(board.entries, index, RIVALS_ABOVE, RIVALS_BELOW);
    const rows = await Promise.all(
      shown.map(async (entry): Promise<RivalRow> => {
        const page = await pages(entry.tag);
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
      }),
    );
    return { rows, observedAtMs: observedMs(shown) };
  };
  const seasonRows = async () => {
    const rank = reset?.you?.rank ?? null;
    if (reset === null || rank === null || reset.seasonId === null) return null;
    const selector = { officialSeasonId: reset.seasonId, dayNumber: reset.finished };
    const read = (offset: number) =>
      ctx.client.getTrackedLeaderboard(BOARD_PAGE, "daily", offset, selector);
    const home = Math.floor((rank - 1) / BOARD_PAGE) * BOARD_PAGE;
    const first = await read(home);
    const total = first.totalEntries;
    const start = Math.max(
      1,
      Math.min(rank - RIVALS_ABOVE, total - RIVALS_ABOVE - RIVALS_BELOW),
    );
    const stop = Math.min(total, start + RIVALS_ABOVE + RIVALS_BELOW);
    const offsets = new Set(
      [start, stop].map((place) => Math.floor((place - 1) / BOARD_PAGE) * BOARD_PAGE),
    );
    offsets.delete(home);
    const others = await Promise.all([...offsets].map(read));
    const entries = [first, ...others]
      .flatMap((board) => board.entries)
      .filter((entry) => entry.rank >= start && entry.rank <= stop)
      .sort((a, b) => a.rank - b.rank);
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
  const [live, season] = await Promise.all([
    liveRows().catch(() => null),
    seasonRows().catch(() => null),
  ]);
  return { live, season };
}

function comparedRow(player: ComparedPlayer, name: string | null): GroupRow {
  const days = player.countedDays;
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
    const counted = season.days.length;
    const total = (pick: (day: SeasonDay) => number | null) =>
      season.days.reduce((sum, day) => sum + (pick(day) ?? 0), 0);
    const attacks = total((day) => day.attacks);
    return [
      {
        tag,
        name: page.profile.name || tag,
        you: tag === selected,
        trophies: page.profile.seasonResetPending ? null : page.profile.trophies,
        gain: counted ? total((day) => day.net) / counted : null,
        hitRate: attacks ? total((day) => day.triples) / attacks : null,
        offense: counted ? total((day) => day.offense) / counted : null,
        defense: counted ? total((day) => day.defense) / counted : null,
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
 * Read the data for each built card on the page. Cards share one read of
 * each player page; a card whose reads fail gets null and says so, without
 * taking the other cards down.
 */
export async function loadCardData(
  cards: readonly CardId[],
  ctx: CardContext,
  identity: GoogleAccountIdentity,
): Promise<CardDataSet> {
  const read = new Map<string, Promise<PlayerPage | null>>();
  const pages: PageReader = (tag) => {
    if (!read.has(tag))
      read.set(
        tag,
        ctx.client.getPlayer(tag).catch(() => null),
      );
    return read.get(tag)!;
  };
  const season = async () => {
    const page = await pages(ctx.selected.tag);
    return page === null ? null : seasonData(page);
  };
  const readers: {
    [Card in keyof CardDataMap]: () => Promise<CardDataMap[Card] | null>;
  } = {
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
  const loaded = await Promise.all(
    cards
      .filter((card): card is keyof CardDataMap => Object.hasOwn(readers, card))
      .map(async (card) => [card, await readers[card]().catch(() => null)] as const),
  );
  return Object.fromEntries(loaded) as CardDataSet;
}
