import { useRef } from "react";
import { data, Form, redirect, useActionData, useLoaderData } from "react-router";

import { type BackHandle } from "../components/BackLink";
import { FormResult } from "../components/CrewForms";
import { ErrorNotice } from "../components/ErrorNotice";
import { isInappropriateName, normalizeGroupName } from "../lib/account-validation";
import {
  formatInviteExpiry,
  MAX_CREW_SIZE,
  MIN_CREW_SIZE,
  type Crew,
} from "../lib/crew-contracts";
import { isCanonicalUuid } from "../lib/validation";
import type { Route } from "./+types/crews.$crewId.settings";
import "../crews.css";

const NO_STORE = { "Cache-Control": "no-store" };
const INTENTS = ["rename", "resize", "revoke", "transfer", "delete"] as const;

/** Back leads to the crew. */
export const handle: BackHandle = {
  back: (match) => {
    const crew = (match.loaderData as { crew?: Crew | null } | undefined)?.crew;
    return crew ? { to: `/crews/${crew.crewId}`, label: crew.name } : null;
  },
};

/** GET /crews/:crewId/settings — Edit crew: name, places, links, owner. */
export async function loader({ request, params }: Route.LoaderArgs) {
  const { loadCrew } = await import("../services/crews.server");
  const loaded = await loadCrew(request, params.crewId, false);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return data(
    { crew: loaded.crew, error: loaded.error, idempotencyKey: freshIdempotencyKey() },
    { status: loaded.error ? 503 : 200, headers: NO_STORE },
  );
}

export function headers() {
  return NO_STORE;
}

export function meta({ loaderData: loaded }: Route.MetaArgs) {
  return [
    {
      title: loaded?.crew ? `Edit ${loaded.crew.name} · Clash Lens` : "Crew · Clash Lens",
    },
  ];
}

/**
 * POST /crews/:crewId/settings — rename, resize, turn off a link, hand over
 * or delete. The private API checks every role and has the final say.
 */
export async function action({ request, params }: Route.ActionArgs) {
  const { crewFormAction, writeCrew } = await import("../services/crews.server");
  return crewFormAction(
    request,
    params.crewId,
    INTENTS,
    async ({ identity, crewId, intent, fields, key }) => {
      const path = `crews/${crewId}`;
      if (intent === "rename") {
        const raw = fields["name"] ?? "";
        const name = normalizeGroupName(raw);
        if (name === null) return { error: "Crew name must be 1–80 characters." };
        if (isInappropriateName(raw)) return { error: "Choose a different crew name." };
        await writeCrew(identity, "PATCH", path, { name }, key(name));
        return { notice: "Name saved." };
      }
      if (intent === "resize") {
        const size = Number(fields["size"]);
        if (!Number.isInteger(size) || size < MIN_CREW_SIZE || size > MAX_CREW_SIZE) {
          return {
            error: `Places must be a whole number from ${MIN_CREW_SIZE} to ${MAX_CREW_SIZE}.`,
          };
        }
        await writeCrew(identity, "PATCH", path, { size }, key(String(size)));
        return { notice: `Size saved: ${size} places.` };
      }
      if (intent === "revoke") {
        const invite = fields["invite"] ?? "";
        if (!isCanonicalUuid(invite))
          return { error: "That link is already off or expired." };
        await writeCrew(
          identity,
          "DELETE",
          `${path}/invites/${invite}`,
          undefined,
          key(invite),
        );
        return { notice: "Link turned off." };
      }
      if (intent === "transfer") {
        const username = fields["username"] ?? "";
        if (username === "") return { error: "Pick the new owner." };
        const handed = (await writeCrew(
          identity,
          "POST",
          `${path}/owner`,
          { username },
          key(username),
        )) as { left_crew?: unknown };
        // An owner with no places left is out of the crew once it is handed over.
        if (handed.left_crew === true) throw redirect("/crews");
        return { notice: `@${username} owns the crew now.` };
      }
      if (fields["confirm"] !== "on")
        return { error: "Tick the box to delete the crew." };
      await writeCrew(identity, "DELETE", path, undefined, key(""));
      throw redirect("/crews");
    },
  );
}

export default function CrewSettingsRoute() {
  const { crew, error, idempotencyKey: loadedKey } = useLoaderData<typeof loader>();
  const result = useActionData<typeof action>();
  const sizeInput = useRef<HTMLInputElement>(null);
  if (crew === null) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
        <h1>Crew unavailable</h1>
        {error ? <ErrorNotice error={error} /> : null}
      </main>
    );
  }
  if (crew.myRole === "member") {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
        <h1>Edit crew</h1>
        <p className="notice">Only the owner and admins can change crew settings.</p>
      </main>
    );
  }
  const key = result?.idempotencyKey ?? loadedKey;
  const smallest = Math.max(MIN_CREW_SIZE, crew.used);
  const step = (by: number) => {
    const input = sizeInput.current;
    if (input === null) return;
    const current = Number(input.value) || crew.size;
    input.value = String(Math.min(MAX_CREW_SIZE, Math.max(smallest, current + by)));
  };
  const others = crew.members.filter((member) => !member.you);
  const hidden = (intent: string) => (
    <>
      <input type="hidden" name="intent" value={intent} />
      <input type="hidden" name="idempotencyKey" value={key} />
    </>
  );
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
      <h1>Edit crew</h1>
      <FormResult result={result} />

      <Form method="post" className="crew-panel crew-setting">
        {hidden("rename")}
        <div className="form-field">
          <label htmlFor="crew-name">Crew name</label>
          <input
            id="crew-name"
            name="name"
            type="text"
            maxLength={80}
            autoComplete="off"
            required
            defaultValue={crew.name}
            key={crew.name}
          />
        </div>
        <button type="submit" className="button secondary">
          Save name
        </button>
      </Form>

      <Form method="post" className="crew-panel crew-setting">
        {hidden("resize")}
        <div className="form-field">
          <label htmlFor="crew-size">Places</label>
          <div className="crew-stepper">
            <button
              type="button"
              className="button secondary"
              aria-label="1 fewer place"
              onClick={() => step(-1)}
            >
              −
            </button>
            <input
              ref={sizeInput}
              id="crew-size"
              name="size"
              type="number"
              inputMode="numeric"
              min={smallest}
              max={MAX_CREW_SIZE}
              required
              defaultValue={crew.size}
              key={crew.size}
              aria-describedby="crew-size-help"
            />
            <button
              type="button"
              className="button secondary"
              aria-label="1 more place"
              onClick={() => step(1)}
            >
              +
            </button>
          </div>
          <p id="crew-size-help" className="form-help">
            {crew.used} in use, so {smallest} to {MAX_CREW_SIZE}
          </p>
        </div>
        <button type="submit" className="button secondary">
          Save size
        </button>
      </Form>

      <section className="crew-panel crew-setting" aria-labelledby="crew-links">
        <h2 id="crew-links">Live invite links</h2>
        {crew.invites.length === 0 ? (
          <p className="crew-sheet-note">No live links</p>
        ) : (
          <ul className="crew-accounts">
            {crew.invites.map((invite) => (
              <li key={invite.inviteId}>
                <span className="crew-who">
                  <b>{invite.mine ? "Your link" : `Made by ${invite.madeBy}`}</b>
                  <span>Until {formatInviteExpiry(invite.expiresAt)}</span>
                </span>
                <Form method="post">
                  {hidden("revoke")}
                  <input type="hidden" name="invite" value={invite.inviteId} />
                  <button type="submit" className="button secondary">
                    Turn off
                  </button>
                </Form>
              </li>
            ))}
          </ul>
        )}
      </section>

      {crew.myRole === "owner" && others.length > 0 ? (
        <section className="crew-panel crew-setting" aria-labelledby="crew-hand-over">
          <h2 id="crew-hand-over">Hand over</h2>
          <details className="crew-confirm">
            <summary className="button secondary">Pick the new owner</summary>
            <Form method="post" className="crew-confirm-body">
              {hidden("transfer")}
              <div className="form-field">
                <label htmlFor="crew-owner">New owner</label>
                <select id="crew-owner" name="username" required defaultValue="">
                  <option value="" disabled>
                    Pick a clasher
                  </option>
                  {others.map((member) => (
                    <option key={member.username} value={member.username}>
                      {member.displayName} (@{member.username})
                    </option>
                  ))}
                </select>
                <p className="form-help">You become an admin.</p>
              </div>
              <button type="submit" className="button danger-button">
                Hand over
              </button>
            </Form>
          </details>
        </section>
      ) : null}

      {crew.myRole === "owner" ? (
        <Form method="post" className="crew-panel crew-setting crew-danger">
          {hidden("delete")}
          <h2>Delete crew</h2>
          <label className="crew-tick">
            <input type="checkbox" name="confirm" required />
            Delete {crew.name} for all {crew.members.length}{" "}
            {crew.members.length === 1 ? "clasher" : "clashers"}
          </label>
          <button type="submit" className="button danger-button">
            Delete crew
          </button>
        </Form>
      ) : null}
    </main>
  );
}
