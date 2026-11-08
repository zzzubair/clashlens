import { CARDS, shownCards, type CardId, type DashboardSettings } from "../lib/dashboard";
import type { CardReads } from "../lib/dashboard-cards";
import type { DashboardActionData, DashboardLoaderData } from "../routes/dashboard";

type Layout = NonNullable<DashboardSettings["layout"]>;

/** The sheet open over Customise, if any. */
export type DashboardSheet =
  { kind: "none" } | { kind: "adding" } | { kind: "settings"; card: CardId };

/**
 * Customise on the Dashboard:
 *
 * - `viewing`: the saved layout shows; nothing is being edited.
 * - `editing`: a draft of the card order shows, with at most one sheet open
 *   (Add a card, or one card's settings) and at most one card being dragged.
 *   `finishing` says Done was pressed: it waits for its own save's answer,
 *   the first one other than the answer that was showing when Done was pressed.
 *
 * Events: Customise, card changes, Use default, Add, settings, dragging,
 * Done, the save's answer, and Cancel. Only `dashboardEditStep` moves it.
 * Done's answer ends editing only when it saved; a failed save keeps the
 * draft and shows the error, and Done can be pressed again. A saved answer
 * closes an open settings sheet. Cancel and Escape discard the draft. The
 * Dashboard component owns it; leaving the page or reloading drops a draft.
 * `status` is what screen readers hear about the last change.
 */
export type DashboardEdit =
  | { mode: "viewing"; status: string }
  | {
      mode: "editing";
      status: string;
      draft: Layout;
      sheet: DashboardSheet;
      dragging: CardId | null;
      finishing: { after: DashboardActionData | undefined } | null;
    };

export type DashboardEditEvent =
  | { type: "customise"; layout: DashboardSettings["layout"] }
  | { type: "change"; draft: CardId[]; status: string }
  | { type: "useDefault" }
  | { type: "cancel" }
  | { type: "openAdd" }
  | { type: "closeAdd" }
  | { type: "openSettings"; card: CardId }
  | { type: "closeSettings" }
  | { type: "dragStart"; card: CardId }
  | { type: "dragOver"; over: CardId; before: boolean }
  | { type: "dragEnd" }
  | { type: "done"; after: DashboardActionData | undefined }
  | { type: "answered"; answer: DashboardActionData | undefined }
  | { type: "saved" };

export const DASHBOARD_VIEWING: DashboardEdit = { mode: "viewing", status: "" };

function editing(
  status: string,
  draft: Layout,
): Extract<DashboardEdit, { mode: "editing" }> {
  return {
    mode: "editing",
    status,
    draft,
    sheet: { kind: "none" },
    dragging: null,
    finishing: null,
  };
}

/** The only place Customise moves; see `DashboardEdit`. */
export function dashboardEditStep(
  state: DashboardEdit,
  event: DashboardEditEvent,
): DashboardEdit {
  if (event.type === "customise") return editing("", event.layout ?? "default");
  if (event.type === "openAdd") {
    // Add a card on an empty dashboard starts Customise with no cards.
    const from = state.mode === "editing" ? state : editing(state.status, []);
    return { ...from, sheet: { kind: "adding" } };
  }
  if (state.mode === "viewing") return state;
  switch (event.type) {
    case "change":
      return { ...state, draft: event.draft, status: event.status };
    case "useDefault":
      return { ...state, draft: "default", status: "Back to the default layout" };
    case "cancel":
      return { mode: "viewing", status: "Changes discarded" };
    case "closeAdd":
      return state.sheet.kind === "adding"
        ? { ...state, sheet: { kind: "none" } }
        : state;
    case "openSettings":
      return { ...state, sheet: { kind: "settings", card: event.card } };
    case "closeSettings":
    case "saved":
      return state.sheet.kind === "settings"
        ? { ...state, sheet: { kind: "none" } }
        : state;
    case "dragStart":
      return { ...state, dragging: event.card };
    case "dragOver": {
      const { dragging } = state;
      if (dragging === null) return state;
      const was = shownCards(state.draft);
      const list = was.filter((card) => card !== dragging);
      list.splice(list.indexOf(event.over) + (event.before ? 0 : 1), 0, dragging);
      return list.join() === was.join() ? state : { ...state, draft: list };
    }
    case "dragEnd":
      return state.dragging === null
        ? state
        : { ...state, dragging: null, status: `Moved ${CARDS[state.dragging].title}` };
    case "done":
      return { ...state, finishing: { after: event.after } };
    case "answered":
      if (state.finishing === null || event.answer === state.finishing.after)
        return state;
      return event.answer?.saved
        ? { mode: "viewing", status: state.status }
        : { ...state, finishing: null };
  }
}

/**
 * The cards' data after the page's first read, as saves change what they
 * show. Each card is:
 *
 * - loading, while the page's read is on its way (its data is null);
 * - shown, from that read or from a later one in `refreshed`;
 * - stale, in `stale`: a save changed the settings it follows, or it has no
 *   data yet, so it shows as loading until it is read again;
 * - read again: one read asks for every stale card, `requested`; a newer read
 *   replaces it, and its answer is merged once (`merged`) into `refreshed`, a
 *   card it leaves out, a failed answer or one for another player as null,
 *   "could not load".
 *
 * Events: `newLoad` (the page read again, which starts over), `outdated`,
 * `requested` and `answered`. Only `cardRefreshStep` moves it; the Dashboard
 * component owns it. There is no retry: a card that could not load stays so
 * until its settings change or the page reads again.
 */
export interface CardRefresh {
  refreshed: CardReads;
  stale: CardId[];
  requested: CardId[];
  merged: DashboardLoaderData | undefined;
}

export type CardRefreshEvent =
  | { type: "newLoad" }
  | { type: "outdated"; cards: CardId[] }
  | { type: "requested"; cards: CardId[] }
  | { type: "answered"; answer: DashboardLoaderData; tag: string };

export const CARDS_FROM_LOAD: CardRefresh = {
  refreshed: {},
  stale: [],
  requested: [],
  merged: undefined,
};

/** The only place card data moves after the first read; see `CardRefresh`. */
export function cardRefreshStep(
  state: CardRefresh,
  event: CardRefreshEvent,
): CardRefresh {
  switch (event.type) {
    case "newLoad":
      return { ...state, refreshed: {}, stale: [] };
    case "outdated":
      return event.cards.length
        ? { ...state, stale: [...new Set([...state.stale, ...event.cards])] }
        : state;
    case "requested":
      return state.requested === event.cards
        ? state
        : { ...state, requested: event.cards };
    case "answered": {
      const { answer, tag } = event;
      if (state.merged === answer) return state;
      const cards = Object.fromEntries(
        state.requested.map((card) => [
          card,
          answer.kind === "signed-in" && answer.selectedTag === tag
            ? (answer.cards[card as keyof CardReads] ?? null)
            : null,
        ]),
      ) as CardReads;
      return {
        ...state,
        merged: answer,
        refreshed: { ...state.refreshed, ...cards },
        stale: state.stale.filter((card) => !state.requested.includes(card)),
      };
    }
  }
}

/** The data each card shows: none while the page's read loads. */
export function cardReads(data: CardReads | null, state: CardRefresh): CardReads | null {
  const reads = data === null ? null : { ...data, ...state.refreshed };
  for (const card of state.stale) if (reads) delete reads[card as keyof CardReads];
  return reads;
}

/**
 * The cards a save makes out of date: those whose numbers follow the changed
 * settings, and shown cards with a view that have no data yet.
 */
export function outdatedCards(
  before: DashboardSettings,
  after: DashboardSettings,
  reads: CardReads,
  hasView: (card: CardId) => card is keyof CardReads,
): CardId[] {
  const missing = shownCards(after.layout).filter(
    (card) => hasView(card) && reads[card] === undefined,
  );
  return [
    ...(before.goal !== after.goal ? (["goal"] as const) : []),
    ...(before.cutoffs.join() !== after.cutoffs.join() ? (["cutoffs"] as const) : []),
    ...(before.group !== after.group || before.groupWindow !== after.groupWindow
      ? (["group-watch"] as const)
      : []),
    ...missing,
  ];
}
