import { Link, redirect, useLoaderData } from "react-router";

import type { Route } from "./+types/login";

export interface LoginLoaderData {
  loginAvailable: boolean;
  returnPath: string;
}

/**
 * GET /login — show the Google sign-in action. An existing valid browser
 * login skips straight to the validated return path (or /account). Login
 * that is disabled or unconfigured keeps the page usable with a safe notice.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<LoginLoaderData> {
  const { getWebsiteConfig } = await import("../server/config.server");
  const { readLoginIdentity } = await import("../server/actions.server");
  const { safeReturnPath, DEFAULT_RETURN_PATH } =
    await import("../server/return-path.server");

  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    return { loginAvailable: false, returnPath: DEFAULT_RETURN_PATH };
  }

  const rawReturnPath = new URL(request.url).searchParams.get("returnPath");
  const returnPath =
    safeReturnPath(rawReturnPath, config.publicOrigin) ?? DEFAULT_RETURN_PATH;
  if (config.loginEnabled && readLoginIdentity(request, config) !== null) {
    throw redirect(returnPath);
  }
  return { loginAvailable: config.loginEnabled, returnPath };
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

export default function LoginRoute() {
  const data = useLoaderData<typeof loader>();
  const googleUrl = `/auth/google?returnPath=${encodeURIComponent(data.returnPath)}`;
  return (
    <main className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="login-title">
        <h1 id="login-title">Sign in</h1>
        <p className="lede">
          Sign in to save public player tags, verify your own players, and keep private
          groups. Public player data stays free and available without an account.
        </p>
      </section>

      {data.loginAvailable ? (
        <section className="login-panel" aria-label="Google sign-in">
          <p className="section-note">
            Sign in with Google. Clash Lens never requests or stores your Google email
            address.
          </p>
          <Link className="button button-primary" to={googleUrl}>
            Continue with Google
          </Link>
        </section>
      ) : (
        <div className="empty-state" role="alert">
          <h2>Sign-in is not available</h2>
          <p>
            Sign-in is not configured right now. Public player data remains available.
          </p>
        </div>
      )}

      <p className="back-link">
        <Link to="/">← Back to home</Link>
      </p>
    </main>
  );
}
