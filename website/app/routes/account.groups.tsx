import { useEffect, useRef } from "react";
import {
  data,
  Form,
  redirect,
  useActionData,
  useFetcher,
  useLoaderData,
  useNavigation,
} from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import type { GroupPlayer, ListedGroup } from "../lib/account-contracts";
import {
  isInappropriateName,
  MAX_GROUP_TAGS,
  normalizeGroupName,
  normalizeSubmittedPlayerTag,
} from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { canonicalPlayerPath, MAX_PLAYER_TAG_INPUT_LENGTH } from "../lib/player-tag";
import { isCanonicalUuid } from "../lib/validation";
import type { Route } from "./+types/account.groups";
import "../account-groups.css";

const NO_STORE = { "Cache-Control": "no-store" };
const ACTIONS = ["create", "update", "delete", "add-player", "remove-player"] as const;
type GroupAction = (typeof ACTIONS)[number];

export interface GroupsLoaderData {
  groups: ListedGroup[];
  /** Fresh idempotency key for the create form. */
  createIdempotencyKey: string;
  /** Fresh per-group idempotency keys for the rename forms. */
  updateIdempotencyKeys: Record<string, string>;
  /** Fresh per-group idempotency keys for the delete forms. */
  deleteIdempotencyKeys: Record<string, string>;
  /** Fresh per-group idempotency keys for the add-player forms. */
  addIdempotencyKeys: Record<string, string>;
  /** Fresh idempotency keys for each member's remove button, by group then tag. */
  removeIdempotencyKeys: Record<string, Record<string, string>>;
  error: WebsiteErrorResponse | null;
}

export interface GroupsActionData {
  action: GroupAction;
  /** The group the fresh group keys belong to (all but create). */
  groupId: string | null;
  /** Fresh idempotency key for the next create attempt. */
  createIdempotencyKey: string;
  /** Fresh idempotency key for the next rename attempt on `groupId`. */
  updateIdempotencyKey: string;
  /** Fresh idempotency key for the next delete attempt on `groupId`. */
  deleteIdempotencyKey: string;
  /** Fresh idempotency key for the next add or remove on `groupId`. */
  playerIdempotencyKey: string;
  fieldErrors: { name?: string; tag?: string; confirm?: string };
  /** What happened to the player just added or removed. */
  notice: string | null;
  generalError: WebsiteErrorResponse | null;
  values: { name: string; tag: string; action: string; groupId: string };
}

/**
 * GET /account/groups — list only the signed-in account's private groups with
 * explicit create, rename, add-player, remove-player and confirmed-delete
 * forms, each bound to its own idempotency key.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<GroupsLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  const empty = {
    groups: [],
    createIdempotencyKey: freshIdempotencyKey(),
    updateIdempotencyKeys: {},
    deleteIdempotencyKeys: {},
    addIdempotencyKeys: {},
    removeIdempotencyKeys: {},
    error: null,
  };
  try {
    const { createPythonClient } = await import("../services/python.server");
    const groups = await createPythonClient(identity).listGroups();
    const loaded: GroupsLoaderData = { ...empty, groups };
    for (const group of groups) {
      loaded.updateIdempotencyKeys[group.groupId] = freshIdempotencyKey();
      loaded.deleteIdempotencyKeys[group.groupId] = freshIdempotencyKey();
      loaded.addIdempotencyKeys[group.groupId] = freshIdempotencyKey();
      loaded.removeIdempotencyKeys[group.groupId] = Object.fromEntries(
        group.players.map((player) => [player.tag, freshIdempotencyKey()]),
      );
    }
    return loaded;
  } catch (cause) {
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(cause)) {
      const { accountSetupPath } = await import("../server/return-path.server");
      const url = new URL(request.url);
      throw redirect(accountSetupPath(url.pathname, url));
    }
    const { safeWebsiteError } = await import("../server/errors.server");
    return { ...empty, error: safeWebsiteError(cause) };
  }
}

/**
 * POST /account/groups — create, rename, or delete a private group, or add or
 * remove one player. The action and group ID are explicit, deletion requires
 * a confirmation checkbox, and every mutation is same-origin with a
 * canonical idempotency UUID. A player joins only after the game confirms
 * the tag belongs to a real player.
 */
export async function action({ request, context }: Route.ActionArgs) {
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

  const actionMode = (form["action"] ?? "") as GroupAction;
  const groupId = form["groupId"] ?? "";
  if (!ACTIONS.includes(actionMode)) return invalidFormResponse();
  if (actionMode !== "create" && !isCanonicalUuid(groupId)) {
    return invalidFormResponse();
  }
  const values = {
    name: form["name"] ?? "",
    tag: form["tag"] ?? "",
    action: actionMode,
    groupId,
  };
  const reply = (status: number, outcome: Partial<GroupsActionData>) =>
    data<GroupsActionData>(
      {
        action: actionMode,
        groupId,
        createIdempotencyKey: actions.freshIdempotencyKey(),
        updateIdempotencyKey: actions.freshIdempotencyKey(),
        deleteIdempotencyKey: actions.freshIdempotencyKey(),
        playerIdempotencyKey: actions.freshIdempotencyKey(),
        fieldErrors: {},
        notice: null,
        generalError: null,
        values,
        ...outcome,
      },
      { status, headers: NO_STORE },
    );
  if (actionMode === "delete" && form["confirm"] !== "on") {
    return reply(400, { fieldErrors: { confirm: "Confirm the deletion to continue." } });
  }

  const normalizedName = normalizeGroupName(values.name);
  if (actionMode === "create" || actionMode === "update") {
    if (normalizedName === null) {
      return reply(400, {
        fieldErrors: {
          name: "Group name must be 1–80 characters and must not contain control characters.",
        },
      });
    }
    if (isInappropriateName(values.name)) {
      return reply(400, { fieldErrors: { name: "Choose a different group name." } });
    }
  }
  const tag = normalizeSubmittedPlayerTag(values.tag);
  if ((actionMode === "add-player" || actionMode === "remove-player") && tag === null) {
    return reply(400, { fieldErrors: { tag: INVALID_TAG } });
  }

  try {
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient(identity);
    const players = await import("../services/group-players.server");
    if (actionMode === "create") {
      const created = await client.createGroup(
        { name: normalizedName as string },
        idempotencyKey,
      );
      throw redirect(`/account/groups#group-${created.groupId}`);
    } else if (actionMode === "update") {
      await client.updateGroup(
        groupId,
        { name: normalizedName as string },
        idempotencyKey,
      );
    } else if (actionMode === "delete") {
      await client.deleteGroup(groupId, idempotencyKey);
    } else if (actionMode === "remove-player") {
      await players.removeGroupPlayer(identity, groupId, tag as string, idempotencyKey);
      return reply(200, { notice: `Removed ${tag} from the group.` });
    } else {
      // Refuse duplicates and a full group before spending a player lookup.
      const group = (await client.listGroups()).find((row) => row.groupId === groupId);
      if (group === undefined) return reply(404, { generalError: GROUP_GONE });
      const member = group.players.find((player) => player.tag === tag);
      if (member !== undefined) {
        const who = member.name === null ? member.tag : `${member.name} (${member.tag})`;
        return reply(409, { fieldErrors: { tag: `${who} is already in this group.` } });
      }
      if (group.tags.length >= MAX_GROUP_TAGS) {
        return reply(422, { fieldErrors: { tag: GROUP_FULL } });
      }
      const { clientAddressContext } = await import("../server/client-address.server");
      const lookup = await players.checkPlayerTag(
        context?.get(clientAddressContext),
        tag as string,
      );
      if (lookup.state === "not_found") {
        return reply(422, { fieldErrors: { tag: notFound(tag as string) } });
      }
      if (lookup.state === "checking") {
        return reply(409, { fieldErrors: { tag: stillChecking(tag as string) } });
      }
      if (lookup.state === "failed" || lookup.state === "unknown") {
        return reply(503, {
          fieldErrors: {
            tag: `Clash of Clans could not be reached to check ${tag}. Try again in a minute.`,
          },
        });
      }
      const added = await players.addGroupPlayer(
        identity,
        groupId,
        tag as string,
        idempotencyKey,
      );
      return reply(200, { notice: addedNotice(added) });
    }
  } catch (cause) {
    if (cause instanceof Response) throw cause;
    if (actions.isAccountNotFoundError(cause)) throw redirect("/account/setup");
    const pythonError = cause as { status?: number; payload?: unknown };
    const code = isRecord(pythonError.payload) ? pythonError.payload.error : undefined;
    const tagError: Record<string, string> = {
      group_player_exists: `${tag} is already in this group.`,
      group_full: GROUP_FULL,
      player_not_found: notFound(tag ?? ""),
      player_not_checked: stillChecking(tag ?? ""),
      rate_limited:
        "Too many player checks from your connection. Wait a minute and try again.",
      invalid_tag: INVALID_TAG,
    };
    if (typeof code === "string" && code in tagError) {
      return reply(pythonError.status ?? 422, { fieldErrors: { tag: tagError[code] } });
    }
    if (pythonError.status === 409 && code === "group_name_conflict") {
      return reply(409, {
        fieldErrors: { name: "A group with this name already exists." },
      });
    }
    if (
      pythonError.status === 422 &&
      (actionMode === "create" || actionMode === "update")
    ) {
      return reply(422, {
        fieldErrors: { name: "This group was not accepted. Choose a different name." },
      });
    }
    const { safeWebsiteError } = await import("../server/errors.server");
    const safeError = safeWebsiteError(cause);
    return reply(422, {
      generalError:
        pythonError.status === 404 && code === "group_not_found"
          ? GROUP_GONE
          : actionMode === "create" &&
              (safeError.error.code === "unavailable" ||
                safeError.error.code === "malformed")
            ? {
                error: {
                  code: safeError.error.code,
                  message:
                    "Could not confirm the group was created. Refresh the page before trying again.",
                },
              }
            : safeError,
    });
  }
  throw redirect("/account/groups");
}

const INVALID_TAG =
  "Enter one valid player tag, like #2PY0LQ. Tags use only 0, 2, 8, 9 and the letters P Y L Q G R J C U V.";
const GROUP_FULL = `This group already has ${MAX_GROUP_TAGS} players, the most a comparison shows. Remove a player to add another.`;
const GROUP_GONE: WebsiteErrorResponse = {
  error: { code: "conflict", message: "The group no longer exists. Refresh the page." },
};

function notFound(tag: string): string {
  return `Clash of Clans has no player with the tag ${tag}. Check the tag and try again.`;
}

function stillChecking(tag: string): string {
  return `Still checking ${tag} with Clash of Clans. Press Add player again in a few seconds.`;
}

function addedNotice(player: GroupPlayer): string {
  const who = player.name === null ? player.tag : `${player.name} (${player.tag})`;
  if (player.state === "tracking") {
    return player.trophies === null
      ? `Added ${who}.`
      : `Added ${who}, ${player.trophies.toLocaleString("en")} trophies.`;
  }
  return `Added ${who}. ${STATE_LABELS[player.state]}.`;
}

/** What a member row says when the player is not tracked in Legend League. */
const STATE_LABELS: Record<GroupPlayer["state"], string> = {
  tracking: "",
  not_in_legend: "Not in Legend League, no data",
  uncertain: "Not confirmed in Legend League yet, no data",
  checking: "Looking up this player…",
  unknown: "Not looked up yet",
  not_found: "Tag not found in Clash of Clans",
  failed: "Lookup failed; open the player to retry",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

async function errorResponse(status: number, generalError: WebsiteErrorResponse) {
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return data<GroupsActionData>(
    {
      action: "create",
      groupId: null,
      createIdempotencyKey: freshIdempotencyKey(),
      updateIdempotencyKey: freshIdempotencyKey(),
      deleteIdempotencyKey: freshIdempotencyKey(),
      playerIdempotencyKey: freshIdempotencyKey(),
      fieldErrors: {},
      notice: null,
      generalError,
      values: { name: "", tag: "", action: "create", groupId: "" },
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

export default function GroupsRoute() {
  const loaderData = useLoaderData<typeof loader>();
  const actionData = useActionData<GroupsActionData>();
  const navigation = useNavigation();
  const creating =
    navigation.state !== "idle" && navigation.formData?.get("action") === "create";

  const createKey =
    actionData && actionData.action === "create"
      ? actionData.createIdempotencyKey
      : loaderData.createIdempotencyKey;
  const createErrors = actionData?.action === "create" ? actionData.fieldErrors : {};

  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="groups-title">
        <h1 id="groups-title">Private groups</h1>
        <p className="lede">
          Groups are visible only to you and hold public player tags for your own
          organization. Compare up to 20 players side by side over 3, 7 or 14 ended Legend
          days.
        </p>
      </section>

      {loaderData.error ? <ErrorNotice error={loaderData.error} /> : null}
      {actionData?.generalError ? <ErrorNotice error={actionData.generalError} /> : null}

      <section className="form-panel" aria-label="Create a group">
        <h2>Create a group</h2>
        <Form key={createKey} method="post" action="." className="stack-form">
          <input type="hidden" name="action" value="create" />
          <input type="hidden" name="idempotencyKey" value={createKey} />
          <NameField
            id="group-create-name"
            value={actionData?.action === "create" ? actionData.values.name : ""}
            error={createErrors.name}
            help="You add players one at a time once the group exists."
          />
          <button type="submit" className="button button-primary" disabled={creating}>
            {creating ? "Creating group…" : "Create group"}
          </button>
        </Form>
      </section>

      <section className="data-section" aria-labelledby="group-list-title">
        <h2 id="group-list-title">Your groups</h2>
        {loaderData.groups.length > 0 ? (
          <ul className="group-card-list">
            {loaderData.groups.map((group) => (
              <GroupCard
                key={group.groupId}
                group={group}
                loaderData={loaderData}
                actionData={
                  actionData?.groupId === group.groupId ? actionData : undefined
                }
              />
            ))}
          </ul>
        ) : (
          <div className="empty-state">
            <h3>No private groups yet</h3>
            <p>Create a group above to compare players by their tags.</p>
          </div>
        )}
      </section>
    </main>
  );
}

function GroupCard({
  group,
  loaderData,
  actionData,
}: {
  group: ListedGroup;
  loaderData: GroupsLoaderData;
  /** A no-JavaScript form result for this group, if any. */
  actionData: GroupsActionData | undefined;
}) {
  const id = group.groupId;
  const add = useFetcher<GroupsActionData>();
  const addForm = useRef<HTMLFormElement>(null);
  const addResult =
    add.data ?? (actionData?.action === "add-player" ? actionData : undefined);
  const adding = add.state !== "idle";
  const tagError = addResult?.fieldErrors.tag;
  useEffect(() => {
    if (add.state === "idle" && add.data?.notice) addForm.current?.reset();
  }, [add.state, add.data]);
  const updateKey =
    actionData?.action === "update"
      ? actionData.updateIdempotencyKey
      : loaderData.updateIdempotencyKeys[id];
  const deleteKey =
    actionData?.action === "delete"
      ? actionData.deleteIdempotencyKey
      : loaderData.deleteIdempotencyKeys[id];
  const count = group.players.length;

  return (
    <li id={`group-${id}`} className="group-card">
      <div className="group-card-head">
        <h3>{group.name}</h3>
        <a className="button button-primary" href={`/account/groups/${id}`}>
          Compare players
        </a>
      </div>
      {count > 0 ? (
        <ul className="player-action-list" aria-label={`Players in ${group.name}`}>
          {group.players.map((player) => (
            <MemberRow
              key={player.tag}
              groupId={id}
              groupName={group.name}
              player={player}
              removeKey={loaderData.removeIdempotencyKeys[id]?.[player.tag] ?? ""}
            />
          ))}
        </ul>
      ) : (
        <p className="muted">No players yet. Add the first one below.</p>
      )}
      {actionData?.action === "remove-player" && actionData.notice ? (
        <p className="form-help" role="status">
          {actionData.notice}
        </p>
      ) : null}

      <add.Form method="post" action="." className="add-player-form" ref={addForm}>
        <input type="hidden" name="action" value="add-player" />
        <input type="hidden" name="groupId" value={id} />
        <input
          type="hidden"
          name="idempotencyKey"
          value={addResult?.playerIdempotencyKey ?? loaderData.addIdempotencyKeys[id]}
        />
        <label htmlFor={`group-add-${id}`}>Add player</label>
        <div className="add-player-row">
          <input
            id={`group-add-${id}`}
            name="tag"
            type="text"
            placeholder="#2PY0LQ"
            required
            maxLength={MAX_PLAYER_TAG_INPUT_LENGTH}
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
            defaultValue={tagError ? addResult?.values.tag : ""}
            aria-invalid={tagError ? true : undefined}
            aria-describedby={`group-add-${id}-message`}
          />
          <button type="submit" className="button button-primary" disabled={adding}>
            {adding ? "Checking…" : "Add player"}
          </button>
        </div>
        {adding ? (
          <p id={`group-add-${id}-message`} className="form-help" role="status">
            Checking the tag with Clash of Clans…
          </p>
        ) : tagError ? (
          <p id={`group-add-${id}-message`} className="field-error" role="alert">
            {tagError}
          </p>
        ) : addResult?.notice ? (
          <p id={`group-add-${id}-message`} className="add-player-done" role="status">
            {addResult.notice}
          </p>
        ) : (
          <p id={`group-add-${id}-message`} className="form-help">
            {count} of {MAX_GROUP_TAGS} players. Each tag is checked with Clash of Clans
            before it joins.
          </p>
        )}
        {addResult?.generalError ? <ErrorNotice error={addResult.generalError} /> : null}
      </add.Form>

      <form method="post" className="stack-form">
        <input type="hidden" name="action" value="update" />
        <input type="hidden" name="groupId" value={id} />
        <input type="hidden" name="idempotencyKey" value={updateKey} />
        <NameField
          id={`group-update-name-${id}`}
          value={actionData?.action === "update" ? actionData.values.name : group.name}
          error={
            actionData?.action === "update" ? actionData.fieldErrors.name : undefined
          }
        />
        <button type="submit" className="button button-secondary">
          Save name
        </button>
      </form>
      <form method="post" className="stack-form danger-form">
        <fieldset className="form-fieldset">
          <legend>Delete group</legend>
          <input type="hidden" name="action" value="delete" />
          <input type="hidden" name="groupId" value={id} />
          <input type="hidden" name="idempotencyKey" value={deleteKey} />
          <label className="confirm-line">
            <input type="checkbox" name="confirm" required />I understand this group and
            its membership will be deleted.
          </label>
          {actionData?.action === "delete" && actionData.fieldErrors.confirm ? (
            <p className="field-error" role="alert">
              {actionData.fieldErrors.confirm}
            </p>
          ) : null}
          <button type="submit" className="button button-secondary danger-button">
            Delete group
          </button>
        </fieldset>
      </form>
    </li>
  );
}

function MemberRow({
  groupId,
  groupName,
  player,
  removeKey,
}: {
  groupId: string;
  groupName: string;
  player: GroupPlayer;
  removeKey: string;
}) {
  const remove = useFetcher<GroupsActionData>();
  const removing = remove.state !== "idle";
  const label = player.name ?? player.tag;
  return (
    <li>
      <span className="player-action-name">
        <a href={canonicalPlayerPath(player.tag)}>{label}</a>
        {player.name === null ? null : <span className="player-tag">{player.tag}</span>}
        <span className="group-member-detail">
          {player.state === "tracking"
            ? player.trophies === null
              ? "Legend League"
              : `${player.trophies.toLocaleString("en")} trophies`
            : STATE_LABELS[player.state]}
        </span>
      </span>
      <remove.Form method="post" action="." className="inline-form">
        <input type="hidden" name="action" value="remove-player" />
        <input type="hidden" name="groupId" value={groupId} />
        <input type="hidden" name="tag" value={player.tag} />
        <input
          type="hidden"
          name="idempotencyKey"
          value={remove.data?.playerIdempotencyKey ?? removeKey}
        />
        <button
          type="submit"
          className="button button-secondary"
          disabled={removing}
          aria-label={`Remove ${label} from ${groupName}`}
        >
          {removing ? "Removing…" : "Remove"}
        </button>
      </remove.Form>
      {remove.data?.fieldErrors.tag ? (
        <p className="field-error" role="alert">
          {remove.data.fieldErrors.tag}
        </p>
      ) : null}
      {remove.data?.generalError ? (
        <ErrorNotice error={remove.data.generalError} />
      ) : null}
    </li>
  );
}

function NameField({
  id,
  value,
  error,
  help,
}: {
  id: string;
  value: string;
  error: string | undefined;
  help?: string;
}) {
  return (
    <div className="form-field">
      <label htmlFor={id}>Group name</label>
      <input
        id={id}
        name="name"
        type="text"
        autoComplete="off"
        defaultValue={value}
        aria-invalid={error ? true : undefined}
        aria-describedby={error || help ? `${id}-message` : undefined}
      />
      {error ? (
        <p id={`${id}-message`} className="field-error" role="alert">
          {error}
        </p>
      ) : help ? (
        <p id={`${id}-message`} className="form-help">
          {help}
        </p>
      ) : null}
    </div>
  );
}
