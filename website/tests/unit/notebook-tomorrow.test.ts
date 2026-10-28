import { describe, expect, it } from "vitest";

import { nearText, parseTomorrow, rangeText } from "../../app/lib/notebook-tomorrow";

const ring = {
  id: "B-RING01",
  name: "Ring",
  where: ["Titans"],
  mine: false,
  running: false,
  ranked: true,
  near: 32,
  recent: 0,
  width: 40,
  widened: false,
  holds: 5.7,
  low: 4.8,
  high: 6.6,
  lost: 1150,
  why: [{ name: "Thrower", share: 1, defenses: 32, tripled: 8, holds: 0.71 }],
};

const answer = {
  kind: "notebook-tomorrow",
  player: { tag: "#2PP", name: "Main" },
  players: [
    { tag: "#2PP", name: "Main" },
    { tag: "#8PP", name: null },
  ],
  day: 7,
  weekday: "Sunday",
  trophies: { now: 5000, attacks_left: 8, defenses_left: 8, about: 5000 },
  width: 40,
  gap: 7,
  meta: [{ name: "Thrower", share: 1 }],
  bases: [ring],
  untried: [
    {
      id: "B-SPIRE1",
      name: "Spire",
      where: ["Your notebook"],
      mine: true,
      running: false,
    },
  ],
};

describe("the Tomorrow screen's answer", () => {
  it("reads the ranked bases, the meta and the account's trophies", () => {
    const tomorrow = parseTomorrow(answer);
    expect(tomorrow?.trophies).toEqual({
      now: 5000,
      attacksLeft: 8,
      defensesLeft: 8,
      about: 5000,
    });
    expect(tomorrow?.bases[0]?.why[0]?.tripled).toBe(8);
    expect(tomorrow?.untried.map((base) => base.name)).toEqual(["Spire"]);
  });

  it("reads an account with no trophies yet, and nothing to rank", () => {
    const tomorrow = parseTomorrow({
      ...answer,
      trophies: null,
      width: null,
      gap: null,
      meta: [],
      bases: [],
    });
    expect(tomorrow?.trophies).toBeNull();
  });

  it("refuses an answer that doesn't fit, rather than drawing wrong numbers", () => {
    expect(parseTomorrow({ ...answer, kind: "notebook" })).toBeNull();
    expect(parseTomorrow({ ...answer, bases: [{ ...ring, holds: 9 }] })).toBeNull();
    expect(parseTomorrow({ ...answer, trophies: { now: 5000 } })).toBeNull();
  });

  it("words a base's range and the defenses behind it", () => {
    expect(rangeText(ring)).toBe("likely 4.8–6.6 · about −1,150 trophies");
    expect(nearText(ring)).toBe("32 defenses within ±40 of you, none in the last 7 days");
  });
});
