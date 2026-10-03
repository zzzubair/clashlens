import {
  mapGroupComparison,
  type ComparisonDays,
  type GroupComparison,
} from "../lib/group-comparison";
import { isCanonicalUuid } from "../lib/validation";
import { PythonApiError, requestJson, type GoogleAccountIdentity } from "./python.server";

/** Read one private group's side-by-side comparison for the signed-in account. */
export async function getGroupComparison(
  identity: GoogleAccountIdentity,
  groupId: string,
  days: ComparisonDays,
): Promise<GroupComparison> {
  if (!isCanonicalUuid(groupId)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const payload = await requestJson<unknown>(
    `/v1/account/groups/${groupId}/comparison?days=${days}`,
    "GET",
    undefined,
    undefined,
    undefined,
    identity,
  );
  const comparison = mapGroupComparison(payload);
  if (comparison === null || comparison.groupId !== groupId) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return comparison;
}
