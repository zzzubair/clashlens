import { Link, redirect, useLoaderData } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import type { AccountSummary } from "../lib/account-contracts";
import type { WebsiteErrorResponse } from "../lib/contracts";
import type { Route } from "./+types/account";

const NO_STORE = { "Cache-Control": "no-store" };

export interface AccountLoaderData {
  summary: AccountSummary | null;
  error: WebsiteErrorResponse | null;
}

/**
 * GET /account — open the signed-in user's public profile. The
 * provider identity signs the private Python calls and never appears here.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<AccountLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);

  let summary: AccountSummary | null = null;
  let error: WebsiteErrorResponse | null = null;
  try {
    const { createPythonClient } = await import("../services/python.server");
    summary = await createPythonClient(identity).getAccountSummary();
  } catch (cause) {
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(cause)) throw redirect("/account/setup");
    error = await safeError(cause);
  }
  if (summary) {
    throw redirect(`/users/${encodeURIComponent(summary.username)}`, {
      headers: NO_STORE,
    });
  }
  return { summary, error };
}

async function safeError(cause: unknown): Promise<WebsiteErrorResponse> {
  const { safeWebsiteError } = await import("../server/errors.server");
  return safeWebsiteError(cause);
}

export function headers() {
  return NO_STORE;
}

export default function AccountRoute() {
  const data = useLoaderData<typeof loader>();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell">
      <section className="hero" aria-labelledby="account-title">
        <h1 id="account-title">Your profile could not be loaded</h1>
      </section>

      {data.error ? <ErrorNotice error={data.error} /> : null}

      <Link className="button button-primary" to="/account" reloadDocument>
        Try again
      </Link>
    </main>
  );
}
