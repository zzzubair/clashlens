import { data } from "react-router";

import { parseChange } from "../lib/notebook";
import type { Route } from "./+types/notebook.save";

const NO_STORE = { "Cache-Control": "no-store" };
// The private API's refusals the page can explain; anything else is
// "unavailable".
const REFUSALS = new Set([
  "invalid_name",
  "invalid_base_link",
  "invalid_note",
  "invalid_characters",
  "invalid_day",
  "player_required",
  "player_not_verified",
  "base_not_found",
  "runs_until_season_end",
  "not_legend_day",
  "not_attacked",
  "already_saved",
]);

/**
 * POST /notebook/save — one change to the signed-in Clasher's notebook, sent
 * as `change` (JSON) by the Notebook and base pages without a reload. Answers
 * what the change did and what undoes it, or an `error` code.
 */
export async function action({ request }: Route.ActionArgs) {
  const { isNotebookEnabled, getWebsiteConfig } = await import("../server/config.server");
  if (!isNotebookEnabled()) throw data(null, { status: 404 });
  if (request.method !== "POST")
    return data({ error: "method" }, { status: 405, headers: NO_STORE });
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  if (!actions.isSameOrigin(request, getWebsiteConfig().publicOrigin))
    return data({ error: "forbidden" }, { status: 403, headers: NO_STORE });
  const form = await actions.parseBoundedFormData(request);
  let change = null;
  try {
    change = parseChange(JSON.parse(form?.["change"] ?? ""));
  } catch {
    // Not JSON: refused below.
  }
  if (change === null)
    return data({ error: "invalid" }, { status: 400, headers: NO_STORE });
  const { saveNotebook } = await import("../services/notebook.server");
  const { PythonApiError } = await import("../services/python.server");
  try {
    return data({ saved: await saveNotebook(identity, change) }, { headers: NO_STORE });
  } catch (cause) {
    const code = (cause as { payload?: { error?: unknown } }).payload?.error;
    if (cause instanceof PythonApiError && typeof code === "string" && REFUSALS.has(code))
      return data({ error: code }, { status: 422, headers: NO_STORE });
    return data({ error: "unavailable" }, { status: 503, headers: NO_STORE });
  }
}
