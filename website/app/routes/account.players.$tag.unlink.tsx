import { data, Link, redirect, useActionData, useLoaderData } from "react-router";

import type { BackHandle } from "../components/BackLink";
import { SavedLine } from "../components/SavedLine";
import type { VerifiedPlayer } from "../lib/account-contracts";
import { normalizePlayerTag } from "../lib/player-tag";
import type { Route } from "./+types/account.players.$tag.unlink";

import "../user-profile.css";

const NO_STORE = { "Cache-Control": "no-store" };
const SETTINGS = "/account/settings";

export const handle: BackHandle = { back: { to: SETTINGS, label: "Settings" } };

export function meta() {
  return [{ title: "Unlink player · Clash Lens" }];
}

export interface UnlinkPlayerLoaderData {
  player: VerifiedPlayer;
  /** It is the default player and another linked player takes over. */
  defaultMoves: boolean;
  idempotencyKey: string;
}

/**
 * GET /account/players/:tag/unlink — say what unlinking one of the
 * Clasher's own players changes and what stays before they confirm it. A
 * player that is not theirs goes back to Settings.
 */
export async function loader({ request, params }: Route.LoaderArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const tag = normalizePlayerTag(params.tag ?? "");
  const { createPythonClient } = await import("../services/python.server");
  let summary;
  try {
    summary = await createPythonClient(identity).getAccountSummary();
  } catch (cause) {
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(cause)) throw redirect("/account/setup");
    throw cause;
  }
  const player = summary.verifiedPlayers.find((linked) => linked.tag === tag);
  if (player === undefined) throw redirect(SETTINGS, { headers: NO_STORE });
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return data<UnlinkPlayerLoaderData>(
    {
      player,
      defaultMoves:
        summary.defaultPlayer === player.tag && summary.verifiedPlayers.length > 1,
      idempotencyKey: freshIdempotencyKey(),
    },
    { headers: NO_STORE },
  );
}

/** POST /account/players/:tag/unlink — unlink it, then back to Settings. */
export async function action({ request, params }: Route.ActionArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  if (!actions.isSameOrigin(request, getWebsiteConfig().publicOrigin)) {
    return new Response(null, { status: 403, headers: NO_STORE });
  }
  const form = await actions.parseBoundedFormData(request);
  const idempotencyKey = form?.["idempotencyKey"] ?? "";
  const tag = normalizePlayerTag(params.tag ?? "");
  if (form === null || !actions.isIdempotencyKey(idempotencyKey) || tag === null) {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  const { unlinkPlayer } = await import("../services/unlink-player.server");
  try {
    await unlinkPlayer(identity, tag, idempotencyKey);
  } catch (cause) {
    if ((cause as { status?: number }).status === 404) {
      throw redirect(SETTINGS, { headers: NO_STORE });
    }
    // A fresh key, so trying again is a new request.
    return data({ idempotencyKey: actions.freshIdempotencyKey() }, { status: 503 });
  }
  return redirect(`${SETTINGS}?saved=unlinked&tag=${encodeURIComponent(tag)}`, {
    headers: NO_STORE,
  });
}

export function headers() {
  return NO_STORE;
}

export default function UnlinkPlayerRoute() {
  const { player, defaultMoves, idempotencyKey } = useLoaderData<typeof loader>();
  const failed = useActionData<typeof action>();
  const name = player.name ?? player.tag;
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      {failed ? (
        <SavedLine failed>{name} wasn&apos;t unlinked. Try again.</SavedLine>
      ) : null}
      <section className="hero" aria-labelledby="unlink-title">
        <h1 id="unlink-title">Unlink {name}?</h1>
        <p className="player-tag">{player.tag}</p>
      </section>
      <section
        className="form-panel stack-form unlink-player"
        aria-labelledby="unlink-changes"
      >
        <h2 id="unlink-changes">What changes</h2>
        <ul>
          <li>{name} is no longer on your Clash Lens account or your profile.</li>
          <li>
            Discord alerts, the daily summary and your Stream overlay stop for {name}.
          </li>
          <li>
            {name} leaves the crews it&apos;s in. If it was your only player in a crew you
            don&apos;t own, you leave that crew too.
          </li>
          {defaultMoves ? (
            <li>The Discord bot&apos;s /stats uses your next player instead.</li>
          ) : null}
        </ul>
        <h2>What stays</h2>
        <ul>
          <li>
            {name} itself, with its Legend days, rankings and history. Anyone can still
            look it up.
          </li>
          <li>You can link it again any time with its API token.</li>
        </ul>
        <form method="post" className="hero-actions">
          <input
            type="hidden"
            name="idempotencyKey"
            value={failed?.idempotencyKey ?? idempotencyKey}
          />
          <button type="submit" className="button button-primary">
            Unlink {name}
          </button>
          <Link className="button button-secondary" to={SETTINGS}>
            Cancel
          </Link>
        </form>
      </section>
    </main>
  );
}
