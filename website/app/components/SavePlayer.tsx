import { useEffect } from "react";
import type { ReactNode } from "react";
import { Link, useFetcher, useRouteLoaderData } from "react-router";

import type { RootLoaderData } from "../root";
import type {
  SavedPlayersActionData,
  SavedPlayersLoaderData,
} from "../routes/account.saved-players";
import { ErrorNotice } from "./ErrorNotice";

// The profile's one action row: the page's own actions, then Save and View
// Saved Players for a signed-in Clasher.
export function SavePlayer({ tag, children }: { tag: string; children?: ReactNode }) {
  const navigation = useRouteLoaderData<RootLoaderData>("root");
  const save = navigation?.loggedIn ? <SignedInSavePlayer key={tag} tag={tag} /> : null;
  return children || save ? (
    <div className="player-actions">
      {children}
      {save}
    </div>
  ) : null;
}

function SignedInSavePlayer({ tag }: { tag: string }) {
  const list = useFetcher<SavedPlayersLoaderData>();
  const mutation = useFetcher<SavedPlayersActionData>();
  const { load } = list;
  const savedPath = `/account/saved-players?tag=${encodeURIComponent(tag)}`;
  useEffect(() => {
    void load(savedPath);
  }, [load, savedPath]);

  const result = mutation.data;
  const saved = result?.saved ?? list.data?.players.some((player) => player.tag === tag);
  const key = saved
    ? (result?.removeIdempotencyKey ?? list.data?.removeIdempotencyKeys[tag])
    : (result?.addIdempotencyKey ?? list.data?.addIdempotencyKey);
  const error = result?.generalError ?? list.data?.error;
  const busy = mutation.state !== "idle" || list.state !== "idle";

  return (
    <>
      {error ? <ErrorNotice error={error} /> : null}
      {list.data?.error ? (
        <button
          type="button"
          className="button button-secondary"
          disabled={busy}
          onClick={() => void load(savedPath)}
        >
          Retry saved players
        </button>
      ) : (
        <mutation.Form method="post" action="/account/saved-players">
          <input type="hidden" name="source" value="player" />
          <input type="hidden" name="mode" value={saved ? "remove" : "add"} />
          <input type="hidden" name="tag" value={tag} />
          <input type="hidden" name="idempotencyKey" value={key ?? ""} />
          <button
            type="submit"
            className="button button-secondary"
            disabled={busy || !key}
          >
            {mutation.state !== "idle"
              ? saved
                ? "Removing…"
                : "Saving…"
              : saved === undefined
                ? "Checking Saved Players…"
                : saved
                  ? "Remove from Saved Players"
                  : "Add to Saved Players"}
          </button>
        </mutation.Form>
      )}
      <Link to="/account/saved-players">
        View Saved Players <span aria-hidden="true">→</span>
      </Link>
    </>
  );
}
