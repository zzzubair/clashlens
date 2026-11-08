import { describe, expect, it } from "vitest";

import type { ArmyVariantExamples } from "../../app/lib/contracts";
import type { PlayerArmies } from "../../app/lib/dashboard-armies";
import {
  armyDaysShown,
  armyDaysStart,
  armyDaysStep,
  type ArmyDaysEvent,
} from "../../app/components/ArmyCards";
import { basesDayRead } from "../../app/components/OpponentsCard";
import { variantExamplesRead } from "../../app/components/VariantExamples";

const armies = (days: number | "season") => ({ days }) as unknown as PlayerArmies;
const PAGE = armies(3);

const run = (events: ArmyDaysEvent[]) => events.reduce(armyDaysStep, armyDaysStart(PAGE));

describe("Armies card days", () => {
  it("keeps the shown numbers while other days load, then shows them", () => {
    const loading = run([{ type: "choose", ask: 1, days: 7, initial: PAGE }]);
    expect(loading).toMatchObject({ phase: "loading", days: 7 });
    expect(armyDaysShown(loading)).toBe(PAGE);
    const week = armies(7);
    const ready = armyDaysStep(loading, {
      type: "answered",
      ask: 1,
      days: 7,
      armies: week,
    });
    expect(ready).toMatchObject({ phase: "ready", days: 7 });
    expect(armyDaysShown(ready)).toBe(week);
  });

  it("shows only the newest choice's answer", () => {
    const week = armies(7);
    const state = run([
      { type: "choose", ask: 1, days: 7, initial: PAGE },
      { type: "choose", ask: 2, days: "season", initial: PAGE },
      { type: "answered", ask: 1, days: 7, armies: week },
    ]);
    expect(state).toMatchObject({ phase: "loading", days: "season" });
    // Still the numbers on screen when 7 days was picked.
    expect(armyDaysShown(state)).toBe(PAGE);
  });

  it("returns to the page's own days at once, and a failed read can be retried", () => {
    const back = run([
      { type: "choose", ask: 1, days: 7, initial: PAGE },
      { type: "choose", ask: 2, days: 3, initial: PAGE },
      { type: "answered", ask: 1, days: 7, armies: armies(7) },
    ]);
    expect(back).toEqual({ phase: "ready", ask: 2, days: 3, armies: PAGE });
    const failed = run([
      { type: "choose", ask: 1, days: 7, initial: PAGE },
      { type: "answered", ask: 1, days: 7, armies: null },
    ]);
    expect(armyDaysShown(failed)).toBeNull();
    const retry = armyDaysStep(failed, {
      type: "choose",
      ask: 2,
      days: 7,
      initial: PAGE,
    });
    expect(retry).toMatchObject({ phase: "loading", shown: null });
  });
});

describe("Bases you attacked days", () => {
  it("names each day's read", () => {
    const read = new Map<number, object | null>([
      [4, { rows: [] }],
      [5, null],
    ]);
    const asking = new Set([6]);
    expect(basesDayRead(null, read, asking)).toBe("today");
    expect(basesDayRead(4, read, asking)).toBe("read");
    expect(basesDayRead(5, read, asking)).toBe("failed");
    expect(basesDayRead(6, read, asking)).toBe("asking");
    expect(basesDayRead(7, read, asking)).toBe("unread");
  });
});

describe("variant example armies", () => {
  it("loads, then shows or fails", () => {
    const examples = { rule: "", groups: [] } as unknown as ArmyVariantExamples;
    expect(variantExamplesRead(undefined)).toEqual({ phase: "loading" });
    expect(variantExamplesRead({ examples: null })).toEqual({ phase: "failed" });
    expect(variantExamplesRead({ examples })).toEqual({ phase: "ready", examples });
  });
});
