import { useRef } from "react";
import {
  data,
  Form,
  Link,
  redirect,
  useActionData,
  useLoaderData,
  useNavigation,
} from "react-router";

import { type BackHandle } from "../components/BackLink";
import { TrophyMark } from "../components/LeaderboardShared";
import { ErrorNotice } from "../components/ErrorNotice";
import type { LinkedPlayerCard } from "../lib/account-contracts";
import { isInappropriateName, normalizeGroupName } from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import {
  DEFAULT_CREW_SIZE,
  MAX_CREW_SIZE,
  MAX_CREWS,
  MIN_CREW_SIZE,
} from "../lib/crew-contracts";
import { normalizePlayerTag } from "../lib/player-tag";
import type { Route } from "./+types/crews.new";
import "../crews.css";

const NO_STORE = { "Cache-Control": "no-store" };
/** Each picked account is its own form field, named by this and its tag. */
const JOIN_FIELD = "join:";

export const handle: BackHandle = { back: { to: "/crews", label: "Crews" } };

export function meta() {
  return [{ title: "Create a crew · Clash Lens" }];
}

type LinkedAccount = Pick<
  LinkedPlayerCard,
  "tag" | "name" | "state" | "trophies" | "league"
>;

export interface NewCrewLoaderData {
  crewCount: number;
  accounts: LinkedAccount[];
  idempotencyKey: string;
  error: WebsiteErrorResponse | null;
}

export interface NewCrewActionData {
  idempotencyKey: string;
  values: { name: string; size: string; tags: string[] };
  fieldErrors: { name?: string; size?: string; accounts?: string };
  generalError: string | WebsiteErrorResponse | null;
}

/** A linked account the game puts outside Legend League cannot join. */
function canJoin(account: LinkedAccount): boolean {
  return !["not_in_legend", "uncertain", "not_found"].includes(account.state);
}

async function guard(request: Request) {
  const { isCrewsEnabled } = await import("../server/config.server");
  if (!isCrewsEnabled()) throw data(null, { status: 404 });
  const { requireLogin } = await import("../server/auth-guard.server");
  return requireLogin(request);
}

async function setupRedirect(request: Request, cause: unknown): Promise<void> {
  const { isAccountNotFoundError } = await import("../server/actions.server");
  if (isAccountNotFoundError(cause)) {
    const { accountSetupPath } = await import("../server/return-path.server");
    const url = new URL(request.url);
    throw redirect(accountSetupPath("/crews/new", url));
  }
}

/** GET /crews/new — how many crews the account is in, and its linked accounts. */
export async function loader({ request }: Route.LoaderArgs) {
  const identity = await guard(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  const idempotencyKey = freshIdempotencyKey();
  try {
    const { listCrews } = await import("../services/crews.server");
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient(identity);
    const [list, summary] = await Promise.all([
      listCrews(identity),
      client.getAccountSummary(),
    ]);
    // The public profile adds whether each linked account is in Legend League.
    const profile = await client.getPublicUser(summary.username);
    const accounts = profile.verifiedPlayers.map(
      ({ tag, name, state, trophies, league }) => ({
        tag,
        name,
        state,
        trophies,
        league,
      }),
    );
    return data<NewCrewLoaderData>(
      { crewCount: list.crews.length, accounts, idempotencyKey, error: null },
      { headers: NO_STORE },
    );
  } catch (cause) {
    await setupRedirect(request, cause);
    const { safeWebsiteError } = await import("../server/errors.server");
    return data<NewCrewLoaderData>(
      { crewCount: 0, accounts: [], idempotencyKey, error: safeWebsiteError(cause) },
      { status: 503, headers: NO_STORE },
    );
  }
}

export function headers() {
  return NO_STORE;
}

/**
 * POST /crews/new — make a crew with the picked accounts; the maker is its
 * owner. The private API repeats every check and has the final say. An
 * account Clash Lens has not checked yet is checked with the game, then
 * the crew is made again.
 */
export async function action({ request, context }: Route.ActionArgs) {
  const identity = await guard(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  const reply = (
    status: number,
    values: NewCrewActionData["values"],
    outcome: Partial<NewCrewActionData>,
  ) =>
    data<NewCrewActionData>(
      {
        idempotencyKey: actions.freshIdempotencyKey(),
        values,
        fieldErrors: {},
        generalError: null,
        ...outcome,
      },
      { status, headers: NO_STORE },
    );
  const empty = { name: "", size: String(DEFAULT_CREW_SIZE), tags: [] };
  if (!actions.isSameOrigin(request, getWebsiteConfig().publicOrigin)) {
    return reply(403, empty, {
      generalError: "This page is out of date. Reload and try again.",
    });
  }
  const form = await actions.parseBoundedFormData(request, {
    ...actions.DEFAULT_FORM_LIMITS,
    maxFields: 3 + MAX_CREW_SIZE,
  });
  const idempotencyKey = form?.["idempotencyKey"] ?? "";
  if (form === null || !actions.isIdempotencyKey(idempotencyKey)) {
    return reply(400, empty, { generalError: "That form could not be read. Try again." });
  }
  const tags = Object.keys(form)
    .filter((field) => field.startsWith(JOIN_FIELD))
    .map((field) => normalizePlayerTag(field.slice(JOIN_FIELD.length)))
    .filter((tag): tag is string => tag !== null);
  const values = { name: form["name"] ?? "", size: form["size"] ?? "", tags };
  const name = normalizeGroupName(values.name);
  const size = Number(values.size);
  const fieldErrors: NewCrewActionData["fieldErrors"] = {};
  if (name === null) fieldErrors.name = "Crew name must be 1–80 characters.";
  else if (isInappropriateName(values.name))
    fieldErrors.name = "Choose a different crew name.";
  if (!Number.isInteger(size) || size < MIN_CREW_SIZE || size > MAX_CREW_SIZE) {
    fieldErrors.size = `Places must be a whole number from ${MIN_CREW_SIZE} to ${MAX_CREW_SIZE}.`;
  }
  if (tags.length === 0) fieldErrors.accounts = "Pick at least one account.";
  else if (Number.isInteger(size) && tags.length > size) {
    fieldErrors.accounts = `Pick at most ${size} accounts, or add places.`;
  }
  if (Object.keys(fieldErrors).length > 0) return reply(400, values, { fieldErrors });

  const { createCrew, crewErrorCode } = await import("../services/crews.server");
  const { checkPlayerTag } = await import("../services/group-players.server");
  const { clientAddressContext } = await import("../server/client-address.server");
  let key = idempotencyKey;
  // Each retry follows a check of one more picked account, so this ends.
  for (let attempt = 0; attempt <= tags.length; attempt += 1) {
    try {
      const crewId = await createCrew(
        identity,
        { name: name as string, size, tags },
        key,
      );
      throw redirect(`/crews/${crewId}`);
    } catch (cause) {
      if (cause instanceof Response) throw cause;
      await setupRedirect(request, cause);
      const code = crewErrorCode(cause);
      const tag = (cause as { payload?: { tag?: unknown } }).payload?.tag;
      const status = (cause as { status?: number }).status ?? 503;
      if (
        code === "player_not_checked" &&
        typeof tag === "string" &&
        attempt < tags.length
      ) {
        const lookup = await checkPlayerTag(context?.get(clientAddressContext), tag);
        if (lookup.state !== "checking") {
          key = actions.freshIdempotencyKey();
          continue;
        }
      }
      const refusal = createRefusal(code, typeof tag === "string" ? tag : "");
      if (refusal !== null) return reply(status, values, refusal);
      const { safeWebsiteError } = await import("../server/errors.server");
      return reply(status, values, { generalError: safeWebsiteError(cause) });
    }
  }
  return reply(409, values, {
    fieldErrors: { accounts: "Still checking your accounts. Press Create crew again." },
  });
}

/** The message for a refused create, where it has one. */
function createRefusal(
  code: string | null,
  tag: string,
): Pick<NewCrewActionData, "fieldErrors" | "generalError"> | null {
  const accounts: Record<string, string> = {
    player_not_linked: `${tag} is not linked to your account.`,
    player_not_in_legend: `${tag} is not in Legend League, so it can't join.`,
    player_not_checked: `Still checking ${tag} with Clash of Clans. Press Create crew again in a few seconds.`,
    crew_full: "Pick fewer accounts, or add places.",
  };
  if (code !== null && code in accounts) {
    return { fieldErrors: { accounts: accounts[code] }, generalError: null };
  }
  if (code === "invalid_crew_name") {
    return { fieldErrors: { name: "Choose a different crew name." }, generalError: null };
  }
  if (code === "invalid_crew_size") {
    return {
      fieldErrors: { size: `Places must be from ${MIN_CREW_SIZE} to ${MAX_CREW_SIZE}.` },
      generalError: null,
    };
  }
  if (code === "crew_limit_reached") {
    return { fieldErrors: {}, generalError: `You're already in ${MAX_CREWS} crews.` };
  }
  return null;
}

export default function NewCrewRoute() {
  const loaderData = useLoaderData<typeof loader>();
  const actionData = useActionData<typeof action>();
  const navigation = useNavigation();
  const sizeInput = useRef<HTMLInputElement>(null);
  const creating = navigation.state === "submitting";
  const { accounts, crewCount } = loaderData;
  const errors = actionData?.fieldErrors ?? {};
  const firstJoinable = accounts.find(canJoin)?.tag;
  const picked = (tag: string) =>
    actionData ? actionData.values.tags.includes(tag) : tag === firstJoinable;
  const step = (by: number) => {
    const input = sizeInput.current;
    if (input === null) return;
    const current = Number(input.value) || DEFAULT_CREW_SIZE;
    input.value = String(Math.min(MAX_CREW_SIZE, Math.max(MIN_CREW_SIZE, current + by)));
  };
  const generalError = actionData?.generalError;

  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
      <h1>Create a crew</h1>
      {loaderData.error ? <ErrorNotice error={loaderData.error} /> : null}
      {typeof generalError === "string" ? (
        <p className="notice" role="alert">
          {generalError}
        </p>
      ) : generalError ? (
        <ErrorNotice error={generalError} />
      ) : null}
      {loaderData.error ? null : crewCount >= MAX_CREWS ? (
        <p className="notice">You're in {crewCount} crews, the most you can be in.</p>
      ) : accounts.length === 0 ? (
        <p className="notice crew-notice-action">
          <span>Link a Clash of Clans account to make a crew.</span>
          <Link className="button button-primary" to="/account/verify-player">
            Link an account
          </Link>
        </p>
      ) : (
        <Form method="post" className="form-panel stack-form crew-form" replace>
          <input
            type="hidden"
            name="idempotencyKey"
            value={actionData?.idempotencyKey ?? loaderData.idempotencyKey}
          />
          <div className="form-field">
            <label htmlFor="crew-name">Crew name</label>
            <input
              id="crew-name"
              name="name"
              type="text"
              maxLength={80}
              autoComplete="off"
              required
              defaultValue={actionData?.values.name ?? ""}
              aria-invalid={errors.name ? true : undefined}
              aria-describedby={errors.name ? "crew-name-error" : undefined}
            />
            {errors.name ? (
              <p id="crew-name-error" className="field-error" role="alert">
                {errors.name}
              </p>
            ) : null}
          </div>
          <div className="form-field">
            <label htmlFor="crew-size">Places</label>
            <div className="crew-stepper">
              <button
                type="button"
                className="button secondary"
                aria-label="5 fewer places"
                onClick={() => step(-5)}
              >
                −
              </button>
              <input
                ref={sizeInput}
                id="crew-size"
                name="size"
                type="number"
                inputMode="numeric"
                min={MIN_CREW_SIZE}
                max={MAX_CREW_SIZE}
                required
                defaultValue={actionData?.values.size ?? String(DEFAULT_CREW_SIZE)}
                aria-invalid={errors.size ? true : undefined}
                aria-describedby={errors.size ? "crew-size-error" : "crew-size-help"}
              />
              <button
                type="button"
                className="button secondary"
                aria-label="5 more places"
                onClick={() => step(5)}
              >
                +
              </button>
            </div>
            {errors.size ? (
              <p id="crew-size-error" className="field-error" role="alert">
                {errors.size}
              </p>
            ) : (
              <p id="crew-size-help" className="form-help">
                One per Clash of Clans account, up to {MAX_CREW_SIZE}
              </p>
            )}
          </div>
          <fieldset
            className="crew-pick"
            aria-describedby={errors.accounts ? "crew-accounts-error" : undefined}
          >
            <legend>Accounts that join</legend>
            <ul>
              {accounts.map((account) => {
                const joinable = canJoin(account);
                return (
                  <li key={account.tag}>
                    <label className={joinable ? undefined : "crew-pick-off"}>
                      <input
                        type="checkbox"
                        name={`${JOIN_FIELD}${account.tag}`}
                        disabled={!joinable}
                        defaultChecked={joinable && picked(account.tag)}
                      />
                      <span className="crew-who">
                        <b>{account.name ?? account.tag}</b>
                        <span>
                          {account.tag}
                          {joinable ? null : ` · Not in Legend League`}
                        </span>
                      </span>
                      {joinable && account.trophies !== null ? (
                        <span className="crew-value">
                          <TrophyMark />
                          {account.trophies.toLocaleString("en-US")}
                        </span>
                      ) : null}
                    </label>
                  </li>
                );
              })}
            </ul>
            {errors.accounts ? (
              <p id="crew-accounts-error" className="field-error" role="alert">
                {errors.accounts}
              </p>
            ) : null}
          </fieldset>
          <button type="submit" className="button button-primary" disabled={creating}>
            {creating ? "Creating crew…" : "Create crew"}
          </button>
        </Form>
      )}
    </main>
  );
}
