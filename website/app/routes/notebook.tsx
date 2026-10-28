import { useState } from "react";
import {
  data,
  Link,
  redirect,
  useLoaderData,
  useNavigate,
  useOutletContext,
} from "react-router";

import type { BackHandle } from "../components/BackLink";
import { ErrorNotice } from "../components/ErrorNotice";
import { ChangedSheet, StillOn } from "../components/NotebookChecks";
import {
  AccountRow,
  ChangeSheet,
  DaysSheet,
  FixDaySheet,
  Library,
  NewBaseSheet,
  NotebookPages,
  recordLine,
  type NewBasePreset,
  type Saver,
} from "../components/NotebookParts";
import type { WebsiteErrorResponse } from "../lib/contracts";
import {
  bookPath,
  CREW_ID,
  SEASON_ID,
  seasonLabel,
  seasonName,
  type Notebook,
} from "../lib/notebook";
import type { Route } from "./+types/notebook";
import "../notebook.css";

const NO_STORE = { "Cache-Control": "no-store" };

export const handle: BackHandle = {
  back: (match) => {
    const crew = (match.loaderData as { notebook?: Notebook | null } | undefined)
      ?.notebook?.crew;
    return crew ? { to: `/crews/${crew.id}`, label: crew.name } : null;
  },
};

export function meta({ loaderData: loaded }: Route.MetaArgs) {
  const crew = loaded?.notebook?.crew;
  return [{ title: crew ? `${crew.name} bases · Clash Lens` : "Notebook · Clash Lens" }];
}

/** GET /notebook — the signed-in Clasher's notebook as this Season's page:
 * each linked account's base and run strip, and the bases. GET
 * /crews/:crewId/bases — a Crew's notebook, its bases only, for its members.
 * With `?season=<id>`, an earlier Season's page, read only. While
 * CLASHLENS_NOTEBOOK_ENABLED is off neither page exists. */
export async function loader({ request, params }: Route.LoaderArgs) {
  const { isNotebookEnabled } = await import("../server/config.server");
  if (!isNotebookEnabled()) throw data(null, { status: 404 });
  const season = new URL(request.url).searchParams.get("season") ?? undefined;
  const crew = (params as { crewId?: string }).crewId;
  if (
    (season !== undefined && !SEASON_ID.test(season)) ||
    (crew !== undefined && !CREW_ID.test(crew))
  )
    throw data(null, { status: 404 });
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { PythonApiError } = await import("../services/python.server");
  try {
    const { getNotebook } = await import("../services/notebook.server");
    const notebook: Notebook = await getNotebook(identity, { season, crew });
    return data({ notebook, error: null }, { headers: NO_STORE });
  } catch (cause) {
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
  | { kind: "changed"; tag: string }
  | { kind: "day"; tag: string; day: number }
  | { kind: "new"; preset: NewBasePreset };

export default function NotebookRoute() {
  const { notebook, error } = useLoaderData<typeof loader>();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell nb-page">
      {notebook ? (
        <NotebookPage notebook={notebook} />
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
  const navigate = useNavigate();
  const player =
    open && open.kind !== "new"
      ? notebook.players.find((item) => item.tag === open.tag)
      : undefined;
  return (
    <>
      <header className="nb-title">
        <h1>{notebook.crew ? `${notebook.crew.name} bases` : "Notebook"}</h1>
        {notebook.seasons.length > 1 ? (
          <label className="nb-season-pick">
            <span className="sr-only">Season</span>
            <select
              value={season.id}
              aria-label="Season"
              onChange={(event) => {
                const id = event.target.value;
                const home = bookPath(notebook);
                void navigate(id === notebook.seasons[0] ? home : `${home}?season=${id}`);
              }}
            >
              {notebook.seasons.map((id, index) => (
                <option key={id} value={id}>
                  {index === 0 ? "This Season" : `Season ${seasonName(id)}`}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <p className="nb-season">
          {seasonLabel(season.start, season.end)} ·{" "}
          {season.current ? `Day ${season.today} of 28` : "finished, locked"}
        </p>
      </header>
      {notebook.crew ? null : <NotebookPages here="notebook" />}
      <p className="lede">
        {notebook.crew
          ? `Bases everyone in ${notebook.crew.name} can copy, and how each held this Season for everyone who ran it. Add one to your notebook to say which account runs it.`
          : season.current
            ? "Your bases this Season and how each one held. Say which base each account runs; Clash Lens fills in every defense, from everyone who ran each base."
            : "This Season is finished, so its days are locked. Open a base to bring it back to this Season."}{" "}
        {recordLine(notebook)}
      </p>
      {notebook.crew ? null : notebook.players.length === 0 ? (
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
                  preset: { run: true, tag: item.tag, day: season.today },
                })
              }
              onDays={() => setOpen({ kind: "days", tag: item.tag })}
              prompt={
                <StillOn
                  notebook={notebook}
                  player={item}
                  saver={saver}
                  onChanged={() => setOpen({ kind: "changed", tag: item.tag })}
                />
              }
            />
          ))}
        </section>
      )}
      <Library
        notebook={notebook}
        onNew={() => setOpen({ kind: "new", preset: { run: false } })}
      />
      {open?.kind === "change" && player ? (
        <ChangeSheet
          notebook={notebook}
          player={player}
          saver={saver}
          onClose={close}
          onNew={(day) =>
            setOpen({ kind: "new", preset: { run: true, tag: player.tag, day } })
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
      {open?.kind === "changed" && player ? (
        <ChangedSheet notebook={notebook} player={player} saver={saver} onClose={close} />
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
