import { useEffect, useRef, useState } from "react";
import {
  Link,
  data,
  redirect,
  useFetcher,
  useLoaderData,
  useNavigation,
  useSearchParams,
} from "react-router";
import type { ShouldRevalidateFunctionArgs } from "react-router";

import { Dashboard } from "../components/DashboardGrid";
import { DashboardIcon } from "../components/DashboardIcon";
import { ErrorNotice } from "../components/ErrorNotice";
import {
  FRONT_PAGE_PLAYERS,
  FrontPage,
  type FrontPageData,
} from "../components/FrontPage";
import type { LinkedPlayerCard } from "../lib/account-contracts";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { publicPageMeta } from "../lib/blog";
import type { DashboardSettings } from "../lib/dashboard";
import {
  CARD_IDS,
  isDashboardSave,
  nextResetMs,
  parsePostedSettings,
  readSettings,
  serializeSettings,
  shownCards,
} from "../lib/dashboard";
import type { CardReads } from "../lib/dashboard-cards";
import { LOOKUP_MESSAGES } from "../lib/player-lookup-text";
import { canonicalPlayerPath, normalizePlayerTag } from "../lib/player-tag";
import type { Route } from "./+types/dashboard";

import "../dashboard.css";
import "../dashboard-cards.css";
import "../dashboard-armies.css";
import "../front-page.css";

const NO_STORE = { "Cache-Control": "no-store" };
// Cards read within this go out with the page; slower ones stream in.
const CARD_WAIT_MS = 250;

export type DashboardLoaderData =
  | ({ kind: "signed-out" } & FrontPageData)
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

export function meta({ loaderData, matches }: Route.MetaArgs) {
  return loaderData?.kind === "signed-out"
    ? publicPageMeta(matches, {
        path: "/",
        title: "Clash of Clans Legend League tracker",
        description:
          "Legend League results, rankings and armies for all Legend I Players.",
      })
    : [{ title: "Dashboard · Clash Lens" }];
}

/**
 * GET / — the signed-in user's Legend dashboard. Anyone else, and everyone
 * while CLASHLENS_DASHBOARD_ENABLED is off, gets the front page: the
 * Dashboard preview (when on), search, Rankings, Analytics and the top
 * players. An old `/?q=` search link opens the search page. The page
 * answers once the account and its linked players are read; each card's
 * data follows as it arrives. `?cards=goal,cutoffs` reads only those cards,
 * for a card whose settings were just saved; `&board=wait` waits a few
 * seconds for the shared board, for Legend day or Bases read before it.
 */
export async function loader({ request }: Route.LoaderArgs) {
  const query = new URL(request.url).searchParams;
  if (query.has("q")) {
    throw redirect(`/search?${new URLSearchParams({ q: query.get("q") ?? "" })}`);
  }
  const { getWebsiteConfig, isDashboardEnabled } =
    await import("../server/config.server");
  const { readLoginIdentity, freshIdempotencyKey, isAccountNotFoundError } =
    await import("../server/actions.server");
  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    return frontPage(false);
  }
  const identity =
    config.loginEnabled && isDashboardEnabled()
      ? await readLoginIdentity(request, config).catch(() => null)
      : null;
  if (identity === null) return frontPage(config.loginEnabled);

  const { createPythonClient } = await import("../services/python.server");
  const client = createPythonClient(identity);
  let account;
  try {
    account = await client.getAccount();
  } catch (cause) {
    // Signed in with Google or Discord but no account made yet.
    if (isAccountNotFoundError(cause)) return frontPage(config.loginEnabled);
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
        { client, selected, settings, waitForBoard: query.get("board") === "wait" },
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

/** The front page, with the top players when the private API answers. */
async function frontPage(loginAvailable: boolean) {
  const { isDashboardEnabled } = await import("../server/config.server");
  let leaderboard = null;
  let error: WebsiteErrorResponse | null = null;
  try {
    const { createPythonClient } = await import("../services/python.server");
    leaderboard = await createPythonClient().getTrackedLeaderboard(
      FRONT_PAGE_PLAYERS,
      "live",
    );
  } catch (cause) {
    const { safeWebsiteError } = await import("../server/errors.server");
    error = safeWebsiteError(cause);
  }
  return data<DashboardLoaderData>(
    {
      kind: "signed-out",
      loginAvailable,
      dashboardEnabled: isDashboardEnabled(),
      leaderboard,
      error,
    },
    { headers: NO_STORE },
  );
}

/**
 * POST / — save the dashboard settings into the account's
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
 * Reload the cards for another account, whatever else happened, such as a
 * save landing during the switch, failed or not. A save's answer carries
 * the settings as saved, and the page reads only the cards they change.
 * The same address again, such as the front page's reread at a Season's
 * end, reads again.
 */
export function shouldRevalidate({
  currentUrl,
  nextUrl,
  formMethod,
  formAction,
  defaultShouldRevalidate,
}: ShouldRevalidateFunctionArgs) {
  if (currentUrl.searchParams.get("player") !== nextUrl.searchParams.get("player"))
    return true;
  if (isDashboardSave(formMethod, formAction)) return false;
  if (
    formMethod ||
    currentUrl.pathname !== nextUrl.pathname ||
    currentUrl.href === nextUrl.href
  ) {
    return defaultShouldRevalidate;
  }
  return false;
}

/** The linked player a switch is loading, before its answer arrives. */
function switchingTo(
  navigation: ReturnType<typeof useNavigation>,
  players: LinkedPlayerCard[],
) {
  const next = navigation.location;
  if (navigation.state !== "loading" || next?.pathname !== "/") return null;
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
        // The tab switches the dashboard; Page beside it opens the player page.
        return (
          <span key={player.tag} className="dash-account-tab">
            <Link
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
                    <b
                      className={net > 0 ? "positive" : net < 0 ? "negative" : undefined}
                    >
                      {net > 0 ? `+${net}` : net < 0 ? `−${Math.abs(net)}` : "0"}
                    </b>{" "}
                    today
                  </>
                ) : null}
              </span>
            </Link>
            <Link
              className="dash-account-page"
              to={canonicalPlayerPath(player.tag)}
              aria-label={`${name} player page`}
            >
              <DashboardIcon name="open" />
              <span aria-hidden="true">Page</span>
            </Link>
          </span>
        );
      })}
      <Link className="dash-account dash-account-add" to="/account/verify-player">
        <DashboardIcon name="plus" />{" "}
        <span className="dash-account-add-text">Link player</span>
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

export default function DashboardRoute() {
  const loaderData = useLoaderData<typeof loader>();
  const dayOver = useDayOver(
    loaderData.kind === "signed-in" ? loaderData.dayEndsMs : Number.POSITIVE_INFINITY,
  );
  if (loaderData.kind === "signed-out") return <FrontPage data={loaderData} />;
  if (loaderData.kind === "error") {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell dash-page">
        <h1>Dashboard</h1>
        <ErrorNotice error={loaderData.error} />
        <a className="button button-primary" href="/">
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
  // Saves live above each player's cards, so one still lands after a switch.
  const fetcher = useFetcher<DashboardActionData>();
  // Settings saved since this load; the next load reads them back.
  const [saved, setSaved] = useState<{
    from: DashboardSettings;
    settings: DashboardSettings;
  } | null>(null);
  // Each save's answer shows as soon as it is back, over the load it lands on.
  const answer = fetcher.data;
  const applied = useRef(answer);
  useEffect(() => {
    if (!answer?.settings || applied.current === answer) return;
    applied.current = answer;
    setSaved({ from: loaderData.settings, settings: answer.settings });
  }, [answer, loaderData.settings]);
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
            fetcher={fetcher}
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
