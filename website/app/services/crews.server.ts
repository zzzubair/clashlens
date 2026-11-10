import { normalizeUsername } from "../lib/account-validation";
import { data, redirect } from "react-router";
import { getPublicUser } from "./public-user.server";
import { isCanonicalPlayerTag, isRecord, isString } from "./python-response.server";

import type { WebsiteErrorResponse } from "../lib/contracts";
import {
  crewRefusal,
  INVITE_CODE,
  mapCreatedCrew,
  mapCrew,
  mapCrewBoards,
  mapCrewList,
  mapCrewView,
  mapInvitePreview,
  mapMadeInvite,
  mapPlayerCrews,
  MAX_CREW_SIZE,
  type Crew,
  type CrewBoards,
  type CrewList,
  type CrewPeriod,
  type CrewView,
  type InviteLink,
  type InvitePreview,
  type LinkedAccount,
  type MadeInvite,
  type PlayerCrew,
} from "../lib/crew-contracts";
import { isCanonicalUuid } from "../lib/validation";
import {
  DEFAULT_FORM_LIMITS,
  followUpIdempotencyKey,
  freshIdempotencyKey,
  isAccountNotFoundError,
  isIdempotencyKey,
  isSameOrigin,
  parseBoundedFormData,
  readLoginIdentity,
} from "../server/actions.server";
import { requireLogin } from "../server/auth-guard.server";
import { type LoginIdentity } from "../server/auth.server";
import { getWebsiteConfig, isCrewsEnabled } from "../server/config.server";
import { safeWebsiteError } from "../server/errors.server";
import { accountSetupPath } from "../server/return-path.server";
import { checkPlayerTag } from "./group-players.server";
import { PythonApiError } from "./python-response.server";
import { requestJson } from "./python-transport.server";
import { createPythonClient } from "./python.server";

// The player page never waits for this list; a late answer hides only the list.
const PLAYER_CREWS_TIMEOUT_MS = 1_000;

/** The signed-in account's crews. */
export async function listCrews(identity: LoginIdentity): Promise<CrewList> {
  const payload = await requestJson<unknown>("/v1/account/crews", {
    method: "GET",
    expectedKind: "crew-list",
    identity: identity,
  });
  return mapped(mapCrewList(payload));
}

/** Make a crew with some of the account's linked players; returns its id. */
export async function createCrew(
  identity: LoginIdentity,
  crew: { name: string; size: number; tags: string[] },
  idempotencyKey: string,
): Promise<string> {
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const payload = await requestJson<unknown>("/v1/account/crews", {
    method: "POST",
    body: Buffer.from(JSON.stringify(crew), "utf8"),
    requestId: idempotencyKey,
    identity: identity,
  });
  return mapped(mapCreatedCrew(payload));
}

/** One crew the account is in: its name, places and members. */
export async function getCrew(identity: LoginIdentity, crewId: string): Promise<Crew> {
  requireCrewId(crewId);
  const payload = await requestJson<unknown>(`/v1/account/crews/${crewId}`, {
    method: "GET",
    expectedKind: "crew",
    identity: identity,
  });
  const crew = mapped(mapCrew(payload));
  if (crew.crewId !== crewId) throw new PythonApiError(502, { error: "malformed" });
  return crew;
}

/** What anyone sees of a crew; `identity` is the viewer's, if signed in. */
export async function viewCrew(
  identity: LoginIdentity | undefined,
  crewId: string,
): Promise<CrewView> {
  requireCrewId(crewId);
  const payload = await requestJson<unknown>(`/v1/crews/${crewId}`, {
    method: "GET",
    expectedKind: "crew-view",
    identity: identity,
  });
  const crew = mapped(mapCrewView(payload));
  if (crew.crewId !== crewId) throw new PythonApiError(502, { error: "malformed" });
  return crew;
}

/** The crews a player has a place in; none while crews are switched off. */
export async function getPlayerCrews(tag: string): Promise<PlayerCrew[]> {
  if (!isCrewsEnabled()) return [];
  const payload = await requestJson<unknown>(
    `/v1/players/${encodeURIComponent(tag)}/crews`,
    {
      method: "GET",
      expectedKind: "player-crews",
      timeoutMs: PLAYER_CREWS_TIMEOUT_MS,
    },
  );
  return mapped(mapPlayerCrews(payload, tag));
}

/** The crew's six boards for one period; anyone can read them. */
export async function getCrewBoards(
  identity: LoginIdentity | undefined,
  crewId: string,
  period: CrewPeriod,
): Promise<CrewBoards> {
  requireCrewId(crewId);
  const payload = await requestJson<unknown>(
    `/v1/crews/${crewId}/boards?period=${period}`,
    { method: "GET", expectedKind: "crew-boards", identity: identity },
  );
  const boards = mapped(mapCrewBoards(payload));
  if (boards.crewId !== crewId || boards.period !== period) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return boards;
}

/** Repeat-safe writes return only values checked by their feature reader. */
async function writeCrew<T>(
  identity: LoginIdentity,
  method: "POST" | "PATCH" | "DELETE",
  path: string,
  body: Record<string, unknown> | undefined,
  idempotencyKey: string,
  read: (payload: unknown) => T | null,
): Promise<T> {
  if (!isCanonicalUuid(idempotencyKey))
    throw new PythonApiError(400, { error: "invalid_input" });
  const payload = await requestJson<unknown>(`/v1/account/${path}`, {
    method,
    body: body === undefined ? undefined : Buffer.from(JSON.stringify(body), "utf8"),
    requestId: idempotencyKey,
    identity,
  });
  return mapped(read(payload));
}

export function updateCrew(
  identity: LoginIdentity,
  crewId: string,
  patch: { name: string } | { size: number },
  key: string,
) {
  requireCrewId(crewId);
  return writeCrew(identity, "PATCH", `crews/${crewId}`, patch, key, (value) =>
    mapCreatedCrew(value) === crewId ? crewId : null,
  );
}

export function changeCrewMember(
  identity: LoginIdentity,
  crewId: string,
  username: string,
  role: "admin" | "member",
  key: string,
) {
  requireCrewId(crewId);
  return writeCrew(
    identity,
    "PATCH",
    `crews/${crewId}/members/${encodeURIComponent(username)}`,
    { role },
    key,
    (value) =>
      isRecord(value) &&
      value.username === normalizeUsername(username) &&
      value.role === role &&
      isString(value.display_name)
        ? { displayName: value.display_name }
        : null,
  );
}

export function removeCrewPlayer(
  identity: LoginIdentity,
  crewId: string,
  tag: string,
  key: string,
) {
  requireCrewId(crewId);
  return writeCrew(
    identity,
    "DELETE",
    `crews/${crewId}/players/${encodeURIComponent(tag)}`,
    undefined,
    key,
    (value) =>
      isRecord(value) &&
      value.removed === true &&
      value.tag === tag &&
      typeof value.left_crew === "boolean"
        ? { leftCrew: value.left_crew }
        : null,
  );
}

export function addCrewPlayers(
  identity: LoginIdentity,
  crewId: string,
  tags: string[],
  key: string,
) {
  requireCrewId(crewId);
  return writeCrew(identity, "POST", `crews/${crewId}/players`, { tags }, key, (value) =>
    isRecord(value) &&
    value.crew_id === crewId &&
    Array.isArray(value.tags) &&
    value.tags.every(isCanonicalPlayerTag)
      ? { tags: value.tags }
      : null,
  );
}

export function handOverCrew(
  identity: LoginIdentity,
  crewId: string,
  username: string,
  key: string,
) {
  requireCrewId(crewId);
  return writeCrew(
    identity,
    "POST",
    `crews/${crewId}/owner`,
    { username },
    key,
    (value) =>
      isRecord(value) && value.crew_id === crewId && typeof value.left_crew === "boolean"
        ? { leftCrew: value.left_crew }
        : null,
  );
}

export function leaveCrew(identity: LoginIdentity, crewId: string, key: string) {
  requireCrewId(crewId);
  return writeCrew(
    identity,
    "DELETE",
    `crews/${crewId}/members/me`,
    undefined,
    key,
    (value) =>
      isRecord(value) && value.crew_id === crewId && value.left === true ? true : null,
  );
}

export function deleteCrew(identity: LoginIdentity, crewId: string, key: string) {
  requireCrewId(crewId);
  return writeCrew(identity, "DELETE", `crews/${crewId}`, undefined, key, (value) =>
    isRecord(value) && value.crew_id === crewId && value.deleted === true ? true : null,
  );
}

export function revokeCrewInvite(
  identity: LoginIdentity,
  crewId: string,
  inviteId: string,
  key: string,
) {
  requireCrewId(crewId);
  return writeCrew(
    identity,
    "DELETE",
    `crews/${crewId}/invites/${inviteId}`,
    undefined,
    key,
    (value) =>
      isRecord(value) && value.revoked === true && value.invite_id === inviteId
        ? true
        : null,
  );
}

/** The caller's newest invite link while it has a day left, or a new one. */
export async function makeInvite(
  identity: LoginIdentity,
  crewId: string,
  fresh: boolean,
  idempotencyKey: string,
): Promise<MadeInvite> {
  requireCrewId(crewId);
  return writeCrew(
    identity,
    "POST",
    `crews/${crewId}/invites`,
    { new: fresh },
    idempotencyKey,
    mapMadeInvite,
  );
}

/** What an invite link shows: to the account, or to anyone when `identity` is undefined. */
export async function getInvite(
  identity: LoginIdentity | undefined,
  code: string,
): Promise<InvitePreview> {
  requireInviteCode(code);
  const payload = await requestJson<unknown>(
    identity === undefined
      ? `/v1/crew-invites/${code}`
      : `/v1/account/crew-invites/${code}`,
    { method: "GET", expectedKind: "crew-invite", identity: identity },
  );
  return mapped(mapInvitePreview(payload));
}

/** Join through an invite link with the picked accounts; returns the crew's id. */
export async function acceptInvite(
  identity: LoginIdentity,
  code: string,
  tags: string[],
  idempotencyKey: string,
): Promise<string> {
  requireInviteCode(code);
  return writeCrew(
    identity,
    "POST",
    `crew-invites/${code}/accept`,
    { tags },
    idempotencyKey,
    mapCreatedCrew,
  );
}

/** The account's linked Clash of Clans accounts, with whether each is in Legend League. */
export async function linkedAccounts(identity: LoginIdentity): Promise<LinkedAccount[]> {
  const client = createPythonClient(identity);
  const summary = await client.getAccountSummary();
  const profile = await getPublicUser(summary.username);
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
  | { crew: CrewView; boards: CrewBoards; error: null }
  | { crew: null; boards: null; error: WebsiteErrorResponse };

/**
 * A crew page's header and boards, for anyone, signed in or not. Crews are
 * hidden while switched off.
 */
export async function loadCrewPage(
  request: Request,
  crewId: string | undefined,
  period: CrewPeriod,
): Promise<CrewPage> {
  if (!isCrewsEnabled()) throw data(null, { status: 404 });
  if (crewId === undefined || !isCanonicalUuid(crewId)) throw data(null, { status: 404 });
  const identity = await viewerIdentity(request);
  try {
    const [crew, boards] = await Promise.all([
      viewCrew(identity, crewId),
      getCrewBoards(identity, crewId, period),
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
): Promise<LoginIdentity> {
  if (!isCrewsEnabled()) throw data(null, { status: 404 });
  const identity = await requireLogin(request);
  if (crewId === undefined || !isCanonicalUuid(crewId)) throw data(null, { status: 404 });
  return identity;
}

/** The viewer's login if they have a working one; otherwise they view signed out. */
async function viewerIdentity(request: Request): Promise<LoginIdentity | undefined> {
  try {
    const config = getWebsiteConfig();
    if (!config.loginEnabled) return undefined;
    return (await readLoginIdentity(request, config)) ?? undefined;
  } catch {
    return undefined;
  }
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
  identity: LoginIdentity;
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
      {
        intent,
        idempotencyKey,
        notice: null,
        error: null,
        invite: null,
        ...outcome,
      },
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

/**
 * The crew a sign-in, sign-up or linking page is on the way to, when the
 * clasher came from its invite link; null otherwise, for a link that has
 * ended, or when it can't be read.
 */
export async function inviteCrewName(returnPath: string | null): Promise<string | null> {
  const code = /^\/crews\/join\/([^/]+)$/.exec(returnPath ?? "")?.[1];
  if (code === undefined || !INVITE_CODE.test(code) || !isCrewsEnabled()) return null;
  try {
    const invite = await getInvite(undefined, code);
    return invite.state === "invalid" || invite.state === "ended" ? null : invite.name;
  } catch {
    return null;
  }
}
