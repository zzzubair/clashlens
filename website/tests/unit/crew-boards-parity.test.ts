import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { SeasonSummary } from "../../app/components/SeasonSummary";
import { signedSide } from "../../app/lib/number-format";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
// The same saved days the Python crew boards test reads
// (python/tests/test_crew_boards_postgres.py): both must give these totals.
type Battle = {
  lens: "offense" | "defense";
  battle_id: string;
  battle_timestamp: string;
  opponent: { tag: string; name: string | null };
  stars: number;
  destruction_percentage: number;
  trophy_change: number;
};
type Side = { total: number; days: number; battles: number };
const fixture = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../../../testdata/crew-boards-parity.json", import.meta.url)),
    "utf8",
  ),
) as {
  now: string;
  season_start: string;
  season_day: number;
  players: {
    tag: string;
    days: { start: string; partial_reasons: string[]; battles: Battle[] }[];
    expected: Record<"season" | "week", { attack: Side; defense: Side }>;
  }[];
};
describe("crew and player summary averages", () => {
  for (const saved of fixture.players) {
    it(`renders ${saved.tag}'s Python totals with their played-day denominator`, () => {
      for (const result of [saved.expected.season, saved.expected.week]) {
        const side = (s: Side) => ({
          count: s.battles,
          stars: [0, 0, 0, 0],
          unknown: 0,
          perDay: s.total / s.days,
          perBattle: s.total / s.battles,
        });
        const html = renderToStaticMarkup(
          createElement(SeasonSummary, {
            title: "Season summary",
            rank: ["Rank", "Unknown"],
            trophies: ["Trophies", "Unknown"],
            attack: side(result.attack),
            defense: side(result.defense),
          }),
        );
        expect(html).toContain("Season summary");
        expect(html).toContain("Trophies");
        // Each side's trophies a Legend day: its total over the days it played.
        expect(html).toContain(signedSide(result.attack.total / result.attack.days, 1));
        expect(html).toContain(
          signedSide(result.defense.total / result.defense.days, -1),
        );
      }
    });
  }
});
