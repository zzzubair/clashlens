import { ProviderCallbackError } from "../components/ProviderCallbackError";

import type { Route } from "./+types/auth.discord.callback";

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
  return <ProviderCallbackError provider="Discord" />;
}
