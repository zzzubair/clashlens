import type { ArmyVariantExamples } from "../lib/contracts";
import { mapArmyUnits } from "./player-response.server";
import { requestJson } from "./python.server";
import { isInteger, isRecord, isString, PythonApiError } from "./python-response.server";

/** A variant's most-used armies for the Armies page's current-Season players. */
export async function getArmyVariantExamples(
  population: string,
  typeId: number,
  variantId: number,
): Promise<ArmyVariantExamples> {
  const query = new URLSearchParams({
    season: "current",
    population,
    category: "army-types",
    type_id: String(typeId),
    variant_id: String(variantId),
  });
  const payload = await requestJson<Record<string, unknown>>(
    `/v1/analytics/armies?${query}`,
    "GET",
    undefined,
    "army-variant-examples",
  );
  const groups = payload.groups;
  if (
    !isString(payload.rule) ||
    !Array.isArray(groups) ||
    !groups.every(
      (group) =>
        isRecord(group) && isInteger(group.attacks) && isString(group.army_share_code),
    )
  )
    throw new PythonApiError(502, { error: "malformed" });
  return {
    rule: payload.rule,
    groups: groups.map((group: Record<string, unknown>) => ({
      attacks: group.attacks as number,
      armyShareCode: group.army_share_code as string,
    })),
    armyUnits: mapArmyUnits(payload.army_units),
  };
}
