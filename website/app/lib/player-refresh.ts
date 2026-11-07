import type {
  RefreshError,
  RefreshStatus,
  RefreshWork,
  WebsiteErrorResponse,
} from "./contracts";
import { isRefreshStatusPayload, isWebsiteErrorResponse } from "./validation";

/** How long one Refresh may take before the page stops checking. */
export const REFRESH_LIMIT_MS = 60_000;

export const REFRESH_TIMED_OUT: WebsiteErrorResponse = {
  error: {
    code: "unavailable",
    message:
      "Couldn't refresh within one minute, so the page stopped checking. Showing saved results.",
  },
};

const POLL_UNAVAILABLE: WebsiteErrorResponse = {
  error: {
    code: "unavailable",
    message: "Saved data is still available, but the live service is unavailable.",
  },
};

const POLL_CONFLICT: WebsiteErrorResponse = {
  error: {
    code: "conflict",
    message: "The request conflicts with current saved data.",
  },
};

type ShownStatus = RefreshStatus | RefreshWork;

/**
 * One player Refresh as the player page sees it, from the button (or the one
 * automatic Refresh, or a no-JS `?refresh=` address) to the page reading itself
 * again.
 *
 * - `idle`: nothing asked on this visit. A no-JS address's status still shows.
 * - `submitted`: the Refresh form was sent; its work ID is not known yet.
 * - `polling`: the work ID is known; its status is read every half second.
 * - `finished`: the work said complete, failed or unavailable. Complete reads
 *   the page again at 0, 3 and 8 seconds; that page read is separate from the
 *   collection finishing.
 * - `stopped`: the page gave up. The form's answer was an error, a status read
 *   failed or named other work, or the one-minute deadline passed. The last
 *   status stays shown, marked failed.
 *
 * Events: `submit`, `answered` (the form's answer), `loaderStatus` (a no-JS
 * address read again), `polled`, `pollFailed` and `timedOut`. Only
 * `playerRefreshStep` moves it. The player page owns it, one per visit: the
 * route mounts afresh on each navigation, so a reload or a new visit starts
 * `idle`. Retry is Refresh again, which starts over at `submitted` with a new
 * minute; nothing survives a restart.
 */
export type PlayerRefresh =
  | { phase: "idle" }
  | { phase: "submitted"; deadline: number }
  | { phase: "polling"; deadline: number; status: ShownStatus }
  | { phase: "finished"; deadline: number; status: ShownStatus }
  | {
      phase: "stopped";
      deadline: number;
      status: ShownStatus | null;
      failure: WebsiteErrorResponse;
    };

export type PlayerRefreshEvent =
  | { type: "submit"; now: number }
  | {
      type: "answered";
      now: number;
      answer: RefreshWork | RefreshError | null | undefined;
      tag: string | null;
    }
  | { type: "loaderStatus"; now: number; status: RefreshStatus }
  | { type: "polled"; status: RefreshStatus }
  | { type: "pollFailed"; failure: WebsiteErrorResponse }
  | { type: "timedOut" };

export const REFRESH_IDLE: PlayerRefresh = { phase: "idle" };

function withStatus(deadline: number, status: ShownStatus): PlayerRefresh {
  return status.state === "complete" ||
    status.state === "failed" ||
    status.state === "unavailable"
    ? { phase: "finished", deadline, status }
    : { phase: "polling", deadline, status };
}

function stop(
  state: Exclude<PlayerRefresh, { phase: "idle" }>,
  failure: WebsiteErrorResponse,
): PlayerRefresh {
  return {
    phase: "stopped",
    deadline: state.deadline,
    status: "status" in state ? state.status : null,
    failure,
  };
}

/** The only place a player Refresh moves; see `PlayerRefresh`. */
export function playerRefreshStep(
  state: PlayerRefresh,
  event: PlayerRefreshEvent,
): PlayerRefresh {
  switch (event.type) {
    case "submit":
      return { phase: "submitted", deadline: event.now + REFRESH_LIMIT_MS };
    case "loaderStatus":
      return withStatus(
        state.phase === "idle" ? event.now + REFRESH_LIMIT_MS : state.deadline,
        event.status,
      );
    case "answered": {
      // Only a submitted form answers; a fresh visit has none.
      if (state.phase === "idle") return state;
      const { answer } = event;
      if (event.now >= state.deadline) return stop(state, REFRESH_TIMED_OUT);
      if (answer && "error" in answer) return stop(state, answer);
      if (answer && answer.tag === event.tag) return withStatus(state.deadline, answer);
      return state;
    }
    case "polled":
      return state.phase === "polling" ? withStatus(state.deadline, event.status) : state;
    case "pollFailed":
      return state.phase === "polling" ? stop(state, event.failure) : state;
    case "timedOut":
      return state.phase === "idle" || state.phase === "finished"
        ? state
        : stop(state, REFRESH_TIMED_OUT);
  }
}

/** The status the page shows: this visit's, or before any, the address's. */
export function shownRefreshStatus(
  state: PlayerRefresh,
  addressStatus: RefreshStatus | null,
): ShownStatus | null {
  if (state.phase === "idle") return addressStatus;
  return state.phase === "submitted" ? null : state.status;
}

/**
 * Whether nothing is left to wait for: the Refresh ended, or none is running
 * and the form is not sending.
 */
export function refreshSettled(state: PlayerRefresh, formIdle: boolean): boolean {
  return (
    state.phase === "finished" ||
    state.phase === "stopped" ||
    ((state.phase === "idle" || state.phase === "submitted") && formIdle)
  );
}

/** The event one status read makes. */
export function pollAnswer(
  ok: boolean,
  payload: unknown,
  workId: string,
  tag: string | undefined,
): PlayerRefreshEvent {
  if (!ok || !isRefreshStatusPayload(payload)) {
    return {
      type: "pollFailed",
      failure: isWebsiteErrorResponse(payload) ? payload : POLL_UNAVAILABLE,
    };
  }
  if (payload.workId !== workId || payload.tag !== tag) {
    return { type: "pollFailed", failure: POLL_CONFLICT };
  }
  return { type: "polled", status: payload };
}

/** A status read that threw, unless it was cancelled on purpose. */
export function pollThrew(error: unknown): PlayerRefreshEvent | null {
  return error instanceof DOMException && error.name === "AbortError"
    ? null
    : { type: "pollFailed", failure: POLL_UNAVAILABLE };
}

/**
 * The first lookup of an untracked tag, while it is still checking:
 *
 * - `waiting`: the page reads itself again each second, never two at once.
 * - `exhausted`: a minute passed; it stops and says the check may still run.
 *
 * Events: each one-second tick (`lookupWaitTick`). The lookup's own answer
 * (tracking, explained or failed) ends the wait without changing this. The
 * player page owns it, one per visit; a reload starts `waiting` again.
 */
export type LookupWait = "waiting" | "exhausted";

/** What one second of waiting does. */
export function lookupWaitTick(
  startedAt: number,
  now: number,
  readRunning: boolean,
): "exhaust" | "read" | "hold" {
  if (now - startedAt >= REFRESH_LIMIT_MS) return "exhaust";
  return readRunning ? "hold" : "read";
}

/**
 * The one automatic Refresh a visit may spend:
 *
 * - `undecided`: no tracked player is shown yet.
 * - `decided`: it was sent once, or skipped as the saved data is at most a
 *   minute old and this is not a reload.
 *
 * Event: the first render with a tracked player. Later page reads and the
 * form's answers never decide again. A reload of the document counts once per
 * document, so a later in-app visit or Back is not taken for it. The player
 * page owns it; a reload starts `undecided`.
 */
export type AutomaticRefresh = "undecided" | "decided";

/** What the visit's one decision sends, or null to skip. */
export function automaticRefreshFor(
  ageSeconds: number,
  documentReload: boolean,
): { trigger: "reload" | "automatic" } | null {
  if (documentReload) return { trigger: "reload" };
  return ageSeconds <= 60 ? null : { trigger: "automatic" };
}

/** Whether this page came from the browser reloading this same address. */
export function reloadedHere(
  navigation: { type: string; name: string } | undefined,
  pathname: string,
): boolean {
  return navigation?.type === "reload" && new URL(navigation.name).pathname === pathname;
}
