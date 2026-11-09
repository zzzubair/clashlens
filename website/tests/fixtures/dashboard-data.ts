import type { LinkedPlayerCard } from "../../app/lib/account-contracts";
import type { ClockBattle, PlayerDay } from "../../app/lib/dashboard";

export function linked(
  tag: string,
  overrides: Partial<LinkedPlayerCard> = {},
): LinkedPlayerCard {
  return {
    tag,
    name: "Lens Main",
    clan: null,
    state: "tracking",
    reason: null,
    trophies: 5696,
    seasonResetPending: false,
    rank: 318,
    league: null,
    today: { net: 46, attacks: 5, defenses: 6 },
    ...overrides,
  };
}

export function emptyDay(overrides: Partial<PlayerDay> = {}): PlayerDay {
  return {
    dayStartMs: Date.UTC(2026, 9, 7, 5),
    dayEndMs: Date.UTC(2026, 9, 8, 5),
    dayNumber: 16,
    dayCount: 28,
    battles: [],
    complete: false,
    net: null,
    gained: null,
    lost: null,
    attacks: null,
    defenses: null,
    lastResetRank: null,
    trophies: 5696,
    observedAtMs: null,
    battlesObservedAtMs: null,
    openDefenses: null,
    autoDefenseEach: null,
    ...overrides,
  };
}

export function fight(
  kind: ClockBattle["kind"],
  hour: number,
  minute: number,
  stars: number,
  trophyChange: number,
): ClockBattle {
  return {
    at: Date.UTC(2026, 9, 7, hour, minute),
    kind,
    stars,
    destruction: stars === 3 ? 100 : 80,
    trophyChange,
    opponent: "Opponent",
  };
}
