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
 * POST /account/profile — update the username and display name through the
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

  const values = {
    username: form["username"] ?? "",
    displayName: form["displayName"] ?? "",
  };
  const validation = actions.validateAccountNames(values);
  if (validation.fieldErrors.username || validation.fieldErrors.displayName) {
    return data<ProfileActionData>(
      {
        idempotencyKey: actions.freshIdempotencyKey(),
        fieldErrors: validation.fieldErrors,
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
    await client.updateAccount(
      {
        username: validation.username as string,
        displayName: validation.displayName as string,
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
  const [values, setValues] = useState(
    actionData?.values ?? {
      username: loaderData.username,
      displayName: loaderData.displayName,
    },
  );
  const [clientErrors, setClientErrors] = useState<{
    username?: string;
    displayName?: string;
  }>({});

  const serverErrors = actionData?.fieldErrors ?? {};
  const usernameError = serverErrors.username ?? clientErrors.username;
  const displayNameError = serverErrors.displayName ?? clientErrors.displayName;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    const errors: { username?: string; displayName?: string } = {};
    if (normalizeUsername(values.username) === null) {
      errors.username =
        "Username must start with a letter and use 3–32 lowercase letters, numbers, or underscores.";
    } else if (isInappropriateName(values.username)) {
      errors.username = "Choose a different username.";
    }
    if (normalizeDisplayName(values.displayName) === null) {
      errors.displayName =
        "Display name must be 1–80 characters and must not contain control characters.";
    } else if (isInappropriateName(values.displayName)) {
      errors.displayName = "Choose a different display name.";
    }
    setClientErrors(errors);
    if (errors.username || errors.displayName) event.preventDefault();
  }

  return (
    <main className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="profile-title">
        <h1 id="profile-title">Edit profile</h1>
        <p className="lede">
          Your username must stay unique; your display name can change freely.
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
          <div className="form-field">
            <label htmlFor="profile-username">Username</label>
            <input
              id="profile-username"
              name="username"
              type="text"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              value={values.username}
              aria-invalid={usernameError ? true : undefined}
              aria-describedby={usernameError ? "profile-username-error" : undefined}
              onChange={(event) => {
                const username = event.currentTarget.value;
                setValues((current) => ({
                  ...current,
                  username,
                }));
              }}
            />
            {usernameError ? (
              <p id="profile-username-error" className="field-error" role="alert">
                {usernameError}
              </p>
            ) : (
              <p className="form-help">
                Lowercase letters, numbers, and underscores. Starts with a letter.
              </p>
            )}
          </div>
          <div className="form-field">
            <label htmlFor="profile-display-name">Display name</label>
            <input
              id="profile-display-name"
              name="displayName"
              type="text"
              autoComplete="nickname"
              value={values.displayName}
              aria-invalid={displayNameError ? true : undefined}
              aria-describedby={
                displayNameError ? "profile-display-name-error" : undefined
              }
              onChange={(event) => {
                const displayName = event.currentTarget.value;
                setValues((current) => ({
                  ...current,
                  displayName,
                }));
              }}
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
