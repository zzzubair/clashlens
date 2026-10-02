import { data } from "react-router";

import type { Route } from "./+types/refresh";
import { normalizePlayerTag } from "../lib/player-tag";
import type { RefreshError } from "../lib/contracts";
import { isCanonicalUuid } from "../lib/validation";

const NO_STORE_HEADERS = { "Cache-Control": "no-store" };
const MAX_FORM_BODY_BYTES = 4_096;
const FORM_CONTENT_TYPE = "application/x-www-form-urlencoded";

function statusForError(code: string): number {
  switch (code) {
    case "invalid_input":
      return 400;
    case "missing":
      return 404;
    case "forbidden":
      return 403;
    case "rate_limited":
      return 429;
    case "conflict":
      return 409;
    case "uncertain":
      return 422;
    case "malformed":
      return 502;
    default:
      return 503;
  }
}

async function safeErrorResponse(error: unknown) {
  const { safeWebsiteError } = await import("../server/errors.server");
  const safe = safeWebsiteError(error);
  return data<RefreshError>(safe, {
    status: statusForError(safe.error.code),
    headers: NO_STORE_HEADERS,
  });
}

async function safeStatusErrorResponse(error: unknown) {
  const { safeWebsiteError } = await import("../server/errors.server");
  const safe = safeWebsiteError(error);
  return new Response(JSON.stringify(safe), {
    status: statusForError(safe.error.code),
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}

async function readRefreshForm(
  request: Request,
): Promise<{ idempotencyKey: string; automatic: boolean } | null> {
  const contentType = request.headers
    .get("Content-Type")
    ?.split(";", 1)[0]
    .trim()
    .toLowerCase();
  if (contentType !== FORM_CONTENT_TYPE) return null;

  const declaredLength = request.headers.get("Content-Length");
  if (
    declaredLength !== null &&
    (!/^\d+$/.test(declaredLength) || Number(declaredLength) > MAX_FORM_BODY_BYTES)
  ) {
    return null;
  }

  const reader = request.body?.getReader();
  if (!reader) return null;
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      total += result.value.byteLength;
      if (total > MAX_FORM_BODY_BYTES) return null;
      chunks.push(result.value);
    }
  } finally {
    reader.releaseLock();
  }

  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let form: URLSearchParams;
  try {
    form = new URLSearchParams(new TextDecoder("utf-8", { fatal: true }).decode(body));
  } catch {
    return null;
  }
  const values = form.getAll("idempotencyKey");
  const triggers = form.getAll("trigger");
  if (
    values.length !== 1 ||
    !isCanonicalUuid(values[0]) ||
    triggers.length > 1 ||
    (triggers.length === 1 && triggers[0] !== "automatic")
  ) {
    return null;
  }
  return { idempotencyKey: values[0], automatic: triggers[0] === "automatic" };
}

export async function action({ request, params, context }: Route.ActionArgs) {
  const { isSameOrigin } = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  if (
    request.method !== "POST" ||
    !isSameOrigin(request, getWebsiteConfig().publicOrigin)
  ) {
    return safeErrorResponse({ status: 403, payload: { error: "forbidden" } });
  }
  const normalized = normalizePlayerTag(params.tag ?? "");
  if (!normalized) {
    return safeErrorResponse({ status: 400, payload: { error: "invalid_input" } });
  }
  const form = await readRefreshForm(request);
  if (form === null) {
    return safeErrorResponse({ status: 400, payload: { error: "invalid_input" } });
  }

  const { clientAddressContext } = await import("../server/client-address.server");
  const identity = context?.get(clientAddressContext);
  if (!identity) {
    return safeErrorResponse({ status: 503, payload: { error: "service_unavailable" } });
  }
  const { allowPublicRefresh, allowAutomaticRefresh } =
    await import("../server/abuse.server");
  if (form.automatic) {
    if (!allowAutomaticRefresh(identity)) {
      // No work or error to show: leave the saved profile and its timestamp visible.
      return data(null, { status: 200, headers: NO_STORE_HEADERS });
    }
  } else if (!allowPublicRefresh(identity)) {
    return safeErrorResponse({
      status: 429,
      payload: { error: "rate_limited", retry_after_seconds: 60 },
    });
  }

  try {
    const client = await import("../services/python.server");
    const work = await client
      .createPythonClient()
      .requestRefresh(normalized, form.idempotencyKey);
    return data(work, { status: 202, headers: NO_STORE_HEADERS });
  } catch (error) {
    return safeErrorResponse(error);
  }
}

export async function loader({ request, params }: Route.LoaderArgs) {
  const normalized = normalizePlayerTag(params.tag ?? "");
  if (!normalized) {
    return safeStatusErrorResponse({ status: 400, payload: { error: "invalid_input" } });
  }
  const workId = new URL(request.url).searchParams.get("workId")?.trim();
  if (!workId || !/^[A-Za-z0-9_-]{1,128}$/.test(workId)) {
    return safeStatusErrorResponse({ status: 400, payload: { error: "invalid_input" } });
  }
  try {
    const client = await import("../services/python.server");
    const work = await client.createPythonClient().getRefreshStatus(workId, normalized);
    return data(work, { headers: NO_STORE_HEADERS });
  } catch (error) {
    return safeStatusErrorResponse(error);
  }
}
