import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ArmyPortraits } from "../../app/components/ArmyPortraits";
import { readArmy, type ArmyUnitNames } from "../../app/lib/army";
import { mapPlayerPage } from "../../app/services/player-response.server";

// A Legend attack's Copy army code as the battle log saved it on 13 Oct 2026.
const CODE =
  "h0p9e14_32-1p3e17_48-2p16e5_24-7p4e52_53i1x5-2x58-1x64d1x70-1x98" +
  "u5x55-9x132-5x7-2x5-1x58-3x109-1x135-4x1-2x28s1x9-3x10-4x120-1x17-1x5-1x35";

const NAMES: ArmyUnitNames = {
  "troop:5": { name: "Balloon", siege: false },
  "troop:55": { name: "Sneaky Goblin", siege: false },
  "troop:135": { name: "Troop Launcher", siege: true },
  "troop:121": { name: "Barbarian Kicker", siege: false },
  "hero:0": { name: "Barbarian King", siege: false },
};

describe("readArmy", () => {
  it("reads every part of a real army in the order it was trained", () => {
    const army = readArmy(CODE, NAMES);
    expect(army?.troops.slice(0, 2)).toEqual([
      { id: "troop:55", count: 5 },
      { id: "troop:132", count: 9 },
    ]);
    expect(army?.troops).toHaveLength(8);
    expect(army?.siege).toEqual([{ id: "troop:135", count: 1 }]);
    expect(army?.spells.map((spell) => spell.id)).toEqual([
      "spell:9",
      "spell:10",
      "spell:120",
      "spell:17",
      "spell:5",
      "spell:35",
    ]);
    expect(army?.clanCastle).toEqual({
      troops: [
        { id: "troop:5", count: 1 },
        { id: "troop:58", count: 2 },
        { id: "troop:64", count: 1 },
      ],
      siege: [],
      spells: [
        { id: "spell:70", count: 1 },
        { id: "spell:98", count: 1 },
      ],
    });
    expect(army?.heroes[0]).toEqual({
      id: "hero:0",
      pet: "pet:9",
      equipment: ["equipment:14", "equipment:32"],
    });
    expect(army?.heroes.map((hero) => hero.id)).toEqual([
      "hero:0",
      "hero:1",
      "hero:2",
      "hero:7",
    ]);
  });

  it("shows no army for a code the Python decoder rejects", () => {
    for (const code of ["", "x", "u", "u0x1", "u1x1u2x2", "u1x1-", "h0e1_2_3", "u1x1q"]) {
      expect(readArmy(code, NAMES)).toBeNull();
    }
  });
});

describe("ArmyPortraits", () => {
  it("draws Fan Kit art with counts, and a unit without art by name", () => {
    const html = renderToStaticMarkup(
      createElement(ArmyPortraits, {
        id: "army-1",
        code: "u5x55-1x121i1x5",
        names: NAMES,
      }),
    );
    expect(html).toMatch(/<img src="[^"]*\/troop-55\.webp[^"]*" alt="5 × Sneaky Goblin"/);
    expect(html).toContain('<span class="army-count" aria-hidden="true">5</span>');
    expect(html).toContain(
      '<span aria-label="1 × Barbarian Kicker">Barbarian Kicker</span>',
    );
    expect(html).toContain('aria-label="Clan Castle"');
    expect(html).toContain('alt="1 × Balloon"');
  });

  it("draws nothing for an unreadable code", () => {
    expect(
      renderToStaticMarkup(
        createElement(ArmyPortraits, { id: "a", code: "u1x", names: {} }),
      ),
    ).toBe("");
  });
});

describe("army unit names from Python", () => {
  it("keeps well-formed names and drops malformed ones", () => {
    const page = mapPlayerPage({
      tag: "#2PP",
      name: "Clasher",
      trophies: 5000,
      screen_ready: {
        days: [],
        current_day_start: null,
        recent_day_starts: [],
        season_day_starts: [],
        season: null,
        data_quality: [],
        army_units: {
          "troop:135": { name: "Troop Launcher", siege: true },
          "troop:1": { name: "Archer", siege: false },
          "troop:2": { siege: false },
        },
        provenance: {
          source: "api_player_daily_logs",
          observed_at: "2026-10-13T05:00:00+00:00",
          freshness: "fresh",
          confidence: "high",
          coverage: "complete",
          version: "test",
        },
      },
    });
    expect(page.armyUnits).toEqual({
      "troop:135": { name: "Troop Launcher", siege: true },
      "troop:1": { name: "Archer", siege: false },
    });
  });
});
