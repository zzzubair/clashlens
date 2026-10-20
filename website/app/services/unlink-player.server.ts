import { isCanonicalUuid } from "../lib/validation";
import { PythonApiError, requestJson, type GoogleAccountIdentity } from "./python.server";

/** Unlink one of the account's own linked players. The private API answers
 * 404 when the player is not linked to this account. */
export async function unlinkPlayer(
  identity: GoogleAccountIdentity,
  tag: string,
  idempotencyKey: string,
): Promise<void> {
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  await requestJson<unknown>(
    `/v1/account/players/${encodeURIComponent(tag)}`,
    "DELETE",
    undefined,
    undefined,
    idempotencyKey,
    identity,
  );
}
