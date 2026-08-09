import { Link, data, redirect, useActionData, useLoaderData } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import type { PrivateGroup } from "../lib/account-contracts";
import {
  isInappropriateName,
  normalizeGroupName,
  normalizeTagList,
} from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { canonicalPlayerPath } from "../lib/player-tag";
import { isCanonicalUuid } from "../lib/validation";
import type { Route } from "./+types/account.groups";

const NO_STORE = { "Cache-Control": "no-store" };

export interface GroupsLoaderData {
  groups: PrivateGroup[];
  /** Fresh idempotency key for the create form. */
  createIdempotencyKey: string;
  /** Fresh per-group idempotency keys for the update forms. */
  updateIdempotencyKeys: Record<string, string>;
  /** Fresh per-group idempotency keys for the delete forms. */
  deleteIdempotencyKeys: Record<string, string>;
  error: WebsiteErrorResponse | null;
}

export interface GroupsActionData {
  action: "create" | "update" | "delete";
  /** The group the fresh group key belongs to (update/delete only). */
  groupId: string | null;
  /** Fresh idempotency key for the next create attempt. */
  createIdempotencyKey: string;
  /** Fresh idempotency key for the next update attempt on `groupId`. */
  updateIdempotencyKey: string;
  /** Fresh idempotency key for the next delete attempt on `groupId`. */
  deleteIdempotencyKey: string;
  fieldErrors: { name?: string; tags?: string; confirm?: string };
  generalError: WebsiteErrorResponse | null;
  values: { name: string; tags: string; action: string; groupId: string };
}

/**
 * GET /account/groups — list only the signed-in account's private groups with
 * explicit create, update, and confirmed-delete forms, each bound to its own
 * idempotency key.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<GroupsLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  try {
    const { createPythonClient } = await import("../services/python.server");
    const groups = await createPythonClient(identity).listGroups();
    const updateIdempotencyKeys: Record<string, string> = {};
    const deleteIdempotencyKeys: Record<string, string> = {};
    for (const group of groups) {
      updateIdempotencyKeys[group.groupId] = freshIdempotencyKey();
      deleteIdempotencyKeys[group.groupId] = freshIdempotencyKey();
    }
    return {
      groups,
      createIdempotencyKey: freshIdempotencyKey(),
      updateIdempotencyKeys,
      deleteIdempotencyKeys,
      error: null,
    };
  } catch (cause) {
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(cause)) throw redirect("/account/setup");
    const { safeWebsiteError } = await import("../server/errors.server");
    return {
      groups: [],
      createIdempotencyKey: freshIdempotencyKey(),
      updateIdempotencyKeys: {},
      deleteIdempotencyKeys: {},
      error: safeWebsiteError(cause),
    };
  }
}

/**
 * POST /account/groups — create, update (rename and replace membership), or
 * delete a private group. The action and group ID are explicit, deletion
 * requires a confirmation checkbox, and every mutation is same-origin with a
 * canonical idempotency UUID.
 */
export async function action({ request }: Route.ActionArgs) {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");

  const config = getWebsiteConfig();
  if (!actions.isSameOrigin(request, config.publicOrigin)) {
    return errorResponse("create", null, 403, {
      error: { code: "forbidden", message: "This action is not allowed." },
    });
  }
  const form = await actions.parseBoundedFormData(request);
  if (form === null) return invalidFormResponse();
  const idempotencyKey = form["idempotencyKey"] ?? "";
  if (!actions.isIdempotencyKey(idempotencyKey)) return invalidFormResponse();

  const actionMode = form["action"] ?? "";
  const groupId = form["groupId"] ?? "";
  if (actionMode !== "create" && actionMode !== "update" && actionMode !== "delete") {
    return invalidFormResponse();
  }
  if (actionMode !== "create" && !isCanonicalUuid(groupId)) {
    return invalidFormResponse();
  }
  if (actionMode === "delete" && form["confirm"] !== "on") {
    return data<GroupsActionData>(
      {
        action: "delete",
        groupId,
        createIdempotencyKey: actions.freshIdempotencyKey(),
        updateIdempotencyKey: actions.freshIdempotencyKey(),
        deleteIdempotencyKey: actions.freshIdempotencyKey(),
        fieldErrors: { confirm: "Confirm the deletion to continue." },
        generalError: null,
        values: { name: "", tags: "", action: "delete", groupId },
      },
      { status: 400, headers: NO_STORE },
    );
  }

  const values = {
    name: form["name"] ?? "",
    tags: form["tags"] ?? "",
    action: actionMode,
    groupId,
  };
  const fieldErrors: { name?: string; tags?: string } = {};
  const normalizedName = actionMode === "delete" ? null : normalizeGroupName(values.name);
  const normalizedTags =
    actionMode === "delete" ? null : normalizeTagList(splitTags(values.tags));
  if (actionMode !== "delete") {
    if (normalizedName === null) {
      fieldErrors.name =
        "Group name must be 1–80 characters and must not contain control characters.";
    } else if (isInappropriateName(values.name)) {
      fieldErrors.name = "Choose a different group name.";
    }
    if (normalizedTags === null) {
      fieldErrors.tags =
        "Enter at least one valid player tag, separated by commas or new lines.";
    }
  }
  if (fieldErrors.name || fieldErrors.tags) {
    return data<GroupsActionData>(
      {
        action: actionMode as "create" | "update" | "delete",
        groupId,
        createIdempotencyKey: actions.freshIdempotencyKey(),
        updateIdempotencyKey: actions.freshIdempotencyKey(),
        deleteIdempotencyKey: actions.freshIdempotencyKey(),
        fieldErrors,
        generalError: null,
        values,
      },
      { status: 400, headers: NO_STORE },
    );
  }

  try {
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient(identity);
    if (actionMode === "create") {
      await client.createGroup(
        { name: normalizedName as string, tags: normalizedTags as string[] },
        idempotencyKey,
      );
    } else if (actionMode === "update") {
      await client.updateGroup(
        groupId,
        { name: normalizedName as string, tags: normalizedTags as string[] },
        idempotencyKey,
      );
    } else {
      await client.deleteGroup(groupId, idempotencyKey);
    }
  } catch (cause) {
    if (actions.isAccountNotFoundError(cause)) throw redirect("/account/setup");
    const pythonError = cause as { status?: number; payload?: unknown };
    const payload = isRecord(pythonError.payload) ? pythonError.payload : {};
    if (pythonError.status === 409 && payload.error === "group_name_conflict") {
      return data<GroupsActionData>(
        {
          action: actionMode as "create" | "update" | "delete",
          groupId,
          createIdempotencyKey: actions.freshIdempotencyKey(),
          updateIdempotencyKey: actions.freshIdempotencyKey(),
          deleteIdempotencyKey: actions.freshIdempotencyKey(),
          fieldErrors: { name: "A group with this name already exists." },
          generalError: null,
          values,
        },
        { status: 409, headers: NO_STORE },
      );
    }
    if (pythonError.status === 422) {
      return data<GroupsActionData>(
        {
          action: actionMode as "create" | "update" | "delete",
          groupId,
          createIdempotencyKey: actions.freshIdempotencyKey(),
          updateIdempotencyKey: actions.freshIdempotencyKey(),
          deleteIdempotencyKey: actions.freshIdempotencyKey(),
          fieldErrors: {
            name: "This group was not accepted. Choose a different name.",
            tags: "Enter at least one valid player tag, separated by commas or new lines.",
          },
          generalError: null,
          values,
        },
        { status: 422, headers: NO_STORE },
      );
    }
    const { safeWebsiteError } = await import("../server/errors.server");
    const generalError: WebsiteErrorResponse =
      pythonError.status === 404 && payload.error === "group_not_found"
        ? {
            error: {
              code: "conflict",
              message: "The group no longer exists. Refresh the page.",
            },
          }
        : safeWebsiteError(cause);
    return data<GroupsActionData>(
      {
        action: actionMode as "create" | "update" | "delete",
        groupId,
        createIdempotencyKey: actions.freshIdempotencyKey(),
        updateIdempotencyKey: actions.freshIdempotencyKey(),
        deleteIdempotencyKey: actions.freshIdempotencyKey(),
        fieldErrors: {},
        generalError,
        values,
      },
      { status: 422, headers: NO_STORE },
    );
  }
  throw redirect("/account/groups");
}

function splitTags(value: string): string[] {
  return value
    .split(/[\n,]+/)
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

async function errorResponse(
  action: "create" | "update" | "delete",
  groupId: string | null,
  status: number,
  generalError: WebsiteErrorResponse,
) {
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return data<GroupsActionData>(
    {
      action,
      groupId,
      createIdempotencyKey: freshIdempotencyKey(),
      updateIdempotencyKey: freshIdempotencyKey(),
      deleteIdempotencyKey: freshIdempotencyKey(),
      fieldErrors: {},
      generalError,
      values: { name: "", tags: "", action, groupId: groupId ?? "" },
    },
    { status, headers: NO_STORE },
  );
}

async function invalidFormResponse() {
  return errorResponse("create", null, 400, {
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

  const createKey =
    actionData && actionData.action === "create"
      ? actionData.createIdempotencyKey
      : loaderData.createIdempotencyKey;

  return (
    <main className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="groups-title">
        <h1 id="groups-title">Private groups</h1>
        <p className="lede">
          Groups are visible only to you and hold public player tags for your own
          organization.
        </p>
      </section>

      {loaderData.error ? <ErrorNotice error={loaderData.error} /> : null}
      {actionData?.generalError ? <ErrorNotice error={actionData.generalError} /> : null}

      <section className="form-panel" aria-label="Create a group">
        <h2>Create a group</h2>
        <form method="post" className="stack-form">
          <input type="hidden" name="action" value="create" />
          <input type="hidden" name="idempotencyKey" value={createKey} />
          <GroupFields
            nameValue={actionData?.action === "create" ? actionData.values.name : ""}
            tagsValue={actionData?.action === "create" ? actionData.values.tags : ""}
            fieldErrors={
              actionData?.action === "create" ? actionData.fieldErrors : undefined
            }
            nameId="group-create-name"
            tagsId="group-create-tags"
          />
          <button type="submit" className="button button-primary">
            Create group
          </button>
        </form>
      </section>

      <section className="data-section" aria-labelledby="group-list-title">
        <h2 id="group-list-title">Your groups</h2>
        {loaderData.groups.length > 0 ? (
          <ul className="group-card-list">
            {loaderData.groups.map((group) => {
              const updateKey =
                actionData?.action === "update" && actionData.groupId === group.groupId
                  ? actionData.updateIdempotencyKey
                  : loaderData.updateIdempotencyKeys[group.groupId];
              const deleteKey =
                actionData?.action === "delete" && actionData.groupId === group.groupId
                  ? actionData.deleteIdempotencyKey
                  : loaderData.deleteIdempotencyKeys[group.groupId];
              return (
                <li key={group.groupId} className="group-card">
                  <h3>{group.name}</h3>
                  {group.tags.length > 0 ? (
                    <ul className="player-link-list">
                      {group.tags.map((tag) => (
                        <li key={tag}>
                          <Link to={canonicalPlayerPath(tag)}>{tag}</Link>
                          <span className="player-tag">{tag}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="muted">No player tags in this group.</p>
                  )}
                  <form method="post" className="stack-form">
                    <fieldset className="form-fieldset">
                      <legend>Edit group</legend>
                      <input type="hidden" name="action" value="update" />
                      <input type="hidden" name="groupId" value={group.groupId} />
                      <input type="hidden" name="idempotencyKey" value={updateKey} />
                      <GroupFields
                        nameValue={
                          actionData?.action === "update" &&
                          actionData.groupId === group.groupId
                            ? actionData.values.name
                            : group.name
                        }
                        tagsValue={
                          actionData?.action === "update" &&
                          actionData.groupId === group.groupId
                            ? actionData.values.tags
                            : group.tags.join(", ")
                        }
                        fieldErrors={
                          actionData?.action === "update" &&
                          actionData.groupId === group.groupId
                            ? actionData.fieldErrors
                            : undefined
                        }
                        nameId={`group-update-name-${group.groupId}`}
                        tagsId={`group-update-tags-${group.groupId}`}
                      />
                      <button type="submit" className="button button-secondary">
                        Save changes
                      </button>
                    </fieldset>
                  </form>
                  <form method="post" className="stack-form danger-form">
                    <fieldset className="form-fieldset">
                      <legend>Delete group</legend>
                      <input type="hidden" name="action" value="delete" />
                      <input type="hidden" name="groupId" value={group.groupId} />
                      <input type="hidden" name="idempotencyKey" value={deleteKey} />
                      <label className="confirm-line">
                        <input type="checkbox" name="confirm" required />I understand this
                        group and its membership will be deleted.
                      </label>
                      {actionData?.action === "delete" &&
                      actionData.groupId === group.groupId &&
                      actionData.fieldErrors.confirm ? (
                        <p className="field-error" role="alert">
                          {actionData.fieldErrors.confirm}
                        </p>
                      ) : null}
                      <button
                        type="submit"
                        className="button button-secondary danger-button"
                      >
                        Delete group
                      </button>
                    </fieldset>
                  </form>
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="empty-state">
            <h3>No private groups yet</h3>
            <p>Create a group above to organize your saved players.</p>
          </div>
        )}
      </section>

      <p className="back-link">
        <Link to="/account">← Back to your account</Link>
      </p>
    </main>
  );
}

function GroupFields({
  nameValue,
  tagsValue,
  fieldErrors,
  nameId,
  tagsId,
}: {
  nameValue: string;
  tagsValue: string;
  fieldErrors: { name?: string; tags?: string } | undefined;
  nameId: string;
  tagsId: string;
}) {
  return (
    <>
      <div className="form-field">
        <label htmlFor={nameId}>Group name</label>
        <input
          id={nameId}
          name="name"
          type="text"
          autoComplete="off"
          defaultValue={nameValue}
          aria-invalid={fieldErrors?.name ? true : undefined}
          aria-describedby={fieldErrors?.name ? `${nameId}-error` : undefined}
        />
        {fieldErrors?.name ? (
          <p id={`${nameId}-error`} className="field-error" role="alert">
            {fieldErrors.name}
          </p>
        ) : null}
      </div>
      <div className="form-field">
        <label htmlFor={tagsId}>Player tags</label>
        <textarea
          id={tagsId}
          name="tags"
          rows={3}
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          defaultValue={tagsValue}
          aria-invalid={fieldErrors?.tags ? true : undefined}
          aria-describedby={fieldErrors?.tags ? `${tagsId}-error` : undefined}
        />
        {fieldErrors?.tags ? (
          <p id={`${tagsId}-error`} className="field-error" role="alert">
            {fieldErrors.tags}
          </p>
        ) : (
          <p className="form-help">Separate tags with commas or new lines.</p>
        )}
      </div>
    </>
  );
}
