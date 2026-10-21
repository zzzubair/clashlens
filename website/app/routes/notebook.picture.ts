import { data } from "react-router";

import { BASE_ID, MAX_PICTURE_BYTES } from "../lib/notebook";
import type { Route } from "./+types/notebook.picture";

const NO_STORE = { "Cache-Control": "no-store" };
// The private API's refusals the page can explain, each with its status;
// anything else is "unavailable".
const REFUSALS: Record<string, number> = {
  not_a_picture: 422,
  picture_too_large: 413,
  request_body_too_large: 413,
  daily_limit: 429,
  base_not_found: 404,
  pictures_unavailable: 503,
};

/**
 * POST /notebook/picture?base=B-… — the signed-in Clasher's picture for one
 * of their bases, sent by the base page as the request body itself (a JPEG,
 * PNG or WebP file), or with `remove=1` and no body to remove it. Answers the
 * picture's new links, or an `error` code.
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
  const url = new URL(request.url);
  const base = url.searchParams.get("base") ?? "";
  const remove = url.searchParams.get("remove") === "1";
  const type = request.headers.get("Content-Type") ?? "";
  if (!BASE_ID.test(base) || (!remove && !type.startsWith("image/")))
    return data({ error: "invalid" }, { status: 400, headers: NO_STORE });
  const image = remove ? null : await readPicture(request);
  if (image === "too_large")
    return data({ error: "picture_too_large" }, { status: 413, headers: NO_STORE });
  if (image?.length === 0)
    return data({ error: "not_a_picture" }, { status: 422, headers: NO_STORE });
  const { saveBasePicture } = await import("../services/notebook.server");
  const { PythonApiError } = await import("../services/python.server");
  try {
    return data(
      { picture: await saveBasePicture(identity, base, image) },
      { headers: NO_STORE },
    );
  } catch (cause) {
    const code = (cause as { payload?: { error?: unknown } }).payload?.error;
    if (cause instanceof PythonApiError && typeof code === "string" && code in REFUSALS)
      return data(
        { error: code === "request_body_too_large" ? "picture_too_large" : code },
        { status: REFUSALS[code], headers: NO_STORE },
      );
    return data({ error: "unavailable" }, { status: 503, headers: NO_STORE });
  }
}

/** The request body, or "too_large" past MAX_PICTURE_BYTES without reading
 * the rest. */
async function readPicture(request: Request): Promise<Buffer | "too_large"> {
  const declared = Number(request.headers.get("Content-Length") ?? "0");
  if (!(declared <= MAX_PICTURE_BYTES)) return "too_large";
  const chunks: Uint8Array[] = [];
  let total = 0;
  const reader = request.body?.getReader();
  if (!reader) return Buffer.alloc(0);
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_PICTURE_BYTES) {
        await reader.cancel();
        return "too_large";
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks);
}
