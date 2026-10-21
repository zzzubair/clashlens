import { useState } from "react";
import { data, Link, redirect, useLoaderData, useOutletContext } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { PastSeasonNotice, SeasonSwitcher } from "../components/NotebookSeasons";
import {
  AccountRow,
  ChangeSheet,
  DaysSheet,
  FixDaySheet,
  Library,
  NewBaseSheet,
  recordLine,
  type NewBasePreset,
  type Saver,
} from "../components/NotebookParts";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { SEASON_ID, type Notebook } from "../lib/notebook";
import type { Route } from "./+types/notebook";
import "../notebook.css";

const NO_STORE = { "Cache-Control": "no-store" };

export function meta() {
  return [{ title: "Notebook · Clash Lens" }];
}

/** GET /notebook — the signed-in Clasher's Season notebook: each linked
 * account's base and run strip, and the Library; `?season=` opens a past
 * Season's. While CLASHLENS_NOTEBOOK_ENABLED is off the page does not exist. */
export async function loader({ request }: Route.LoaderArgs) {
  const { isNotebookEnabled } = await import("../server/config.server");
  if (!isNotebookEnabled()) throw data(null, { status: 404 });
  const season = new URL(request.url).searchParams.get("season") ?? undefined;
  if (season !== undefined && !SEASON_ID.test(season)) throw data(null, { status: 404 });
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { PythonApiError } = await import("../services/python.server");
  try {
    const { getNotebook } = await import("../services/notebook.server");
    const notebook: Notebook = await getNotebook(identity, undefined, season);
    return data({ notebook, error: null }, { headers: NO_STORE });
  } catch (cause) {
    // No such Season, or one still to come.
    if (cause instanceof PythonApiError && cause.status === 404)
      throw data(null, { status: 404 });
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(cause)) {
      const { accountSetupPath } = await import("../server/return-path.server");
      const url = new URL(request.url);
      throw redirect(accountSetupPath(url.pathname, url));
    }
    const { safeWebsiteError } = await import("../server/errors.server");
    const error: WebsiteErrorResponse = safeWebsiteError(cause);
    return data({ notebook: null, error }, { status: 503, headers: NO_STORE });
  }
}

export function headers() {
  return NO_STORE;
}

type Open =
  | { kind: "change"; tag: string }
  | { kind: "days"; tag: string }
  | { kind: "day"; tag: string; day: number }
  | { kind: "new"; preset: NewBasePreset };

export default function NotebookRoute() {
  const { notebook, error } = useLoaderData<typeof loader>();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell nb-page">
      {notebook ? (
        <NotebookPage key={notebook.season.id} notebook={notebook} />
      ) : (
        <>
          <h1>Notebook</h1>
          {error ? <ErrorNotice error={error} /> : null}
        </>
      )}
    </main>
  );
}

function NotebookPage({ notebook }: { notebook: Notebook }) {
  const saver = useOutletContext<Saver>();
  const [open, setOpen] = useState<Open | null>(null);
  const close = () => setOpen(null);
  const { season } = notebook;
  const player =
    open && open.kind !== "new"
      ? notebook.players.find((item) => item.tag === open.tag)
      : undefined;
  return (
    <>
      <header className="nb-title">
        <h1>Notebook</h1>
        <SeasonSwitcher notebook={notebook} />
      </header>
      <PastSeasonNotice notebook={notebook} />
      <p className="lede">
        Your bases this Season and how each one held. Say which base each account runs;
        Clash Lens fills in every defense. {recordLine(notebook)}
      </p>
      {notebook.players.length === 0 ? (
        <p className="nb-empty">
          Link your Clash of Clans account to say which base it runs.{" "}
          <Link to="/account/verify-player">Link a player</Link>
        </p>
      ) : (
        <section className="nb-section" aria-label="Your accounts">
          {notebook.players.map((item) => (
            <AccountRow
              key={item.tag}
              notebook={notebook}
              player={item}
              onChange={() => setOpen({ kind: "change", tag: item.tag })}
              onAdd={() =>
                setOpen({
                  kind: "new",
                  preset: { status: "active", tag: item.tag, day: season.today },
                })
              }
              onDays={() => setOpen({ kind: "days", tag: item.tag })}
            />
          ))}
        </section>
      )}
      <Library
        notebook={notebook}
        onNew={
          season.current
            ? () => setOpen({ kind: "new", preset: { status: "to_try" } })
            : undefined
        }
      />
      {open?.kind === "change" && player ? (
        <ChangeSheet
          notebook={notebook}
          player={player}
          saver={saver}
          onClose={close}
          onNew={(day) =>
            setOpen({ kind: "new", preset: { status: "active", tag: player.tag, day } })
          }
        />
      ) : null}
      {open?.kind === "days" && player ? (
        <DaysSheet
          notebook={notebook}
          player={player}
          onDay={(day) => setOpen({ kind: "day", tag: player.tag, day })}
          onClose={close}
        />
      ) : null}
      {open?.kind === "day" && player ? (
        <FixDaySheet
          notebook={notebook}
          tag={player.tag}
          day={open.day}
          saver={saver}
          onClose={close}
        />
      ) : null}
      {open?.kind === "new" ? (
        <NewBaseSheet
          notebook={notebook}
          preset={open.preset}
          saver={saver}
          onClose={close}
        />
      ) : null}
    </>
  );
}
