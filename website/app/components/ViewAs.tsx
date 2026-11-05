import { Form, Link } from "react-router";

import { SavedLine } from "./SavedLine";
import { VIEW_AS_PARAM } from "../lib/dashboard";
import type { OwnerFollow } from "../server/view-as.server";

/** The owner's own dashboard, even while a player is pinned. */
export const OWN_DASHBOARD = "/?mine=1";

/** The site owner's own choices, read for the owner only. */
export interface OwnerChoices {
  /** The player a plain load of the dashboard opens, if pinned. */
  pinned: string | null;
  /** The owner's own Discord switches for the viewed player. */
  follow: OwnerFollow;
  /** How the last choice went, shown once. */
  saved: "saved" | "failed" | null;
  /** Whether the owner's account has Discord connected, for the switches. */
  discord: boolean;
  idempotencyKey: string;
}

/**
 * The site owner's View as box: a player tag opens that player's dashboard,
 * read only. Only the owner's dashboard shows it, and only the owner's
 * dashboard reads what it sends. While a player is pinned, the owner's own
 * dashboard also links back to them.
 */
export function ViewAsForm({ pinned = null }: { pinned?: string | null }) {
  return (
    <form className="dash-view-as-form" method="get" action="/">
      <label htmlFor="dash-view-as-tag">View as player</label>
      <input
        id="dash-view-as-tag"
        name={VIEW_AS_PARAM}
        placeholder="#TAG"
        required
        autoComplete="off"
        autoCapitalize="characters"
        spellCheck={false}
      />
      <button className="button button-secondary" type="submit">
        View
      </button>
      {pinned ? (
        // Straight to the pinned player's View as: from `?mine=1`, a link to
        // `/` would change no address the dashboard reloads for.
        <Link
          className="dash-view-as-pinned"
          to={`/?${new URLSearchParams({ [VIEW_AS_PARAM]: pinned })}`}
        >
          Pinned: {pinned}
        </Link>
      ) : null}
    </form>
  );
}

/**
 * While View as is on: whose dashboard this is, that it is read only, and
 * the way out. For the owner's pinned player that is My dashboard; for any
 * other, Exit (back to the pinned player, if any) and My dashboard when a
 * player is pinned. Then the owner's own choices for this player.
 */
export function ViewAsBar({
  viewAs,
  choices,
}: {
  viewAs: { tag: string; name: string | null } | "not-found";
  choices: OwnerChoices | null;
}) {
  const tag = viewAs === "not-found" ? null : viewAs.tag;
  const pinned = choices?.pinned ?? null;
  return (
    <>
      {choices?.saved === "saved" ? <SavedLine>Saved.</SavedLine> : null}
      {choices?.saved === "failed" ? (
        <SavedLine failed>Not saved. Try again.</SavedLine>
      ) : null}
      <section className="notice notice-warning dash-view-as" aria-label="View as">
        <p role="status">
          {viewAs === "not-found" ? (
            <b>Player not found</b>
          ) : (
            <>
              Viewing as{" "}
              <b>
                <bdi>{viewAs.name ?? viewAs.tag}</bdi>
              </b>{" "}
              ({viewAs.tag}) – read only
            </>
          )}
        </p>
        <span className="dash-view-as-exits">
          {pinned !== null && pinned === tag ? null : (
            <a className="button button-secondary" href="/">
              Exit
            </a>
          )}
          {pinned !== null ? (
            <a className="button button-secondary" href={OWN_DASHBOARD}>
              My dashboard
            </a>
          ) : null}
        </span>
        {tag !== null && choices !== null ? (
          <OwnerControls tag={tag} choices={choices} />
        ) : null}
      </section>
      <ViewAsForm />
    </>
  );
}

/** Pin, and the owner's own Discord switches for the viewed player. */
function OwnerControls({ tag, choices }: { tag: string; choices: OwnerChoices }) {
  const pinned = choices.pinned === tag;
  const choice = (intent: string, on?: boolean) => (
    <>
      <input type="hidden" name="intent" value={intent} />
      <input type="hidden" name="tag" value={tag} />
      <input type="hidden" name="idempotencyKey" value={choices.idempotencyKey} />
      {on === undefined ? null : (
        <input type="hidden" name="on" value={on ? "off" : "on"} />
      )}
    </>
  );
  const switches = [
    ["attack", "Attack alerts", choices.follow.attack],
    ["day_summary", "Day summary", choices.follow.daySummary],
  ] as const;
  return (
    <div className="dash-view-as-own">
      <Form method="post" action="/account/view-as" replace preventScrollReset>
        {choice(pinned ? "unpin" : "pin")}
        <button className="button button-secondary" type="submit">
          {pinned ? "Unpin" : "Pin"}
        </button>
      </Form>
      {switches.map(([intent, label, on]) => (
        <Form
          key={intent}
          method="post"
          action="/account/view-as"
          replace
          preventScrollReset
        >
          {choice(intent, on)}
          <button
            className="switch"
            type="submit"
            role="switch"
            aria-checked={on}
            aria-label={`${label} for ${tag} on your Discord`}
          >
            <span className="switch-track" aria-hidden="true" />
            {label}
          </button>
        </Form>
      ))}
      {choices.discord ? null : (
        <p className="dash-view-as-note">
          <Link to="/account/discord-alerts">Connect Discord</Link> to get these.
        </p>
      )}
    </div>
  );
}
