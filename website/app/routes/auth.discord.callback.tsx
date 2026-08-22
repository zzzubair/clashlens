import { Link, useLoaderData } from "react-router";

import type { Route } from "./+types/auth.discord.callback";

/**
 * GET /auth/discord/callback — validate the Discord callback exactly once and
 * complete its intent: a browser login, or a link/unlink started from an
 * authenticated account. The transaction cookie is always cleared, Discord
 * responses and access tokens never leave this request, and every failure
 * renders a safe error.
 */
export async function loader({ request }: Route.LoaderArgs) {
  const { getWebsiteConfig } = await import("../server/config.server");
  const discord = await import("../server/discord-oauth.server");
  const { completeProviderCallback, callbackErrorResponse, redirectOutcomeResponse } =
    await import("../server/provider-callback.server");

  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    return callbackErrorResponse({
      kind: "error",
      status: 503,
      code: "unavailable",
      message: "Sign-in could not be completed. Try again.",
    });
  }

  const outcome = await completeProviderCallback(
    request,
    "discord",
    async (params, transaction) => {
      const service = await discord.createDiscordOAuthService(config);
      return service.validateCallback(params, transaction);
    },
  );
  if (outcome.kind === "redirect") return redirectOutcomeResponse(outcome);
  return callbackErrorResponse(outcome);
}

const NO_STORE = { "Cache-Control": "no-store" };

export function headers() {
  return NO_STORE;
}

export default function CallbackErrorRoute() {
  const data = useLoaderData<typeof loader>() as {
    error: { code: string; message: string } | null;
  };
  return (
    <main className="page-shell narrow-shell" role="alert">
      <p className="eyebrow">Clash Lens</p>
      <h1>Sign-in could not be completed</h1>
      <p>{data.error?.message ?? "Discord sign-in could not be completed. Try again."}</p>
      <Link className="button button-primary" to="/login">
        Try signing in again
      </Link>
    </main>
  );
}
