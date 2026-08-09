import { useState, type FormEvent } from "react";
import { Link, data, redirect, useActionData, useLoaderData } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import {
  isInappropriateName,
  normalizeDisplayName,
  normalizeUsername,
} from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import type { Route } from "./+types/account.setup";

const NO_STORE = { "Cache-Control": "no-store" };

export interface SetupLoaderData {
  idempotencyKey: string;
}

export interface SetupActionData {
  /** Fresh idempotency key for the next submission attempt. */
  idempotencyKey: string;
  fieldErrors: { username?: string; displayName?: string };
  generalError: WebsiteErrorResponse | null;
  values: { username: string; displayName: string };
}

/**
 * GET /account/setup — first-time account creation form. Requires a valid
 * browser login; the account is created only through the private Python API.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<SetupLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  await requireLogin(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return { idempotencyKey: freshIdempotencyKey() };
}

/**
 * POST /account/setup — create the account. Same-origin, bounded form,
 * canonical idempotency UUID, early name validation (including the strict
 * inappropriate-name feedback), and safe Python error mapping.
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
    return data<SetupActionData>(
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
    await createPythonClient(identity).createAccount(
      {
        username: validation.username as string,
        displayName: validation.displayName as string,
      },
      idempotencyKey,
    );
  } catch (error) {
    const outcome = actions.mapAccountNameError(error);
    if (outcome.kind === "account_exists") throw redirect("/account");
    if (outcome.kind === "field") {
      return data<SetupActionData>(
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
      return data<SetupActionData>(
        {
          idempotencyKey: actions.freshIdempotencyKey(),
          fieldErrors: {},
          generalError: outcome.generalError,
          values,
        },
        { status: 422, headers: NO_STORE },
      );
    }
    throw redirect("/account/setup");
  }
  throw redirect("/account");
}

async function forbiddenResponse() {
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return data<SetupActionData>(
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
  return data<SetupActionData>(
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

export default function AccountSetupRoute() {
  const loaderData = useLoaderData<typeof loader>();
  const actionData = useActionData<SetupActionData>();
  const [values, setValues] = useState(
    actionData?.values ?? { username: "", displayName: "" },
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
      <section className="hero" aria-labelledby="setup-title">
        <h1 id="setup-title">Create your account</h1>
        <p className="lede">
          Choose a unique username and a display name. Your Google identity is never shown
          publicly and your email address is never used.
        </p>
      </section>

      {actionData?.generalError ? <ErrorNotice error={actionData.generalError} /> : null}

      <section className="form-panel" aria-label="Account setup form">
        <form method="post" className="stack-form" onSubmit={handleSubmit} noValidate>
          <input
            type="hidden"
            name="idempotencyKey"
            value={actionData?.idempotencyKey ?? loaderData.idempotencyKey}
          />
          <div className="form-field">
            <label htmlFor="setup-username">Username</label>
            <input
              id="setup-username"
              name="username"
              type="text"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              value={values.username}
              aria-invalid={usernameError ? true : undefined}
              aria-describedby={usernameError ? "setup-username-error" : undefined}
              onChange={(event) => {
                const username = event.currentTarget.value;
                setValues((current) => ({
                  ...current,
                  username,
                }));
              }}
            />
            {usernameError ? (
              <p id="setup-username-error" className="field-error" role="alert">
                {usernameError}
              </p>
            ) : (
              <p className="form-help">
                Lowercase letters, numbers, and underscores. Starts with a letter.
              </p>
            )}
          </div>
          <div className="form-field">
            <label htmlFor="setup-display-name">Display name</label>
            <input
              id="setup-display-name"
              name="displayName"
              type="text"
              autoComplete="nickname"
              value={values.displayName}
              aria-invalid={displayNameError ? true : undefined}
              aria-describedby={displayNameError ? "setup-display-name-error" : undefined}
              onChange={(event) => {
                const displayName = event.currentTarget.value;
                setValues((current) => ({
                  ...current,
                  displayName,
                }));
              }}
            />
            {displayNameError ? (
              <p id="setup-display-name-error" className="field-error" role="alert">
                {displayNameError}
              </p>
            ) : (
              <p className="form-help">1–80 characters. Shown on your public page.</p>
            )}
          </div>
          <button type="submit" className="button button-primary">
            Create account
          </button>
        </form>
      </section>

      <p className="back-link">
        <Link to="/account">← Back to your account</Link>
      </p>
    </main>
  );
}
