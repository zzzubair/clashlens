import type { ListedCrew } from "../lib/crew-contracts";
import { isCanonicalUuid } from "../lib/validation";
import { type LoginIdentity } from "../server/auth.server";
import { isRecord, isString, PythonApiError } from "./python-response.server";
import { requestJson } from "./python-transport.server";

/** A crew the Clasher owns where the player is their only one. */
export type HandOverCrew = Pick<ListedCrew, "crewId" | "name">;

/** Unlink one of the account's own linked players. Returns the crews to hand
 * over first, empty once it is unlinked. The private API answers 404 when the
 * player is not linked to this account. */
export async function unlinkPlayer(
  identity: LoginIdentity,
  tag: string,
  idempotencyKey: string,
): Promise<HandOverCrew[]> {
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  try {
    await requestJson<unknown>(`/v1/account/players/${encodeURIComponent(tag)}`, {
      method: "DELETE",
      requestId: idempotencyKey,
      identity: identity,
    });
    return [];
  } catch (cause) {
    const crews = handOverCrews(cause);
    if (crews === null) throw cause;
    return crews;
  }
}

function handOverCrews(cause: unknown): HandOverCrew[] | null {
  if (!(cause instanceof PythonApiError) || cause.status !== 409) return null;
  const payload = cause.payload;
  if (!isRecord(payload) || payload.error !== "owner_must_hand_over") return null;
  const crews = Array.isArray(payload.crews) ? payload.crews : [];
  const mapped = crews.flatMap((crew: unknown) =>
    isRecord(crew) &&
    isString(crew.crew_id) &&
    isCanonicalUuid(crew.crew_id) &&
    isString(crew.name)
      ? [{ crewId: crew.crew_id, name: crew.name }]
      : [],
  );
  return mapped.length > 0 && mapped.length === crews.length ? mapped : null;
}
