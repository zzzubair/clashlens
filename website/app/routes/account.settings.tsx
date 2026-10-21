import { data, Link, redirect, useActionData, useLoaderData } from "react-router";

import type { BackHandle } from "../components/BackLink";
import { ErrorNotice } from "../components/ErrorNotice";
import { SavedLine } from "../components/SavedLine";
import type { VerifiedPlayer } from "../lib/account-contracts";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { normalizePlayerTag } from "../lib/player-tag";
import type { Route } from "./+types/account.settings";

import "../user-profile.css";

const NO_STORE = { "Cache-Control": "no-store" };
/** This page; every save comes back here with `?saved=`. */
const PAGE = "/account/settings";

export const handle: BackHandle = { back: { to: "/account", label: "Your profile" } };

export function meta() {
  return [{ title: "Settings · Clash Lens" }];
}

const PROVIDERS = ["google", "discord"] as const;
type Provider = (typeof PROVIDERS)[number];
const PROVIDER_LABELS: Record<Provider, string> = {
  google: "Google",
  discord: "Discord",
};

export interface SettingsLoaderData {
  username: string;
  displayName: string;
  providers: string[];
  players: VerifiedPlayer[];
  /** The default player: the one the Discord bot's /stats uses when none is named. */
  defaultPlayer: string | null;
  saved: Saved | null;
  idempotencyKey: string;
  error: WebsiteErrorResponse | null;
}

/** What the last save says at the top of the page. */
export interface Saved {
  text: string;
  failed: boolean;
}

export interface SettingsActionData {
  idempotencyKey: string;
  displayName: string;
  displayNameError: string | null;
  error: WebsiteErrorResponse | null;
}

/**
 * GET /account/settings — the signed-in Clasher's settings on one page: the
 * display name, their Clash players and which one is the default player, and
 * how they sign in. An identity with no account yet goes to setup.
 */
export async function loader({ request }: Route.LoaderArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  const saved = savedMessage(new URL(request.url).searchParams);
  try {
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient(identity);
    const [account, summary] = await Promise.all([
      client.getAccount(),
      client.getAccountSummary(),
    ]);
    return data<SettingsLoaderData>(
      {
        username: account.username,
        displayName: account.displayName,
        providers: account.providers,
        players: summary.verifiedPlayers,
        defaultPlayer: summary.defaultPlayer,
        saved,
        idempotencyKey: freshIdempotencyKey(),
        error: null,
      },
      { headers: NO_STORE },
    );
  } catch (cause) {
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(cause)) throw redirect("/account/setup");
    const { safeWebsiteError } = await import("../server/errors.server");
    return data<SettingsLoaderData>(
      {
        username: "",
        displayName: "",
        providers: [],
        players: [],
        defaultPlayer: null,
        saved: null,
        idempotencyKey: freshIdempotencyKey(),
        error: safeWebsiteError(cause),
      },
      { status: 503, headers: NO_STORE },
    );
  }
}

/** The line a finished save shows, from `?saved=`. Only fixed words and a
 * checked player tag ever reach the page. */
function savedMessage(params: URLSearchParams): Saved | null {
  const saved = params.get("saved");
  const done = (text: string) => ({ text, failed: false });
  if (saved === "name") return done("Display name saved.");
  if (saved === "default") return done("Default player saved.");
  if (saved === "default-failed") {
    return { text: "The default player could not be saved. Try again.", failed: true };
  }
  if (saved === "unlinked") {
    const tag = normalizePlayerTag(params.get("tag") ?? "");
    return done(tag === null ? "Player unlinked." : `${tag} unlinked.`);
  }
  for (const provider of PROVIDERS) {
    const label = PROVIDER_LABELS[provider];
    if (saved === `${provider}-connected`) return done(`${label} connected.`);
    if (saved === `${provider}-disconnected`) return done(`${label} disconnected.`);
  }
  return null;
}

/**
 * POST /account/settings — save the display name, choose the default player,
 * or start connecting or disconnecting Google or Discord. Connecting and
 * disconnecting finish at the provider's callback, which comes back here.
 */
export async function action({ request }: Route.ActionArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    throw redirect("/login");
  }
  if (!actions.isSameOrigin(request, config.publicOrigin)) {
    return new Response(null, { status: 403, headers: NO_STORE });
  }
  const form = await actions.parseBoundedFormData(request);
  const idempotencyKey = form?.["idempotencyKey"] ?? "";
  if (form === null || !actions.isIdempotencyKey(idempotencyKey)) {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  const { createPythonClient } = await import("../services/python.server");
  const client = createPythonClient(identity);

  if (form["intent"] === "display-name") {
    const displayName = form["displayName"] ?? "";
    const reply = (
      status: number,
      displayNameError: string | null,
      error: WebsiteErrorResponse | null = null,
    ) =>
      data<SettingsActionData>(
        {
          idempotencyKey: actions.freshIdempotencyKey(),
          displayName,
          displayNameError,
          error,
        },
        { status, headers: NO_STORE },
      );
    const checked = actions.validateAccountNames({ username: "", displayName });
    if (checked.fieldErrors.displayName) {
      return reply(400, checked.fieldErrors.displayName);
    }
    try {
      const account = await client.getAccount();
      await client.updateAccount(
        {
          username: account.username,
          displayName: checked.displayName as string,
          preferences: account.preferences,
        },
        idempotencyKey,
      );
    } catch (cause) {
      const outcome = actions.mapAccountNameError(cause);
      if (outcome.kind === "account_not_found") throw redirect("/account/setup");
      if (outcome.kind === "field") {
        return reply(outcome.status, outcome.fieldErrors.displayName ?? null);
      }
      if (outcome.kind === "general") return reply(422, null, outcome.generalError);
      throw redirect(PAGE);
    }
    return redirect(`${PAGE}?saved=name`, { headers: NO_STORE });
  }

  if (form["intent"] === "default-player") {
    try {
      await client.setDefaultPlayer(form["tag"] ?? "", idempotencyKey);
    } catch {
      return redirect(`${PAGE}?saved=default-failed`, { headers: NO_STORE });
    }
    return redirect(`${PAGE}?saved=default`, { headers: NO_STORE });
  }

  const intent = form["intent"];
  const provider = form["provider"];
  if (
    (intent !== "link" && intent !== "unlink") ||
    (provider !== "google" && provider !== "discord")
  ) {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  const cookies = await import("../server/auth-cookies.server");
  const loginCookie = actions
    .parseCookieHeader(request.headers.get("cookie"))
    .get(cookies.LOGIN_COOKIE_NAME);
  if (loginCookie === undefined) throw redirect("/login");
  const { startProviderAuthorization } = await import("../server/provider-start.server");
  try {
    return await startProviderAuthorization(
      config,
      provider,
      intent,
      PAGE,
      cookies.createLoginSessionBinding(loginCookie),
    );
  } catch {
    throw redirect("/login");
  }
}

export function headers() {
  return NO_STORE;
}

export default function AccountSettingsRoute() {
  const page = useLoaderData<typeof loader>();
  const answer = useActionData<SettingsActionData>();
  if (page.error) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
        <h1>Settings</h1>
        <aside className="notice notice-unavailable" role="alert">
          <strong>Your settings could not be loaded.</strong> <a href={PAGE}>Try again</a>
        </aside>
      </main>
    );
  }
  const key = answer?.idempotencyKey ?? page.idempotencyKey;
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      {page.saved && !answer ? (
        <SavedLine failed={page.saved.failed}>{page.saved.text}</SavedLine>
      ) : null}
      <section className="hero" aria-labelledby="settings-title">
        <h1 id="settings-title">Settings</h1>
        <p className="lede">Your name, your Clash players and how you sign in.</p>
      </section>
      {answer?.error ? <ErrorNotice error={answer.error} /> : null}

      <section className="settings-group" aria-labelledby="settings-profile">
        <h2 id="settings-profile">Profile</h2>
        <form method="post" className="settings-row settings-name" noValidate>
          <input type="hidden" name="intent" value="display-name" />
          <input type="hidden" name="idempotencyKey" value={key} />
          <div className="settings-label">
            <label htmlFor="settings-display-name">Display name</label>
            {answer?.displayNameError ? (
              <p id="settings-display-name-error" className="field-error" role="alert">
                {answer.displayNameError}
              </p>
            ) : (
              <p className="form-help">Shown on your profile and in crews.</p>
            )}
          </div>
          <div className="settings-control">
            <input
              id="settings-display-name"
              name="displayName"
              type="text"
              autoComplete="nickname"
              defaultValue={answer?.displayName ?? page.displayName}
              aria-invalid={answer?.displayNameError ? true : undefined}
              aria-describedby={
                answer?.displayNameError ? "settings-display-name-error" : undefined
              }
            />
            <button type="submit" className="button button-secondary">
              Save
            </button>
          </div>
        </form>
        <div className="settings-row">
          <div className="settings-label">
            <span>Username</span>
            <p className="form-help">Usernames can&apos;t be changed.</p>
          </div>
          <span className="settings-value">@{page.username}</span>
        </div>
      </section>

      <section className="settings-group" aria-labelledby="settings-players">
        <h2 id="settings-players">Clash players</h2>
        {page.players.length === 0 ? (
          <p className="form-help">You haven&apos;t linked a Clash player yet.</p>
        ) : (
          <>
            <p id="settings-default-help" className="form-help">
              The Discord bot&apos;s /stats uses your default player when you don&apos;t
              name one.
            </p>
            <ul className="settings-list">
              {page.players.map((player) => (
                <PlayerRow
                  key={player.tag}
                  player={player}
                  isDefault={page.defaultPlayer === player.tag}
                  idempotencyKey={key}
                />
              ))}
            </ul>
          </>
        )}
        <p>
          <Link className="button button-secondary" to="/account/verify-player">
            {page.players.length === 0 ? "Link your Clash player" : "Link another player"}
          </Link>
        </p>
      </section>

      <section className="settings-group" aria-labelledby="settings-sign-in">
        <h2 id="settings-sign-in">Sign-in</h2>
        <ul className="settings-list">
          {PROVIDERS.map((provider) => {
            const connected = page.providers.includes(provider);
            const only = connected && page.providers.length === 1;
            return (
              <li key={provider} className="settings-row">
                <div className="settings-label">
                  <span>{PROVIDER_LABELS[provider]}</span>
                  <p className="form-help" id={`settings-${provider}-state`}>
                    {only
                      ? "Connected. It's your only way to sign in, so connect another before you disconnect it."
                      : connected
                        ? "Connected."
                        : "Not connected."}
                  </p>
                </div>
                <form method="post" className="inline-form">
                  <input
                    type="hidden"
                    name="intent"
                    value={connected ? "unlink" : "link"}
                  />
                  <input type="hidden" name="provider" value={provider} />
                  <input type="hidden" name="idempotencyKey" value={key} />
                  <button
                    type="submit"
                    className="button button-secondary"
                    disabled={only}
                    aria-describedby={`settings-${provider}-state`}
                  >
                    {connected ? "Disconnect" : "Connect"}
                  </button>
                </form>
              </li>
            );
          })}
        </ul>
      </section>
    </main>
  );
}

/** One linked player: Default, or the button to make it so, and Unlink. */
function PlayerRow({
  player,
  isDefault,
  idempotencyKey,
}: {
  player: VerifiedPlayer;
  isDefault: boolean;
  idempotencyKey: string;
}) {
  const id = `settings-player-${player.tag.slice(1)}`;
  return (
    <li className="settings-row">
      <div className="settings-label" id={id}>
        <span>{player.name ?? player.tag}</span>
        <p className="form-help player-tag">{player.tag}</p>
      </div>
      <div className="settings-control">
        {isDefault ? (
          <span className="linked-player-default">
            <span aria-hidden="true">✓</span> Default
          </span>
        ) : (
          <form method="post" className="inline-form">
            <input type="hidden" name="intent" value="default-player" />
            <input type="hidden" name="tag" value={player.tag} />
            <input type="hidden" name="idempotencyKey" value={idempotencyKey} />
            <button
              type="submit"
              className="button button-secondary"
              aria-describedby={`${id} settings-default-help`}
            >
              Make default
            </button>
          </form>
        )}
        <Link
          className="button button-secondary"
          to={`/account/players/${encodeURIComponent(player.tag)}/unlink`}
          aria-describedby={id}
        >
          Unlink
        </Link>
      </div>
    </li>
  );
}
