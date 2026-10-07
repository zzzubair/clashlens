import { Link, data, redirect, useLoaderData, useSearchParams } from "react-router";
import type { ShouldRevalidateFunctionArgs } from "react-router";
import type { ReactNode } from "react";

import { DashboardGrid, PlaceholderCard } from "../components/DashboardGrid";
import { DashboardIcon } from "../components/DashboardIcon";
import { ErrorNotice } from "../components/ErrorNotice";
import type { LinkedPlayerCard } from "../lib/account-contracts";
import type { PlayerPage, WebsiteErrorResponse } from "../lib/contracts";
import type { DashboardLayout, DashboardTab, PlayerDay } from "../lib/dashboard";
import {
  DASHBOARD_TABS,
  defaultTab,
  isDashboardTab,
  parsePostedLayout,
  readSavedLayout,
  serializeLayout,
} from "../lib/dashboard";
import { normalizePlayerTag } from "../lib/player-tag";
import type { Route } from "./+types/dashboard";

import "../dashboard.css";

const NO_STORE = { "Cache-Control": "no-store" };
/** Player pages read per load: the switcher's player plus pinned Legend clocks. */
const MAX_PLAYER_DAYS = 4;

export type DashboardLoaderData =
  | { kind: "signed-out"; loginAvailable: boolean }
  | { kind: "error"; error: WebsiteErrorResponse }
  | {
      kind: "signed-in";
      players: LinkedPlayerCard[];
      playersUnavailable: boolean;
      selectedTag: string | null;
      layout: DashboardLayout;
      days: Record<string, PlayerDay>;
      idempotencyKey: string;
    };

export interface DashboardActionData {
  saved: boolean;
  error: string | null;
  idempotencyKey: string;
}

export function meta() {
  return [{ title: "Dashboard · Clash Lens" }];
}

/**
 * GET /dashboard — the signed-in user's Legend dashboard. A visitor who is
 * not signed in sees a preview of what an account adds instead.
 */
export async function loader({ request }: Route.LoaderArgs) {
  const { getWebsiteConfig } = await import("../server/config.server");
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
  let account;
  try {
    account = await createPythonClient(identity).getAccount();
  } catch (cause) {
    if (isAccountNotFoundError(cause))
      throw redirect("/account/setup?returnPath=/dashboard");
    const { safeWebsiteError } = await import("../server/errors.server");
    return data<DashboardLoaderData>(
      { kind: "error", error: safeWebsiteError(cause) },
      { status: 503, headers: NO_STORE },
    );
  }

  const publicClient = createPythonClient();
  let players: LinkedPlayerCard[] = [];
  let playersUnavailable = false;
  try {
    players = (await publicClient.getPublicUser(account.username)).verifiedPlayers;
  } catch {
    playersUnavailable = true;
  }

  const layout = readSavedLayout(account.preferences.dashboard);
  const requested = normalizePlayerTag(
    new URL(request.url).searchParams.get("player") ?? "",
  );
  const selected =
    players.find((player) => player.tag === requested) ??
    players.find((player) => player.state === "tracking") ??
    players[0] ??
    null;

  const clockTags = new Set<string>();
  if (selected?.state === "tracking") clockTags.add(selected.tag);
  for (const card of layout.tabs.today) {
    const pinned = players.find((player) => player.tag === card.player);
    if (card.card === "clock" && pinned?.state === "tracking") clockTags.add(pinned.tag);
  }
  const days: Record<string, PlayerDay> = {};
  await Promise.all(
    [...clockTags].slice(0, MAX_PLAYER_DAYS).map(async (tag) => {
      try {
        days[tag] = playerDay(await publicClient.getPlayer(tag));
      } catch {
        // The clock still shows the time and live trophies without battles.
      }
    }),
  );

  return data<DashboardLoaderData>(
    {
      kind: "signed-in",
      players,
      playersUnavailable,
      selectedTag: selected?.tag ?? null,
      layout,
      days,
      idempotencyKey: freshIdempotencyKey(),
    },
    { headers: NO_STORE },
  );
}

function playerDay(page: PlayerPage): PlayerDay {
  const day = page.currentDay;
  const battles = day
    ? [
        ...day.offenseEvents.map((event) => ({ event, kind: "attack" as const })),
        ...day.defenseEvents.map((event) => ({ event, kind: "defense" as const })),
      ]
        .map(({ event, kind }) => ({
          at: Date.parse(event.battleTimestamp),
          kind,
          stars: event.stars,
          trophyChange: event.trophyChange,
        }))
        .filter((battle) => Number.isFinite(battle.at))
    : [];
  return {
    dayNumber: page.season?.currentDayNumber ?? null,
    dayCount: page.season?.dayCount ?? null,
    battles,
  };
}

/**
 * POST /dashboard — save the layout into the account's preferences box,
 * keeping every other preference as it was.
 */
export async function action({ request }: Route.ActionArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  const fail = (status: number, error: string) =>
    data<DashboardActionData>(
      { saved: false, error, idempotencyKey: actions.freshIdempotencyKey() },
      { status, headers: NO_STORE },
    );

  if (!actions.isSameOrigin(request, getWebsiteConfig().publicOrigin)) {
    return fail(403, "This page is out of date. Reload and try again.");
  }
  const form = await actions.parseBoundedFormData(request);
  const idempotencyKey = form?.["idempotencyKey"] ?? "";
  if (form === null || !actions.isIdempotencyKey(idempotencyKey)) {
    return fail(400, "That layout could not be read.");
  }
  let posted: unknown;
  try {
    posted = JSON.parse(form["layout"] ?? "");
  } catch {
    return fail(400, "That layout could not be read.");
  }
  const layout = parsePostedLayout(posted);
  if (layout === null) return fail(400, "That layout could not be read.");

  try {
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient(identity);
    const account = await client.getAccount();
    const preferences = { ...account.preferences, dashboard: serializeLayout(layout) };
    if (new TextEncoder().encode(JSON.stringify(preferences)).byteLength > 4096) {
      return fail(413, "Too many cards to save. Remove a few and try again.");
    }
    await client.updateAccount(
      { username: account.username, displayName: account.displayName, preferences },
      idempotencyKey,
    );
  } catch (cause) {
    if (actions.isAccountNotFoundError(cause)) throw redirect("/account/setup");
    return fail(503, "Your layout could not be saved. Try again.");
  }
  return data<DashboardActionData>(
    { saved: true, error: null, idempotencyKey: actions.freshIdempotencyKey() },
    { headers: NO_STORE },
  );
}

export function headers() {
  return NO_STORE;
}

/** Switching tabs only changes what is shown, so it skips the server. */
export function shouldRevalidate({
  currentUrl,
  nextUrl,
  formMethod,
  defaultShouldRevalidate,
}: ShouldRevalidateFunctionArgs) {
  if (formMethod || currentUrl.pathname !== nextUrl.pathname) {
    return defaultShouldRevalidate;
  }
  return currentUrl.searchParams.get("player") !== nextUrl.searchParams.get("player");
}

function useTab(): DashboardTab {
  const [searchParams] = useSearchParams();
  const tab = searchParams.get("tab");
  return isDashboardTab(tab) ? tab : "today";
}

function tabLink(searchParams: URLSearchParams, changes: Record<string, string>): string {
  const next = new URLSearchParams(searchParams);
  for (const [key, value] of Object.entries(changes)) next.set(key, value);
  return `?${next.toString()}`;
}

function DashboardTabs({ tab, meta }: { tab: DashboardTab; meta?: ReactNode }) {
  const [searchParams] = useSearchParams();
  return (
    <div className="dash-tabs">
      <nav className="dash-tab-list" aria-label="Dashboard views">
        {DASHBOARD_TABS.map((item) => (
          <Link
            key={item.id}
            className="dash-tab"
            to={tabLink(searchParams, { tab: item.id })}
            aria-current={item.id === tab ? "page" : undefined}
            preventScrollReset
            replace
          >
            <DashboardIcon
              name={
                item.id === "today" ? "clock" : item.id === "season" ? "cal" : "users"
              }
            />
            {item.label}
            {item.id === "crew" ? (
              <span className="dash-tag" title="Placeholder word, still being decided">
                word TBD
              </span>
            ) : null}
          </Link>
        ))}
      </nav>
      {meta}
    </div>
  );
}

function AccountSwitcher({
  players,
  selectedTag,
}: {
  players: LinkedPlayerCard[];
  selectedTag: string | null;
}) {
  const [searchParams] = useSearchParams();
  return (
    <nav className="dash-accounts" aria-label="Your Clash players">
      {players.map((player) => (
        <Link
          key={player.tag}
          className="dash-account"
          to={tabLink(searchParams, { player: player.tag })}
          aria-current={player.tag === selectedTag ? "true" : undefined}
          preventScrollReset
          replace
        >
          <b>{player.name ?? player.tag}</b>
          <small>
            {player.state === "tracking" ? (
              <>
                {player.trophies?.toLocaleString("en-US") ?? "–"}
                {player.rank !== null ? ` · #${player.rank.toLocaleString("en-US")}` : ""}
              </>
            ) : (
              <>
                <DashboardIcon name="shieldOff" /> Not in Legends
              </>
            )}
          </small>
        </Link>
      ))}
      <Link className="dash-account dash-account-add" to="/account/verify-player">
        <DashboardIcon name="plus" /> Link player
      </Link>
    </nav>
  );
}

const TEASER_TILES = [
  { icon: "clock", label: "Legend clock in your time" },
  { icon: "chart", label: "Live rank + rank at Reset" },
  { icon: "target", label: "How the bases you hit hold" },
  { icon: "shield", label: "Shield or not, in one look" },
  { icon: "flag", label: "Your goal, on pace or not" },
  { icon: "users", label: "Race your crew" },
] as const;

function SignedOutPreview({ loginAvailable }: { loginAvailable: boolean }) {
  const returnPath = encodeURIComponent("/dashboard");
  return (
    <div className="dash-teaser">
      <div className="dash-teaser-behind" aria-hidden="true" inert>
        <DashboardTabs tab="today" />
        <div className="dash-grid">
          {defaultTab("today").map((card, index) => (
            <PlaceholderCard key={index} placed={card} />
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
  const tab = useTab();
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
  const selected =
    loaderData.players.find((player) => player.tag === loaderData.selectedTag) ?? null;
  const day = selected ? (loaderData.days[selected.tag] ?? null) : null;
  return (
    <main id="main-content" tabIndex={-1} className="page-shell dash-page">
      <h1 className="sr-only">Dashboard</h1>
      {loaderData.players.length > 0 ? (
        <AccountSwitcher
          players={loaderData.players}
          selectedTag={loaderData.selectedTag}
        />
      ) : null}
      {loaderData.playersUnavailable ? (
        <p className="notice notice-warning" role="status">
          Your linked players could not be loaded. Cards show again on the next load.
        </p>
      ) : null}
      <DashboardGrid
        tab={tab}
        layout={loaderData.layout}
        players={loaderData.players}
        selected={selected}
        days={loaderData.days}
        idempotencyKey={loaderData.idempotencyKey}
        renderTabs={(meta) => <DashboardTabs tab={tab} meta={meta} />}
        dayLabel={day?.dayNumber ? `Day ${day.dayNumber} of ${day.dayCount ?? 28}` : null}
        noPlayers={!loaderData.playersUnavailable && loaderData.players.length === 0}
      />
    </main>
  );
}
