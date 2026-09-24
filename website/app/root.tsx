import { useEffect, useRef, type ReactNode } from "react";
import {
  Form,
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
import { ThemeToggle, themeInitialization } from "./components/ThemeToggle";
import "./app.css";
import "./theme.css";
import "./explore.css";
import "./appearance.css";
import "./details.css";

export interface RootLoaderData {
  loggedIn: boolean;
  accountLabel: string | null;
  accountUsername: string | null;
  logoutIdempotencyKey: string | null;
}

/**
 * Root loader for the navigation bar. Logged-out public requests never call
 * the private Python service. Signed-in requests resolve only the public
 * account label; no provider identity reaches the browser. Any missing or
 * broken login configuration falls back to logged-out.
 */
export async function loader({ request }: LoaderFunctionArgs): Promise<RootLoaderData> {
  try {
    const { loadRootNavigation } = await import("./server/root-navigation.server");
    return await loadRootNavigation(request);
  } catch {
    return {
      loggedIn: false,
      accountLabel: null,
      accountUsername: null,
      logoutIdempotencyKey: null,
    };
  }
}

export function Layout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <meta charSet="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Clash Lens</title>
        <meta name="theme-color" content="#2456ff" suppressHydrationWarning />
        <meta name="color-scheme" content="light dark" />
        <script dangerouslySetInnerHTML={{ __html: themeInitialization }} />
        <link rel="icon" href="data:," />
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
            Clash Lens is an unofficial fan project and is not affiliated with or endorsed
            by Supercell. See the{" "}
            <a href="https://supercell.com/en/fan-content-policy/">
              Supercell Fan Content Policy
            </a>
            .
          </p>
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
          Clash Lens
        </Link>
        <nav className="primary-nav" aria-label="Main navigation">
          <NavLink to="/" end>
            Home
          </NavLink>
          <NavLink to="/leaderboards/tracked">Rankings</NavLink>
          <NavLink to="/analytics/armies">Armies</NavLink>
          <NavLink to="/account/saved-players">Saved players</NavLink>
          <NavLink to="/account/groups">Groups</NavLink>
        </nav>
        <nav className="site-nav" aria-label="Account and appearance">
          <ThemeToggle />
          {data.loggedIn ? (
            <>
              <Link
                className="nav-link nav-account"
                to={
                  data.accountUsername
                    ? `/users/${encodeURIComponent(data.accountUsername)}`
                    : "/account"
                }
              >
                <svg
                  width="20"
                  height="20"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  aria-hidden="true"
                  focusable="false"
                >
                  <circle cx="12" cy="8" r="4" />
                  <path d="M4 21v-2a8 8 0 0 1 16 0v2" />
                </svg>
                <span className="nav-account-name">{data.accountLabel ?? "Account"}</span>
              </Link>
              <Form method="post" action="/logout" className="nav-form">
                <input
                  type="hidden"
                  name="idempotencyKey"
                  value={data.logoutIdempotencyKey ?? ""}
                />
                <button type="submit" className="nav-button">
                  Log out
                </button>
              </Form>
            </>
          ) : (
            <Link className="nav-link nav-link-primary" to="/login">
              Account
            </Link>
          )}
        </nav>
      </header>
      {location.pathname !== "/" ? <div className="page-back">{backLink}</div> : null}
      <Outlet />
    </>
  );
}

export function ErrorBoundary({ error }: Route.ErrorBoundaryProps) {
  const isNotFound = isRouteErrorResponse(error) && error.status === 404;
  return (
    <>
      <header className="site-header">
        <Link className="site-brand" to="/" aria-label="Clash Lens home">
          Clash Lens
        </Link>
        <div className="site-nav">
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
        <a className="button button-primary" href="/">
          Return home
        </a>
      </main>
    </>
  );
}

export function headers() {
  return { "Cache-Control": "no-store" };
}
