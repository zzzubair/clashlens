import { Link } from "react-router";

/** What a page answers, with status 401, to a visitor who is not signed in. */
export interface NeedsAccountData {
  needsAccount: true;
  /** Where Create account and Sign in come back to; null when unsafe. */
  returnPath: string | null;
}

/** "[Create account] Already have one? [Sign in]", both coming back to `returnPath`. */
export function AccountButtons({ returnPath }: { returnPath: string | null }) {
  const back = returnPath === null ? "" : `?returnPath=${encodeURIComponent(returnPath)}`;
  return (
    <p className="account-buttons">
      <Link className="button button-primary" to={`/signup${back}`}>
        Create account
      </Link>
      {/* The question wraps with the button it introduces, never apart from it. */}
      <span className="account-buttons-pair">
        <span>Already have one?</span>
        <Link className="button button-secondary" to={`/login${back}`}>
          Sign in
        </Link>
      </span>
    </p>
  );
}

// Each page that needs an account, by address, with its title and why.
const PAGES: ReadonlyArray<readonly [string, string, string]> = [
  [
    "/crews",
    "Crews",
    "Invite-only groups with shared Legend boards. Sign in to make or join one.",
  ],
  [
    "/account/watchlists",
    "Watchlists",
    "Private lists of players, side by side. Sign in to keep them.",
  ],
  [
    "/account/discord-alerts",
    "Discord alerts",
    "A Discord message when someone attacks your player. Sign in to set it up.",
  ],
  [
    "/account/verify-player",
    "Link your Clash player",
    "See your own Legend days. Sign in first.",
  ],
  [
    "/account/obs-overlay",
    "Stream overlay",
    "Your Legend day, live in OBS. Sign in to make one.",
  ],
  [
    "/account/setup",
    "Create your Clash Lens account",
    "Start with Google or Discord, then choose your username.",
  ],
];

const ACCOUNT_PAGE = [
  "",
  "Your account",
  "Your profile, linked players and settings. Sign in to see them.",
] as const;

/** The title and reason for the page at `pathname`. */
export function needsAccountText(pathname: string): { title: string; reason: string } {
  const [, title, reason] =
    PAGES.find(([path]) => pathname === path || pathname.startsWith(`${path}/`)) ??
    ACCOUNT_PAGE;
  return { title, reason };
}

/**
 * What a page that needs an account shows a visitor who is not signed in,
 * in place of itself: what the page is for, and Create account and Sign in
 * as two separate buttons that both come back here.
 */
export function NeedsAccount({
  pathname,
  returnPath,
}: {
  pathname: string;
  returnPath: string | null;
}) {
  const { title, reason } = needsAccountText(pathname);
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      <h1>{title}</h1>
      <section className="form-panel needs-account" aria-label="Account needed">
        <p>{reason}</p>
        <AccountButtons returnPath={returnPath} />
      </section>
    </main>
  );
}
