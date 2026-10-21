import { data } from "react-router";

import { parseShareChange } from "../lib/notebook-share";
import type { Route } from "./+types/notebook.share";

const NO_STORE = { "Cache-Control": "no-store" };
// The private API's refusals the page can explain; anything else is
// "unavailable".
const REFUSALS = new Set(["base_not_found", "share_not_found"]);

/**
 * POST /notebook/share — share one of the signed-in Clasher's bases through
 * an unlisted link, change what its page shows, or stop sharing it, sent as
 * `change` (JSON) by the base page. Answers how the base is shared now
 * (`share`, null once stopped), or an `error` code.
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
    change = parseShareChange(JSON.parse(form?.["change"] ?? ""));
  } catch {
    // Not JSON: refused below.
  }
  if (change === null)
    return data({ error: "invalid" }, { status: 400, headers: NO_STORE });
  const { saveShare } = await import("../services/notebook-share.server");
  const { PythonApiError } = await import("../services/python.server");
  try {
    return data({ share: await saveShare(identity, change) }, { headers: NO_STORE });
  } catch (cause) {
    const code = (cause as { payload?: { error?: unknown } }).payload?.error;
    if (cause instanceof PythonApiError && typeof code === "string" && REFUSALS.has(code))
      return data({ error: code }, { status: 422, headers: NO_STORE });
    return data({ error: "unavailable" }, { status: 503, headers: NO_STORE });
  }
}
