import { expect, it } from "vitest";

import { linkedBattleArrival, linkedBattleId } from "../../app/components/LinkedBattle";
import type { RankedDaySummary } from "../../app/lib/contracts";
import { overlaySource } from "../../app/lib/obs-overlay-view";

const dayWith = (...ids: string[]) =>
  ({
    offenseEvents: ids.map((battleId) => ({ battleId })),
    defenseEvents: [],
  }) as unknown as RankedDaySummary;

it("waits for a linked battle, shows it where it lands, then gives up", () => {
  expect(linkedBattleArrival(null, undefined, [], 24)).toEqual({ kind: "none" });
  expect(linkedBattleArrival("b1", undefined, [dayWith("b0")], 24)).toEqual({
    kind: "waiting",
  });
  expect(linkedBattleArrival("b1", undefined, [dayWith("b0")], 0)).toEqual({
    kind: "givenUp",
  });
  // A linked day shown on its own wins over the log.
  expect(linkedBattleArrival("b1", dayWith("b1"), [dayWith("b1")], 0)).toEqual({
    kind: "shown",
    in: "day",
  });
  expect(linkedBattleArrival("b1", dayWith("b0"), [dayWith("b1")], 3)).toEqual({
    kind: "shown",
    in: "log",
  });
  expect(linkedBattleId("#battle-a%2Fb")).toBe("a/b");
  expect(linkedBattleId("#top")).toBeNull();
});

it("shows the demo before a picked player, and asks for a player with neither", () => {
  expect(overlaySource({ demo: true, tag: "" })).toBe("demo");
  expect(overlaySource({ demo: true, tag: "2PP" })).toBe("demo");
  expect(overlaySource({ demo: false, tag: "2PP" })).toBe("player");
  expect(overlaySource({ demo: false, tag: "" })).toBe("none");
});
