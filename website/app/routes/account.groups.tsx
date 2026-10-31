import { type MouseEvent, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import {
  data,
  Form,
  Link,
  redirect,
  replace,
  useActionData,
  useFetcher,
  useLoaderData,
  useNavigation,
  useSearchParams,
} from "react-router";

import { useBackState } from "../components/BackLink";
import { ErrorNotice } from "../components/ErrorNotice";
import { TrophyIcon } from "../components/GameIcons";
import { dayBar, dayRange, rowScale } from "../components/DayScale";
import { LastDays } from "../components/LastDays";
import type { GroupPlayer, ListedGroup } from "../lib/account-contracts";
import { addedNotice, GROUP_LIMIT, STATE_LABELS } from "../lib/group-text";
import {
  isInappropriateName,
  MAX_GROUP_TAGS,
  MAX_GROUPS,
  normalizeGroupName,
  normalizeSubmittedPlayerTag,
} from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import type { GroupComparison } from "../lib/group-comparison";
import { canonicalPlayerPath, MAX_PLAYER_TAG_INPUT_LENGTH } from "../lib/player-tag";
import { expireSeasonTrophies, useSeasonEnded } from "../lib/season-end";
import { isCanonicalUuid } from "../lib/validation";
import type { Route } from "./+types/account.groups";
import "../account-groups.css";

const NO_STORE = { "Cache-Control": "no-store" };
const ACTIONS = ["create", "update", "delete", "add-player", "remove-player"] as const;
type GroupAction = (typeof ACTIONS)[number];

/** One row of a card's preview: a player with their result today and on each
 *  of the last 7 finished Legend days, and whether they are the Clasher's own. */
export interface PreviewPlayer extends GroupPlayer {
  you: boolean;
  today: number | null;
  /** The 7 days' total, for screen readers; the bars show it to the eye. */
  net: number | null;
  days: (number | null)[];
}

export interface GroupsLoaderData {
  groups: ListedGroup[];
  /** Each list's players as its comparison reads them, with the Clasher's own
   *  players beside them, by group; a list whose comparison could not be read
   *  has none and shows names and trophies only. */
  previews?: Record<string, PreviewPlayer[]>;
  /** The 7 finished days every preview covers, named once over its bars. */
  previewDays?: string | null;
  /** The Season the members' trophies were read in, once loaded. */
  season: string | null;
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
 * GET /account/watchlists — list only the signed-in account's private groups with
 * explicit create, rename, add-player, remove-player and confirmed-delete
 * forms, each bound to its own idempotency key.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<GroupsLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  const empty = {
    groups: [],
    previews: {},
    season: null,
    createIdempotencyKey: freshIdempotencyKey(),
    updateIdempotencyKeys: {},
    deleteIdempotencyKeys: {},
    addIdempotencyKeys: {},
    removeIdempotencyKeys: {},
    error: null,
  };
  try {
    const { createPythonClient } = await import("../services/python.server");
    const { groups, season } = await createPythonClient(identity).listGroups();
    const { getGroupComparison } = await import("../services/group-comparison.server");
    // Each card shows today and the last 7 days without opening it: every
    // list's comparison is read at once, and one that fails only loses its bars.
    const comparisons = await Promise.all(
      groups.map((group) =>
        group.players.length === 0
          ? null
          : getGroupComparison(identity, group.groupId, 7).catch(() => null),
      ),
    );
    const loaded: GroupsLoaderData = {
      ...empty,
      groups,
      previews: Object.fromEntries(
        groups.flatMap((group, index) => {
          const comparison = comparisons[index];
          return comparison == null ? [] : [[group.groupId, previewPlayers(comparison)]];
        }),
      ),
      // Every list covers the same 7 days, up to the last Reset.
      previewDays: dayRange(
        comparisons.find((comparison) => comparison != null)?.dayStarts ?? [],
      ),
      season,
    };
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
 * POST /account/watchlists — create, rename, or delete a private group, or add or
 * remove one player. The action and group ID are explicit, deletion requires
 * an explicit confirmation, and every mutation is same-origin with a
 * canonical idempotency UUID. A player joins only after the game confirms
 * the tag belongs to a real player.
 */
export async function action({ request, context }: Route.ActionArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const players = await import("../services/group-players.server");
  const adding = await import("../services/group-add.server");
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
          name: "Watchlist name must be 1–80 characters, with no line breaks or hidden characters.",
        },
      });
    }
    if (isInappropriateName(values.name)) {
      return reply(400, { fieldErrors: { name: "Choose a different watchlist name." } });
    }
  }
  const tag = normalizeSubmittedPlayerTag(values.tag);
  if ((actionMode === "add-player" || actionMode === "remove-player") && tag === null) {
    return reply(400, { fieldErrors: { tag: adding.INVALID_TAG } });
  }

  try {
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient(identity);
    if (actionMode === "create") {
      const created = await client.createGroup(
        { name: normalizedName as string },
        idempotencyKey,
      );
      throw redirect(`/account/watchlists#group-${created.groupId}`);
    } else if (actionMode === "update") {
      await client.updateGroup(
        groupId,
        { name: normalizedName as string },
        idempotencyKey,
      );
      return reply(200, { notice: "Saved the new name." });
    } else if (actionMode === "delete") {
      await client.deleteGroup(groupId, idempotencyKey);
    } else if (actionMode === "remove-player") {
      await players.removeGroupPlayer(identity, groupId, tag as string, idempotencyKey);
      return reply(200, { notice: `Removed ${tag} from the watchlist.` });
    } else {
      // Refuse duplicates and a full group before spending a player lookup.
      const group = (await client.listGroups()).groups.find(
        (row) => row.groupId === groupId,
      );
      if (group === undefined) return reply(404, { generalError: GROUP_GONE });
      const { clientAddressContext } = await import("../server/client-address.server");
      const added = await adding.addToGroup(
        identity,
        context?.get(clientAddressContext),
        group,
        tag as string,
        idempotencyKey,
      );
      return "player" in added
        ? reply(200, { notice: addedNotice(added.player) })
        : reply(added.status, { fieldErrors: { tag: added.tagError } });
    }
  } catch (cause) {
    if (cause instanceof Response) throw cause;
    if (actions.isAccountNotFoundError(cause)) throw redirect("/account/setup");
    const pythonError = cause as { status?: number; payload?: unknown };
    const code = isRecord(pythonError.payload) ? pythonError.payload.error : undefined;
    const tagError = adding.groupTagError(code, tag ?? "");
    if (tagError !== null) {
      return reply(pythonError.status ?? 422, { fieldErrors: { tag: tagError } });
    }
    if (pythonError.status === 409 && code === "group_name_conflict") {
      return reply(409, {
        fieldErrors: { name: "A watchlist with this name already exists." },
      });
    }
    if (code === "group_limit_reached") {
      return reply(422, { fieldErrors: { name: GROUP_LIMIT } });
    }
    if (
      pythonError.status === 422 &&
      (actionMode === "create" || actionMode === "update")
    ) {
      return reply(422, {
        fieldErrors: {
          name: "This watchlist was not accepted. Choose a different name.",
        },
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
                    "Could not confirm the watchlist was created. Refresh the page before trying again.",
                },
              }
            : safeError,
    });
  }
  // Replace the deleted group's history entry so Back does not return to it.
  throw replace("/account/watchlists");
}

function previewPlayers(comparison: GroupComparison): PreviewPlayer[] {
  return comparison.players.map((player) => ({
    tag: player.tag,
    name: player.name,
    trophies: player.trophies,
    seasonResetPending: player.seasonResetPending,
    state: player.status,
    you: player.you,
    today: player.today?.net ?? null,
    net: player.net,
    days: player.days.map((day) => day.net),
  }));
}

const GROUP_GONE: WebsiteErrorResponse = {
  error: {
    code: "conflict",
    message: "The watchlist no longer exists. Refresh the page.",
  },
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
  const seasonEnded = useSeasonEnded(loaderData.season);
  const groups = seasonEnded
    ? loaderData.groups.map((group) => ({
        ...group,
        players: group.players.map(expireSeasonTrophies),
      }))
    : loaderData.groups;
  const preview = (groupId: string) => {
    const players = loaderData.previews?.[groupId];
    if (players === undefined) return null;
    return seasonEnded ? players.map(expireSeasonTrophies) : players;
  };
  const navigation = useNavigation();
  const creating =
    navigation.state !== "idle" && navigation.formData?.get("action") === "create";

  const createKey =
    actionData && actionData.action === "create"
      ? actionData.createIdempotencyKey
      : loaderData.createIdempotencyKey;
  const createErrors = actionData?.action === "create" ? actionData.fieldErrors : {};
  const atLimit = !loaderData.error && groups.length >= MAX_GROUPS;
  const listTitle = useRef<HTMLHeadingElement>(null);
  const shownGroups = useRef(groups.length);
  useEffect(() => {
    // A deleted group's Edit takes the focus with it; hand it to the list.
    if (groups.length < shownGroups.current && document.activeElement === document.body) {
      listTitle.current?.focus();
    }
    shownGroups.current = groups.length;
  }, [groups.length]);

  const createForm = (
    <section className="form-panel" aria-label="Create a watchlist">
      <h2>Create a watchlist</h2>
      <Form key={createKey} method="post" action="." className="stack-form" replace>
        <input type="hidden" name="action" value="create" />
        <input type="hidden" name="idempotencyKey" value={createKey} />
        <NameField
          id="group-create-name"
          value={actionData?.action === "create" ? actionData.values.name : ""}
          error={createErrors.name}
          help={
            atLimit
              ? GROUP_LIMIT
              : `Up to ${MAX_GROUPS} watchlists. Add players once it exists.`
          }
        />
        <button
          type="submit"
          className="button button-primary"
          disabled={creating || atLimit}
        >
          {creating ? "Creating watchlist…" : "Create watchlist"}
        </button>
      </Form>
    </section>
  );

  return (
    <main id="main-content" tabIndex={-1} className="page-shell">
      <header className="page-hero">
        <h1 id="groups-title">Watchlists</h1>
        <p>Private lists of up to 20 players, side by side.</p>
        {/* With lists to show, making one waits behind a button, on the
            title's right. */}
        {groups.length > 0 || loaderData.error ? (
          <details
            key={createKey}
            className="group-create"
            open={createErrors.name !== undefined}
          >
            <summary className="button button-secondary">New watchlist</summary>
            {createForm}
          </details>
        ) : null}
      </header>

      {actionData?.generalError ? <ErrorNotice error={actionData.generalError} /> : null}

      <section className="data-section" aria-labelledby="group-list-title">
        {/* The page title already names them; the heading stays for screen readers. */}
        <h2 id="group-list-title" className="sr-only" ref={listTitle} tabIndex={-1}>
          Your watchlists
        </h2>
        {loaderData.error ? (
          <aside className="notice notice-unavailable" role="alert">
            <strong>Watchlists could not be loaded.</strong>{" "}
            <a href="/account/watchlists">Try again</a>
          </aside>
        ) : groups.length > 0 ? (
          <ul className="group-card-list">
            {groups.map((group) => (
              <GroupCard
                key={group.groupId}
                group={group}
                preview={preview(group.groupId)}
                loaderData={loaderData}
                actionData={
                  actionData?.groupId === group.groupId ? actionData : undefined
                }
              />
            ))}
          </ul>
        ) : (
          <>
            <div className="empty-state">
              <h3>No watchlists yet</h3>
            </div>
            {createForm}
          </>
        )}
      </section>
    </main>
  );
}

const PANELS = ["add", "edit"] as const;
type Panel = (typeof PANELS)[number];

function GroupCard({
  group,
  preview,
  loaderData,
  actionData,
}: {
  group: ListedGroup;
  /** The list's players with today and their days, when the comparison was read. */
  preview: PreviewPlayer[] | null;
  loaderData: GroupsLoaderData;
  /** A no-JavaScript form result for this group, if any. */
  actionData: GroupsActionData | undefined;
}) {
  const id = group.groupId;
  const add = useFetcher<GroupsActionData>();
  const addForm = useRef<HTMLFormElement>(null);
  const tagInput = useRef<HTMLInputElement>(null);
  const more = useRef<HTMLDetailsElement>(null);
  const addResult =
    add.data ?? (actionData?.action === "add-player" ? actionData : undefined);
  const checking = add.state !== "idle";
  const tagError = addResult?.fieldErrors.tag;
  useEffect(() => {
    if (add.state === "idle" && add.data?.notice) addForm.current?.reset();
  }, [add.state, add.data]);
  const editResult = actionData?.action === "add-player" ? undefined : actionData;
  const count = group.players.length;
  // The Clasher's own players outside the list still join its rows, as in the
  // comparison, so the count says how many: "6 watched + 2 yours".
  const yours =
    preview?.filter(
      (player) => player.you && !group.players.some(({ tag }) => tag === player.tag),
    ).length ?? 0;
  // Opened by its link without JavaScript, or kept open after that panel's form result.
  const [searchParams] = useSearchParams();
  const [panel, setPanel] = useState<Panel | null>(() =>
    actionData === undefined
      ? searchParams.get("group") === id
        ? (PANELS.find((name) => name === searchParams.get("panel")) ?? null)
        : null
      : editResult === undefined
        ? "add"
        : "edit",
  );
  const toggle = (name: Panel) => ({
    href:
      panel === name
        ? `/account/watchlists#group-${id}`
        : `/account/watchlists?group=${id}&panel=${name}#group-${id}`,
    "aria-expanded": panel === name,
    "aria-controls": `group-${id}-${name}`,
    onClick: (event: MouseEvent) => {
      event.preventDefault();
      more.current?.removeAttribute("open");
      flushSync(() => setPanel(panel === name ? null : name));
      if (name === "add" && panel !== name) tagInput.current?.focus();
    },
  });

  return (
    <li id={`group-${id}`} className="group-card">
      <div className="group-card-head">
        <div className="group-card-title">
          <h3>{group.name}</h3>
          {count > 0 ? (
            <p className="group-card-meta">
              {count} watched{yours > 0 ? ` + ${yours} yours` : ""}
            </p>
          ) : null}
        </div>
        {/* Adding and editing wait in the card's one quiet menu, so its
            title has room and Compare is the clear way on. */}
        <details className="group-more" ref={more}>
          <summary title="Add a player, rename, remove players or delete">
            <span aria-hidden="true">⋯</span>
            <span className="sr-only">More for {group.name}</span>
          </summary>
          <div className="group-more-menu">
            <a className="group-text-button" {...toggle("add")}>
              <LineIcon d="M12 5v14M5 12h14" />
              Add player
            </a>
            <a className="group-text-button" {...toggle("edit")}>
              <LineIcon d="M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4" />
              Edit
            </a>
          </div>
        </details>
        {/* The whole card opens the comparison; this link stretches over it,
            and its button is all that shows of it. */}
        <Link
          className="group-card-open button-primary"
          to={`/account/watchlists/${id}`}
          aria-label={`Compare players in ${group.name}`}
        >
          Compare
          <LineIcon d="M5 12h14M13 6l6 6-6 6" />
        </Link>
      </div>

      {/* Both panels stay mounted while closed, so their request state survives. */}
      <div id={`group-${id}-add`} className="group-panel" hidden={panel !== "add"}>
        <add.Form method="post" action="." className="add-player-form" ref={addForm}>
          <input type="hidden" name="action" value="add-player" />
          <input type="hidden" name="groupId" value={id} />
          <input
            type="hidden"
            name="idempotencyKey"
            value={addResult?.playerIdempotencyKey ?? loaderData.addIdempotencyKeys[id]}
          />
          <label htmlFor={`group-add-${id}`}>Player tag</label>
          <div className="add-player-row">
            <input
              ref={tagInput}
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
            <button type="submit" className="button button-primary" disabled={checking}>
              {checking ? "Checking…" : "Add"}
            </button>
          </div>
          {checking ? (
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
          {addResult?.generalError ? (
            <ErrorNotice error={addResult.generalError} />
          ) : null}
        </add.Form>
      </div>
      <div id={`group-${id}-edit`} className="group-panel" hidden={panel !== "edit"}>
        <EditGroup group={group} loaderData={loaderData} actionData={editResult} />
      </div>

      {panel === "edit" ? null : count > 0 ? (
        <Members group={group} preview={preview} days={loaderData.previewDays ?? null} />
      ) : (
        <p className="muted group-members">No players yet.</p>
      )}
    </li>
  );
}

/** Rename, remove players and delete, folded under the card's Edit. */
function EditGroup({
  group,
  loaderData,
  actionData,
}: {
  group: ListedGroup;
  loaderData: GroupsLoaderData;
  /** A no-JavaScript rename, remove or delete result for this group, if any. */
  actionData: GroupsActionData | undefined;
}) {
  const id = group.groupId;
  const deleteStep = useRef<HTMLDetailsElement>(null);
  const deleteSummary = useRef<HTMLElement>(null);
  const rename = useFetcher<GroupsActionData>();
  const deletion = useFetcher<GroupsActionData>();
  const renameResult =
    rename.data ?? (actionData?.action === "update" ? actionData : undefined);
  const deleteResult =
    deletion.data ?? (actionData?.action === "delete" ? actionData : undefined);
  const removeResult = actionData?.action === "remove-player" ? actionData : undefined;
  const saving = rename.state !== "idle";
  const deleting = deletion.state !== "idle";

  return (
    <div className="group-edit">
      <rename.Form method="post" action="." className="stack-form">
        <input type="hidden" name="action" value="update" />
        <input type="hidden" name="groupId" value={id} />
        <input
          type="hidden"
          name="idempotencyKey"
          value={
            renameResult?.updateIdempotencyKey ?? loaderData.updateIdempotencyKeys[id]
          }
        />
        <NameField
          id={`group-update-name-${id}`}
          value={renameResult?.fieldErrors.name ? renameResult.values.name : group.name}
          error={renameResult?.fieldErrors.name}
        />
        <button type="submit" className="button button-secondary" disabled={saving}>
          {saving ? "Saving…" : "Save name"}
        </button>
        {!saving && renameResult?.notice ? (
          <p className="add-player-done" role="status">
            {renameResult.notice}
          </p>
        ) : null}
        {renameResult?.generalError ? (
          <ErrorNotice error={renameResult.generalError} />
        ) : null}
      </rename.Form>

      <section aria-labelledby={`group-edit-${id}-players`}>
        <h4 id={`group-edit-${id}-players`}>Players</h4>
        {group.players.length > 0 ? (
          <ul className="player-action-list">
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
          <p className="muted">No players yet.</p>
        )}
        {removeResult?.notice ? (
          <p className="form-help" role="status">
            {removeResult.notice}
          </p>
        ) : null}
        {removeResult?.fieldErrors.tag ? (
          <p className="field-error" role="alert">
            {removeResult.fieldErrors.tag}
          </p>
        ) : null}
        {removeResult?.generalError ? (
          <ErrorNotice error={removeResult.generalError} />
        ) : null}
      </section>

      <deletion.Form method="post" action="." className="group-delete">
        <input type="hidden" name="action" value="delete" />
        <input type="hidden" name="groupId" value={id} />
        <input
          type="hidden"
          name="idempotencyKey"
          value={
            deleteResult?.deleteIdempotencyKey ?? loaderData.deleteIdempotencyKeys[id]
          }
        />
        <input type="hidden" name="confirm" value="on" />
        <details
          ref={deleteStep}
          className="group-delete-step"
          open={deleteResult !== undefined}
        >
          <summary ref={deleteSummary} className="button button-secondary danger-button">
            Delete watchlist
          </summary>
          <p>
            Delete {group.name} and its list of players? This cannot be undone. The
            players stay on Clash Lens.
          </p>
          <div className="group-card-actions">
            <button type="submit" className="button danger-button" disabled={deleting}>
              {deleting ? "Deleting…" : "Yes, delete watchlist"}
            </button>
            <a
              className="button button-secondary"
              href="/account/watchlists"
              onClick={(event) => {
                event.preventDefault();
                if (deleteStep.current) deleteStep.current.open = false;
                deleteSummary.current?.focus();
              }}
            >
              Keep watchlist
            </a>
          </div>
        </details>
        {deleteResult?.generalError ? (
          <ErrorNotice error={deleteResult.generalError} />
        ) : null}
      </deletion.Form>
    </div>
  );
}

/**
 * A card lists its players highest first, as the comparison does. The saved
 * list has no order of its own (it comes back by tag), so this only changes
 * how it reads.
 */
function byTrophies<T extends GroupPlayer>(players: T[]): T[] {
  return [...players].sort(
    (left, right) =>
      (right.trophies ?? -1) - (left.trophies ?? -1) || left.tag.localeCompare(right.tag),
  );
}

/** A small line icon in the text colour, for the card's buttons. */
function LineIcon({ d }: { d: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={d} />
    </svg>
  );
}

/**
 * A card's players, highest first, each with today's result and the last 7
 * days as small bars, so the race reads without opening the list. The
 * Clasher's own players join it, marked You, as in the comparison.
 */
function Members({
  group,
  preview,
  days,
}: {
  group: ListedGroup;
  preview: PreviewPlayer[] | null;
  /** The bars' days, such as "22–28 Oct". */
  days: string | null;
}) {
  const backState = useBackState("Your watchlists");
  const rows: (GroupPlayer & Partial<PreviewPlayer>)[] = byTrophies(
    preview ?? group.players,
  );
  return (
    <div className="group-members">
      {preview === null ? null : (
        <div className="group-members-key">
          <span aria-hidden="true">Today</span>
          <LastDays count={7} range={days} />
          <span aria-hidden="true">Trophies</span>
        </div>
      )}
      <ol className="group-member-list" aria-label={`Players in ${group.name}`}>
        {rows.map((player) => (
          <li key={player.tag} className={player.you ? "group-member-you" : undefined}>
            <span className="group-member-who">
              <Link to={canonicalPlayerPath(player.tag)} state={backState}>
                {player.name ?? player.tag}
              </Link>
              {player.you ? (
                <span className="you-badge" aria-hidden="true">
                  You
                </span>
              ) : null}
            </span>
            <span className="sr-only">
              {player.tag}
              {!player.you
                ? ""
                : group.players.some(({ tag }) => tag === player.tag)
                  ? ", you"
                  : ", you, not in this watchlist"}
            </span>
            {player.state !== "tracking" ? (
              <span className="group-member-detail group-member-state">
                {STATE_LABELS[player.state]}
              </span>
            ) : player.seasonResetPending ? (
              <span className="group-member-detail group-member-state">
                Waiting for this player&apos;s Season reset
              </span>
            ) : (
              <>
                {player.days === undefined ? null : (
                  <>
                    <span className="group-member-today">
                      <span className="sr-only">Today </span>
                      {player.today == null ? "–" : <Signed value={player.today} />}
                    </span>
                    <DayBars days={player.days} />
                    {player.net == null ? null : (
                      <span className="sr-only">, last 7 days {signed(player.net)}</span>
                    )}
                  </>
                )}
                <span className="group-member-detail group-member-trophies">
                  {player.trophies === null ? (
                    "Legend League"
                  ) : (
                    <>
                      <TrophyIcon />
                      {player.trophies.toLocaleString("en")}
                      <span className="sr-only"> trophies</span>
                    </>
                  )}
                </span>
              </>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

/**
 * One player's last days as thin bars up (won) or down (lost) from their
 * zero line, on the player's own scale so every row shows its shape, all at
 * one strength in their gain or loss colour, as Rankings draws its own.
 */
function DayBars({ days }: { days: (number | null)[] }) {
  const scale = rowScale(days);
  return (
    <span className="group-day-bars" aria-hidden="true">
      {days.map((net, index) => (
        <span
          key={index}
          data-net={net === null || net === 0 ? "flat" : net > 0 ? "up" : "down"}
          style={{ "--size": dayBar(net ?? 0, scale) } as React.CSSProperties}
        />
      ))}
    </span>
  );
}

function Signed({ value }: { value: number }) {
  return (
    <span className={value > 0 ? "score-positive" : value < 0 ? "score-negative" : ""}>
      {signed(value)}
    </span>
  );
}

function signed(value: number): string {
  const text = Math.abs(value).toLocaleString("en");
  return value > 0 ? `+${text}` : value < 0 ? `−${text}` : text;
}

/** A member's name and trophies, in Edit's list beside their Remove button. */
function MemberName({ player }: { player: GroupPlayer }) {
  const backState = useBackState("Your watchlists");
  return (
    <span className="player-action-name">
      <Link to={canonicalPlayerPath(player.tag)} state={backState}>
        {player.name ?? player.tag}
      </Link>
      {player.name === null ? null : <span className="player-tag">{player.tag}</span>}
      <span className="group-member-detail">
        {player.state === "tracking" ? (
          player.seasonResetPending ? (
            "Waiting for this player's Season reset"
          ) : player.trophies === null ? (
            "Legend League"
          ) : (
            <>
              <TrophyIcon />
              <span className="score">{player.trophies.toLocaleString("en")}</span>
              <span className="sr-only"> trophies</span>
            </>
          )
        ) : (
          STATE_LABELS[player.state]
        )}
      </span>
    </span>
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
  return (
    <li>
      <MemberName player={player} />
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
          aria-label={`Remove ${player.name ?? player.tag} from ${groupName}`}
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
      <label htmlFor={id}>Watchlist name</label>
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
