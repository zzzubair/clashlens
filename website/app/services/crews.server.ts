import { data, redirect } from "react-router";

import type { WebsiteErrorResponse } from "../lib/contracts";
import {
  DEFAULT_FORM_LIMITS,
  followUpIdempotencyKey,
  freshIdempotencyKey,
  isAccountNotFoundError,
  isIdempotencyKey,
  isSameOrigin,
  parseBoundedFormData,
} from "../server/actions.server";
import { requireLogin } from "../server/auth-guard.server";
import { getWebsiteConfig, isCrewsEnabled } from "../server/config.server";
import { safeWebsiteError } from "../server/errors.server";
import { accountSetupPath } from "../server/return-path.server";
import {
  crewRefusal,
  INVITE_CODE,
  MAX_CREW_SIZE,
  mapCreatedCrew,
  mapCrew,
  mapCrewBoards,
  mapCrewList,
  mapInvitePreview,
  mapMadeInvite,
  type Crew,
  type CrewBoards,
  type CrewList,
  type CrewPeriod,
  type InviteLink,
  type InvitePreview,
  type LinkedAccount,
  type MadeInvite,
} from "../lib/crew-contracts";
import { isCanonicalUuid } from "../lib/validation";
import { checkPlayerTag } from "./group-players.server";
import {
  createPythonClient,
  PythonApiError,
  requestJson,
  type GoogleAccountIdentity,
} from "./python.server";

/** The signed-in account's crews. */
export async function listCrews(identity: GoogleAccountIdentity): Promise<CrewList> {
  const payload = await requestJson<unknown>(
    "/v1/account/crews",
    "GET",
    undefined,
    "crew-list",
    undefined,
    identity,
  );
  return mapped(mapCrewList(payload));
}

/** Make a crew with some of the account's linked players; returns its id. */
export async function createCrew(
  identity: GoogleAccountIdentity,
  crew: { name: string; size: number; tags: string[] },
  idempotencyKey: string,
): Promise<string> {
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const payload = await requestJson<unknown>(
    "/v1/account/crews",
    "POST",
    Buffer.from(JSON.stringify(crew), "utf8"),
    undefined,
    idempotencyKey,
    identity,
  );
  return mapped(mapCreatedCrew(payload));
}

/** One crew the account is in: its name, places and members. */
export async function getCrew(
  identity: GoogleAccountIdentity,
  crewId: string,
): Promise<Crew> {
  requireCrewId(crewId);
  const payload = await requestJson<unknown>(
    `/v1/account/crews/${crewId}`,
    "GET",
    undefined,
    "crew",
    undefined,
    identity,
  );
  const crew = mapped(mapCrew(payload));
  if (crew.crewId !== crewId) throw new PythonApiError(502, { error: "malformed" });
  return crew;
}

/** The crew's six boards for one period. */
export async function getCrewBoards(
  identity: GoogleAccountIdentity,
  crewId: string,
  period: CrewPeriod,
): Promise<CrewBoards> {
  requireCrewId(crewId);
  const payload = await requestJson<unknown>(
    `/v1/account/crews/${crewId}/boards?period=${period}`,
    "GET",
    undefined,
    "crew-boards",
    undefined,
    identity,
  );
  const boards = mapped(mapCrewBoards(payload));
  if (boards.crewId !== crewId || boards.period !== period) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return boards;
}

/** A repeat-safe crew write; the answer is checked by the caller. */
export async function writeCrew(
  identity: GoogleAccountIdentity,
  method: "POST" | "PATCH" | "DELETE",
  path: string,
  body: Record<string, unknown> | undefined,
  idempotencyKey: string,
): Promise<unknown> {
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  return requestJson<unknown>(
    `/v1/account/${path}`,
    method,
    body === undefined ? undefined : Buffer.from(JSON.stringify(body), "utf8"),
    undefined,
    idempotencyKey,
    identity,
  );
}

/** The caller's newest invite link while it has a day left, or a new one. */
export async function makeInvite(
  identity: GoogleAccountIdentity,
  crewId: string,
  fresh: boolean,
  idempotencyKey: string,
): Promise<MadeInvite> {
  requireCrewId(crewId);
  const payload = await writeCrew(
    identity,
    "POST",
    `crews/${crewId}/invites`,
    { new: fresh },
    idempotencyKey,
  );
  return mapped(mapMadeInvite(payload));
}

/** What an invite link shows the signed-in account. */
export async function getInvite(
  identity: GoogleAccountIdentity,
  code: string,
): Promise<InvitePreview> {
  requireInviteCode(code);
  const payload = await requestJson<unknown>(
    `/v1/account/crew-invites/${code}`,
    "GET",
    undefined,
    "crew-invite",
    undefined,
    identity,
  );
  return mapped(mapInvitePreview(payload));
}

/** Join through an invite link with the picked accounts; returns the crew's id. */
export async function acceptInvite(
  identity: GoogleAccountIdentity,
  code: string,
  tags: string[],
  idempotencyKey: string,
): Promise<string> {
  requireInviteCode(code);
  const payload = await writeCrew(
    identity,
    "POST",
    `crew-invites/${code}/accept`,
    { tags },
    idempotencyKey,
  );
  return mapped(mapCreatedCrew(payload));
}

/** The account's linked Clash of Clans accounts, with whether each is in Legend League. */
export async function linkedAccounts(
  identity: GoogleAccountIdentity,
): Promise<LinkedAccount[]> {
  const client = createPythonClient(identity);
  const summary = await client.getAccountSummary();
  const profile = await client.getPublicUser(summary.username);
  return profile.verifiedPlayers.map(({ tag, name, state, trophies, league }) => ({
    tag,
    name,
    state,
    trophies,
    league,
  }));
}

export type CheckedJoin<T> =
  { ok: true; value: T } | { ok: false; cause: unknown; unfinished: boolean };

/**
 * Send a join (create, accept or add) that may name accounts Clash Lens has
 * not checked yet. Each one is checked with the game, then the join is sent
 * again under a key that follows from the first, so pressing the button
 * again replays the same requests. `unfinished` means the private API gave
 * no final answer, so the form keeps its key.
 */
export async function joinCheckingAccounts<T>(
  clientAddress: string | undefined,
  idempotencyKey: string,
  tagCount: number,
  send: (key: string) => Promise<T>,
): Promise<CheckedJoin<T>> {
  let key = idempotencyKey;
  // Each retry follows a check of one more picked account, so this ends.
  for (let attempt = 0; ; attempt += 1) {
    try {
      return { ok: true, value: await send(key) };
    } catch (cause) {
      const tag = (cause as { payload?: { tag?: unknown } }).payload?.tag;
      const status = (cause as { status?: number }).status ?? 503;
      if (
        crewErrorCode(cause) !== "player_not_checked" ||
        typeof tag !== "string" ||
        attempt >= tagCount
      ) {
        return { ok: false, cause, unfinished: status >= 500 };
      }
      let lookup;
      try {
        lookup = await checkPlayerTag(clientAddress, tag);
      } catch (lookupCause) {
        return { ok: false, cause: lookupCause, unfinished: true };
      }
      if (lookup.state === "checking") return { ok: false, cause, unfinished: true };
      key = followUpIdempotencyKey(key, tag);
    }
  }
}

function requireInviteCode(code: string): void {
  if (!INVITE_CODE.test(code)) throw new PythonApiError(400, { error: "invalid_input" });
}

function requireCrewId(crewId: string): void {
  if (!isCanonicalUuid(crewId)) throw new PythonApiError(400, { error: "invalid_input" });
}

function mapped<T>(value: T | null): T {
  if (value === null) throw new PythonApiError(502, { error: "malformed" });
  return value;
}

export type CrewPage =
  | { crew: Crew; boards: CrewBoards; error: null }
  | { crew: null; boards: null; error: WebsiteErrorResponse };

/**
 * A crew page's header and boards for the signed-in account. Crews are
 * hidden while switched off, and a crew the account is not in reads like a
 * missing page.
 */
export async function loadCrewPage(
  request: Request,
  crewId: string | undefined,
  period: CrewPeriod,
): Promise<CrewPage> {
  const identity = await crewGuard(request, crewId);
  try {
    const [crew, boards] = await Promise.all([
      getCrew(identity, crewId as string),
      getCrewBoards(identity, crewId as string, period),
    ]);
    return { crew, boards, error: null };
  } catch (cause) {
    return { crew: null, boards: null, error: loadFailure(request, cause) };
  }
}

export type LoadedCrew =
  | { crew: Crew; accounts: LinkedAccount[]; error: null }
  | { crew: null; accounts: []; error: WebsiteErrorResponse };

/**
 * A crew for Members and Edit crew, with the signed-in account's linked
 * accounts when asked for.
 */
export async function loadCrew(
  request: Request,
  crewId: string | undefined,
  withAccounts: boolean,
): Promise<LoadedCrew> {
  const identity = await crewGuard(request, crewId);
  try {
    const [crew, accounts] = await Promise.all([
      getCrew(identity, crewId as string),
      withAccounts ? linkedAccounts(identity) : [],
    ]);
    return { crew, accounts, error: null };
  } catch (cause) {
    return { crew: null, accounts: [], error: loadFailure(request, cause) };
  }
}

async function crewGuard(
  request: Request,
  crewId: string | undefined,
): Promise<GoogleAccountIdentity> {
  if (!isCrewsEnabled()) throw data(null, { status: 404 });
  const identity = await requireLogin(request);
  if (crewId === undefined || !isCanonicalUuid(crewId)) throw data(null, { status: 404 });
  return identity;
}

function loadFailure(request: Request, cause: unknown): WebsiteErrorResponse {
  if (isAccountNotFoundError(cause)) {
    const url = new URL(request.url);
    throw redirect(accountSetupPath(url.pathname, url));
  }
  const code = crewErrorCode(cause);
  // The private API's own switch answers crews_disabled.
  if (code === "crew_not_found" || code === "crews_disabled") {
    throw data(null, { status: 404 });
  }
  return safeWebsiteError(cause);
}

/** The error code of a refused crew request, if it has one. */
export function crewErrorCode(cause: unknown): string | null {
  const payload = (cause as { payload?: unknown } | null)?.payload;
  const code =
    typeof payload === "object" && payload !== null
      ? (payload as Record<string, unknown>).error
      : undefined;
  return typeof code === "string" ? code : null;
}

/** What a crew page's form action answers. */
export interface CrewActionData {
  intent: string;
  /** The key the page's forms send next: the same one until a final answer. */
  idempotencyKey: string;
  notice: string | null;
  error: string | WebsiteErrorResponse | null;
  invite: InviteLink | null;
}

/** A crew page's checked form, ready for one write. */
export interface CrewForm {
  identity: GoogleAccountIdentity;
  crewId: string;
  intent: string;
  fields: Record<string, string>;
  /**
   * The key for one write, from the page's key and what the write does, so
   * a page's forms never share a key and the same write sent again after a
   * lost answer replays.
   */
  key: (step: string) => string;
}

/**
 * Run one form on a crew page: crews switched on, signed in, same origin, a
 * crew id, a known intent and a form key, then `run`. A refusal reads as
 * its message; a crew switched off reads as a missing page.
 */
export async function crewFormAction(
  request: Request,
  crewId: string | undefined,
  intents: readonly string[],
  run: (form: CrewForm) => Promise<Partial<CrewActionData>>,
) {
  const identity = await crewGuard(request, crewId);
  const reply = (
    status: number,
    intent: string,
    idempotencyKey: string,
    outcome: Partial<CrewActionData>,
  ) =>
    data<CrewActionData>(
      { intent, idempotencyKey, notice: null, error: null, invite: null, ...outcome },
      { status, headers: { "Cache-Control": "no-store" } },
    );
  if (!isSameOrigin(request, getWebsiteConfig().publicOrigin)) {
    return reply(403, "", freshIdempotencyKey(), {
      error: "This page is out of date. Reload and try again.",
    });
  }
  const fields = await parseBoundedFormData(request, {
    ...DEFAULT_FORM_LIMITS,
    maxFields: 4 + MAX_CREW_SIZE,
  });
  const idempotencyKey = fields?.["idempotencyKey"] ?? "";
  const intent = fields?.["intent"] ?? "";
  if (fields === null || !isIdempotencyKey(idempotencyKey) || !intents.includes(intent)) {
    return reply(400, intent, freshIdempotencyKey(), {
      error: "That form could not be read. Try again.",
    });
  }
  const key = (step: string) =>
    followUpIdempotencyKey(idempotencyKey, `${intent}\n${step}`);
  try {
    const outcome = await run({
      identity,
      crewId: crewId as string,
      intent,
      fields,
      key,
    });
    return reply(200, intent, freshIdempotencyKey(), outcome);
  } catch (cause) {
    if (cause instanceof Response) throw cause;
    if (isAccountNotFoundError(cause)) {
      const url = new URL(request.url);
      throw redirect(accountSetupPath(url.pathname, url));
    }
    const code = crewErrorCode(cause);
    if (code === "crews_disabled") throw data(null, { status: 404 });
    const status = (cause as { status?: number }).status ?? 503;
    // Without a final answer the write may have happened, so the same key
    // goes again and replays it.
    const next = status >= 500 ? idempotencyKey : freshIdempotencyKey();
    const payload = (cause as { payload?: object }).payload;
    return reply(status, intent, next, {
      error: crewRefusal(code, payload) ?? safeWebsiteError(cause),
    });
  }
}
