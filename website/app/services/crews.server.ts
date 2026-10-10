import { data, redirect } from "react-router";

import type { WebsiteErrorResponse } from "../lib/contracts";
import { isAccountNotFoundError } from "../server/actions.server";
import { requireLogin } from "../server/auth-guard.server";
import { isCrewsEnabled } from "../server/config.server";
import { safeWebsiteError } from "../server/errors.server";
import { accountSetupPath } from "../server/return-path.server";
import {
  mapCreatedCrew,
  mapCrew,
  mapCrewBoards,
  mapCrewList,
  type Crew,
  type CrewBoards,
  type CrewList,
  type CrewPeriod,
} from "../lib/crew-contracts";
import { isCanonicalUuid } from "../lib/validation";
import { PythonApiError, requestJson, type GoogleAccountIdentity } from "./python.server";

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
  if (!isCrewsEnabled()) throw data(null, { status: 404 });
  const identity = await requireLogin(request);
  if (crewId === undefined || !isCanonicalUuid(crewId)) throw data(null, { status: 404 });
  try {
    const [crew, boards] = await Promise.all([
      getCrew(identity, crewId),
      getCrewBoards(identity, crewId, period),
    ]);
    return { crew, boards, error: null };
  } catch (cause) {
    if (isAccountNotFoundError(cause)) {
      const url = new URL(request.url);
      throw redirect(accountSetupPath(url.pathname, url));
    }
    const code = crewErrorCode(cause);
    // The private API's own switch answers crews_disabled.
    if (code === "crew_not_found" || code === "crews_disabled") {
      throw data(null, { status: 404 });
    }
    return { crew: null, boards: null, error: safeWebsiteError(cause) };
  }
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
