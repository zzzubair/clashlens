import { Link } from "react-router";

export interface NeedsAccountData {
  needsAccount: true;
  returnPath: string | null;
}

export function AccountButtons({ returnPath }: { returnPath: string | null }) {
  const back = returnPath === null ? "" : `?returnPath=${encodeURIComponent(returnPath)}`;
  return (
    <p className="account-buttons">
      <Link className="button button-primary" to={`/signup${back}`}>
        Create account
      </Link>
      <span className="account-buttons-pair">
        <span>Already have one?</span>
        <Link className="button button-secondary" to={`/login${back}`}>
          Sign in
        </Link>
      </span>
    </p>
  );
}

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

export function needsAccountText(pathname: string): { title: string; reason: string } {
  const [, title, reason] =
    PAGES.find(([path]) => pathname === path || pathname.startsWith(`${path}/`)) ??
    ACCOUNT_PAGE;
  return { title, reason };
}

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
      <header className="page-hero">
        <h1>{title}</h1>
      </header>
      <section className="form-panel needs-account" aria-label="Account needed">
        <p>{reason}</p>
        <AccountButtons returnPath={returnPath} />
      </section>
    </main>
  );
}
