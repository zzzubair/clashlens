import { describe, expect, it } from "vitest";

import {
  CARDS_FROM_LOAD,
  DASHBOARD_VIEWING,
  cardReads,
  cardRefreshStep,
  dashboardEditStep,
  outdatedCards,
  type CardRefreshEvent,
  type DashboardEdit,
  type DashboardEditEvent,
} from "../../app/components/DashboardState";
import { defaultSettings, type CardId } from "../../app/lib/dashboard";
import type { CardReads } from "../../app/lib/dashboard-cards";
import type {
  DashboardActionData,
  DashboardLoaderData,
} from "../../app/routes/dashboard";

const edit = (events: DashboardEditEvent[], from: DashboardEdit = DASHBOARD_VIEWING) =>
  events.reduce(dashboardEditStep, from);
const answer = (saved: boolean): DashboardActionData => ({
  saved,
  error: saved ? null : "Couldn't save.",
  idempotencyKey: "key",
  settings: null,
});

describe("Customise", () => {
  it("edits a draft and Done ends only on its own save's answer", () => {
    const earlier = answer(true);
    const editing = edit([
      { type: "customise", layout: null },
      { type: "change", draft: ["goal", "legend-day"], status: "Moved Goal earlier" },
      { type: "done", after: earlier },
    ]);
    expect(editing).toMatchObject({
      mode: "editing",
      draft: ["goal", "legend-day"],
      status: "Moved Goal earlier",
      finishing: { after: earlier },
    });
    // The answer that was showing when Done was pressed is not Done's.
    expect(edit([{ type: "answered", answer: earlier }], editing)).toBe(editing);
    expect(edit([{ type: "answered", answer: answer(true) }], editing)).toEqual({
      mode: "viewing",
      status: "Moved Goal earlier",
    });
  });

  it("keeps the draft after a failed save, and Done can be pressed again", () => {
    const failed = answer(false);
    const kept = edit([
      { type: "customise", layout: ["goal"] },
      { type: "done", after: undefined },
      { type: "answered", answer: failed },
    ]);
    expect(kept).toMatchObject({ mode: "editing", draft: ["goal"], finishing: null });
    const again = edit(
      [
        { type: "done", after: failed },
        { type: "answered", answer: answer(true) },
      ],
      kept,
    );
    expect(again.mode).toBe("viewing");
  });

  it("discards the draft on Cancel, and ignores edits outside Customise", () => {
    const cancelled = edit([
      { type: "customise", layout: "default" },
      { type: "useDefault" },
      { type: "cancel" },
    ]);
    expect(cancelled).toEqual({ mode: "viewing", status: "Changes discarded" });
    expect(edit([{ type: "change", draft: [], status: "x" }], cancelled)).toBe(cancelled);
    expect(edit([{ type: "dragStart", card: "goal" }], cancelled)).toBe(cancelled);
  });

  it("opens Add a card from an empty dashboard straight into Customise", () => {
    expect(edit([{ type: "openAdd" }])).toMatchObject({
      mode: "editing",
      draft: [],
      sheet: { kind: "adding" },
    });
    const closed = edit([{ type: "openAdd" }, { type: "closeAdd" }]);
    expect(closed).toMatchObject({ mode: "editing", sheet: { kind: "none" } });
  });

  it("closes a card's settings once a save lands, or on close", () => {
    const open = edit([
      { type: "customise", layout: null },
      { type: "openSettings", card: "goal" },
    ]);
    expect(open).toMatchObject({ sheet: { kind: "settings", card: "goal" } });
    expect(edit([{ type: "saved" }], open)).toMatchObject({ sheet: { kind: "none" } });
    expect(edit([{ type: "closeSettings" }], open)).toMatchObject({
      sheet: { kind: "none" },
    });
    // A save answer does not close Add a card.
    const adding = edit([{ type: "openAdd" }]);
    expect(edit([{ type: "saved" }], adding)).toBe(adding);
  });

  it("moves a dragged card where it is dropped and says so", () => {
    const dragging = edit([
      { type: "customise", layout: ["legend-day", "goal", "heat-map"] },
      { type: "dragStart", card: "heat-map" },
    ]);
    const over = edit([{ type: "dragOver", over: "legend-day", before: true }], dragging);
    expect(over).toMatchObject({ draft: ["heat-map", "legend-day", "goal"] });
    // Already in place: nothing changes.
    expect(edit([{ type: "dragOver", over: "legend-day", before: true }], over)).toBe(
      over,
    );
    expect(edit([{ type: "dragEnd" }], over)).toMatchObject({
      dragging: null,
      status: "Moved Season heat map",
    });
  });
});

const cardsEvent = (events: CardRefreshEvent[]) =>
  events.reduce(cardRefreshStep, CARDS_FROM_LOAD);
const loaded = (cards: CardReads, selectedTag = "#2PP") =>
  ({ kind: "signed-in", selectedTag, cards }) as DashboardLoaderData;

describe("card data after a save", () => {
  it("marks changed cards stale, reads them once and merges the answer once", () => {
    const stale = cardsEvent([
      { type: "outdated", cards: ["goal"] },
      { type: "outdated", cards: ["goal", "cutoffs"] },
    ]);
    expect(stale.stale).toEqual(["goal", "cutoffs"]);
    // A stale card shows as loading.
    const page = { goal: null, cutoffs: null, "heat-map": null } as CardReads;
    expect(Object.keys(cardReads(page, stale) ?? {})).toEqual(["heat-map"]);

    const requested = cardRefreshStep(stale, { type: "requested", cards: stale.stale });
    const reply = loaded({ goal: { rank: 1 } } as unknown as CardReads);
    const merged = cardRefreshStep(requested, {
      type: "answered",
      answer: reply,
      tag: "#2PP",
    });
    expect(merged.stale).toEqual([]);
    // A card the answer leaves out could not load.
    expect(merged.refreshed).toEqual({ goal: { rank: 1 }, cutoffs: null });
    expect(
      cardRefreshStep(merged, { type: "answered", answer: reply, tag: "#2PP" }),
    ).toBe(merged);
  });

  it("treats an answer for another player as could not load, and a new load starts over", () => {
    const requested = cardsEvent([
      { type: "outdated", cards: ["goal"] },
      { type: "requested", cards: ["goal"] },
    ]);
    const other = cardRefreshStep(requested, {
      type: "answered",
      answer: loaded({ goal: { rank: 1 } } as unknown as CardReads, "#9QQ"),
      tag: "#2PP",
    });
    expect(other.refreshed).toEqual({ goal: null });
    expect(cardRefreshStep(other, { type: "newLoad" })).toMatchObject({
      refreshed: {},
      stale: [],
    });
    expect(cardReads(null, other)).toBeNull();
  });

  it("finds the cards a save makes out of date", () => {
    const has = (card: CardId): card is keyof CardReads => card !== "goal";
    const after = {
      ...defaultSettings(),
      goal: 50,
      layout: ["goal", "heat-map"] as CardId[],
    };
    expect(outdatedCards(defaultSettings(), after, {}, has)).toEqual([
      "goal",
      "heat-map",
    ]);
    expect(outdatedCards(after, after, { "heat-map": null }, has)).toEqual([]);
  });
});
