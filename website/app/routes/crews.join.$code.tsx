import { useEffect, useState } from "react";
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
import { ErrorNotice } from "../components/ErrorNotice";
import { TrophyMark } from "../components/LeaderboardShared";
import type { WebsiteErrorResponse } from "../lib/contracts";
import {
  crewRefusal,
  formatInviteExpiry,
  INVITE_CODE,
  MAX_CREW_SIZE,
  MAX_CREWS,
  type InviteAccount,
  type InvitePreview,
} from "../lib/crew-contracts";
import { normalizePlayerTag } from "../lib/player-tag";
import type { Route } from "./+types/crews.join.$code";
import "../crews.css";

const NO_STORE = { "Cache-Control": "no-store" };
/** Each picked account is its own form field, named by this and its tag. */
const JOIN_FIELD = "join:";

export const handle: BackHandle = { back: { to: "/crews", label: "Crews" } };

export function meta() {
  return [{ title: "Crew invite · Clash Lens" }];
}

export interface JoinLoaderData {
  code: string;
  invite: InvitePreview | null;
  idempotencyKey: string;
  error: WebsiteErrorResponse | null;
}

export interface JoinActionData {
  idempotencyKey: string;
  tags: string[];
  error: string | WebsiteErrorResponse | null;
}

const ELIGIBILITY: Record<InviteAccount["eligibility"], string | null> = {
  ok: null,
  not_in_legend: "Not in Legend League",
  already_in_crew: "Already in this crew",
  unchecked: "Not checked yet",
};

/** Crews switched on, signed in (coming back here after), and a well-formed code. */
async function guard(request: Request, code: string | undefined) {
  const { isCrewsEnabled } = await import("../server/config.server");
  if (!isCrewsEnabled()) throw data(null, { status: 404 });
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  if (code === undefined || !INVITE_CODE.test(code)) throw data(null, { status: 404 });
  return { identity, code };
}

async function setupRedirect(request: Request, cause: unknown): Promise<void> {
  const { isAccountNotFoundError } = await import("../server/actions.server");
  if (isAccountNotFoundError(cause)) {
    const { accountSetupPath } = await import("../server/return-path.server");
    const url = new URL(request.url);
    throw redirect(accountSetupPath(url.pathname, url));
  }
}

/**
 * GET /crews/join/:code — the crew an invite link is for and which of the
 * clasher's accounts can join. Accounts Clash Lens hasn't checked yet are
 * checked with the game first.
 */
export async function loader({ request, params, context }: Route.LoaderArgs) {
  const { identity, code } = await guard(request, params.code);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  const idempotencyKey = freshIdempotencyKey();
  try {
    const { getInvite } = await import("../services/crews.server");
    let invite = await getInvite(identity, code);
    const unchecked = invite.accounts.filter(
      (account) => account.eligibility === "unchecked",
    );
    if (invite.state === "ok" && unchecked.length > 0) {
      const { checkPlayerTag } = await import("../services/group-players.server");
      const { clientAddressContext } = await import("../server/client-address.server");
      for (const account of unchecked) {
        await checkPlayerTag(context?.get(clientAddressContext), account.tag);
      }
      invite = await getInvite(identity, code);
    }
    return data<JoinLoaderData>(
      { code, invite, idempotencyKey, error: null },
      { headers: NO_STORE },
    );
  } catch (cause) {
    await setupRedirect(request, cause);
    const { safeWebsiteError } = await import("../server/errors.server");
    return data<JoinLoaderData>(
      { code, invite: null, idempotencyKey, error: safeWebsiteError(cause) },
      { status: 503, headers: NO_STORE },
    );
  }
}

export function headers() {
  return NO_STORE;
}

/**
 * POST /crews/join/:code — join with the picked accounts. The private API
 * repeats every check and has the final say. The form keeps its key until
 * the private API gives a final answer, so pressing Join again after a lost
 * answer replays the same requests.
 */
export async function action({ request, params, context }: Route.ActionArgs) {
  const { identity, code } = await guard(request, params.code);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  const reply = (
    status: number,
    tags: string[],
    error: JoinActionData["error"],
    idempotencyKey = actions.freshIdempotencyKey(),
  ) =>
    data<JoinActionData>({ idempotencyKey, tags, error }, { status, headers: NO_STORE });
  if (!actions.isSameOrigin(request, getWebsiteConfig().publicOrigin)) {
    return reply(403, [], "This page is out of date. Reload and try again.");
  }
  const form = await actions.parseBoundedFormData(request, {
    ...actions.DEFAULT_FORM_LIMITS,
    maxFields: 1 + MAX_CREW_SIZE,
  });
  const idempotencyKey = form?.["idempotencyKey"] ?? "";
  if (form === null || !actions.isIdempotencyKey(idempotencyKey)) {
    return reply(400, [], "That form could not be read. Try again.");
  }
  const tags = Object.keys(form)
    .filter((field) => field.startsWith(JOIN_FIELD))
    .map((field) => normalizePlayerTag(field.slice(JOIN_FIELD.length)))
    .filter((tag): tag is string => tag !== null);
  if (tags.length === 0) {
    return reply(400, tags, "Pick at least one account.", idempotencyKey);
  }
  const { acceptInvite, crewErrorCode, joinCheckingAccounts } =
    await import("../services/crews.server");
  const { clientAddressContext } = await import("../server/client-address.server");
  const joined = await joinCheckingAccounts(
    context?.get(clientAddressContext),
    idempotencyKey,
    tags.length,
    (key) => acceptInvite(identity, code, tags, key),
  );
  if (joined.ok) throw redirect(`/crews/${joined.value}`);
  const { cause, unfinished } = joined;
  await setupRedirect(request, cause);
  const status = (cause as { status?: number }).status ?? 503;
  const formKey = unfinished ? idempotencyKey : undefined;
  const message = crewRefusal(
    crewErrorCode(cause),
    (cause as { payload?: object }).payload,
  );
  if (message !== null) return reply(status, tags, message, formKey);
  const { safeWebsiteError } = await import("../server/errors.server");
  return reply(status, tags, safeWebsiteError(cause), formKey);
}

export default function JoinCrewRoute() {
  const { code, invite, idempotencyKey, error } = useLoaderData<typeof loader>();
  const answer = useActionData<typeof action>();
  if (invite === null) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
        <h1>Crew invite</h1>
        {error ? <ErrorNotice error={error} /> : null}
      </main>
    );
  }
  if (invite.state === "invalid") {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
        <h1>This invite link doesn&apos;t work</h1>
        <p className="notice">
          It expired or was turned off. Ask the crew for a new one.
        </p>
      </main>
    );
  }
  const open = Math.max(0, invite.size - invite.used);
  const linkAccount = `/account/verify-player?return=/crews/join/${code}`;
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
      <p className="eyebrow">Crew invite</p>
      <h1>{invite.name}</h1>
      <p className="crew-meta">
        {invite.ownerName ? `Owner: ${invite.ownerName} · ` : null}
        {invite.used} of {invite.size} places · Works until{" "}
        {formatInviteExpiry(invite.expiresAt)}
      </p>
      {invite.inCrew ? (
        <p className="notice crew-notice-action">
          <span>You&apos;re in this crew.</span>
          <Link className="button secondary" to={`/crews/${invite.crewId}`}>
            Open crew
          </Link>
        </p>
      ) : null}
      {invite.state === "full" ? (
        <p className="notice">This crew is full.</p>
      ) : invite.state === "limit" ? (
        <p className="notice crew-notice-action">
          <span>You&apos;re already in {MAX_CREWS} crews, the most you can be in.</span>
          <Link className="button secondary" to="/crews">
            See my crews
          </Link>
        </p>
      ) : invite.accounts.length === 0 ? (
        <p className="notice crew-notice-action">
          <span>Link a Clash of Clans account to join.</span>
          <Link className="button button-primary" to={linkAccount}>
            Link an account
          </Link>
        </p>
      ) : (
        <JoinForm
          accounts={invite.accounts}
          open={open}
          idempotencyKey={answer?.idempotencyKey ?? idempotencyKey}
          answer={answer}
          linkAccount={linkAccount}
        />
      )}
    </main>
  );
}

function JoinForm({
  accounts,
  open,
  idempotencyKey,
  answer,
  linkAccount,
}: {
  accounts: InviteAccount[];
  open: number;
  idempotencyKey: string;
  answer: JoinActionData | undefined;
  linkAccount: string;
}) {
  const navigation = useNavigation();
  const joinable = accounts.filter((account) => account.eligibility === "ok");
  const [picked, setPicked] = useState<string[]>(() =>
    answer ? answer.tags : joinable.length > 0 && open > 0 ? [joinable[0]!.tag] : [],
  );
  // Without JavaScript the picks can't be counted here; the server checks them.
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => setHydrated(true), []);
  const count = picked.length;
  const tooMany = count > open;
  const toggle = (tag: string, on: boolean) =>
    setPicked((current) =>
      on ? [...current, tag] : current.filter((value) => value !== tag),
    );
  const error = answer?.error;
  return (
    <Form method="post" className="form-panel stack-form crew-form" replace>
      <input type="hidden" name="idempotencyKey" value={idempotencyKey} />
      {typeof error === "string" ? (
        <p className="field-error" role="alert">
          {error}
        </p>
      ) : error ? (
        <ErrorNotice error={error} />
      ) : null}
      <fieldset className="crew-pick">
        <legend>
          Which accounts join? {open} {open === 1 ? "place" : "places"} open
        </legend>
        <ul>
          {accounts.map((account) => {
            const why = ELIGIBILITY[account.eligibility];
            return (
              <li key={account.tag}>
                <label className={why === null ? undefined : "crew-pick-off"}>
                  <input
                    type="checkbox"
                    name={`${JOIN_FIELD}${account.tag}`}
                    disabled={why !== null}
                    checked={why === null && picked.includes(account.tag)}
                    onChange={(event) => toggle(account.tag, event.currentTarget.checked)}
                  />
                  <span className="crew-who">
                    <b>{account.name ?? account.tag}</b>
                    <span>
                      {account.tag}
                      {why === null ? null : ` · ${why}`}
                    </span>
                  </span>
                  {why === null && account.trophies !== null ? (
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
      </fieldset>
      <button
        type="submit"
        className="button button-primary"
        disabled={
          (hydrated && (count === 0 || tooMany)) || navigation.state === "submitting"
        }
      >
        {!hydrated
          ? "Join with the picked accounts"
          : count === 0
          ? "Pick at least one account"
          : tooMany
            ? `Pick at most ${open}`
            : `Join with ${count} ${count === 1 ? "account" : "accounts"}`}
      </button>
      <Link className="crew-link-button" to={linkAccount}>
        Link another account
      </Link>
    </Form>
  );
}
