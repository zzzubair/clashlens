import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { expect, it } from "vitest";
import { GoalCard } from "../../app/components/BoardCards";
import type { GoalData } from "../../app/lib/dashboard-cards";

const data: GoalData = {
  rank: 200,
  name: "Nova",
  finished: 8,
  window: 7,
  you: 5300,
  youThen: 5249,
  line: 5320,
  lineThen: 5276,
  history: [5249, null, null, null, null, null, null, 5300],
  plan: {
    need: 7.285714285714286,
    pace: 7.285714285714286,
    drift: 6.285714285714286,
    gap: -20,
    daysLeft: 20,
    status: "on",
    shields: [
      { days: 0, need: 7.2857142857142865, onPace: false },
      { days: 1, need: 7.669172932330827, onPace: false },
      { days: 2, need: 8.095238095238095, onPace: false },
      { days: 3, need: 8.571428571428571, onPace: false },
    ],
  },
};
const render = (value: GoalData) =>
  renderToStaticMarkup(
    createElement(MemoryRouter, null, createElement(GoalCard, { data: value })),
  );

it("renders Python's default verdict when the rounded zero-skip plan disagrees", () => {
  const html = render(data);
  expect(html).toContain("On pace");
  expect(html).not.toContain("Behind by 0");
});

it("hides the skip menu on Day 28 when zero is the only choice", () => {
  const html = render({
    ...data,
    finished: 27,
    plan: {
      ...data.plan!,
      daysLeft: 1,
      need: 26.285714285714285,
      status: "behind",
      shields: [{ days: 0, need: 26.285714285714285, onPace: false }],
    },
  });
  expect(html).not.toContain("Plan: skip");
});
