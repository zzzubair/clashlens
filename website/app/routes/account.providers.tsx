import { redirect, useLoaderData } from "react-router";

import type { BackHandle } from "../components/BackLink";
import type { VerifiedPlayer } from "../lib/account-contracts";
import type { WebsiteErrorResponse } from "../lib/contracts";
import {
  DISCORD_INVITE_URL,
  type AttackAlerts,
  type UndeliveredReason,
} from "../lib/discord";
import type { Route } from "./+types/account.providers";

const NO_STORE = { "Cache-Control": "no-store" };

export const handle: BackHandle = { back: { to: "/account", label: "Your account" } };

const PROVIDER_LABELS: Record<string, string> = {
  discord: "Discord",
  google: "Google",
};

export interface ProvidersLoaderData {
  providers: string[];
  /** The account's verified players, for the Discord bot's default player. */
  players: VerifiedPlayer[];
  defaultPlayer: string | null;
  /** The outcome of the last default-player save, shown once. */
  saved: "saved" | "failed" | null;
  /** Each verified player's Discord attack alert switch; null when unreadable. */
  alerts: AttackAlerts | null;
  /** The outcome of the last attack alert switch, shown once. */
  alertSaved: "saved" | "failed" | null;
  idempotencyKey: string;
  error: WebsiteErrorResponse | null;
}

/**
 * GET /account/providers — show the sign-in connections on the account and
 * the actions to link or unlink each provider. The provider identities sign
 * the private Python calls; subjects never appear in page data.
 */
export async function loader({
  request,
}: Route.LoaderArgs): Promise<ProvidersLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  const search = new URL(request.url).searchParams;
  const outcome = search.get("default");
  const saved = outcome === "saved" || outcome === "failed" ? outcome : null;
  const alertOutcome = search.get("alerts");
  const alertSaved =
    alertOutcome === "saved" || alertOutcome === "failed" ? alertOutcome : null;
  try {
    const { createPythonClient } = await import("../services/python.server");
    const { getAttackAlerts } = await import("../services/discord-alerts.server");
    const client = createPythonClient(identity);
    const [account, summary, alerts] = await Promise.all([
      client.getAccount(),
      client.getAccountSummary(),
      // The rest of the page still works when the alert settings can't be read.
      getAttackAlerts(identity).catch(() => null),
    ]);
    return {
      providers: account.providers,
      players: summary.verifiedPlayers,
      defaultPlayer: summary.defaultPlayer,
      saved,
      alerts,
      alertSaved,
      idempotencyKey: freshIdempotencyKey(),
      error: null,
    };
  } catch (error) {
    if (error instanceof Response) throw error;
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(error)) throw redirect("/account/setup");
    const { safeWebsiteError } = await import("../server/errors.server");
    return {
      providers: [],
      players: [],
      defaultPlayer: null,
      saved: null,
      alerts: null,
      alertSaved: null,
      idempotencyKey: freshIdempotencyKey(),
      error: safeWebsiteError(error),
    };
  }
}

/**
 * POST /account/providers — choose the Discord bot's default player, turn one
 * player's Discord attack alerts on or off, or start
 * a fresh OAuth authorization for linking or unlinking one provider. Linking
 * completes only at the provider callback after real provider authentication.
 */
export async function action({ request }: Route.ActionArgs): Promise<Response> {
  if (request.method !== "POST") {
    return new Response(null, { status: 405, headers: NO_STORE });
  }
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
  if (form === null || !actions.isIdempotencyKey(form["idempotencyKey"] ?? "")) {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  const intent = form["intent"];
  if (intent === "default-player") {
    const tag = form["tag"] ?? "";
    try {
      const { createPythonClient } = await import("../services/python.server");
      await createPythonClient(identity).setDefaultPlayer(
        tag,
        form["idempotencyKey"] ?? "",
      );
    } catch {
      return redirect("/account/providers?default=failed", { headers: NO_STORE });
    }
    return redirect("/account/providers?default=saved", { headers: NO_STORE });
  }
  if (intent === "attack-alert") {
    const on = form["on"];
    if (on !== "on" && on !== "off") {
      return new Response(null, { status: 400, headers: NO_STORE });
    }
    try {
      const { setAttackAlert } = await import("../services/discord-alerts.server");
      await setAttackAlert(
        identity,
        form["tag"] ?? "",
        on === "on",
        form["idempotencyKey"] ?? "",
      );
    } catch {
      return redirect("/account/providers?alerts=failed", { headers: NO_STORE });
    }
    return redirect("/account/providers?alerts=saved", { headers: NO_STORE });
  }
  if (intent !== "link" && intent !== "unlink") {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  const requestedProvider = form["provider"];
  if (requestedProvider !== "google" && requestedProvider !== "discord") {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  const cookies = await import("../server/auth-cookies.server");
  const loginCookie = actions
    .parseCookieHeader(request.headers.get("cookie"))
    .get(cookies.LOGIN_COOKIE_NAME);
  if (
    loginCookie === undefined ||
    (await actions.readLoginIdentity(request, config)) === null
  ) {
    throw redirect("/login");
  }
  const { startProviderAuthorization } = await import("../server/provider-start.server");
  try {
    return await startProviderAuthorization(
      config,
      requestedProvider,
      intent,
      "/account/providers",
      cookies.createLoginSessionBinding(loginCookie),
    );
  } catch {
    throw redirect("/login");
  }
}

export function headers() {
  return NO_STORE;
}

export default function AccountProvidersRoute() {
  const data = useLoaderData<typeof loader>();
  if (data.error) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
        <h1>Sign-in connections</h1>
        <aside className="notice notice-unavailable" role="alert">
          <strong>Sign-in connections could not be loaded.</strong>{" "}
          <a href="/account/providers">Try again</a>
        </aside>
      </main>
    );
  }
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="providers-title">
        <h1 id="providers-title">Sign-in connections</h1>
        <p className="lede">
          Connect Google, Discord, or both to sign in to the same Clash Lens account.
          Linking asks you to sign in with that service.
        </p>
      </section>

      <section className="form-panel stack-form" aria-label="Connected sign-in providers">
        <p id="provider-requirement" className="form-help">
          <strong>Keep at least one sign-in connection.</strong> To unlink your only
          connection, link the other first.
        </p>
        <ul className="player-link-list">
          {(["discord", "google"] as const).map((provider) => {
            const linked = data.providers.includes(provider);
            const isLast = linked && data.providers.length === 1;
            return (
              <li key={provider}>
                <span>{PROVIDER_LABELS[provider]}</span>
                {linked ? (
                  <form method="post" className="inline-form">
                    <input type="hidden" name="intent" value="unlink" />
                    <input type="hidden" name="provider" value={provider} />
                    <input
                      type="hidden"
                      name="idempotencyKey"
                      value={data.idempotencyKey}
                    />
                    <button
                      type="submit"
                      className="button button-secondary"
                      disabled={isLast}
                      aria-describedby={isLast ? "provider-requirement" : undefined}
                    >
                      Unlink
                    </button>
                  </form>
                ) : (
                  <form method="post" className="inline-form">
                    <input type="hidden" name="intent" value="link" />
                    <input type="hidden" name="provider" value={provider} />
                    <input
                      type="hidden"
                      name="idempotencyKey"
                      value={data.idempotencyKey}
                    />
                    <button type="submit" className="button button-secondary">
                      Link
                    </button>
                  </form>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      <DiscordBotDefault
        players={data.players}
        defaultPlayer={data.defaultPlayer}
        saved={data.saved}
        idempotencyKey={data.idempotencyKey}
      />

      <AttackAlertsSection
        connected={data.providers.includes("discord")}
        alerts={data.alerts}
        saved={data.alertSaved}
        idempotencyKey={data.idempotencyKey}
      />
    </main>
  );
}

/** The player the Clash Lens Discord bot uses when a command doesn't name one. */
function DiscordBotDefault({
  players,
  defaultPlayer,
  saved,
  idempotencyKey,
}: {
  players: VerifiedPlayer[];
  defaultPlayer: string | null;
  saved: "saved" | "failed" | null;
  idempotencyKey: string;
}) {
  const label = (player: VerifiedPlayer) => `${player.name ?? "Unnamed"} ${player.tag}`;
  return (
    <section className="form-panel stack-form" aria-labelledby="bot-default-title">
      <h2 id="bot-default-title">Discord bot</h2>
      {saved === "saved" ? (
        <p className="notice" role="status">
          Default player saved.
        </p>
      ) : saved === "failed" ? (
        <p className="notice notice-unavailable" role="alert">
          The default player could not be saved. Try again.
        </p>
      ) : null}
      {players.length === 0 ? (
        <p className="form-help">
          <a href="/account/verify-player">Link a player</a> to see your Legend days in
          the Clash Lens Discord bot.
        </p>
      ) : players.length === 1 ? (
        <p className="form-help">
          The bot uses {label(players[0])}, your only linked player.
        </p>
      ) : (
        <form method="post" className="stack-form">
          <input type="hidden" name="intent" value="default-player" />
          <input type="hidden" name="idempotencyKey" value={idempotencyKey} />
          <label htmlFor="bot-default-player">Default player</label>
          <p id="bot-default-help" className="form-help">
            The bot uses this player when a command doesn't name one.
          </p>
          <select
            id="bot-default-player"
            name="tag"
            defaultValue={defaultPlayer ?? ""}
            aria-describedby="bot-default-help"
            required
          >
            <option value="" disabled>
              Choose a player
            </option>
            {players.map((player) => (
              <option key={player.tag} value={player.tag}>
                {label(player)}
              </option>
            ))}
          </select>
          <button type="submit" className="button button-primary">
            Save
          </button>
        </form>
      )}
    </section>
  );
}

/** What a Clasher can do about the newest alert Discord did not deliver. */
const UNDELIVERED_HELP: Record<UndeliveredReason, string> = {
  no_shared_server:
    "Discord only lets the bot message you while you share a server with it. Join the Clash Lens Discord server, then alerts start with the next attack.",
  dms_closed:
    "Your Discord privacy settings block messages from the bot. In the server you share with it, allow direct messages from server members, and check you haven't blocked the bot.",
  unknown_user:
    "Discord couldn't find the connected Discord account. Unlink Discord above and link it again.",
  other: "Discord refused the message. Alerts try again with the next attack.",
  unreachable:
    "Clash Lens couldn't reach Discord in time. Alerts try again with the next attack.",
};

/** Per-player switches for a Discord DM each time the player is attacked. */
function AttackAlertsSection({
  connected,
  alerts,
  saved,
  idempotencyKey,
}: {
  connected: boolean;
  alerts: AttackAlerts | null;
  saved: "saved" | "failed" | null;
  idempotencyKey: string;
}) {
  const label = (player: { tag: string; name: string | null }) =>
    `${player.name ?? "Unnamed"} ${player.tag}`;
  const missed = alerts?.undelivered;
  const missedPlayer = alerts?.players.find((player) => player.tag === missed?.tag);
  const example = alerts?.players[0];
  return (
    <section className="form-panel stack-form" aria-labelledby="alerts-title">
      <h2 id="alerts-title">Alerts</h2>
      {saved === "saved" ? (
        <p className="notice" role="status">
          Attack alerts saved.
        </p>
      ) : saved === "failed" ? (
        <p className="notice notice-unavailable" role="alert">
          Attack alerts could not be saved. Try again.
        </p>
      ) : null}
      {!connected ? (
        <p className="form-help">
          Link Discord above to get a Discord message each time one of your players is
          attacked.
        </p>
      ) : alerts === null ? (
        <p className="notice notice-unavailable" role="alert">
          Attack alerts could not be loaded. <a href="/account/providers">Try again</a>
        </p>
      ) : alerts.players.length === 0 ? (
        <p className="form-help">
          <a href="/account/verify-player">Link a player</a> to get attack alerts in
          Discord.
        </p>
      ) : (
        <>
          <p id="attack-alerts-help" className="form-help">
            Attack alerts: the Clash Lens bot sends you a Discord message as soon as Clash
            Lens saves an attack on that player. The bot can only message you while you
            share a Discord server with it, such as the{" "}
            <a href={DISCORD_INVITE_URL} target="_blank" rel="noopener noreferrer">
              Clash Lens Discord server
            </a>
            .
          </p>
          {missed ? (
            <div className="notice notice-unavailable" role="alert">
              <p>
                <strong>
                  An attack alert for {missedPlayer ? label(missedPlayer) : missed.tag}{" "}
                  wasn&apos;t delivered.
                </strong>
              </p>
              <p>{UNDELIVERED_HELP[missed.reason]}</p>
            </div>
          ) : null}
          <ul className="player-link-list" aria-describedby="attack-alerts-help">
            {alerts.players.map((player) => (
              <li key={player.tag}>
                <span>{label(player)}</span>
                <form method="post" className="inline-form">
                  <input type="hidden" name="intent" value="attack-alert" />
                  <input type="hidden" name="idempotencyKey" value={idempotencyKey} />
                  <input type="hidden" name="tag" value={player.tag} />
                  <input type="hidden" name="on" value={player.on ? "off" : "on"} />
                  <button
                    type="submit"
                    role="switch"
                    aria-checked={player.on}
                    aria-label={`Attack alerts for ${label(player)}`}
                    className={`button ${player.on ? "button-primary" : "button-secondary"}`}
                  >
                    {player.on ? "Alerts on" : "Alerts off"}
                  </button>
                </form>
              </li>
            ))}
          </ul>
          <figure className="attack-alert-example">
            <figcaption className="form-help">
              An attack alert looks like this:
            </figcaption>
            <div className="attack-alert-message">
              <strong>{example ? label(example) : "Wanderer #2PP"} was attacked</strong>
              <span>Drift #9RR: 2★ · 87% · −24 trophies</span>
              <span>Drift&apos;s hit rate this Season: 62%</span>
            </div>
          </figure>
        </>
      )}
    </section>
  );
}
