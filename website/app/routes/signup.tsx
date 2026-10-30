import { Link, useLoaderData } from "react-router";

import type { SignInPageData } from "../server/sign-in-page.server";
import type { Route } from "./+types/signup";
import { ProviderMark } from "../components/ProviderMark";
import { TeaserBoard } from "../components/DashboardTeaser";
import "../front-page.css";

export function meta() {
  return [{ title: "Create account · Clash Lens" }];
}

/**
 * GET /signup — make a new account with Google or Discord, then choose a
 * username. It remembers where the Clasher was going, and someone already
 * signed in to an account goes straight there.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<SignInPageData> {
  const { loadSignInPage } = await import("../server/sign-in-page.server");
  return loadSignInPage(request, "signup");
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

export default function SignupRoute() {
  const data = useLoaderData<typeof loader>();
  const back = encodeURIComponent(data.returnPath);
  return (
    <main
      id="main-content"
      tabIndex={-1}
      className="page-shell narrow-shell sign-in-page"
    >
      <div className="sign-in-main">
        <h1>Create your Clash Lens account</h1>
        <p className="lede">
          {data.crewName
            ? `To join ${data.crewName}.`
            : "Free. Player pages stay public without one."}
        </p>
        {data.loginAvailable ? (
          <>
            <section className="form-panel sign-in-panel" aria-label="Create account">
              <p className="button-row">
                <Link
                  className="button button-secondary sign-in-google"
                  to={`/auth/google?returnPath=${back}&signup=1`}
                >
                  <ProviderMark provider="google" />
                  Sign up with Google
                </Link>
                <Link
                  className="button button-secondary"
                  to={`/auth/discord?returnPath=${back}&signup=1`}
                >
                  <ProviderMark provider="discord" />
                  Sign up with Discord
                </Link>
              </p>
              <p className="sign-in-new">
                Already have an account?{" "}
                <Link to={`/login?returnPath=${back}`}>Sign in</Link>
              </p>
            </section>
            <p className="sign-in-privacy">
              Clash Lens never asks for your email. <Link to="/privacy">Privacy</Link>
            </p>
          </>
        ) : (
          <div className="empty-state" role="alert">
            <h2>Sign-up is not available</h2>
            <p>
              Sign-in is not configured right now. Public player data remains available.
            </p>
          </div>
        )}
      </div>
      {/* A quiet picture of what an account opens, where there is room. */}
      {data.loginAvailable ? <TeaserBoard link={false} /> : null}
    </main>
  );
}
