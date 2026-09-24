import { useEffect, useState } from "react";
import { data, redirect, useActionData, useLoaderData } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import type { VerificationStatus } from "../lib/account-contracts";
import {
  MAX_VERIFICATION_TOKEN_LENGTH,
  normalizeSubmittedPlayerTag,
} from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import type { Route } from "./+types/account.verify-player";

const NO_STORE = { "Cache-Control": "no-store" };

export interface VerifyPlayerLoaderData {
  idempotencyKey: string;
}

export interface VerifyPlayerActionData {
  /** Fresh idempotency key for the next submission attempt. */
  idempotencyKey: string;
  status: VerificationStatus | null;
  verificationRequestId: string | null;
  fieldErrors: { tag?: string; token?: string };
  generalError: WebsiteErrorResponse | null;
  /** The submitted tag only. The API token never appears in returned data. */
  values: { tag: string };
}

const STATUS_MESSAGES: Record<VerificationStatus, string> = {
  linked: "The player was verified and linked to your account.",
  already_linked: "This player is already linked to your account.",
  invalid_token:
    "The API token is invalid or expired. Check the token in Clash of Clans for this player and try again.",
  verification_unavailable:
    "Player verification is temporarily unavailable. Try again later.",
  support_required:
    "This player is linked to another account. The request was recorded for review and the link will not change automatically.",
  in_progress: "Verification is already in progress. Check again shortly.",
};

/**
 * GET /account/verify-player — link a player with their in-game API token.
 * The loader carries only a fresh idempotency key; no token state exists
 * server-side before or after submission.
 */
export async function loader({
  request,
}: Route.LoaderArgs): Promise<VerifyPlayerLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  await requireLogin(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return { idempotencyKey: freshIdempotencyKey() };
}

/**
 * POST /account/verify-player — submit a player tag and API token to the
 * private Python API. The token travels only inside the same-origin request
 * body, is never returned, stored, cached, or logged, and the response
 * carries only safe status outcomes. A failed attempt returns a fresh
 * idempotency key for the retry.
 */
export async function action({ request }: Route.ActionArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");

  const config = getWebsiteConfig();
  if (!actions.isSameOrigin(request, config.publicOrigin)) {
    return errorResponse(403, {
      error: { code: "forbidden", message: "This action is not allowed." },
    });
  }
  const form = await actions.parseBoundedFormData(request);
  if (form === null) return invalidFormResponse();
  const idempotencyKey = form["idempotencyKey"] ?? "";
  if (!actions.isIdempotencyKey(idempotencyKey)) return invalidFormResponse();

  const rawTag = form["tag"] ?? "";
  const token = form["token"] ?? "";
  const fieldErrors: { tag?: string; token?: string } = {};
  const tag = normalizeSubmittedPlayerTag(rawTag);
  if (tag === null) fieldErrors.tag = "Enter a valid player tag.";
  if (!isBoundedToken(token)) {
    fieldErrors.token = "Enter the API token from Clash of Clans.";
  }
  if (fieldErrors.tag || fieldErrors.token) {
    return data<VerifyPlayerActionData>(
      {
        idempotencyKey: actions.freshIdempotencyKey(),
        status: null,
        verificationRequestId: null,
        fieldErrors,
        generalError: null,
        values: { tag: rawTag },
      },
      { status: 400, headers: NO_STORE },
    );
  }

  try {
    const { createPythonClient } = await import("../services/python.server");
    const result = await createPythonClient(identity).verifyPlayerToken(
      tag as string,
      token,
      idempotencyKey,
    );
    if (result.status === "linked" || result.status === "already_linked") {
      return redirect("/account", { status: 303, headers: NO_STORE });
    }
    return data<VerifyPlayerActionData>(
      {
        idempotencyKey: actions.freshIdempotencyKey(),
        status: result.status,
        verificationRequestId: result.verificationRequestId ?? null,
        fieldErrors: {},
        generalError: null,
        values: { tag: result.tag ?? (tag as string) },
      },
      { status: 200, headers: NO_STORE },
    );
  } catch (cause) {
    if (actions.isAccountNotFoundError(cause)) throw redirect("/account/setup");
    const { safeWebsiteError } = await import("../server/errors.server");
    return data<VerifyPlayerActionData>(
      {
        idempotencyKey: actions.freshIdempotencyKey(),
        status: null,
        verificationRequestId: null,
        fieldErrors: {},
        generalError: safeWebsiteError(cause),
        values: { tag: rawTag },
      },
      { status: 422, headers: NO_STORE },
    );
  }
}

/** Exact Python token rule: 1–512 printable ASCII characters, no whitespace. */
function isBoundedToken(value: string): boolean {
  return (
    value.length >= 1 &&
    value.length <= MAX_VERIFICATION_TOKEN_LENGTH &&
    [...value].every((character) => {
      const code = character.charCodeAt(0);
      return code >= 0x21 && code <= 0x7e;
    })
  );
}

async function errorResponse(status: number, generalError: WebsiteErrorResponse) {
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return data<VerifyPlayerActionData>(
    {
      status: null,
      verificationRequestId: null,
      idempotencyKey: freshIdempotencyKey(),
      fieldErrors: {},
      generalError,
      values: { tag: "" },
    },
    { status, headers: NO_STORE },
  );
}

async function invalidFormResponse() {
  return errorResponse(400, {
    error: {
      code: "invalid_input",
      message: "Check the submitted value and try again.",
    },
  });
}

export function headers() {
  return NO_STORE;
}

export default function VerifyPlayerRoute() {
  const loaderData = useLoaderData<typeof loader>();
  const actionData = useActionData<VerifyPlayerActionData>();
  const [token, setToken] = useState("");

  // The API token is cleared after every submission attempt so it is
  // never left in the page after the request completes.
  useEffect(() => {
    setToken("");
  }, [actionData]);

  const tagValue = actionData?.values.tag !== undefined ? actionData.values.tag : "";
  const status = actionData?.status ?? null;

  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="verify-title">
        <h1 id="verify-title">Link account</h1>
        <p className="lede">
          Link a Clash of Clans account you own to your Clash Lens profile.
        </p>
      </section>

      {actionData?.generalError ? <ErrorNotice error={actionData.generalError} /> : null}
      {status ? (
        <div
          className={`status-banner ${status === "linked" || status === "already_linked" ? "status-banner-success" : "status-banner-warning"}`}
          role="status"
        >
          {STATUS_MESSAGES[status]}
          {actionData?.verificationRequestId ? (
            <span className="status-reference">
              Request reference: {actionData.verificationRequestId}
            </span>
          ) : null}
        </div>
      ) : null}

      <section className="form-panel" aria-label="Link account form">
        <h2>Enter your player tag and API token</h2>
        <form method="post" className="stack-form" noValidate>
          <input
            type="hidden"
            name="idempotencyKey"
            value={actionData?.idempotencyKey ?? loaderData.idempotencyKey}
          />
          <div className="form-field">
            <label htmlFor="verify-tag">Player tag</label>
            <input
              id="verify-tag"
              name="tag"
              type="text"
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              defaultValue={tagValue}
              aria-invalid={actionData?.fieldErrors?.tag ? true : undefined}
              aria-describedby={
                actionData?.fieldErrors?.tag ? "verify-tag-error" : undefined
              }
            />
            {actionData?.fieldErrors?.tag ? (
              <p id="verify-tag-error" className="field-error" role="alert">
                {actionData.fieldErrors.tag}
              </p>
            ) : (
              <p className="form-help">The public tag of the player you own.</p>
            )}
          </div>
          <div className="form-field">
            <label htmlFor="verify-token">API token</label>
            <p id="verify-token-help" className="form-help">
              Open Clash of Clans and go to{" "}
              <strong>Settings → More Settings → API token → Show</strong>. Copy the token
              and paste it below. Linking another account? Switch to that account in the
              game first.
            </p>
            <input
              id="verify-token"
              name="token"
              type="text"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              value={token}
              aria-invalid={actionData?.fieldErrors?.token ? true : undefined}
              aria-describedby={
                actionData?.fieldErrors?.token
                  ? "verify-token-help verify-token-privacy verify-token-error"
                  : "verify-token-help verify-token-privacy"
              }
              onChange={(event) => setToken(event.currentTarget.value)}
            />
            {actionData?.fieldErrors?.token ? (
              <p id="verify-token-error" className="field-error" role="alert">
                {actionData.fieldErrors.token}
              </p>
            ) : null}
            <p id="verify-token-privacy" className="form-help">
              Clash Lens uses this token to confirm you own the account and never stores
              it.
            </p>
          </div>
          <button type="submit" className="button button-primary">
            Link account
          </button>
        </form>
      </section>
    </main>
  );
}
