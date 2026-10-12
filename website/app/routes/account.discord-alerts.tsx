import { redirect, useLoaderData } from "react-router";

import type { BackHandle } from "../components/BackLink";
import type { AttackAlerts, UndeliveredReason } from "../lib/discord";
import type { WebsiteErrorResponse } from "../lib/contracts";
import type { Route } from "./+types/account.discord-alerts";

const NO_STORE = { "Cache-Control": "no-store" };
/** This page; connecting Discord from it comes back here. */
const PAGE = "/account/discord-alerts";

export const handle: BackHandle = { back: { to: "/account", label: "Your account" } };

/** The outcome of the last switch and its player, shown once on that row. */
type Saved = { outcome: "on" | "off" | "failed"; tag: string } | null;

export interface DiscordAlertsLoaderData {
  connected: boolean;
  /** Each verified player's attack alert switch; null when unreadable. */
  alerts: AttackAlerts | null;
  saved: Saved;
  idempotencyKey: string;
  error: WebsiteErrorResponse | null;
}

/**
 * GET /account/discord-alerts — each verified player's Discord attack alert
 * switch, or the step to connect Discord first.
 */
export async function loader({
  request,
}: Route.LoaderArgs): Promise<DiscordAlertsLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  const params = new URL(request.url).searchParams;
  const outcome = params.get("alerts");
  const saved: Saved =
    outcome === "on" || outcome === "off" || outcome === "failed"
      ? { outcome, tag: params.get("tag") ?? "" }
      : null;
  try {
    const { createPythonClient } = await import("../services/python.server");
    const { getAttackAlerts } = await import("../services/discord-alerts.server");
    const [account, alerts] = await Promise.all([
      createPythonClient(identity).getAccount(),
      // The connect step still works when the switches can't be read.
      getAttackAlerts(identity).catch(() => null),
    ]);
    return {
      connected: account.providers.includes("discord"),
      alerts,
      saved,
      idempotencyKey: freshIdempotencyKey(),
      error: null,
    };
  } catch (error) {
    if (error instanceof Response) throw error;
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(error)) throw redirect("/account/setup");
    const { safeWebsiteError } = await import("../server/errors.server");
    return {
      connected: false,
      alerts: null,
      saved: null,
      idempotencyKey: freshIdempotencyKey(),
      error: safeWebsiteError(error),
    };
  }
}

/**
 * POST /account/discord-alerts — turn one player's attack alerts on or off,
 * or start connecting Discord, which comes back to this page.
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
  if (form["intent"] === "connect-discord") {
    const cookies = await import("../server/auth-cookies.server");
    const loginCookie = actions
      .parseCookieHeader(request.headers.get("cookie"))
      .get(cookies.LOGIN_COOKIE_NAME);
    if (loginCookie === undefined) throw redirect("/login");
    const { startProviderAuthorization } =
      await import("../server/provider-start.server");
    try {
      return await startProviderAuthorization(
        config,
        "discord",
        "link",
        PAGE,
        cookies.createLoginSessionBinding(loginCookie),
      );
    } catch {
      throw redirect("/login");
    }
  }
  let on = form["on"];
  if (form["intent"] !== "attack-alert" || (on !== "on" && on !== "off")) {
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
    on = "failed";
  }
  const tag = encodeURIComponent(form["tag"] ?? "");
  return redirect(`${PAGE}?alerts=${on}&tag=${tag}`, { headers: NO_STORE });
}

export function headers() {
  return NO_STORE;
}

/** What a Clasher can do about the newest DM Discord did not deliver. */
const UNDELIVERED_HELP: Record<UndeliveredReason, string> = {
  no_shared_server:
    "The Clash Lens bot isn't added to your Discord, so Discord blocked the message. Reconnect Discord to add it.",
  dms_closed:
    "Your Discord privacy settings block messages from the bot. Check you haven't blocked the Clash Lens bot, then reconnect Discord.",
  unknown_user:
    "Discord couldn't find the connected Discord account. Unlink Discord in Sign-in connections and link it again.",
  other: "Discord refused the message. Reconnect Discord to try again.",
  unreachable:
    "Clash Lens couldn't reach Discord in time. Alerts try again with the next attack.",
};

export default function DiscordAlertsRoute() {
  const data = useLoaderData<typeof loader>();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="discord-alerts-title">
        <h1 id="discord-alerts-title">Discord alerts</h1>
        <p className="lede">
          Get a Discord message from the Clash Lens bot as soon as Clash Lens saves an
          attack on one of your players.
        </p>
      </section>
      {data.error ? (
        <aside className="notice notice-unavailable" role="alert">
          <strong>Discord alerts could not be loaded.</strong>{" "}
          <a href={PAGE}>Try again</a>
        </aside>
      ) : data.connected ? (
        <AttackAlertSwitches
          alerts={data.alerts}
          saved={data.saved}
          idempotencyKey={data.idempotencyKey}
        />
      ) : (
        <section className="form-panel stack-form" aria-labelledby="connect-title">
          <h2 id="connect-title">Connect Discord first</h2>
          <p className="form-help">
            Alerts go to the Discord account connected to Clash Lens. Connecting asks you
            to sign in with Discord and adds the Clash Lens bot to your Discord so it can
            message you, then brings you back here.
          </p>
          <ConnectDiscord
            idempotencyKey={data.idempotencyKey}
            label="Connect Discord"
            primary
          />
        </section>
      )}
    </main>
  );
}

/** Starts the Discord connect, which also adds the bot, and comes back here.
 * Reconnecting keeps the same Discord account and confirms the switches again. */
function ConnectDiscord({
  idempotencyKey,
  label,
  primary = false,
}: {
  idempotencyKey: string;
  label: string;
  primary?: boolean;
}) {
  return (
    <form method="post">
      <input type="hidden" name="intent" value="connect-discord" />
      <input type="hidden" name="idempotencyKey" value={idempotencyKey} />
      <button
        type="submit"
        className={`button ${primary ? "button-primary" : "button-secondary"}`}
      >
        {label}
      </button>
    </form>
  );
}

/** What happened to the last switch. */
function SavedNote({ outcome }: { outcome: "on" | "off" | "failed" }) {
  return outcome === "failed" ? (
    <p className="attack-alert-saved notice notice-unavailable" role="alert">
      Not saved. Try again.
    </p>
  ) : (
    <p className="attack-alert-saved" role="status">
      {outcome === "on"
        ? "Saved. The bot is sending you a confirmation message on Discord now. If it doesn't arrive, reload this page."
        : "Saved."}
    </p>
  );
}

/** Per-player switches for a Discord DM each time the player is attacked. */
function AttackAlertSwitches({
  alerts,
  saved,
  idempotencyKey,
}: {
  alerts: AttackAlerts | null;
  saved: Saved;
  idempotencyKey: string;
}) {
  const label = (player: { tag: string; name: string | null }) =>
    `${player.name ?? "Unnamed"} ${player.tag}`;
  const missed = alerts?.undelivered;
  const missedPlayer = alerts?.players.find((player) => player.tag === missed?.tag);
  const example = alerts?.players[0];
  return (
    <section className="form-panel stack-form" aria-labelledby="alerts-title">
      <h2 id="alerts-title">Attack alerts</h2>
      {saved && !alerts?.players.some((player) => player.tag === saved.tag) ? (
        <SavedNote outcome={saved.outcome} />
      ) : null}
      {alerts === null ? (
        <p className="notice notice-unavailable" role="alert">
          Attack alerts could not be loaded. <a href={PAGE}>Try again</a>
        </p>
      ) : alerts.players.length === 0 ? (
        <p className="form-help">
          <a href="/account/verify-player">Link a player</a> to get attack alerts in
          Discord.
        </p>
      ) : (
        <>
          <p id="attack-alerts-help" className="form-help">
            Each player&apos;s alerts are on or off on their own. Turning them on sends
            you a confirmation message on Discord straight away.
          </p>
          {missed ? (
            <div className="notice notice-unavailable" role="alert">
              <p>
                <strong>
                  A Discord message about{" "}
                  {missedPlayer ? label(missedPlayer) : missed.tag} wasn&apos;t delivered.
                </strong>
              </p>
              <p>{UNDELIVERED_HELP[missed.reason]}</p>
              {missed.reason === "unreachable" ? null : (
                <ConnectDiscord
                  idempotencyKey={idempotencyKey}
                  label="Reconnect Discord"
                />
              )}
            </div>
          ) : null}
          <ul className="player-link-list" aria-describedby="attack-alerts-help">
            {alerts.players.map((player) => (
              <li
                key={player.tag}
                className={`attack-alert-row${player.on ? " attack-alert-on" : ""}`}
              >
                <div className="attack-alert-state">
                  <strong>{label(player)}</strong>
                  <span
                    className={`state-badge ${player.on ? "state-live" : "state-missing"}`}
                  >
                    {player.on ? "✓ On" : "Off"}
                  </span>
                  <span className="attack-alert-meaning">
                    {player.on
                      ? `You'll get a Discord DM when someone attacks ${player.name ?? player.tag}.`
                      : `No alerts for ${player.name ?? player.tag}.`}
                  </span>
                </div>
                <form method="post" className="inline-form">
                  <input type="hidden" name="intent" value="attack-alert" />
                  <input type="hidden" name="idempotencyKey" value={idempotencyKey} />
                  <input type="hidden" name="tag" value={player.tag} />
                  <input type="hidden" name="on" value={player.on ? "off" : "on"} />
                  <button
                    type="submit"
                    aria-label={`Turn ${player.on ? "off" : "on"} attack alerts for ${label(player)}`}
                    className={`button ${player.on ? "button-secondary" : "button-primary"}`}
                  >
                    {player.on ? "Turn off" : "Turn on"}
                  </button>
                </form>
                {saved?.tag === player.tag ? <SavedNote outcome={saved.outcome} /> : null}
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
