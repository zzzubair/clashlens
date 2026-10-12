import { data, Form, Link, redirect, useActionData, useLoaderData } from "react-router";

import { type BackHandle, useBackState } from "../components/BackLink";
import { ConfirmForm, FormResult } from "../components/CrewForms";
import { RoleChip } from "../components/CrewBoards";
import { ErrorNotice } from "../components/ErrorNotice";
import { TrophyMark } from "../components/LeaderboardShared";
import {
  canJoin,
  type Crew,
  type CrewMember,
  type CrewPlayer,
  type LinkedAccount,
} from "../lib/crew-contracts";
import { canonicalPlayerPath, normalizePlayerTag } from "../lib/player-tag";
import type { Route } from "./+types/crews.$crewId.members";
import "../crews.css";

const NO_STORE = { "Cache-Control": "no-store" };
/** Each account picked to add is its own form field, named by this and its tag. */
const JOIN_FIELD = "join:";
const INTENTS = ["remove", "add", "promote", "demote", "leave"] as const;

/** Back leads to the crew. */
export const handle: BackHandle = {
  back: (match) => {
    const crew = (match.loaderData as { crew?: Crew | null } | undefined)?.crew;
    return crew ? { to: `/crews/${crew.crewId}`, label: crew.name } : null;
  },
};

/** GET /crews/:crewId/members — who is in the crew, and their accounts. */
export async function loader({ request, params }: Route.LoaderArgs) {
  const { loadCrew } = await import("../services/crews.server");
  const loaded = await loadCrew(request, params.crewId, true);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return data(
    { ...loaded, idempotencyKey: freshIdempotencyKey() },
    { status: loaded.error ? 503 : 200, headers: NO_STORE },
  );
}

export function headers() {
  return NO_STORE;
}

export function meta({ loaderData: loaded }: Route.MetaArgs) {
  return [
    {
      title: loaded?.crew
        ? `Members · ${loaded.crew.name} · Clash Lens`
        : "Crew · Clash Lens",
    },
  ];
}

/**
 * POST /crews/:crewId/members — remove your own account or kick someone
 * else's, add more of your accounts, make or remove an admin, or leave.
 * The private API checks every role and has the final say.
 */
export async function action({ request, params, context }: Route.ActionArgs) {
  const crews = await import("../services/crews.server");
  const { clientAddressContext } = await import("../server/client-address.server");
  return crews.crewFormAction(
    request,
    params.crewId,
    INTENTS,
    async ({ identity, crewId, intent, fields, key }) => {
      const path = `crews/${crewId}`;
      if (intent === "leave") {
        await crews.writeCrew(
          identity,
          "DELETE",
          `${path}/members/me`,
          undefined,
          key(""),
        );
        throw redirect("/crews");
      }
      if (intent === "promote" || intent === "demote") {
        const username = fields["username"] ?? "";
        const changed = (await crews.writeCrew(
          identity,
          "PATCH",
          `${path}/members/${encodeURIComponent(username)}`,
          { role: intent === "promote" ? "admin" : "member" },
          key(username),
        )) as { display_name?: unknown };
        const who =
          typeof changed.display_name === "string" ? changed.display_name : username;
        return {
          notice:
            intent === "promote"
              ? `${who} is now an admin.`
              : `${who} is no longer an admin.`,
        };
      }
      if (intent === "remove") {
        const tag = normalizePlayerTag(fields["tag"] ?? "") ?? "";
        const removed = (await crews.writeCrew(
          identity,
          "DELETE",
          `${path}/players/${encodeURIComponent(tag)}`,
          undefined,
          key(tag),
        )) as { left_crew?: unknown };
        const own = fields["own"] === "1";
        // Your last account out takes you out of the crew.
        if (own && removed.left_crew === true) throw redirect("/crews");
        return { notice: own ? `Removed ${tag}.` : `Kicked ${tag}.` };
      }
      const tags = pickedTags(fields);
      if (tags.length === 0) return { error: "Pick at least one account." };
      const added = await crews.joinCheckingAccounts(
        context?.get(clientAddressContext),
        key(tags.join(",")),
        tags.length,
        (next) => crews.writeCrew(identity, "POST", `${path}/players`, { tags }, next),
      );
      if (!added.ok) throw added.cause;
      return {
        notice: `Added ${tags.length} ${tags.length === 1 ? "account" : "accounts"}.`,
      };
    },
  );
}

function pickedTags(fields: Record<string, string>): string[] {
  return Object.keys(fields)
    .filter((field) => field.startsWith(JOIN_FIELD))
    .map((field) => normalizePlayerTag(field.slice(JOIN_FIELD.length)))
    .filter((tag): tag is string => tag !== null);
}

export default function CrewMembersRoute() {
  const {
    crew,
    accounts,
    error,
    idempotencyKey: loadedKey,
  } = useLoaderData<typeof loader>();
  const result = useActionData<typeof action>();
  if (crew === null) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
        <h1>Crew unavailable</h1>
        {error ? <ErrorNotice error={error} /> : null}
      </main>
    );
  }
  const key = result?.idempotencyKey ?? loadedKey;
  const clashers = crew.members.length;
  // You first, then the owner, admins and members.
  const members = [...crew.members].sort((a, b) => Number(b.you) - Number(a.you));
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
      <h1>Members</h1>
      <p className="crew-meta">
        {clashers} {clashers === 1 ? "clasher" : "clashers"} · {crew.used} of {crew.size}{" "}
        places
      </p>
      <FormResult result={result} />
      <ul className="crew-members">
        {members.map((member) => (
          <MemberCard
            key={member.username}
            crew={crew}
            member={member}
            accounts={accounts}
            idempotencyKey={key}
          />
        ))}
      </ul>
    </main>
  );
}

function MemberCard({
  crew,
  member,
  accounts,
  idempotencyKey,
}: {
  crew: Crew;
  member: CrewMember;
  accounts: LinkedAccount[];
  idempotencyKey: string;
}) {
  const backState = useBackState("Members");
  const role = crew.myRole;
  const canKick =
    !member.you && (role === "owner" || (role === "admin" && member.role === "member"));
  const count = member.players.length;
  return (
    <li className="crew-panel crew-member">
      <div className="crew-member-top">
        <span className="crew-icon" aria-hidden="true">
          {Array.from(member.displayName)[0]}
        </span>
        <span className="crew-who">
          <b>
            {member.displayName}
            {member.you ? <span className="crew-chip crew-chip-you">You</span> : null}
          </b>
          <span>
            @{member.username} · {count} {count === 1 ? "account" : "accounts"}
          </span>
        </span>
        <RoleChip role={member.role} />
      </div>
      <ul className="crew-accounts">
        {member.players.map((player) => (
          <li key={player.tag}>
            <span className="crew-who">
              <Link to={canonicalPlayerPath(player.tag)} state={backState}>
                {player.name ?? player.tag}
              </Link>
              <span>
                {player.tag}
                <PlayerStatus player={player} />
              </span>
            </span>
            {player.trophies !== null && player.status !== "not_in_legend" ? (
              <span className="crew-value">
                <TrophyMark />
                {player.trophies.toLocaleString("en-US")}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
      {member.you ? (
        <details className="crew-edit">
          <summary className="button secondary">Edit</summary>
          <div className="crew-tray">
            {/* The owner keeps a place until they hand over or delete the crew. */}
            {(member.role === "owner" && count === 1 ? [] : member.players).map(
              (player) => (
                <ConfirmForm
                  key={player.tag}
                  label={`Remove ${player.name ?? player.tag}`}
                  question={`${player.tag} leaves the crew${
                    count === 1 ? ". It's your last account here, so you leave too." : "."
                  }`}
                  confirm={count === 1 ? "Remove and leave" : "Remove"}
                  intent="remove"
                  idempotencyKey={idempotencyKey}
                  fields={{ tag: player.tag, own: "1" }}
                />
              ),
            )}
            <AddAccounts
              crew={crew}
              mine={member.players}
              accounts={accounts}
              idempotencyKey={idempotencyKey}
            />
            {member.role === "owner" ? null : (
              <ConfirmForm
                label="Leave crew"
                question={`All your accounts leave ${crew.name}.`}
                confirm="Leave crew"
                intent="leave"
                idempotencyKey={idempotencyKey}
              />
            )}
          </div>
        </details>
      ) : canKick ? (
        <details className="crew-edit">
          <summary className="button secondary">Edit</summary>
          <div className="crew-tray">
            {role === "owner" ? (
              <Form method="post">
                <input
                  type="hidden"
                  name="intent"
                  value={member.role === "admin" ? "demote" : "promote"}
                />
                <input type="hidden" name="username" value={member.username} />
                <input type="hidden" name="idempotencyKey" value={idempotencyKey} />
                <button type="submit" className="button secondary">
                  {member.role === "admin" ? "Remove admin" : "Make admin"}
                </button>
              </Form>
            ) : null}
            {member.players.map((player) => (
              <ConfirmForm
                key={player.tag}
                label={`Kick ${player.name ?? player.tag}`}
                question={`${player.tag} leaves the crew. ${
                  count === 1
                    ? `It's ${member.displayName}'s only account here, so they leave too.`
                    : `${member.displayName}'s other accounts stay.`
                }`}
                confirm="Kick"
                intent="remove"
                idempotencyKey={idempotencyKey}
                fields={{ tag: player.tag }}
              />
            ))}
          </div>
        </details>
      ) : null}
    </li>
  );
}

/** A placed account that boards leave out, and why. */
function PlayerStatus({ player }: { player: CrewPlayer }) {
  if (player.status === "tracking") return null;
  return (
    <span className="crew-chip crew-chip-quiet">
      {player.status === "not_in_legend"
        ? "Not in Legend now"
        : "No Legend battles this Season"}
    </span>
  );
}

/** Your linked Legend accounts that aren't in the crew yet, to add. */
function AddAccounts({
  crew,
  mine,
  accounts,
  idempotencyKey,
}: {
  crew: Crew;
  mine: CrewPlayer[];
  accounts: LinkedAccount[];
  idempotencyKey: string;
}) {
  const spare = accounts.filter(
    (account) => canJoin(account) && !mine.some((player) => player.tag === account.tag),
  );
  const open = Math.max(0, crew.size - crew.used);
  if (spare.length === 0 || open === 0) return null;
  return (
    <details className="crew-confirm">
      <summary className="button secondary">Add my accounts</summary>
      <Form method="post" className="crew-confirm-body">
        <fieldset className="crew-pick">
          <legend>
            {open} {open === 1 ? "place" : "places"} open
          </legend>
          <ul>
            {spare.map((account) => (
              <li key={account.tag}>
                <label>
                  <input type="checkbox" name={`${JOIN_FIELD}${account.tag}`} />
                  <span className="crew-who">
                    <b>{account.name ?? account.tag}</b>
                    <span>{account.tag}</span>
                  </span>
                  {account.trophies !== null ? (
                    <span className="crew-value">
                      <TrophyMark />
                      {account.trophies.toLocaleString("en-US")}
                    </span>
                  ) : null}
                </label>
              </li>
            ))}
          </ul>
        </fieldset>
        <input type="hidden" name="intent" value="add" />
        <input type="hidden" name="idempotencyKey" value={idempotencyKey} />
        <button type="submit" className="button button-primary">
          Add
        </button>
      </Form>
    </details>
  );
}
