import { useState, type FormEvent } from "react";
import { data, redirect, useActionData, useLoaderData } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import {
  isInappropriateName,
  normalizeDisplayName,
  normalizeUsername,
} from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import type { Route } from "./+types/account.profile";

const NO_STORE = { "Cache-Control": "no-store" };

export interface ProfileLoaderData {
  username: string;
  displayName: string;
  idempotencyKey: string;
  error: WebsiteErrorResponse | null;
}

export interface ProfileActionData {
  /** Fresh idempotency key for the next submission attempt. */
  idempotencyKey: string;
  fieldErrors: { username?: string; displayName?: string };
  generalError: WebsiteErrorResponse | null;
  values: { username: string; displayName: string };
}

/**
 * GET /account/profile — the name-editing form, prefilled from the private
 * Python account. An identity with no account yet goes to setup.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<ProfileLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  try {
    const { createPythonClient } = await import("../services/python.server");
    const account = await createPythonClient(identity).getAccount();
    return {
      username: account.username,
      displayName: account.displayName,
      idempotencyKey: freshIdempotencyKey(),
      error: null,
    };
  } catch (cause) {
    const actions = await import("../server/actions.server");
    const outcome = actions.mapAccountNameError(cause);
    if (outcome.kind === "account_not_found") throw redirect("/account/setup");
    const { safeWebsiteError } = await import("../server/errors.server");
    return {
      username: "",
      displayName: "",
      idempotencyKey: freshIdempotencyKey(),
      error: safeWebsiteError(cause),
    };
  }
}

/**
 * POST /account/profile — update the display name through the
 * existing Python rules, passing the stored preferences through unchanged.
 */
export async function action({ request }: Route.ActionArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");

  const config = getWebsiteConfig();
  if (!actions.isSameOrigin(request, config.publicOrigin)) {
    return forbiddenResponse();
  }
  const form = await actions.parseBoundedFormData(request);
  if (form === null) return invalidFormResponse();
  const idempotencyKey = form["idempotencyKey"] ?? "";
  if (!actions.isIdempotencyKey(idempotencyKey)) return invalidFormResponse();

  // The page shows the username as fixed text; a posted one must still match.
  const postedUsername = form["username"];
  const values = {
    username: postedUsername ?? "",
    displayName: form["displayName"] ?? "",
  };
  const { displayName, fieldErrors } = actions.validateAccountNames(values);
  if (fieldErrors.displayName) {
    return data<ProfileActionData>(
      {
        idempotencyKey: actions.freshIdempotencyKey(),
        fieldErrors: { displayName: fieldErrors.displayName },
        generalError: null,
        values,
      },
      { status: 400, headers: NO_STORE },
    );
  }

  try {
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient(identity);
    const account = await client.getAccount();
    if (
      postedUsername !== undefined &&
      normalizeUsername(postedUsername) !== account.username
    ) {
      return data<ProfileActionData>(
        {
          idempotencyKey: actions.freshIdempotencyKey(),
          fieldErrors: { username: "To request a username change, contact support." },
          generalError: null,
          values: { ...values, username: account.username },
        },
        { status: 400, headers: NO_STORE },
      );
    }
    await client.updateAccount(
      {
        username: account.username,
        displayName: displayName as string,
        preferences: account.preferences,
      },
      idempotencyKey,
    );
  } catch (error) {
    const outcome = actions.mapAccountNameError(error);
    if (outcome.kind === "account_not_found") throw redirect("/account/setup");
    if (outcome.kind === "field") {
      return data<ProfileActionData>(
        {
          idempotencyKey: actions.freshIdempotencyKey(),
          fieldErrors: outcome.fieldErrors,
          generalError: null,
          values,
        },
        { status: outcome.status, headers: NO_STORE },
      );
    }
    if (outcome.kind === "general") {
      return data<ProfileActionData>(
        {
          idempotencyKey: actions.freshIdempotencyKey(),
          fieldErrors: {},
          generalError: outcome.generalError,
          values,
        },
        { status: 422, headers: NO_STORE },
      );
    }
    throw redirect("/account/profile");
  }
  throw redirect("/account/profile");
}

async function forbiddenResponse() {
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return data<ProfileActionData>(
    {
      idempotencyKey: freshIdempotencyKey(),
      fieldErrors: {},
      generalError: {
        error: { code: "forbidden", message: "This action is not allowed." },
      },
      values: { username: "", displayName: "" },
    },
    { status: 403, headers: NO_STORE },
  );
}

async function invalidFormResponse() {
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return data<ProfileActionData>(
    {
      idempotencyKey: freshIdempotencyKey(),
      fieldErrors: {},
      generalError: {
        error: {
          code: "invalid_input",
          message: "Check the submitted value and try again.",
        },
      },
      values: { username: "", displayName: "" },
    },
    { status: 400, headers: NO_STORE },
  );
}

export function headers() {
  return NO_STORE;
}

export default function AccountProfileRoute() {
  const loaderData = useLoaderData<typeof loader>();
  const actionData = useActionData<ProfileActionData>();
  const [displayName, setDisplayName] = useState(
    actionData?.values.displayName ?? loaderData.displayName,
  );
  const [clientError, setClientError] = useState<string>();

  const usernameError = actionData?.fieldErrors.username;
  const displayNameError = actionData?.fieldErrors.displayName ?? clientError;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    let error: string | undefined;
    if (normalizeDisplayName(displayName) === null) {
      error =
        "Display name must be 1–80 characters and must not contain control characters.";
    } else if (isInappropriateName(displayName)) {
      error = "Choose a different display name.";
    }
    setClientError(error);
    if (error) event.preventDefault();
  }

  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="profile-title">
        <h1 id="profile-title">Edit profile</h1>
        <p className="lede">
          Update the display name shown on your public profile. Your username is fixed.
        </p>
      </section>

      {loaderData.error ? <ErrorNotice error={loaderData.error} /> : null}
      {actionData?.generalError ? <ErrorNotice error={actionData.generalError} /> : null}

      <section className="form-panel" aria-label="Profile form">
        <form method="post" className="stack-form" onSubmit={handleSubmit} noValidate>
          <input
            type="hidden"
            name="idempotencyKey"
            value={actionData?.idempotencyKey ?? loaderData.idempotencyKey}
          />
          <dl className="form-field fixed-field">
            <dt>Username</dt>
            <dd className="fixed-value">
              {loaderData.username ? `@${loaderData.username}` : null}
            </dd>
            {usernameError ? (
              <dd>
                <p className="field-error" role="alert">
                  {usernameError}
                </p>
              </dd>
            ) : null}
            <dd className="form-help">
              Usernames can't be changed. Contact support if you need a new one.
            </dd>
          </dl>
          <div className="form-field">
            <label htmlFor="profile-display-name">Display name</label>
            <input
              id="profile-display-name"
              name="displayName"
              type="text"
              autoComplete="nickname"
              value={displayName}
              aria-invalid={displayNameError ? true : undefined}
              aria-describedby={
                displayNameError ? "profile-display-name-error" : undefined
              }
              onChange={(event) => setDisplayName(event.currentTarget.value)}
            />
            {displayNameError ? (
              <p id="profile-display-name-error" className="field-error" role="alert">
                {displayNameError}
              </p>
            ) : (
              <p className="form-help">1–80 characters. Shown on your public page.</p>
            )}
          </div>
          <button type="submit" className="button button-primary">
            Save changes
          </button>
        </form>
      </section>
    </main>
  );
}
