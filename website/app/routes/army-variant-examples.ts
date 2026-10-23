import { data } from "react-router";

import type { ArmyVariantExamples } from "../lib/contracts";
import type { Route } from "./+types/army-variant-examples";

export type VariantExamplesResponse = { examples: ArmyVariantExamples | null };

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * GET /resources/analytics/armies/examples?population=&season=&start=&end=&type=&variant=
 * — what a variant row on the Armies page loads when it opens: that variant's
 * most-used armies over the same current-Season Legend days as the page.
 */
export async function loader({ request }: Route.LoaderArgs) {
  const query = new URL(request.url).searchParams;
  const population = query.get("population") ?? "";
  const season = query.get("season") ?? "";
  const [startDay, endDay, typeId, variantId] = ["start", "end", "type", "variant"].map(
    (key) => Number(query.get(key) ?? ""),
  );
  if (
    !/^top-\d+$/.test(population) ||
    !/^\d+$/.test(season) ||
    ![startDay, endDay, typeId, variantId].every(Number.isInteger)
  )
    return data<VariantExamplesResponse>(
      { examples: null },
      { status: 400, headers: NO_STORE },
    );
  const { getArmyVariantExamples } = await import("../services/army-examples.server");
  // Returned, not thrown: the row says its armies could not load, never the page.
  const examples = await getArmyVariantExamples(
    population,
    { season, startDay, endDay },
    typeId,
    variantId,
  ).catch(() => null);
  return data<VariantExamplesResponse>({ examples }, { headers: NO_STORE });
}
