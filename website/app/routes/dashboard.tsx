import { useEffect, useState } from "react";
import {
  Link,
  data,
  redirect,
  useLoaderData,
  useNavigation,
  useSearchParams,
} from "react-router";
import type { ShouldRevalidateFunctionArgs } from "react-router";

import { Dashboard } from "../components/DashboardGrid";
import { DashboardIcon } from "../components/DashboardIcon";
import { ErrorNotice } from "../components/ErrorNotice";
import type { LinkedPlayerCard } from "../lib/account-contracts";
import type { WebsiteErrorResponse } from "../lib/contracts";
import type { DashboardSettings } from "../lib/dashboard";
import {
  CARDS,
  CARD_IDS,
  DEFAULT_LAYOUT,
  isDashboardSave,
  nextResetMs,
  parsePostedSettings,
  readSettings,
  serializeSettings,
  shownCards,
} from "../lib/dashboard";
import type { CardReads } from "../lib/dashboard-cards";
import { LOOKUP_MESSAGES } from "../lib/player-lookup-text";
import { normalizePlayerTag } from "../lib/player-tag";
import type { Route } from "./+types/dashboard";

import "../dashboard.css";
import "../dashboard-cards.css";
import "../dashboard-armies.css";

const NO_STORE = { "Cache-Control": "no-store" };
// Cards read within this go out with the page; slower ones stream in.
const CARD_WAIT_MS = 250;

export type DashboardLoaderData =
  | { kind: "signed-out"; loginAvailable: boolean }
  | { kind: "error"; error: WebsiteErrorResponse }
  | {
      kind: "signed-in";
      players: LinkedPlayerCard[];
      playersUnavailable: boolean;
      selectedTag: string | null;
      settings: DashboardSettings;
      /** Each card's read, sent on as it finishes. */
      cards: CardReads;
      /** The Reset that ends the Legend day the cards were read for. */
      dayEndsMs: number;
      idempotencyKey: string;
    };

export interface DashboardActionData {
  saved: boolean;
  error: string | null;
  idempotencyKey: string;
  /** The settings as saved, which the page shows without reading itself again. */
  settings: DashboardSettings | null;
}

export function meta() {
  return [{ title: "Dashboard · Clash Lens" }];
}

/**
 * GET /dashboard — the signed-in user's Legend dashboard. A visitor who is
 * not signed in sees a preview of what an account adds instead. While
 * CLASHLENS_DASHBOARD_ENABLED is off the page does not exist. The page
 * answers once the account and its linked players are read; each card's
 * data follows as it arrives. `?cards=goal,cutoffs` reads only those cards,
 * for a card whose settings were just saved.
 */
export async function loader({ request }: Route.LoaderArgs) {
  const { getWebsiteConfig, isDashboardEnabled } =
    await import("../server/config.server");
  if (!isDashboardEnabled()) throw data(null, { status: 404 });
  const { readLoginIdentity, freshIdempotencyKey, isAccountNotFoundError } =
    await import("../server/actions.server");
  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    return data<DashboardLoaderData>(
      { kind: "signed-out", loginAvailable: false },
      { headers: NO_STORE },
    );
  }
  const identity = config.loginEnabled
    ? await readLoginIdentity(request, config).catch(() => null)
    : null;
  if (identity === null) {
    return data<DashboardLoaderData>(
      { kind: "signed-out", loginAvailable: config.loginEnabled },
      { headers: NO_STORE },
    );
  }

  const { createPythonClient } = await import("../services/python.server");
  const client = createPythonClient(identity);
  let account;
  try {
    account = await client.getAccount();
  } catch (cause) {
    if (isAccountNotFoundError(cause))
      throw redirect("/account/setup?returnPath=/dashboard");
    const { safeWebsiteError } = await import("../server/errors.server");
    return data<DashboardLoaderData>(
      { kind: "error", error: safeWebsiteError(cause) },
      { status: 503, headers: NO_STORE },
    );
  }

  const dayEndsMs = nextResetMs(Date.now());
  let players: LinkedPlayerCard[] = [];
  let playersUnavailable = false;
  try {
    players = (await client.getPublicUser(account.username)).verifiedPlayers;
  } catch {
    playersUnavailable = true;
  }

  const settings = readSettings(account.preferences.dashboard);
  const query = new URL(request.url).searchParams;
  const requested = normalizePlayerTag(query.get("player") ?? "");
  const only = query.get("cards")?.split(",");
  const selected =
    players.find((player) => player.tag === requested) ??
    players.find((player) => player.state === "tracking") ??
    players[0] ??
    null;

  let cards: CardReads = {};
  if (selected?.state === "tracking") {
    const { readCards, settleWithin } = await import("../services/dashboard.server");
    cards = await settleWithin(
      readCards(
        only
          ? CARD_IDS.filter((card) => only.includes(card))
          : shownCards(settings.layout),
        { client, selected, settings },
        identity,
      ),
      CARD_WAIT_MS,
    );
  }

  return data<DashboardLoaderData>(
    {
      kind: "signed-in",
      players,
      playersUnavailable,
      selectedTag: selected?.tag ?? null,
      settings,
      cards,
      dayEndsMs,
      idempotencyKey: freshIdempotencyKey(),
    },
    { headers: NO_STORE },
  );
}

/**
 * POST /dashboard — save the dashboard settings into the account's
 * preferences box, keeping every other preference as it was.
 */
export async function action({ request }: Route.ActionArgs) {
  const { isDashboardEnabled } = await import("../server/config.server");
  if (!isDashboardEnabled()) throw data(null, { status: 404 });
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  const fail = (status: number, error: string) =>
    data<DashboardActionData>(
      {
        saved: false,
        error,
        idempotencyKey: actions.freshIdempotencyKey(),
        settings: null,
      },
      { status, headers: NO_STORE },
    );

  if (!actions.isSameOrigin(request, getWebsiteConfig().publicOrigin)) {
    return fail(403, "This page is out of date. Reload and try again.");
  }
  const form = await actions.parseBoundedFormData(request);
  const idempotencyKey = form?.["idempotencyKey"] ?? "";
  if (form === null || !actions.isIdempotencyKey(idempotencyKey)) {
    return fail(400, "Your dashboard could not be read.");
  }
  let posted: unknown;
  try {
    posted = JSON.parse(form["settings"] ?? "");
  } catch {
    return fail(400, "Your dashboard could not be read.");
  }
  const settings = parsePostedSettings(posted);
  if (settings === null) return fail(400, "Your dashboard could not be read.");

  const stored = serializeSettings(settings);
  try {
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient(identity);
    const account = await client.getAccount();
    const preferences = { ...account.preferences, dashboard: stored };
    if (new TextEncoder().encode(JSON.stringify(preferences)).byteLength > 4096) {
      return fail(413, "Your settings are too large to save.");
    }
    await client.updateAccount(
      { username: account.username, displayName: account.displayName, preferences },
      idempotencyKey,
    );
  } catch (cause) {
    if (actions.isAccountNotFoundError(cause)) throw redirect("/account/setup");
    return fail(503, "Your dashboard could not be saved. Try again.");
  }
  return data<DashboardActionData>(
    {
      saved: true,
      error: null,
      idempotencyKey: actions.freshIdempotencyKey(),
      // As the next load reads them back.
      settings: readSettings(stored),
    },
    { headers: NO_STORE },
  );
}

export function headers() {
  return NO_STORE;
}

/**
 * Reload the cards for another account, never for anything else. A save's
 * answer carries the settings as saved, and the page reads only the cards
 * they change.
 */
export function shouldRevalidate({
  currentUrl,
  nextUrl,
  formMethod,
  formAction,
  defaultShouldRevalidate,
}: ShouldRevalidateFunctionArgs) {
  if (isDashboardSave(formMethod, formAction)) return false;
  if (formMethod || currentUrl.pathname !== nextUrl.pathname) {
    return defaultShouldRevalidate;
  }
  return currentUrl.searchParams.get("player") !== nextUrl.searchParams.get("player");
}

/** The linked player a switch is loading, before its answer arrives. */
function switchingTo(
  navigation: ReturnType<typeof useNavigation>,
  players: LinkedPlayerCard[],
) {
  const next = navigation.location;
  if (navigation.state !== "loading" || next?.pathname !== "/dashboard") return null;
  const tag = normalizePlayerTag(new URLSearchParams(next.search).get("player") ?? "");
  return players.find((player) => player.tag === tag) ?? null;
}

function accountLine(player: LinkedPlayerCard): string {
  if (player.state === "not_in_legend") return "Not in Legend I";
  if (player.state !== "tracking") return "Not tracked yet";
  const parts = [
    player.trophies === null || player.seasonResetPending
      ? "–"
      : player.trophies.toLocaleString("en-US"),
    player.rank === null ? null : `#${player.rank.toLocaleString("en-US")}`,
  ].filter(Boolean);
  return parts.join(" · ");
}

function AccountTabs({
  players,
  selectedTag,
  dayOver,
}: {
  players: LinkedPlayerCard[];
  selectedTag: string | null;
  dayOver: boolean;
}) {
  const [searchParams] = useSearchParams();
  return (
    <nav className="dash-accounts" aria-label="Your Clash players">
      {players.map((player) => {
        const next = new URLSearchParams(searchParams);
        next.set("player", player.tag);
        const net =
          !dayOver && player.state === "tracking" ? (player.today?.net ?? null) : null;
        const name = player.name ?? player.tag;
        return (
          <Link
            key={player.tag}
            className="dash-account"
            to={`?${next.toString()}`}
            aria-current={player.tag === selectedTag ? "page" : undefined}
            preventScrollReset
            replace
          >
            <bdi className="dash-name" title={name}>
              {name}
            </bdi>
            <span className="dash-account-line">
              {accountLine(player)}
              {net !== null ? (
                <>
                  {" · "}
                  <b className={net > 0 ? "positive" : net < 0 ? "negative" : undefined}>
                    {net > 0 ? `+${net}` : net < 0 ? `−${Math.abs(net)}` : "0"}
                  </b>
                </>
              ) : null}
            </span>
          </Link>
        );
      })}
      <Link className="dash-account dash-account-add" to="/account/verify-player">
        <DashboardIcon name="plus" /> Link player
      </Link>
    </nav>
  );
}

function LinkPlayerPrompt() {
  return (
    <section className="dash-message" aria-labelledby="dash-link-title">
      <h2 id="dash-link-title">
        <DashboardIcon name="link" /> Link your Clash player
      </h2>
      <ol className="dash-link-steps">
        <li>In Clash of Clans, open Settings → More Settings.</li>
        <li>Copy your player tag and API token.</li>
        <li>Paste both on the next page.</li>
      </ol>
      <Link className="button button-primary" to="/account/verify-player">
        Link player
      </Link>
    </section>
  );
}

const TEASER_TILES = [
  { icon: "clock", label: "Legend clock in your time" },
  { icon: "flag", label: "Your goal, on pace or not" },
  { icon: "list", label: "Rivals just above and below you" },
  { icon: "grid", label: "Every Season day at a glance" },
  { icon: "bars", label: "Top lines and your gap" },
  { icon: "users", label: "How you compare with your groups" },
] as const;

function SignedOutPreview({ loginAvailable }: { loginAvailable: boolean }) {
  const returnPath = encodeURIComponent("/dashboard");
  return (
    <div className="dash-teaser">
      <div className="dash-teaser-behind" aria-hidden="true" inert>
        <div className="dash-grid">
          {DEFAULT_LAYOUT.filter((card) => CARDS[card].built).map((card) => (
            <section key={card} className={`dash-card dash-card-${CARDS[card].size}`}>
              <h2>{CARDS[card].title}</h2>
            </section>
          ))}
        </div>
      </div>
      <section className="dash-teaser-panel" aria-labelledby="dash-teaser-title">
        <h1 id="dash-teaser-title">Your Legend day, on one page</h1>
        <ul className="dash-teaser-tiles">
          {TEASER_TILES.map((tile) => (
            <li key={tile.label}>
              <DashboardIcon name={tile.icon} />
              {tile.label}
            </li>
          ))}
        </ul>
        {loginAvailable ? (
          <div className="button-row">
            <Link
              className="button button-primary"
              to={`/auth/google?returnPath=${returnPath}`}
              reloadDocument
            >
              Continue with Google
            </Link>
            <Link
              className="button button-secondary"
              to={`/auth/discord?returnPath=${returnPath}`}
              reloadDocument
            >
              Continue with Discord
            </Link>
          </div>
        ) : (
          <p className="dash-note" role="status">
            Sign-in is not available right now.
          </p>
        )}
        <p className="dash-note">
          <DashboardIcon name="info" /> Free. Player pages stay public.
        </p>
      </section>
    </div>
  );
}

export default function DashboardRoute() {
  const loaderData = useLoaderData<typeof loader>();
  const dayOver = useDayOver(
    loaderData.kind === "signed-in" ? loaderData.dayEndsMs : Number.POSITIVE_INFINITY,
  );
  if (loaderData.kind === "signed-out") {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell dash-page">
        <SignedOutPreview loginAvailable={loaderData.loginAvailable} />
      </main>
    );
  }
  if (loaderData.kind === "error") {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell dash-page">
        <h1>Dashboard</h1>
        <ErrorNotice error={loaderData.error} />
        <a className="button button-primary" href="/dashboard">
          Try again
        </a>
      </main>
    );
  }
  return <SignedInDashboard loaderData={loaderData} dayOver={dayOver} />;
}

function SignedInDashboard({
  loaderData,
  dayOver,
}: {
  loaderData: Extract<DashboardLoaderData, { kind: "signed-in" }>;
  dayOver: boolean;
}) {
  // A switch shows the new player's tab and empty cards at once, never the
  // last player's numbers under the new name.
  const switching = switchingTo(useNavigation(), loaderData.players);
  // Settings saved since this load; the next load reads them back.
  const [saved, setSaved] = useState<{
    from: DashboardSettings;
    settings: DashboardSettings;
  } | null>(null);
  const settings =
    saved?.from === loaderData.settings ? saved.settings : loaderData.settings;
  const selectedTag = switching?.tag ?? loaderData.selectedTag;
  const selected =
    switching ??
    loaderData.players.find((player) => player.tag === loaderData.selectedTag) ??
    null;
  return (
    <main id="main-content" tabIndex={-1} className="page-shell dash-page">
      <h1 className="sr-only">Dashboard</h1>
      {loaderData.players.length > 0 ? (
        <AccountTabs
          players={loaderData.players}
          selectedTag={selectedTag}
          dayOver={dayOver}
        />
      ) : null}
      {loaderData.playersUnavailable ? (
        <p className="notice notice-warning" role="status">
          Your linked players could not be loaded. Cards show again on the next load.
        </p>
      ) : null}
      {selected === null ? (
        loaderData.playersUnavailable ? null : (
          <LinkPlayerPrompt />
        )
      ) : selected.state === "not_in_legend" ? (
        <section className="dash-message" aria-live="polite">
          <h2>Unfortunately, the dashboard is Legend I only.</h2>
          <p>Support for lower leagues is coming in the future!</p>
        </section>
      ) : selected.state !== "tracking" ? (
        <section className="dash-message" aria-live="polite">
          <h2>{selected.name ?? selected.tag}</h2>
          <p>{LOOKUP_MESSAGES[selected.state]}</p>
        </section>
      ) : (
        <>
          {dayOver ? <NewDayNotice /> : null}
          <Dashboard
            key={selected.tag}
            tag={selected.tag}
            settings={settings}
            data={switching ? null : loaderData.cards}
            dayOver={dayOver}
            idempotencyKey={loaderData.idempotencyKey}
            onSaved={(next) => setSaved({ from: loaderData.settings, settings: next })}
          />
        </>
      )}
    </main>
  );
}

/** A page left open past the Reset says so instead of showing yesterday as today. */
function NewDayNotice() {
  const [searchParams] = useSearchParams();
  return (
    <p className="notice notice-warning dash-new-day" role="status">
      A new Legend day has started.{" "}
      <Link to={`?${searchParams.toString()}`} reloadDocument>
        Reload for today's numbers
      </Link>
    </p>
  );
}

/** Whether the Legend day the page was read for has ended, checked every 30 seconds. */
function useDayOver(dayEndsMs: number): boolean {
  const [, setTick] = useState(0);
  const over = Date.now() >= dayEndsMs;
  useEffect(() => {
    if (over || !Number.isFinite(dayEndsMs)) return;
    const timer = window.setInterval(() => setTick((tick) => tick + 1), 30_000);
    return () => window.clearInterval(timer);
  }, [over, dayEndsMs]);
  return over;
}
