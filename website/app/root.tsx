import type { ReactNode } from "react";
import {
  isRouteErrorResponse,
  Links,
  Meta,
  Outlet,
  Scripts,
  ScrollRestoration,
} from "react-router";

import type { Route } from "./+types/root";
import "./app.css";

export function Layout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <head>
        <meta charSet="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Clash Lens | Legend I data</title>
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
  return <Outlet />;
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
