import { useEffect, useRef, type ReactNode } from "react";
import {
  Link,
  NavLink,
  isRouteErrorResponse,
  useLoaderData,
  useLocation,
  useNavigate,
  type LoaderFunctionArgs,
  Links,
  Meta,
  Outlet,
  Scripts,
  ScrollRestoration,
} from "react-router";

import type { Route } from "./+types/root";
import { AccountMenu } from "./components/AccountMenu";
import { HeaderSearch } from "./components/PlayerSearch";
import { ThemeToggle, themeInitialization } from "./components/ThemeToggle";
import { UpdatesNotice } from "./components/UpdatesNotice";
import type { UpdateStatus } from "./lib/contracts";
import { DISCORD_INVITE_URL } from "./lib/discord";
import {
  LOGGED_OUT,
  keepPageOnLostConnection,
  usePreloadInlineAnswerCode,
  useRememberShownPage,
} from "./lib/keep-page";
import "./app.css";
import "./theme.css";
import "./explore.css";
import "./appearance.css";
import "./header-search.css";
import "./brand.css";
import "./account-menu.css";

export interface RootLoaderData {
  loggedIn: boolean;
  accountLabel: string | null;
  accountUsername: string | null;
  logoutIdempotencyKey: string | null;
  updateStatus: UpdateStatus | null;
  dashboardEnabled?: boolean;
}

/**
 * Root loader for the navigation bar and the delayed-updates notice. The
 * notice reads one shared, briefly cached status from the private Python
 * service. Signed-in requests also resolve only the public account label; no
 * provider identity reaches the browser. Any missing or broken login
 * configuration falls back to logged-out.
 */
export async function loader({ request }: LoaderFunctionArgs): Promise<RootLoaderData> {
  const updateStatus = import("./server/update-status.server")
    .then(({ loadUpdateStatus }) => loadUpdateStatus())
    .catch(() => null);
  const dashboardEnabled = import("./server/config.server")
    .then(({ isDashboardEnabled }) => isDashboardEnabled())
    .catch(() => false);
  try {
    const { loadRootNavigation } = await import("./server/root-navigation.server");
    return {
      ...(await loadRootNavigation(request)),
      updateStatus: await updateStatus,
      dashboardEnabled: await dashboardEnabled,
    };
  } catch {
    return {
      ...LOGGED_OUT,
      updateStatus: await updateStatus,
      dashboardEnabled: await dashboardEnabled,
    };
  }
}

export const clientMiddleware: Route.ClientMiddlewareFunction[] = [
  keepPageOnLostConnection,
];

export function meta() {
  return [{ title: "Clash Lens" }];
}

export function Layout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <meta charSet="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="theme-color" content="#1d1426" suppressHydrationWarning />
        <meta name="color-scheme" content="light dark" />
        <script dangerouslySetInnerHTML={{ __html: themeInitialization }} />
        <link rel="icon" href="/favicon.ico" sizes="32x32" />
        <link rel="apple-touch-icon" href="/apple-touch-icon.png" sizes="180x180" />
        <link rel="manifest" href="/site.webmanifest" />
        <Meta />
        <Links />
      </head>
      <body>
        <a className="skip-link" href="#main-content">
          Skip to main content
        </a>
        {children}
        <footer className="page-footer">
          <p>
            This material is unofficial and is not endorsed by Supercell. For more
            information see Supercell's Fan Content Policy:{" "}
            <a href="https://www.supercell.com/fan-content-policy">
              www.supercell.com/fan-content-policy
            </a>
            .
          </p>
          <nav aria-label="Site information">
            <a href="/blog">Blog</a>
            <a href="/about">About</a>
          </nav>
        </footer>
        <ScrollRestoration />
        <Scripts />
      </body>
    </html>
  );
}

export default function App() {
  const data = useLoaderData<typeof loader>();
  const location = useLocation();
  const navigate = useNavigate();
  const previousPath = useRef(location.pathname);
  useRememberShownPage();
  usePreloadInlineAnswerCode();
  useEffect(() => {
    if (previousPath.current !== location.pathname && !location.hash) {
      document.getElementById("main-content")?.focus({ preventScroll: true });
    }
    previousPath.current = location.pathname;
  }, [location.pathname, location.hash]);
  const backLink = (
    <Link
      className="header-back"
      to="/"
      replace
      onClick={(event) => {
        const hasSameOriginReferrer =
          window.history.length > 1 &&
          document.referrer !== "" &&
          new URL(document.referrer).origin === window.location.origin;
        if ((window.history.state?.idx ?? 0) > 0 || hasSameOriginReferrer) {
          event.preventDefault();
          void navigate(-1);
        }
      }}
    >
      <span aria-hidden="true">←</span> Back
    </Link>
  );
  return (
    <>
      <header className="site-header">
        <Link className="site-brand" to="/" aria-label="Clash Lens home">
          <img
            className="site-brand-logo"
            src="/images/clashlens-wordmark.svg"
            alt=""
            width="78"
            height="48"
          />
        </Link>
        <nav className="primary-nav" aria-label="Main navigation">
          <NavLink to="/" end>
            Home
          </NavLink>
          {data.dashboardEnabled && <NavLink to="/dashboard">Dashboard</NavLink>}
          <NavLink to="/leaderboards/tracked?view=live&page=1">Rankings</NavLink>
          <NavLink to="/analytics/armies">Armies</NavLink>
          <NavLink to="/blog">Blog</NavLink>
          <NavLink to="/account/saved-players">Saved players</NavLink>
          <NavLink to="/account/groups">Groups</NavLink>
          <NavLink to="/about">About</NavLink>
        </nav>
        <nav className="site-nav" aria-label="Account and appearance">
          {/* Home keeps its own large search. */}
          {location.pathname !== "/" ? <HeaderSearch key={location.pathname} /> : null}
          <ThemeToggle />
          {data.loggedIn ? (
            <AccountMenu
              label={data.accountLabel ?? "Account"}
              accountPath={
                data.accountUsername
                  ? `/users/${encodeURIComponent(data.accountUsername)}`
                  : "/account"
              }
              logoutIdempotencyKey={data.logoutIdempotencyKey ?? ""}
            />
          ) : (
            <Link className="nav-link nav-link-primary" to="/login">
              Account
            </Link>
          )}
          <a
            className="nav-link nav-discord"
            href={DISCORD_INVITE_URL}
            target="_blank"
            rel="noopener noreferrer"
          >
            <span>
              <span className="nav-discord-join">Join </span>Discord
            </span>
            <span className="sr-only">, opens in a new tab</span>
          </a>
        </nav>
      </header>
      {data.updateStatus ? <UpdatesNotice status={data.updateStatus} /> : null}
      {location.pathname !== "/" ? <div className="page-back">{backLink}</div> : null}
      <Outlet />
    </>
  );
}

export function ErrorBoundary({ error }: Route.ErrorBoundaryProps) {
  const isNotFound = isRouteErrorResponse(error) && error.status === 404;
  const location = useLocation();
  return (
    <>
      <header className="site-header">
        <Link className="site-brand" to="/" aria-label="Clash Lens home">
          <img
            className="site-brand-logo"
            src="/images/clashlens-wordmark.svg"
            alt=""
            width="78"
            height="48"
          />
        </Link>
        <div className="site-nav">
          <HeaderSearch key={location.pathname} />
          <ThemeToggle />
        </div>
      </header>
      <main
        id="main-content"
        tabIndex={-1}
        className="page-shell narrow-shell"
        role="alert"
      >
        <p className="eyebrow">Clash Lens</p>
        <h1>{isNotFound ? "Page not found" : "The page could not be loaded"}</h1>
        <p>
          {isNotFound
            ? "This route does not exist."
            : "The website returned a safe error. Saved data is not changed by this page error."}
        </p>
        <p className="hero-actions">
          {isNotFound ? null : (
            <a
              className="button button-primary"
              href={`${location.pathname}${location.search}`}
            >
              Try again
            </a>
          )}
          <a
            className={`button ${isNotFound ? "button-primary" : "button-secondary"}`}
            href="/"
          >
            Return home
          </a>
        </p>
      </main>
    </>
  );
}

export function headers() {
  return { "Cache-Control": "no-store" };
}
