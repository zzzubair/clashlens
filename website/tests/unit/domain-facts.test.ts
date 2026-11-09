import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import DOMAIN from "../../../testdata/w6-domain-contract.json";
import cases from "../../../testdata/w6-presentation-parity.json";
import { mapGroupComparison } from "../../app/lib/group-comparison";
import { mapGroups } from "../../app/lib/account-contracts";
import { mapCrewList } from "../../app/lib/crew-contracts";
import { parseOverlayLink, parseSettingsContract } from "../../app/lib/obs-overlay";
import { readConfig } from "../../app/lib/obs-overlay-view";
import {
  mapDashboardGoal,
  mapDashboardWindows,
} from "../../app/services/player-facts.server";
import { RecentFormCard, OffenseConsistencyCard } from "../../app/components/SeasonCards";
import { GoalCard } from "../../app/components/BoardCards";
import type { SeasonData } from "../../app/lib/dashboard-cards";
import { CALENDAR } from "../fixtures/player-route-data";

const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
const emptySeason = (): SeasonData => ({
  today: 9,
  startMs: null,
  endMs: null,
  dayCount: 28,
  days: [],
  windows: { "3": null, "7": null, season: null },
  averages: { offense: null, defense: null },
});
function suppliedWindow(index: number) {
  const example = cases.filter((c) => c.rule === "dashboard_windows")[index];
  const raw = example.args[0] as { today: number; days: { day: number }[] };
  const season = emptySeason();
  season.today = raw.today;
  season.days = raw.days.map(({ day }) => ({
    day,
    kind: "played",
    attackSlots: 8,
    maximumAttackGain: 320,
    attacks: null,
    defenses: null,
    offense: null,
    defense: null,
    net: null,
    triples: null,
    held: null,
    endTrophies: null,
    rank: null,
    hits: null,
  }));
  season.windows = mapDashboardWindows(example.expected, season);
  return season;
}

describe("supplied dashboard results", () => {
  it("renders form and consistency with no locally available battle totals", () => {
    const form = text(
      renderToStaticMarkup(createElement(RecentFormCard, { data: suppliedWindow(8) })),
    );
    expect(form).toContain("+32");
    const consistency = text(
      renderToStaticMarkup(
        createElement(OffenseConsistencyCard, { data: suppliedWindow(9) }),
      ),
    );
    expect(consistency).toContain("25%");
    expect(consistency).toContain("−228");
    expect(consistency).toContain("+12");
  });
  it.each(cases.filter((c) => c.rule === "goal_plan"))(
    "reads the supplied Goal plan",
    (example) => {
      const data = mapDashboardGoal({
        ...(example.args[0] as Record<string, unknown>),
        plan: example.expected,
      })!;
      expect(data.plan?.status ?? null).toBe(
        (example.expected as { status?: string } | null)?.status ?? null,
      );
      expect(() =>
        renderToStaticMarkup(
          createElement(GoalCard, { data: { ...data, name: "Clasher" } }),
        ),
      ).not.toThrow();
    },
  );
  it("rejects a malformed window and an unknown Goal verdict", () => {
    expect(() => mapDashboardWindows(undefined, emptySeason())).toThrow();
    expect(() =>
      mapDashboardWindows({ "3": {}, "7": null, season: null }, emptySeason()),
    ).toThrow();
    const example = cases.find((c) => c.rule === "goal_plan" && c.expected !== null)!;
    expect(() =>
      mapDashboardGoal({
        ...(example.args[0] as Record<string, unknown>),
        plan: { ...(example.expected as Record<string, unknown>), status: "invented" },
      }),
    ).toThrow();
  });
});

describe("Python-owned form metadata", () => {
  it("accepts the supplied comparison window, including a changed window set", () => {
    const reply = {
      kind: "group-comparison",
      limits: { ...DOMAIN.groups, comparison_days: [4] },
      calendar: CALENDAR,
      group_id: "6c1e3f8a-2a44-4b7d-9c0e-1f2a3b4c5d6e",
      name: "Mates",
      days: 4,
      day_starts: Array(4).fill(CALENDAR.day_start),
      today_start: CALENDAR.day_start,
      generated_at: CALENDAR.as_of,
      season: CALENDAR.season_id,
      players: [],
    };
    expect(mapGroupComparison(reply)?.days).toBe(4);
    expect(mapGroupComparison({ ...reply, limits: undefined })).toBeNull();
    expect(mapGroupComparison({ ...reply, limits: DOMAIN.groups })).toBeNull();
  });
  it("rejects absent and malformed capacity metadata even for empty lists", () => {
    const group = { calendar: CALENDAR, season: CALENDAR.season_id, groups: [] };
    expect(mapGroups(group)).toBeNull();
    expect(mapGroups({ ...group, limits: DOMAIN.groups })).not.toBeNull();
    expect(mapCrewList({ kind: "crew-list", crews: [], max_crews: 5 })).toBeNull();
    expect(
      mapCrewList({
        kind: "crew-list",
        crews: [],
        max_crews: 5,
        limits: { ...DOMAIN.crews, max_size: null },
      }),
    ).toBeNull();
  });
  it("uses overlay choices, parts and pixel limits from the supplied contract", () => {
    const contract = parseSettingsContract({
      ...DOMAIN.overlay,
      numeric_max: 777,
      parts: ["net"],
      choices: { ...DOMAIN.overlay.choices, bg: ["none"] },
      defaults: { ...DOMAIN.overlay.defaults, bg: "none" },
    })!;
    const config = readConfig("?w=900&show=net,rank&bg=tint", contract);
    expect(config.w).toBe(777);
    const unconfigured = readConfig("", contract);
    expect([unconfigured.w, unconfigured.h, unconfigured.screen]).toEqual([0, 0, false]);
    expect([...config.show]).toEqual(["net"]);
    expect(config.bg).toBe("none");
    expect(
      parseOverlayLink({ kind: "obs-overlay-link", settings: {}, version: 1, tag: null }),
    ).toBeNull();
    expect(parseSettingsContract({ ...DOMAIN.overlay, defaults: {} })).toBeNull();
  });
});
