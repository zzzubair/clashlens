import {
  data,
  Form,
  Link,
  redirect,
  useActionData,
  useLoaderData,
  useNavigation,
} from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import type { ListedGroup } from "../lib/account-contracts";
import {
  isInappropriateName,
  MAX_GROUP_TAGS,
  normalizeGroupName,
  normalizeSubmittedPlayerTag,
} from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { addedNotice, GROUP_LIMIT } from "../lib/group-text";
import { canonicalPlayerPath } from "../lib/player-tag";
import type { Route } from "./+types/account.groups.add";
import "../account-groups.css";

const NO_STORE = { "Cache-Control": "no-store" };

/** One of the account's groups as a place the player can go. */
interface GroupChoice {
  groupId: string;
  name: string;
  count: number;
  /** The player is already in this group. */
  member: boolean;
}

export interface AddToGroupLoaderData {
  /** The player to add, or null when the address holds no readable tag. */
  tag: string | null;
  groups: GroupChoice[];
  addIdempotencyKey: string;
  createIdempotencyKey: string;
  error: WebsiteErrorResponse | null;
}

export interface AddToGroupActionData {
  notice: string | null;
  /** The group the player just joined, for its link. */
  addedTo: string | null;
  fieldErrors: { name?: string; tag?: string };
  generalError: WebsiteErrorResponse | null;
  addIdempotencyKey: string;
  createIdempotencyKey: string;
  values: { name: string; groupId: string };
}

function choices(groups: ListedGroup[], tag: string): GroupChoice[] {
  return groups.map((group) => ({
    groupId: group.groupId,
    name: group.name,
    count: group.tags.length,
    member: group.tags.includes(tag),
  }));
}

/**
 * GET /account/groups/add?tag= — where a player is saved: into the only
 * group, a chosen one of several, or a first group created on the spot.
 */
export async function loader({ request }: Route.LoaderArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  const tag = normalizeSubmittedPlayerTag(
    new URL(request.url).searchParams.get("tag") ?? "",
  );
  const loaded: AddToGroupLoaderData = {
    tag,
    groups: [],
    addIdempotencyKey: freshIdempotencyKey(),
    createIdempotencyKey: freshIdempotencyKey(),
    error: null,
  };
  if (tag === null) return data(loaded, { status: 400, headers: NO_STORE });
  try {
    const { createPythonClient } = await import("../services/python.server");
    const { groups } = await createPythonClient(identity).listGroups();
    return data({ ...loaded, groups: choices(groups, tag) }, { headers: NO_STORE });
  } catch (cause) {
    const actions = await import("../server/actions.server");
    if (actions.isAccountNotFoundError(cause)) {
      const { accountSetupPath } = await import("../server/return-path.server");
      const url = new URL(request.url);
      throw redirect(accountSetupPath(url.pathname, url));
    }
    const { safeWebsiteError } = await import("../server/errors.server");
    return data({ ...loaded, error: safeWebsiteError(cause) }, { headers: NO_STORE });
  }
}

/**
 * POST /account/groups/add — add the player to a chosen group, or, for an
 * account with no groups, create the first one and add the player to it.
 * Same-origin only, with a canonical idempotency UUID for each change.
 */
export async function action({ request, context }: Route.ActionArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const players = await import("../services/group-players.server");
  const adding = await import("../services/group-add.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  const form = actions.isSameOrigin(request, getWebsiteConfig().publicOrigin)
    ? await actions.parseBoundedFormData(request)
    : null;
  const values = { name: form?.["name"] ?? "", groupId: form?.["groupId"] ?? "" };
  const reply = (status: number, outcome: Partial<AddToGroupActionData>) =>
    data<AddToGroupActionData>(
      {
        notice: null,
        addedTo: null,
        fieldErrors: {},
        generalError: null,
        addIdempotencyKey: actions.freshIdempotencyKey(),
        createIdempotencyKey: actions.freshIdempotencyKey(),
        values,
        ...outcome,
      },
      { status, headers: NO_STORE },
    );
  const tag = normalizeSubmittedPlayerTag(form?.["tag"] ?? "");
  const addKey = form?.["addIdempotencyKey"] ?? "";
  const createKey = form?.["createIdempotencyKey"] ?? "";
  const creating = form?.["action"] === "create";
  if (
    form === null ||
    tag === null ||
    !actions.isIdempotencyKey(addKey) ||
    (creating && !actions.isIdempotencyKey(createKey))
  ) {
    return reply(400, {
      generalError: {
        error: {
          code: "invalid_input",
          message: "This page is out of date. Reload and try again.",
        },
      },
    });
  }

  const { clientAddressContext } = await import("../server/client-address.server");
  const clientAddress = context?.get(clientAddressContext);
  try {
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient(identity);
    const { groups } = await client.listGroups();
    let group: ListedGroup | undefined;
    if (creating) {
      // The form only offers a new group to an account that has none.
      if (groups.length > 0) {
        return reply(409, { fieldErrors: { tag: "Choose one of your groups below." } });
      }
      const name = normalizeGroupName(values.name);
      if (name === null || isInappropriateName(values.name)) {
        return reply(400, {
          fieldErrors: { name: "Choose a group name of 1–80 characters." },
        });
      }
      // Check the player first, so a mistyped tag leaves no empty group behind.
      const refusal = adding.lookupRefusal(
        await players.checkPlayerTag(clientAddress, tag),
        tag,
      );
      if (refusal !== null)
        return reply(refusal.status, { fieldErrors: { tag: refusal.tagError } });
      const created = await client.createGroup({ name }, createKey);
      group = { ...created, players: [] };
    } else {
      group = groups.find((row) => row.groupId === values.groupId);
      if (group === undefined) {
        return reply(values.groupId === "" ? 400 : 404, {
          fieldErrors: {
            tag:
              values.groupId === "" ? "Choose a group." : "That group no longer exists.",
          },
        });
      }
    }
    const added = await adding.addToGroup(identity, clientAddress, group, tag, addKey);
    if (!("player" in added)) {
      const tagError = creating
        ? `Created ${group.name}, but: ${added.tagError}`
        : added.tagError;
      return reply(added.status, { fieldErrors: { tag: tagError } });
    }
    return reply(200, {
      notice: addedNotice(added.player, group.name),
      addedTo: group.groupId,
    });
  } catch (cause) {
    if (cause instanceof Response) throw cause;
    if (actions.isAccountNotFoundError(cause)) throw redirect("/account/setup");
    const { status, payload } = cause as {
      status?: number;
      payload?: { error?: unknown };
    };
    const code = payload?.error;
    const tagError = adding.groupTagError(code, tag);
    if (tagError !== null)
      return reply(status ?? 422, { fieldErrors: { tag: tagError } });
    if (code === "group_name_conflict") {
      return reply(409, {
        fieldErrors: { name: "A group with this name already exists." },
      });
    }
    if (code === "group_limit_reached")
      return reply(422, { fieldErrors: { name: GROUP_LIMIT } });
    const { safeWebsiteError } = await import("../server/errors.server");
    return reply(503, { generalError: safeWebsiteError(cause) });
  }
}

export function headers() {
  return NO_STORE;
}

export default function AddToGroupRoute() {
  const loaderData = useLoaderData<typeof loader>();
  const actionData = useActionData<AddToGroupActionData>();
  const navigation = useNavigation();
  const busy = navigation.state === "submitting";
  const { tag, groups } = loaderData;
  const addKey = actionData?.addIdempotencyKey ?? loaderData.addIdempotencyKey;
  const createKey = actionData?.createIdempotencyKey ?? loaderData.createIdempotencyKey;
  const errors = actionData?.fieldErrors ?? {};
  const open = groups.filter((group) => !group.member && group.count < MAX_GROUP_TAGS);
  const only = groups.length === 1 ? groups[0] : undefined;

  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="add-title">
        <h1 id="add-title">
          {tag === null ? "Add a player to a group" : `Add ${tag} to a group`}
        </h1>
        <p className="lede">
          Players are saved in your private groups. Only you can see them.
        </p>
      </section>

      {actionData?.generalError ? <ErrorNotice error={actionData.generalError} /> : null}
      {actionData?.notice ? (
        <p className="add-player-done" role="status">
          {actionData.notice}{" "}
          <Link to={`/account/groups#group-${actionData.addedTo}`}>Open your groups</Link>
        </p>
      ) : null}

      {tag === null ? (
        <aside className="notice notice-unavailable" role="alert">
          <strong>That player tag could not be read.</strong> Open a player and choose Add
          to group again.
        </aside>
      ) : loaderData.error ? (
        <aside className="notice notice-unavailable" role="alert">
          <strong>Your groups could not be loaded.</strong>{" "}
          <a href={`/account/groups/add?tag=${encodeURIComponent(tag)}`}>Try again</a>
        </aside>
      ) : (
        <section className="form-panel" aria-labelledby="add-form-title">
          <Form method="post" className="stack-form">
            <input type="hidden" name="tag" value={tag} />
            <input type="hidden" name="addIdempotencyKey" value={addKey} />
            {groups.length === 0 ? (
              <>
                <h2 id="add-form-title">Create your first group</h2>
                <input type="hidden" name="action" value="create" />
                <input type="hidden" name="createIdempotencyKey" value={createKey} />
                <div className="form-field">
                  <label htmlFor="add-group-name">Group name</label>
                  <input
                    id="add-group-name"
                    name="name"
                    type="text"
                    autoComplete="off"
                    required
                    defaultValue={actionData?.values.name ?? ""}
                    aria-invalid={errors.name ? true : undefined}
                    aria-describedby="add-group-name-message"
                  />
                  <p
                    id="add-group-name-message"
                    className={errors.name ? "field-error" : "form-help"}
                    role={errors.name ? "alert" : undefined}
                  >
                    {errors.name ??
                      `You have no groups yet. Name one and ${tag} goes straight into it.`}
                  </p>
                </div>
                <button type="submit" className="button button-primary" disabled={busy}>
                  {busy ? "Adding…" : "Create group and add"}
                </button>
              </>
            ) : only ? (
              <>
                <h2 id="add-form-title">{only.name}</h2>
                <input type="hidden" name="action" value="add" />
                <input type="hidden" name="groupId" value={only.groupId} />
                <p className="form-help">
                  {only.member
                    ? `${tag} is already in ${only.name}, your only group.`
                    : only.count >= MAX_GROUP_TAGS
                      ? `${only.name} already has ${MAX_GROUP_TAGS} players, the most a group holds. Remove a player there to add another.`
                      : `${only.name} is your only group, so ${tag} goes there.`}
                </p>
                <button
                  type="submit"
                  className="button button-primary"
                  disabled={busy || open.length === 0}
                >
                  {busy ? "Adding…" : `Add to ${only.name}`}
                </button>
              </>
            ) : (
              <>
                <h2 id="add-form-title">Choose a group</h2>
                <fieldset className="group-choices" aria-labelledby="add-form-title">
                  <input type="hidden" name="action" value="add" />
                  {groups.map((group) => {
                    const full = group.count >= MAX_GROUP_TAGS;
                    return (
                      <label key={group.groupId} className="group-choice">
                        <input
                          type="radio"
                          name="groupId"
                          value={group.groupId}
                          required
                          disabled={group.member || full}
                          defaultChecked={actionData?.values.groupId === group.groupId}
                        />
                        <span>
                          <strong>{group.name}</strong>{" "}
                          <span className="group-member-detail">
                            {group.member
                              ? `${tag} is already in this group`
                              : full
                                ? `Full: ${MAX_GROUP_TAGS} of ${MAX_GROUP_TAGS} players`
                                : `${group.count} of ${MAX_GROUP_TAGS} players`}
                          </span>
                        </span>
                      </label>
                    );
                  })}
                  <button
                    type="submit"
                    className="button button-primary"
                    disabled={busy || open.length === 0}
                  >
                    {busy ? "Adding…" : "Add to group"}
                  </button>
                </fieldset>
              </>
            )}
            {errors.tag ? (
              <p className="field-error" role="alert">
                {errors.tag}
              </p>
            ) : null}
          </Form>
          <p>
            <Link to={canonicalPlayerPath(tag)}>
              Back to {tag} <span aria-hidden="true">→</span>
            </Link>
          </p>
        </section>
      )}
    </main>
  );
}
