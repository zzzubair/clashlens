import { ProviderCallbackError } from "../components/ProviderCallbackError";

import type { Route } from "./+types/auth.google";

/**
 * GET /auth/google/callback — validate the provider callback exactly once and
 * complete its intent: a browser login, or a link/unlink started from an
 * authenticated account. The transaction cookie is always cleared, provider
 * responses and tokens are never exposed, and every failure renders a safe
 * error.
 */
export async function loader({ request }: Route.LoaderArgs) {
  const { getWebsiteConfig } = await import("../server/config.server");
  const oidc = await import("../server/google-oidc.server");
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
    "google",
    async (params, transaction) => {
      const service = await oidc.createGoogleOidcService(config);
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
  return <ProviderCallbackError provider="Google" />;
}
