import { data } from "react-router";

import type { ArmyVariantExamples } from "../lib/contracts";
import type { Route } from "./+types/army-variant-examples";

export type VariantExamplesResponse = { examples: ArmyVariantExamples | null };

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * GET /resources/analytics/armies/examples?population=&type=&variant= — what
 * a variant row on the Armies page loads when it opens: that variant's
 * most-used armies over the current Season's finished Legend days.
 */
export async function loader({ request }: Route.LoaderArgs) {
  const query = new URL(request.url).searchParams;
  const population = query.get("population") ?? "";
  const [typeId, variantId] = [query.get("type"), query.get("variant")].map(Number);
  if (
    !/^top-\d+$/.test(population) ||
    !Number.isInteger(typeId) ||
    !Number.isInteger(variantId)
  )
    return data<VariantExamplesResponse>(
      { examples: null },
      { status: 400, headers: NO_STORE },
    );
  const { getArmyVariantExamples } = await import("../services/army-examples.server");
  // Returned, not thrown: the row says its armies could not load, never the page.
  const examples = await getArmyVariantExamples(population, typeId, variantId).catch(
    () => null,
  );
  return data<VariantExamplesResponse>({ examples }, { headers: NO_STORE });
}
