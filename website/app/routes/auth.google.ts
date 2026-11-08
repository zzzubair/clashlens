import type { Route } from "./+types/auth.google";

export async function loader({ request }: Route.LoaderArgs): Promise<Response> {
  const { startProviderLogin } = await import("../server/provider-start.server");
  return startProviderLogin(request, "google");
}
