import { Form, redirect, useLoaderData, useNavigation } from "react-router";

import type { BackHandle } from "../components/BackLink";
import { SavedLine } from "../components/SavedLine";
import type { AttackAlertPlayer, AttackAlerts, UndeliveredReason } from "../lib/discord";
import type { WebsiteErrorResponse } from "../lib/contracts";
import type { Route } from "./+types/account.discord-alerts";

import "../user-profile.css";

const NO_STORE = { "Cache-Control": "no-store" };
/** This page; connecting Discord from it comes back here. */
const PAGE = "/account/discord-alerts";

export const handle: BackHandle = { back: { to: "/account", label: "Your profile" } };

/** The outcome of the last switch, its player and which switch it was,
 * shown once at the top of the page. */
type Saved = {
  outcome: "on" | "off" | "failed";
  tag: string;
  kind: "attack" | "summary";
} | null;

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
  const kind = params.has("summary") ? "summary" : "attack";
  const outcome = params.get(kind === "summary" ? "summary" : "alerts");
  const saved: Saved =
    outcome === "on" || outcome === "off" || outcome === "failed"
      ? { outcome, tag: params.get("tag") ?? "", kind }
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
 * POST /account/discord-alerts — turn one player's attack alerts or Legend
 * day summary on or off, or start connecting Discord, which comes back to
 * this page.
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
  const summary = form["intent"] === "day-summary";
  if ((!summary && form["intent"] !== "attack-alert") || (on !== "on" && on !== "off")) {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  try {
    const { setAttackAlert } = await import("../services/discord-alerts.server");
    await setAttackAlert(
      identity,
      form["tag"] ?? "",
      on === "on",
      form["idempotencyKey"] ?? "",
      summary ? "day_summary" : "attack",
    );
  } catch {
    on = "failed";
  }
  const tag = encodeURIComponent(form["tag"] ?? "");
  return redirect(`${PAGE}?${summary ? "summary" : "alerts"}=${on}&tag=${tag}`, {
    headers: NO_STORE,
  });
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
    "Discord couldn't find the connected Discord account. Disconnect Discord in Settings and connect it again.",
  other: "Discord refused the message. Reconnect Discord to try again.",
  unreachable:
    "Clash Lens couldn't get it to Discord in time and stopped trying. The next attack sends a new message.",
};

export default function DiscordAlertsRoute() {
  const data = useLoaderData<typeof loader>();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      {data.saved ? <SavedNote saved={data.saved} alerts={data.alerts} /> : null}
      <section className="hero" aria-labelledby="discord-alerts-title">
        <h1 id="discord-alerts-title">Discord alerts</h1>
        <p className="lede">
          The Clash Lens bot messages you on Discord when someone attacks your players,
          and sums up their Legend day after each Reset.
        </p>
      </section>
      {data.error ? (
        <aside className="notice notice-unavailable" role="alert">
          <strong>Discord alerts could not be loaded.</strong>{" "}
          <a href={PAGE}>Try again</a>
        </aside>
      ) : data.connected ? (
        <AlertRows alerts={data.alerts} idempotencyKey={data.idempotencyKey} />
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

const SWITCH_NAMES = { attack: "Attack alerts", summary: "Daily summary" } as const;

/** What the last switch did, at the top of the page; only attack alerts send
 * a confirmation. */
function SavedNote({
  saved,
  alerts,
}: {
  saved: NonNullable<Saved>;
  alerts: AttackAlerts | null;
}) {
  if (saved.outcome === "failed")
    return <SavedLine failed>Not saved. Try again.</SavedLine>;
  const player = alerts?.players.find((one) => one.tag === saved.tag);
  const name = player ? (player.name ?? player.tag) : null;
  return (
    <SavedLine>
      {SWITCH_NAMES[saved.kind]} {saved.outcome}
      {name === null ? "" : ` for ${name}`}.
      {saved.outcome === "on" && saved.kind === "attack"
        ? " The bot is sending you a confirmation message on Discord now. If it doesn't arrive, reload this page."
        : null}
    </SavedLine>
  );
}

/** One row per verified player, each with its two switches. */
function AlertRows({
  alerts,
  idempotencyKey,
}: {
  alerts: AttackAlerts | null;
  idempotencyKey: string;
}) {
  if (alerts === null) {
    return (
      <p className="notice notice-unavailable" role="alert">
        Your alerts could not be loaded. <a href={PAGE}>Try again</a>
      </p>
    );
  }
  if (alerts.players.length === 0) {
    return (
      <p className="form-help">
        <a href="/account/verify-player">Link a player</a> to get alerts in Discord.
      </p>
    );
  }
  const missed = alerts.undelivered;
  const missedPlayer = alerts.players.find((player) => player.tag === missed?.tag);
  const example = alerts.players[0];
  return (
    <>
      {missed ? (
        <div className="notice notice-unavailable" role="alert">
          <p>
            <strong>
              A Discord message about{" "}
              {missedPlayer
                ? `${missedPlayer.name ?? "Unnamed"} ${missedPlayer.tag}`
                : missed.tag}{" "}
              wasn&apos;t delivered.
            </strong>
          </p>
          <p>{UNDELIVERED_HELP[missed.reason]}</p>
          {missed.reason === "unreachable" ? null : (
            <ConnectDiscord idempotencyKey={idempotencyKey} label="Reconnect Discord" />
          )}
        </div>
      ) : null}
      <ul className="settings-list">
        {alerts.players.map((player) => (
          <li key={player.tag} className="settings-row">
            <div className="settings-label">
              <span>{player.name ?? "Unnamed"}</span>
              <p className="form-help player-tag">{player.tag}</p>
            </div>
            <div className="settings-control">
              <AlertSwitch
                player={player}
                kind="attack"
                idempotencyKey={idempotencyKey}
              />
              <AlertSwitch
                player={player}
                kind="summary"
                idempotencyKey={idempotencyKey}
              />
            </div>
          </li>
        ))}
      </ul>
      <p className="form-help">
        Attack alerts message you as soon as Clash Lens saves an attack on that player.
        The daily summary arrives after each Reset&apos;s Daily board is published, with
        the day&apos;s trophies, attacks, defenses and place.
      </p>
      <figure className="attack-alert-example">
        <figcaption className="form-help">
          An attack alert looks like this. Your phone&apos;s notification shows its first
          line:
        </figcaption>
        <strong>{`🛡️ ${example.name ?? "Unnamed"} −24 🏆`}</strong>
        <div className="attack-alert-message">
          <strong>Drift</strong>
          <strong className="attack-alert-result">★★☆ · 87%</strong>
          <small>Attacker stats</small>
          <span>🏆 5,312 · 🌍 #1,204 · 🎯 62%</span>
          <small>Open battle · Alert settings</small>
        </div>
      </figure>
    </>
  );
}

/** One switch that saves as soon as it is flipped. While the save runs it
 * already shows where it is going, and no switch saves again until the page
 * has the next request key. */
function AlertSwitch({
  player,
  kind,
  idempotencyKey,
}: {
  player: AttackAlertPlayer;
  kind: "attack" | "summary";
  idempotencyKey: string;
}) {
  const intent = kind === "attack" ? "attack-alert" : "day-summary";
  const navigation = useNavigation();
  const busy = navigation.state !== "idle";
  const pending = navigation.formData;
  const flipping = pending?.get("intent") === intent && pending.get("tag") === player.tag;
  const saved = kind === "attack" ? player.on : player.summaryOn;
  const on = flipping ? pending.get("on") === "on" : saved;
  return (
    <Form method="post" className="inline-form" preventScrollReset replace>
      <input type="hidden" name="intent" value={intent} />
      <input type="hidden" name="idempotencyKey" value={idempotencyKey} />
      <input type="hidden" name="tag" value={player.tag} />
      <input type="hidden" name="on" value={saved ? "off" : "on"} />
      <button
        type="submit"
        role="switch"
        aria-checked={on}
        aria-disabled={busy || undefined}
        onClick={(event) => {
          if (busy) event.preventDefault();
        }}
        aria-label={`${SWITCH_NAMES[kind]} for ${player.name ?? "Unnamed"} ${player.tag}`}
        className="switch"
      >
        <span className="switch-track" aria-hidden="true" />
        {SWITCH_NAMES[kind]}
      </button>
    </Form>
  );
}
