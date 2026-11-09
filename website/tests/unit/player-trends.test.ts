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
          // Each window's numbers sit in its own card, under its own heading.
          const title = `Last ${key} days${span < Number(key) ? ` (${span} so far)` : ""}`;
          const card = html.split(`<h3>${title}</h3>`)[1]?.split("</article>")[0];
          expect(card, title).toBeDefined();
          expect(card).toContain(`<dd>${counted} of ${span}</dd>`);
          expect(card).toContain(
            total === null
              ? "<dd>Unavailable</dd>"
              : `<dd>${total > 0 ? "+" : ""}${total.toLocaleString("en-GB")}</dd>`,
          );
        }
    });
  }
  it("rejects a missing trend window", () =>
    expect(() =>
      mapTrophyTrends({ "7": { span: 7, counted: 0, total: null } }),
    ).toThrow());
});
