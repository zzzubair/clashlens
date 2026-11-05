import { redirect } from "react-router";

import { VIEW_AS_PARAM } from "../lib/dashboard";
import type { Route } from "./+types/account.view-as";

const NO_STORE = { "Cache-Control": "private, no-store" };
const INTENTS = ["pin", "unpin", "attack", "day_summary"] as const;

/**
 * POST /account/view-as — the site owner's own choices from the View as bar:
 * `intent=pin` or `unpin` the viewed `tag`, or `intent=attack` or
 * `day_summary` with `on=on|off` for the owner's own Discord switch for it.
 * It never runs with `view_as` on its address (root middleware), and only
 * the Python service decides who the owner is; anyone else's request changes
 * nothing and comes back the same way whatever the tag. Each answer goes
 * back to the viewed player's View as, which says whether it saved.
 */
export async function action({ request }: Route.ActionArgs): Promise<Response> {
  if (request.method !== "POST") {
    return new Response(null, { status: 405, headers: NO_STORE });
  }
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  if (!actions.isSameOrigin(request, getWebsiteConfig().publicOrigin)) {
    return new Response(null, { status: 403, headers: NO_STORE });
  }
  const form = await actions.parseBoundedFormData(request);
  const idempotencyKey = form?.["idempotencyKey"] ?? "";
  const intent = INTENTS.find((one) => one === form?.["intent"]);
  const { saveOwnerChoice, viewAsTag } = await import("../server/view-as.server");
  const tag = viewAsTag(form?.["tag"] ?? null);
  const on = form?.["on"];
  if (
    form === null ||
    !actions.isIdempotencyKey(idempotencyKey) ||
    intent === undefined ||
    tag === null ||
    (intent !== "pin" && intent !== "unpin" && on !== "on" && on !== "off")
  ) {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  let saved = "saved";
  try {
    await saveOwnerChoice(
      identity,
      intent === "pin" || intent === "unpin"
        ? { kind: "pin", tag: intent === "pin" ? tag : null }
        : { kind: "follow", tag, alert: intent, on: on === "on" },
      idempotencyKey,
    );
  } catch {
    saved = "failed";
  }
  return redirect(`/?${new URLSearchParams({ [VIEW_AS_PARAM]: tag, saved })}`, {
    headers: NO_STORE,
  });
}

export function headers() {
  return NO_STORE;
}
