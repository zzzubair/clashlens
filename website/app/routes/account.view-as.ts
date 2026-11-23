import { redirect } from "react-router";

import { VIEW_AS_PARAM } from "../lib/dashboard";
import type { Route } from "./+types/account.view-as";

const NO_STORE = { "Cache-Control": "private, no-store" };
const INTENTS = ["pin", "unpin", "attack", "day_summary"] as const;

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
