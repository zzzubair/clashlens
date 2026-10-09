import type { LegendsHeld, OpponentRow, RankRange } from "../lib/dashboard";
import { requestJson } from "./python.server";
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
