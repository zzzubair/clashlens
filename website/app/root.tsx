import { useEffect, useRef, type ReactNode } from "react";
import {
  Link,
  NavLink,
  isRouteErrorResponse,
  useLoaderData,
  useRouteLoaderData,
  useLocation,
  useMatches,
  type LoaderFunctionArgs,
  type ShouldRevalidateFunctionArgs,
  Links,
  Meta,
  Outlet,
  Scripts,
  ScrollRestoration,
} from "react-router";

import type { Route } from "./+types/root";
import { AccountMenu } from "./components/AccountMenu";
import {
  Analytics,
  AnalyticsChoiceBanner,
  AnalyticsChoicesButton,
  useErrorEvent,
} from "./components/Analytics";
import { BackLink } from "./components/BackLink";
import { NeedsAccount, type NeedsAccountData } from "./components/NeedsAccount";
import { HeaderSearch } from "./components/PlayerSearch";
import { SiteMenu } from "./components/SiteMenu";
import { ThemeToggle, themeInitialization } from "./components/ThemeToggle";
import { UpdatesNotice } from "./components/UpdatesNotice";
import type { AnalyticsSettings } from "./lib/analytics";
import type { UpdateStatus } from "./lib/contracts";
import { isDashboardSave } from "./lib/dashboard";
import { DISCORD_INVITE_URL } from "./lib/discord";
import {
  LOGGED_OUT,
  keepPageOnLostConnection,
  usePreloadInlineAnswerCode,
  useRememberShownPage,
} from "./lib/keep-page";
import "./app.css";
import "./theme.css";
import "./site-header.css";
import "./sign-in.css";
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
  crewsEnabled?: boolean;
  analytics?: AnalyticsSettings | null;
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
  const switches = import("./server/config.server")
    .then(({ isDashboardEnabled, isCrewsEnabled, loadAnalyticsSettings }) => ({
      dashboardEnabled: isDashboardEnabled(),
      crewsEnabled: isCrewsEnabled(),
      analytics: loadAnalyticsSettings(),
    }))
    .catch(() => ({ dashboardEnabled: false, crewsEnabled: false, analytics: null }));
  try {
    const { loadRootNavigation } = await import("./server/root-navigation.server");
    return {
      ...(await loadRootNavigation(request)),
      updateStatus: await updateStatus,
      ...(await switches),
    };
  } catch {
    return {
      ...LOGGED_OUT,
      updateStatus: await updateStatus,
      ...(await switches),
    };
  }
}

/** Restart a signed-in browser's 30 days on each visit; see login-renewal.server. */
export const middleware: Route.MiddlewareFunction[] = [
  async ({ request }, next) => {
    const { renewLoginCookie } = await import("./server/login-renewal.server");
    return renewLoginCookie(request, next);
  },
];

/** A dashboard save changes nothing the navigation bar shows. */
export function shouldRevalidate({
  formMethod,
  formAction,
  defaultShouldRevalidate,
}: ShouldRevalidateFunctionArgs) {
  return isDashboardSave(formMethod, formAction) ? false : defaultShouldRevalidate;
}

export const clientMiddleware: Route.ClientMiddlewareFunction[] = [
  keepPageOnLostConnection,
];

export function meta() {
  return [
    { title: "Clash Lens" },
    {
      name: "description",
      content: "Legend League results, rankings and armies for all Legend I Players.",
    },
  ];
}

/** A route handle for a page shown on its own, with no site around it and a
 * see-through background: the OBS overlay. */
export interface BareHandle {
  bare: true;
}

function useBarePage(): boolean {
  return useMatches().some((match) => (match.handle as BareHandle | undefined)?.bare);
}

export function Layout({ children }: { children: ReactNode }) {
  if (useBarePage()) {
    return (
      <html lang="en" className="obs-page">
        <head>
          <meta charSet="utf-8" />
          <meta name="viewport" content="width=device-width, initial-scale=1" />
          <Meta />
          <Links />
        </head>
        <body>
          {children}
          <Scripts />
        </body>
      </html>
    );
  }
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
            <a href="/privacy">Privacy</a>
            <AnalyticsChoicesButton>Analytics choices</AnalyticsChoicesButton>
          </nav>
        </footer>
        <AnalyticsChoiceBanner />
        <Analytics />
        <ScrollRestoration />
        <Scripts />
      </body>
    </html>
  );
}

export default function App() {
  const data = useLoaderData<typeof loader>();
  const location = useLocation();
  const bare = useBarePage();
  const previousPath = useRef(location.pathname);
  useRememberShownPage();
  usePreloadInlineAnswerCode();
  useEffect(() => {
    if (previousPath.current !== location.pathname && !location.hash) {
      document.getElementById("main-content")?.focus({ preventScroll: true });
    }
    previousPath.current = location.pathname;
  }, [location.pathname, location.hash]);
  if (bare) return <Outlet />;
  return (
    <>
      <SiteHeader data={data} />
      {data.updateStatus ? <UpdatesNotice status={data.updateStatus} /> : null}
      <BackLink />
      <Outlet />
    </>
  );
}

// The tabs, most useful first. Dashboard and Crews follow their switches.
function SiteTabs({ data }: { data: RootLoaderData | undefined }) {
  return (
    <>
      {data?.dashboardEnabled && (
        <NavLink to="/" end>
          Dashboard
        </NavLink>
      )}
      <NavLink to="/leaderboards/tracked?view=live&page=1">Rankings</NavLink>
      <NavLink to="/analytics/armies">Analytics</NavLink>
      {data?.crewsEnabled && <NavLink to="/crews">Crews</NavLink>}
      <NavLink to="/account/watchlists">Watchlists</NavLink>
      <NavLink to="/blog">Blog</NavLink>
    </>
  );
}

function DiscordLink() {
  return (
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
  );
}

// Sign-in pages already offer both ways in, so they don't come back to themselves.
const SIGN_IN_PAGES = new Set(["/login", "/signup", "/account/setup"]);

// The same top bar on every page, error pages included. Without the root
// data (the server failed before it), it shows the signed-out menu. Below
// 64rem the tabs, theme and Discord fold into one Menu, so the bar is one row.
function SiteHeader({ data }: { data: RootLoaderData | undefined }) {
  const location = useLocation();
  const signIn = SIGN_IN_PAGES.has(location.pathname)
    ? "/login"
    : `/login?returnPath=${encodeURIComponent(location.pathname)}`;
  return (
    <header className="site-header">
      <Link className="site-brand" to="/" aria-label="Clash Lens">
        <img
          className="site-brand-logo"
          src="/images/clashlens-wordmark.svg"
          alt=""
          width="78"
          height="48"
        />
      </Link>
      <nav className="primary-nav" aria-label="Main navigation">
        <SiteTabs data={data} />
      </nav>
      <div className="site-nav">
        {/* The search page keeps its own large search. */}
        {location.pathname !== "/search" ? (
          <HeaderSearch key={location.pathname} />
        ) : null}
        <span className="site-nav-wide">
          <ThemeToggle />
        </span>
        {data?.loggedIn ? (
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
          <Link className="nav-link nav-link-primary" to={signIn}>
            Sign in
          </Link>
        )}
        <span className="site-nav-wide">
          <DiscordLink />
        </span>
        <SiteMenu>
          <nav className="site-menu-tabs" aria-label="Main navigation">
            <SiteTabs data={data} />
          </nav>
          <div className="site-menu-extras">
            <ThemeToggle />
            <DiscordLink />
          </div>
        </SiteMenu>
      </div>
    </header>
  );
}

export function ErrorBoundary({ error }: Route.ErrorBoundaryProps) {
  const isNotFound = isRouteErrorResponse(error) && error.status === 404;
  const location = useLocation();
  const data = useRouteLoaderData<typeof loader>("root");
  const needsAccount = needsAccountOf(error);
  useErrorEvent(needsAccount ? null : isNotFound ? "not_found" : "error");
  if (needsAccount) {
    return (
      <>
        <SiteHeader data={data} />
        <NeedsAccount pathname={location.pathname} returnPath={needsAccount.returnPath} />
      </>
    );
  }
  return (
    <>
      <SiteHeader data={data} />
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
            ? "This page doesn't exist."
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
            Go to Clash Lens
          </a>
        </p>
      </main>
    </>
  );
}

/** A page that needs an account, opened while signed out; see requireLogin. */
function needsAccountOf(error: unknown): NeedsAccountData | null {
  if (!isRouteErrorResponse(error) || error.status !== 401) return null;
  const value = error.data as Partial<NeedsAccountData> | null;
  return value?.needsAccount === true
    ? {
        needsAccount: true,
        returnPath: typeof value.returnPath === "string" ? value.returnPath : null,
      }
    : null;
}

export function headers() {
  return { "Cache-Control": "no-store" };
}
