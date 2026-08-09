import type { ReactNode } from "react";
import {
  Form,
  Link,
  isRouteErrorResponse,
  useLoaderData,
  type LoaderFunctionArgs,
  Links,
  Meta,
  Outlet,
  Scripts,
  ScrollRestoration,
} from "react-router";

import type { Route } from "./+types/root";
import "./app.css";
import "./theme.css";

export interface RootLoaderData {
  loggedIn: boolean;
}

/**
 * Root loader for the navigation bar. It parses only the signed login cookie
 * locally (never calling the private Python service for public pages) and
 * yields a boolean so no provider identity ever reaches the browser. Any
 * missing or broken login configuration falls back to logged-out.
 */
export async function loader({ request }: LoaderFunctionArgs): Promise<RootLoaderData> {
  let loggedIn = false;
  try {
    const { getWebsiteConfig } = await import("./server/config.server");
    const { readLoginIdentity } = await import("./server/actions.server");
    const config = getWebsiteConfig();
    if (config.loginEnabled) {
      loggedIn = readLoginIdentity(request, config) !== null;
    }
  } catch {
    loggedIn = false;
  }
  return { loggedIn };
}

export function Layout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <head>
        <meta charSet="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Clash Lens</title>
        <meta name="theme-color" content="#28170F" />
        <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
        <link rel="icon" href="/favicon-32x32.png" sizes="32x32" type="image/png" />
        <link rel="apple-touch-icon" href="/apple-touch-icon.png" />
        <link rel="manifest" href="/site.webmanifest" />
        <Meta />
        <Links />
      </head>
      <body>
        <a className="skip-link" href="#main-content">
          Skip to main content
        </a>
        <div id="main-content" tabIndex={-1}>
          {children}
        </div>
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
  return (
    <>
      <header className="site-header">
        <Link className="site-brand" to="/">
          Clash Lens
        </Link>
        <nav className="site-nav" aria-label="Account navigation">
          {data.loggedIn ? (
            <>
              <Link className="nav-link" to="/account">
                Account
              </Link>
              <Form method="post" action="/logout" className="nav-form">
                <button type="submit" className="nav-button">
                  Log out
                </button>
              </Form>
            </>
          ) : (
            <Link className="nav-link nav-link-primary" to="/login">
              Log in
            </Link>
          )}
        </nav>
      </header>
      <Outlet />
    </>
  );
}

export function ErrorBoundary({ error }: Route.ErrorBoundaryProps) {
  const isNotFound = isRouteErrorResponse(error) && error.status === 404;
  return (
    <main className="page-shell narrow-shell" role="alert">
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
  );
}

export function headers() {
  return { "Cache-Control": "no-store" };
}
