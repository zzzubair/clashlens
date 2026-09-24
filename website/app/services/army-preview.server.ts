import type { ArmyAnalytics } from "../lib/contracts";
import captured from "./army-preview-data.json";

type BattleGroup = Pick<
  ArmyAnalytics,
  | "totalAttacks"
  | "usableArmySample"
  | "armyStates"
  | "armyStatesSumConfirmed"
  | "unknownAffectedAttacks"
  | "unknownComponentOccurrences"
  | "perspectiveDisagreementCount"
  | "missingTrophyMembershipEvidence"
  | "versions"
> & {
  battleFrom: string;
  battleTo: string;
  invalidBattleRows: number;
  categories: Record<string, ArmyAnalytics["rows"]>;
};

// One bounded capture of official battle logs. The existing Python battle
// parser, army decoder and build_army_result produced these aggregates.
// This preview never treats the capture as a complete daily publication.
const capture = captured as unknown as {
  fetchedAt: string;
  rankingFetchedAt: string;
  playerCount: number;
  sourceSha256: string;
  groups: Record<string, Record<"offense" | "defense", BattleGroup>>;
};

const sortFields = {
  "usage-rate": "usageRate",
  "usage-count": "usageCount",
  "three-star-rate": "threeStarRate",
  "average-stars": "averageStars",
  "average-destruction": "averageDestruction",
} as const;

export function recentArmyAnalytics(source: URLSearchParams) {
  const lens = source.get("lens") === "defense" ? "defense" : "offense";
  const requestedPopulation = source.get("population") ?? "top-100";
  const population = Object.hasOwn(capture.groups, requestedPopulation)
    ? requestedPopulation
    : "top-100";
  const group = capture.groups[population][lens];
  const category = Object.hasOwn(group.categories, source.get("category") ?? "")
    ? source.get("category")!
    : "troops";
  const sort = Object.hasOwn(sortFields, source.get("sort") ?? "")
    ? (source.get("sort")! as keyof typeof sortFields)
    : "usage-rate";
  const { categories, battleFrom, battleTo, invalidBattleRows, ...summary } = group;
  const field = sortFields[sort];
  const analytics: ArmyAnalytics = {
    ...summary,
    kind: "army-analytics",
    selection: { lens, season: "recent", startDay: 0, endDay: 0, population, category, sort },
    cohortEvidence: {
      staleOrUncertainCohortMembers: 0,
      streakExcludedPlayers: 0,
      shieldedPlayerDays: 0,
    },
    collectionCoverage: { state: "recent-snapshot", completedDays: 0 },
    freshness: { state: "frozen" },
    reproducibility: {
      officialSeasonId: "recent-battle-logs",
      legendDays: [0, 0],
      snapshotVersions: [],
    },
    publicationIdentity: `recent-${capture.sourceSha256}`,
    rows: [...categories[category]].sort(
      (a, b) => b[field]! - a[field]! || a.key.localeCompare(b.key),
    ),
  };
  return {
    analytics,
    snapshot: {
      fetchedAt: capture.fetchedAt,
      playerCount: capture.playerCount,
      battleFrom,
      battleTo,
      invalidBattleRows,
    },
  };
}
