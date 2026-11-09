import { mapAttackAlerts, type AttackAlerts } from "../lib/discord";
import { isCanonicalUuid } from "../lib/validation";
import { type LoginIdentity } from "../server/auth.server";
import { PythonApiError } from "./python-response.server";
import { requestJson } from "./python-transport.server";

/** Each verified player's Discord attack alert switch, and any undelivered alert. */
export async function getAttackAlerts(identity: LoginIdentity): Promise<AttackAlerts> {
  const payload = await requestJson<unknown>("/v1/account/discord-alerts", {
    method: "GET",
    expectedKind: "discord-attack-alerts",
    identity: identity,
  });
  const alerts = mapAttackAlerts(payload);
  if (alerts === null) throw new PythonApiError(502, { error: "malformed" });
  return alerts;
}

/** Turn one verified player's Discord attack alerts, or its Legend day
 * summary, on or off, or the account's dark attack alert picture, which names
 * no player. */
export async function setAttackAlert(
  identity: LoginIdentity,
  tag: string,
  on: boolean,
  idempotencyKey: string,
  alert: "attack" | "day_summary" | "dark_picture" = "attack",
): Promise<void> {
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  await requestJson<unknown>("/v1/account/discord-alerts", {
    method: "POST",
    body: Buffer.from(
      JSON.stringify(alert === "dark_picture" ? { on, alert } : { tag, on, alert }),
      "utf8",
    ),
    requestId: idempotencyKey,
    identity: identity,
  });
}
