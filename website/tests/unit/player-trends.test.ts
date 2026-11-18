import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import cases from "../../../testdata/w6-presentation-parity.json";
import { PlayerTrends } from "../../app/components/PlayerTrends";
import { mapTrophyTrends } from "../../app/services/player-facts.server";
import { PLAYER } from "../fixtures/player-route-data";

const examples = cases.filter((c) => c.rule === "trophy_trends");
const render = (example: unknown) =>
  renderToStaticMarkup(
    createElement(PlayerTrends, {
      player: { ...PLAYER, trophyTrends: mapTrophyTrends(example) },
    }),
  );

describe("player trophy trends from Python", () => {
  it("reads every shared example, and draws nothing without trends", () => {
    for (const example of examples) {
      const html = render(example.expected);
      if (example.expected === null) expect(html).toBe("");
      // The cards carry no explanation paragraph.
      else expect(html).not.toContain("<p");
    }
  });

  // Each shared example's two cards: heading, days counted and trophy change.
  it.each([
    [0, ["Last 7 days", "7 of 7", "+40"], ["Last 14 days", "14 of 14", "+110"]],
    [
      1,
      ["Last 7 days (3 so far)", "3 of 3", "+60"],
      ["Last 14 days (3 so far)", "3 of 3", "+60"],
    ],
    [7, ["Last 7 days", "1 of 7", "-15"], ["Last 14 days", "1 of 14", "-15"]],
    [
      8,
      ["Last 7 days", "0 of 7", "Unavailable"],
      ["Last 14 days", "0 of 14", "Unavailable"],
    ],
    [10, ["Last 7 days", "1 of 7", "0"], ["Last 14 days", "1 of 14", "0"]],
  ])("shows shared example %i's windows in their own cards", (index, ...windows) => {
    const html = render(examples[index].expected);
    for (const [title, counted, change] of windows) {
      const card = html.split(`<h3>${title}</h3>`)[1]?.split("</article>")[0];
      expect(card, title).toBeDefined();
      expect(card).toContain(`<dd>${counted}</dd>`);
      expect(card).toContain(`<dd>${change}</dd>`);
    }
  });

  it("rejects a missing trend window", () =>
    expect(() =>
      mapTrophyTrends({ "7": { span: 7, counted: 0, total: null } }),
    ).toThrow());
});
