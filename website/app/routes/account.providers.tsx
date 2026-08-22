import { redirect, useLoaderData } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import type { WebsiteErrorResponse } from "../lib/contracts";
import type { Route } from "./+types/account.providers";

const NO_STORE = { "Cache-Control": "no-store" };

const PROVIDER_LABELS: Record<string, string> = {
  discord: "Discord",
  google: "Google",
};

export interface ProvidersLoaderData {
  providers: string[];
  idempotencyKey: string;
  error: WebsiteErrorResponse | null;
}

/**
 * GET /account/providers — show the sign-in connections on the account and
 * the actions to link or unlink each provider. The provider identities sign
 * the private Python calls; subjects never appear in page data.
 */
export async function loader({
  request,
}: Route.LoaderArgs): Promise<ProvidersLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  try {
    const { createPythonClient } = await import("../services/python.server");
    const account = await createPythonClient(identity).getAccount();
    return {
      providers: account.providers,
      idempotencyKey: freshIdempotencyKey(),
      error: null,
    };
  } catch (error) {
    if (error instanceof Response) throw error;
    const { safeWebsiteError } = await import("../server/errors.server");
    return {
      providers: [],
      idempotencyKey: freshIdempotencyKey(),
      error: safeWebsiteError(error),
    };
  }
}

/**
 * POST /account/providers — start a fresh OAuth authorization for linking or
 * unlinking one provider. The completion happens only at the provider
 * callback after real provider authentication.
 */
export async function action({ request }: Route.ActionArgs): Promise<Response> {
  if (request.method !== "POST") {
    return new Response(null, { status: 405, headers: NO_STORE });
  }
  const { requireLogin } = await import("../server/auth-guard.server");
  await requireLogin(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");

  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    throw redirect("/login");
  }
  if (!actions.isSameOrigin(request, config.publicOrigin)) {
    return new Response(null, { status: 403, headers: NO_STORE });
  }
  const form = await actions.parseBoundedFormData(request);
  if (form === null || !actions.isIdempotencyKey(form["idempotencyKey"] ?? "")) {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  const intent = form["intent"];
  if (intent !== "link" && intent !== "unlink") {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  const requestedProvider = form["provider"];
  if (requestedProvider !== "google" && requestedProvider !== "discord") {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  const cookies = await import("../server/auth-cookies.server");
  const loginCookie = actions
    .parseCookieHeader(request.headers.get("cookie"))
    .get(cookies.LOGIN_COOKIE_NAME);
  if (loginCookie === undefined || actions.readLoginIdentity(request, config) === null) {
    throw redirect("/login");
  }
  const { startProviderAuthorization } = await import("../server/provider-start.server");
  try {
    return await startProviderAuthorization(
      config,
      requestedProvider,
      intent,
      "/account/providers",
      cookies.createLoginSessionBinding(loginCookie),
    );
  } catch {
    throw redirect("/login");
  }
}

export function headers() {
  return NO_STORE;
}

export default function AccountProvidersRoute() {
  const data = useLoaderData<typeof loader>();
  return (
    <main className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="providers-title">
        <h1 id="providers-title">Sign-in connections</h1>
        <p className="lede">
          Your Clash Lens account stays yours no matter which providers are connected.
          Linking needs a fresh sign-in with that provider; your last connection cannot be
          removed.
        </p>
      </section>

      {data.error ? <ErrorNotice error={data.error} /> : null}

      <section className="form-panel" aria-label="Connected sign-in providers">
        <ul className="player-link-list">
          {(["discord", "google"] as const).map((provider) => {
            const linked = data.providers.includes(provider);
            const isLast = linked && data.providers.length === 1;
            return (
              <li key={provider}>
                <span>{PROVIDER_LABELS[provider]}</span>
                {linked ? (
                  <form method="post" className="inline-form">
                    <input type="hidden" name="intent" value="unlink" />
                    <input type="hidden" name="provider" value={provider} />
                    <input
                      type="hidden"
                      name="idempotencyKey"
                      value={data.idempotencyKey}
                    />
                    <button
                      type="submit"
                      className="button button-secondary"
                      disabled={isLast}
                      title={
                        isLast
                          ? "Your last remaining sign-in connection cannot be removed."
                          : undefined
                      }
                    >
                      Unlink
                    </button>
                  </form>
                ) : (
                  <form method="post" className="inline-form">
                    <input type="hidden" name="intent" value="link" />
                    <input type="hidden" name="provider" value={provider} />
                    <input
                      type="hidden"
                      name="idempotencyKey"
                      value={data.idempotencyKey}
                    />
                    <button type="submit" className="button button-secondary">
                      Link
                    </button>
                  </form>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      <p className="hero-actions">
        <a className="button button-primary" href="/account">
          Back to your account
        </a>
      </p>
    </main>
  );
}
