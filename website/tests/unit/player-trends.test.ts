import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import cases from "../../../testdata/w6-presentation-parity.json";
import { PlayerTrends } from "../../app/components/PlayerTrends";
import { mapTrophyTrends } from "../../app/services/player-facts.server";
import { PLAYER } from "../fixtures/player-route-data";

describe("player trophy trends from Python", () => {
  for (const [index, example] of cases
    .filter((c) => c.rule === "trophy_trends")
    .entries()) {
    it(`renders the original numerical example ${index}`, () => {
      const trophyTrends = mapTrophyTrends(example.expected);
      const html = renderToStaticMarkup(
        createElement(PlayerTrends, { player: { ...PLAYER, trophyTrends } }),
      );
      if (trophyTrends === null) expect(html).toBe("");
      else
        for (const key of ["7", "14"] as const) {
          const { total, counted, span } = trophyTrends[key];
          expect(html).toContain(`<dd>${counted} of ${span}</dd>`);
          expect(html).toContain(
            total === null ? "Unavailable" : `${total > 0 ? "+" : ""}${total}`,
          );
        }
    });
  }
  it("rejects a missing trend window", () =>
    expect(() =>
      mapTrophyTrends({ "7": { span: 7, counted: 0, total: null } }),
    ).toThrow());
});
