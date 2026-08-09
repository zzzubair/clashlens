import { Link, data, redirect, useActionData, useLoaderData } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import type { SavedPlayer } from "../lib/account-contracts";
import { normalizeSubmittedPlayerTag } from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { canonicalPlayerPath } from "../lib/player-tag";
import type { Route } from "./+types/account.saved-players";

const NO_STORE = { "Cache-Control": "no-store" };

export interface SavedPlayersLoaderData {
  players: SavedPlayer[];
  /** Fresh idempotency key for the add form. */
  addIdempotencyKey: string;
  /** Fresh per-player idempotency keys for the remove forms. */
  removeIdempotencyKeys: Record<string, string>;
  error: WebsiteErrorResponse | null;
}

export interface SavedPlayersActionData {
  mode: "add" | "remove";
  /** The player tag the fresh remove key belongs to. */
  tag: string | null;
  /** Fresh idempotency key for the next add attempt. */
  addIdempotencyKey: string;
  /** Fresh idempotency key for the next remove attempt on `tag`. */
  removeIdempotencyKey: string;
  fieldErrors: { tag?: string };
  generalError: WebsiteErrorResponse | null;
  values: { tag: string; mode: string };
}

/**
 * GET /account/saved-players — list saved public player tags with explicit
 * add and per-player remove forms, each bound to its own idempotency key.
 */
export async function loader({
  request,
}: Route.LoaderArgs): Promise<SavedPlayersLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  try {
    const { createPythonClient } = await import("../services/python.server");
    const players = await createPythonClient(identity).listSavedTags();
    const removeIdempotencyKeys: Record<string, string> = {};
    for (const player of players) {
      removeIdempotencyKeys[player.tag] = freshIdempotencyKey();
    }
    return {
      players,
      addIdempotencyKey: freshIdempotencyKey(),
      removeIdempotencyKeys,
      error: null,
    };
  } catch (cause) {
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(cause)) throw redirect("/account/setup");
    const { safeWebsiteError } = await import("../server/errors.server");
    return {
      players: [],
      addIdempotencyKey: freshIdempotencyKey(),
      removeIdempotencyKeys: {},
      error: safeWebsiteError(cause),
    };
  }
}

/**
 * POST /account/saved-players — add or remove one normalized player tag.
 * The action mode is explicit, the request is same-origin with a canonical
 * idempotency UUID, and a failed attempt returns a fresh key for the retry.
 */
export async function action({ request }: Route.ActionArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");

  const config = getWebsiteConfig();
  if (!actions.isSameOrigin(request, config.publicOrigin)) {
    return errorResponse(403, {
      error: { code: "forbidden", message: "This action is not allowed." },
    });
  }
  const form = await actions.parseBoundedFormData(request);
  if (form === null) return invalidFormResponse();
  const idempotencyKey = form["idempotencyKey"] ?? "";
  if (!actions.isIdempotencyKey(idempotencyKey)) return invalidFormResponse();

  const mode = form["mode"] ?? "";
  const rawTag = form["tag"] ?? "";
  if (mode !== "add" && mode !== "remove") {
    return errorResponse(400, {
      error: {
        code: "invalid_input",
        message: "Check the submitted value and try again.",
      },
    });
  }
  const tag = normalizeSubmittedPlayerTag(rawTag);
  if (tag === null) {
    return data<SavedPlayersActionData>(
      {
        mode,
        tag: null,
        addIdempotencyKey: actions.freshIdempotencyKey(),
        removeIdempotencyKey: actions.freshIdempotencyKey(),
        fieldErrors: { tag: "Enter a valid player tag." },
        generalError: null,
        values: { tag: rawTag, mode },
      },
      { status: 400, headers: NO_STORE },
    );
  }

  try {
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient(identity);
    if (mode === "add") await client.addSavedTag(tag, idempotencyKey);
    else await client.removeSavedTag(tag, idempotencyKey);
  } catch (cause) {
    if (actions.isAccountNotFoundError(cause)) throw redirect("/account/setup");
    const { safeWebsiteError } = await import("../server/errors.server");
    return data<SavedPlayersActionData>(
      {
        mode,
        tag,
        addIdempotencyKey: actions.freshIdempotencyKey(),
        removeIdempotencyKey: actions.freshIdempotencyKey(),
        fieldErrors: {},
        generalError: safeWebsiteError(cause),
        values: { tag: rawTag, mode },
      },
      { status: 422, headers: NO_STORE },
    );
  }
  throw redirect("/account/saved-players");
}

async function errorResponse(status: number, generalError: WebsiteErrorResponse) {
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return data<SavedPlayersActionData>(
    {
      mode: "add",
      tag: null,
      addIdempotencyKey: freshIdempotencyKey(),
      removeIdempotencyKey: freshIdempotencyKey(),
      fieldErrors: {},
      generalError,
      values: { tag: "", mode: "add" },
    },
    { status, headers: NO_STORE },
  );
}

async function invalidFormResponse() {
  return errorResponse(400, {
    error: {
      code: "invalid_input",
      message: "Check the submitted value and try again.",
    },
  });
}

export function headers() {
  return NO_STORE;
}

export default function SavedPlayersRoute() {
  const loaderData = useLoaderData<typeof loader>();
  const actionData = useActionData<SavedPlayersActionData>();
  const addKey =
    actionData && actionData.mode === "add"
      ? actionData.addIdempotencyKey
      : loaderData.addIdempotencyKey;

  return (
    <main className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="saved-title">
        <h1 id="saved-title">Saved players</h1>
        <p className="lede">
          Saved tags are public player shortcuts. Saving a player does not claim
          ownership.
        </p>
      </section>

      {loaderData.error ? <ErrorNotice error={loaderData.error} /> : null}
      {actionData?.generalError ? <ErrorNotice error={actionData.generalError} /> : null}

      <section className="form-panel" aria-label="Add a saved player">
        <h2>Add a player</h2>
        <form method="post" className="stack-form">
          <input type="hidden" name="mode" value="add" />
          <input type="hidden" name="idempotencyKey" value={addKey} />
          <div className="form-field">
            <label htmlFor="saved-tag">Player tag</label>
            <input
              id="saved-tag"
              name="tag"
              type="text"
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              aria-invalid={actionData?.fieldErrors?.tag ? true : undefined}
              aria-describedby={
                actionData?.fieldErrors?.tag ? "saved-tag-error" : undefined
              }
              defaultValue={actionData?.mode === "add" ? actionData.values.tag : ""}
            />
            {actionData?.fieldErrors?.tag ? (
              <p id="saved-tag-error" className="field-error" role="alert">
                {actionData.fieldErrors.tag}
              </p>
            ) : (
              <p className="form-help">A public Clash of Clans player tag.</p>
            )}
          </div>
          <button type="submit" className="button button-primary">
            Save player
          </button>
        </form>
      </section>

      <section className="data-section" aria-labelledby="saved-list-title">
        <h2 id="saved-list-title">Your saved players</h2>
        {loaderData.players.length > 0 ? (
          <ul className="player-action-list">
            {loaderData.players.map((player) => {
              const removeKey =
                actionData?.mode === "remove" && actionData.tag === player.tag
                  ? actionData.removeIdempotencyKey
                  : loaderData.removeIdempotencyKeys[player.tag];
              return (
                <li key={player.tag}>
                  <span className="player-action-name">
                    <Link to={canonicalPlayerPath(player.tag)}>
                      {player.name ?? player.tag}
                    </Link>
                    <span className="player-tag">{player.tag}</span>
                  </span>
                  <form method="post" className="inline-form">
                    <input type="hidden" name="mode" value="remove" />
                    <input type="hidden" name="tag" value={player.tag} />
                    <input type="hidden" name="idempotencyKey" value={removeKey} />
                    <button type="submit" className="button button-secondary">
                      Remove
                    </button>
                  </form>
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="empty-state">
            <h3>No saved players yet</h3>
            <p>Add a player tag above to keep it one click away.</p>
          </div>
        )}
      </section>

      <p className="back-link">
        <Link to="/account">← Back to your account</Link>
      </p>
    </main>
  );
}
