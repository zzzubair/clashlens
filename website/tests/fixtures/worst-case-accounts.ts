/**
 * Worst-case but realistic account data for groups, the group comparison and
 * public user pages: names at their limits, right-to-left and styled scripts,
 * missing values, counts of 0 and 1, and a full 20-player group.
 */
import type { ListedGroup, PublicUser } from "../../app/lib/account-contracts";
import type { ComparedPlayer, GroupComparison } from "../../app/lib/group-comparison";
import { worstCasePlayers } from "./worst-case-players";

/** Clash player names, 15 characters at most, as the game allows. */
export const WORST_PLAYER_NAMES = [
  ...worstCasePlayers.map((player) => player.name),
  "محمد⚔️الفارس",
  "전설의클래시왕",
  "ผู้พิชิตตำนาน",
  "🔥🔥🔥",
];

/** Real tags reach ten characters after the hash. */
const TAGS = [
  "#2PP0JLQ8VV",
  "#9LUQ8RJYCV",
  "#Q2L0GYRP9C",
  "#8YQG2VUL0J",
  "#2RJUQ9C0LP",
  "#YL9QG0P8RU",
  "#P0UJ8CQ2YL",
  "#LQ9R0V2GYP",
  "#C8P2YQ0LJU",
  "#G0Y9PLQ2RV",
  "#U2QJ0LYP9C",
  "#V9C0LRQ8GY",
  "#R8L2PQY0JU",
  "#J0GQ9U2PLY",
  "#2YQ8LC0RUJ",
  "#PQL0Y9G2VR",
  "#8C0J2LQPYU",
  "#Y2RL0Q9PGC",
  "#L0V8QY2PUJ",
  "#Q9PY2L0GRC",
] as const;

/** Display names and group names allow 80 characters; usernames 32. */
export const WORST_DISPLAY_NAME =
  "Aleksandra Wiśniewska-Kowalczyk | ᴸᴱᴳᴱᴺᴰ pusher since 2014 | ⚔️ محمد الفارس";
export const WORST_USERNAME = "aleksandra_wisniewska_kowalczyk1";
export const WORST_GROUP_NAME =
  "https://discord.gg/legend-league-pushers-family-and-friends-main-alt-season-3";

const DAY_MS = 86_400_000;
const LAST_DAY = Date.parse("2026-08-05T05:00:00Z");

function dayStarts(days: number): string[] {
  return Array.from({ length: days }, (_, index) =>
    new Date(LAST_DAY - (days - 1 - index) * DAY_MS).toISOString(),
  );
}

const STATUSES: ComparedPlayer["status"][] = [
  "tracking",
  "tracking",
  "tracking",
  "not_in_legend",
  "tracking",
  "failed",
  "tracking",
  "checking",
  "tracking",
  "uncertain",
  "tracking",
  "unknown",
  "tracking",
  "not_found",
  "tracking",
  "tracking",
  "tracking",
  "tracking",
  "tracking",
  "tracking",
];

function comparedPlayer(index: number, days: number): ComparedPlayer {
  const starts = dayStarts(days);
  const tracking = STATUSES[index] === "tracking";
  const quiet = index % 7 === 5;
  const attacks = quiet ? 0 : index === 2 ? 1 : days * 8;
  const states = ["complete", "correcting", "partial", "missing", "retired"] as const;
  return {
    tag: TAGS[index],
    name: index === 12 ? null : WORST_PLAYER_NAMES[index % WORST_PLAYER_NAMES.length],
    you: index === 0 || index === 19,
    inGroup: index !== 19,
    status: STATUSES[index],
    trophies: index === 4 || !tracking ? null : 6_500 - index * 85,
    seasonResetPending: index === 4,
    observedAt: tracking ? "2025-06-01T05:00:00.000Z" : null,
    ageSeconds: tracking ? 37_000_000 + index : null,
    freshness: tracking ? (index % 2 === 0 ? "stale" : "fresh") : null,
    today: tracking
      ? {
          net: index === 6 ? null : 320 - index * 40,
          gained: 320,
          lost: 320,
          attacks: 8,
          defenses: index % 2,
        }
      : null,
    days: starts.map((start, day) => ({
      start,
      state: tracking ? states[(index + day) % states.length] : "missing",
      net: tracking && (index + day) % 5 < 3 ? ((day % 3) - 1) * 320 : null,
    })),
    countedDays: tracking ? (index === 2 ? 1 : days) : 0,
    countedAttacks: tracking ? attacks : 0,
    net: tracking ? -1_234 + index * 97 : null,
    netPerDay: tracking ? -88.142857 + index : null,
    vsGroup: tracking ? 12.5 - index : null,
    attack: {
      count: attacks,
      stars: Math.round(attacks * 2.83),
      destruction: attacks * 97,
      threeStars: Math.round(attacks * 0.83),
      trophies: attacks * 34,
    },
    defense: {
      count: quiet ? 0 : index === 3 ? 1 : days * 8,
      stars: quiet ? 0 : index === 3 ? 1 : days * 8 * 2,
      destruction: quiet ? 0 : index === 3 ? 41 : days * 8 * 88,
      trophies: quiet ? 0 : index === 3 ? 5 : days * 8 * 30,
      starCounts: quiet
        ? [0, 0, 0, 0]
        : index === 3
          ? [0, 1, 0, 0]
          : [1, 1, days * 8 - 3, 1],
    },
  };
}

/** A full 20-player comparison over 14 days, plus the signed-in player outside it. */
export function worstComparison(days: 3 | 7 | 14 = 14): GroupComparison {
  return {
    groupId: "6c1e3f8a-2a44-4b7d-9c0e-1f2a3b4c5d6e",
    name: WORST_GROUP_NAME,
    days,
    dayStarts: dayStarts(days),
    todayStart: new Date(LAST_DAY + DAY_MS).toISOString(),
    generatedAt: new Date(LAST_DAY + DAY_MS + 3_600_000).toISOString(),
    // The Season these days fall in: first Reset 2026-07-13 05:00 UTC.
    season: "1783918800",
    players: Array.from({ length: 20 }, (_, index) => comparedPlayer(index, days)),
  };
}

const PLAYER_STATES: ListedGroup["players"][number]["state"][] = [
  "tracking",
  "not_in_legend",
  "uncertain",
  "checking",
  "unknown",
  "not_found",
  "failed",
];

/** A full group, a one-player group and an empty group. */
export function worstGroups(): ListedGroup[] {
  const players = TAGS.map((tag, index) => ({
    tag,
    name: index === 12 ? null : WORST_PLAYER_NAMES[index % WORST_PLAYER_NAMES.length],
    trophies: index % 3 === 2 ? null : 6_500 - index * 85,
    seasonResetPending: index === 8,
    state: index < 7 ? PLAYER_STATES[index] : ("tracking" as const),
  }));
  const group = (groupId: string, name: string, members: typeof players) => ({
    groupId,
    name,
    tags: members.map((player) => player.tag),
    players: members,
  });
  return [
    group("6c1e3f8a-2a44-4b7d-9c0e-1f2a3b4c5d6e", WORST_GROUP_NAME, players),
    group("7d2f4a9b-3b55-4c8e-8d1f-2a3b4c5d6e7f", "ف", players.slice(0, 1)),
    group(
      "8e3a5b0c-4c66-4d9f-9e2a-3b4c5d6e7f80",
      "Clan war family ᴸᴱᴳᴱᴺᴰˢ 🏆 王者荣耀 — mains, alts and friends from our Discord",
      [],
    ),
  ];
}

/** A public user at every limit, with one card per lookup state. */
export function worstPublicUser(): PublicUser {
  const card = (
    index: number,
    overrides: Partial<PublicUser["verifiedPlayers"][number]>,
  ) => ({
    tag: TAGS[index],
    name: WORST_PLAYER_NAMES[index % WORST_PLAYER_NAMES.length] as string | null,
    clan: "ᴸᴱᴳᴱᴺᴰˢ ᴼᶠ ᴵᴿᴬᴺ" as string | null,
    state: "tracking" as const,
    reason: null,
    trophies: 6_499 - index,
    seasonResetPending: false,
    rank: 13_204 + index,
    league: null as string | null,
    today: { net: -320 + index * 40, attacks: 8, defenses: 1 },
    ...overrides,
  });
  return {
    username: WORST_USERNAME,
    displayName: WORST_DISPLAY_NAME,
    verifiedPlayers: [
      card(0, {}),
      card(1, {
        clan: "نخبة العرب ⚔️",
        rank: 1,
        today: { net: 0, attacks: 0, defenses: 0 },
      }),
      card(3, { clan: null, rank: null, today: { net: null, attacks: 1, defenses: 8 } }),
      card(4, {
        trophies: null,
        seasonResetPending: true,
        rank: null,
        today: null,
        reason: "season_unconfirmed",
      }),
      card(5, {
        name: null,
        clan: null,
        state: "not_in_legend",
        trophies: 4_812,
        rank: null,
        league: "Electro League 33",
      }),
      card(6, { state: "failed", trophies: null, rank: null, today: null }),
    ],
  };
}
