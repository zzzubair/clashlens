import { Link, useLoaderData } from "react-router";

import type { SignInPageData } from "../server/sign-in-page.server";
import type { Route } from "./+types/login";
import { ProviderMark } from "../components/ProviderMark";
import { SignInReason } from "../components/SignInReason";
import { TeaserBoard } from "../components/DashboardTeaser";
import "../front-page.css";

export function meta() {
  return [{ title: "Sign in · Clash Lens" }];
}

/**
 * GET /login — sign in to an existing account with Google or Discord. An
 * existing valid browser login skips straight to the validated return path
 * (or /account). `?none=google` (or discord) comes back from a sign-in that
 * no account uses, and asks whether to make one instead of making it.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<SignInPageData> {
  const { loadSignInPage } = await import("../server/sign-in-page.server");
  return loadSignInPage(request, "login");
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

const PROVIDER_NAMES = { google: "Google", discord: "Discord" } as const;

export default function LoginRoute() {
  const data = useLoaderData<typeof loader>();
  const back = encodeURIComponent(data.returnPath);
  const none = data.noAccount;
  if (none !== null) {
    const other = none === "google" ? "discord" : "google";
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
        <h1>No account found</h1>
        <section className="sign-in-panel" aria-label="Create an account">
          <p>No Clash Lens account uses this {PROVIDER_NAMES[none]} sign-in.</p>
          <p className="button-row">
            <Link
              className="button button-primary"
              to={`/auth/${none}?returnPath=${back}&signup=1`}
            >
              Create account with {PROVIDER_NAMES[none]}
            </Link>
            <Link
              className="button button-secondary"
              to={`/auth/${other}?returnPath=${back}`}
            >
              Try {PROVIDER_NAMES[other]} instead
            </Link>
          </p>
        </section>
      </main>
    );
  }
  return (
    <main
      id="main-content"
      tabIndex={-1}
      className="page-shell narrow-shell sign-in-page"
    >
      <div className="sign-in-main">
        {/* The brand's small mark, so the page reads as Clash Lens's own. */}
        <img
          className="sign-in-mark"
          src="/images/mark-cl-block.svg"
          alt=""
          width="56"
          height="56"
        />
        <h1>Sign in</h1>
        <SignInReason returnPath={data.returnPath} />
        <p className="lede">
          {data.crewName ? `To join ${data.crewName}, use` : "Use"} the account you signed
          up with.
        </p>
        {data.loginAvailable ? (
          <>
            <section className="sign-in-panel" aria-label="Sign in">
              <p className="button-row">
                <Link
                  className="button button-secondary sign-in-google"
                  to={`/auth/google?returnPath=${back}`}
                >
                  <ProviderMark provider="google" />
                  Sign in with Google
                </Link>
                <Link
                  className="button button-secondary"
                  to={`/auth/discord?returnPath=${back}`}
                >
                  <ProviderMark provider="discord" />
                  Sign in with Discord
                </Link>
              </p>
              <p className="sign-in-new">
                New here? <Link to={`/signup?returnPath=${back}`}>Create account</Link>
              </p>
            </section>
            <p className="sign-in-privacy">
              Clash Lens never asks for your email. <Link to="/privacy">Privacy</Link>
            </p>
          </>
        ) : (
          <div className="empty-state" role="alert">
            <h2>Sign-in is not available</h2>
            <p>
              Sign-in is not configured right now. Public player data remains available.
            </p>
          </div>
        )}
      </div>
      {/* A quiet picture of what signing in opens, where there is room. */}
      {data.loginAvailable ? <TeaserBoard link={false} /> : null}
    </main>
  );
}
