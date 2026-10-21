import { useEffect, useRef, useState } from "react";
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
import { CopyToSeason, PastSeasonNotice } from "../components/NotebookSeasons";
import {
  BaseTile,
  recordLine,
  Sheet,
  StatusSheet,
  statusText,
  type Saver,
} from "../components/NotebookParts";
import type { WebsiteErrorResponse } from "../lib/contracts";
import {
  averageLost,
  BASE_ID,
  baseRuns,
  dayDate,
  heldText,
  notebookPath,
  playerName,
  SEASON_ID,
  type BaseRecord,
  type Notebook,
  type NotebookBase,
} from "../lib/notebook";
import type { Route } from "./+types/notebook.base";
import "../notebook.css";

const NO_STORE = { "Cache-Control": "no-store" };
// A note saves this long after the last key press.
const NOTE_WAIT_MS = 800;

export const handle: BackHandle = {
  // Back to the notebook the base is in.
  back: (match) => ({
    to: notebookPath(
      (match.loaderData as { notebook?: Notebook | null } | undefined)?.notebook?.season,
    ),
    label: "Notebook",
  }),
};

export function meta({ loaderData: loaded }: Route.MetaArgs) {
  const name = loaded?.notebook?.bases.find((base) => base.id === loaded.code)?.name;
  return [{ title: `${name ?? "Base"} · Notebook · Clash Lens` }];
}

/** GET /notebook/bases/:code — one base: its record, note and link; with
 * `?season=`, in that past Season's notebook. The base ID is for support,
 * so the page never shows it. */
export async function loader({ request, params }: Route.LoaderArgs) {
  const { isNotebookEnabled } = await import("../server/config.server");
  if (!isNotebookEnabled()) throw data(null, { status: 404 });
  const code = params.code;
  const season = new URL(request.url).searchParams.get("season") ?? undefined;
  if (!BASE_ID.test(code) || (season !== undefined && !SEASON_ID.test(season)))
    throw data(null, { status: 404 });
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { PythonApiError } = await import("../services/python.server");
  try {
    const { getNotebook } = await import("../services/notebook.server");
    const notebook: Notebook = await getNotebook(identity, code, season);
    return data({ notebook, code, error: null }, { headers: NO_STORE });
  } catch (cause) {
    // Not in this account's notebook, or deleted a moment ago.
    if (cause instanceof PythonApiError && cause.status === 404)
      return data(
        { notebook: null, code, error: null },
        { status: 404, headers: NO_STORE },
      );
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(cause)) {
      const { accountSetupPath } = await import("../server/return-path.server");
      const url = new URL(request.url);
      throw redirect(accountSetupPath(url.pathname, url));
    }
    const { safeWebsiteError } = await import("../server/errors.server");
    const error: WebsiteErrorResponse = safeWebsiteError(cause);
    return data({ notebook: null, code, error }, { status: 503, headers: NO_STORE });
  }
}

export function headers() {
  return NO_STORE;
}

export default function NotebookBaseRoute() {
  const { notebook, code, error } = useLoaderData<typeof loader>();
  const base = notebook?.bases.find((item) => item.id === code);
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell nb-page">
      {notebook && base ? (
        <BasePage key={`${notebook.season.id} ${code}`} notebook={notebook} base={base} />
      ) : (
        <>
          <h1>Base</h1>
          {error ? (
            <ErrorNotice error={error} />
          ) : (
            <p>
              That base isn't in your notebook. <Link to="/notebook">Notebook</Link>
            </p>
          )}
        </>
      )}
    </main>
  );
}

function BasePage({ notebook, base }: { notebook: Notebook; base: NotebookBase }) {
  const saver = useOutletContext<Saver>();
  const [open, setOpen] = useState<"status" | "edit" | null>(null);
  const detail = notebook.base;
  const lost = averageLost(base.record);
  const accounts = new Set(detail?.players.map((item) => item.tag)).size;
  const { current, editable } = notebook.season;
  return (
    <>
      <PastSeasonNotice notebook={notebook} onBase />
      <BaseTile name={base.name} size="lg" />
      <div className="nb-base-title">
        <h1>{base.name}</h1>
        {/* A past Season's status is how the base ended it. */}
        {current ? (
          <button
            type="button"
            className={`nb-status nb-status-${base.status} nb-status-button`}
            onClick={() => setOpen("status")}
            aria-label={`Status: ${statusText(base, notebook)}. Change status`}
          >
            {statusText(base, notebook)} ▾
          </button>
        ) : (
          <span className={`nb-status nb-status-${base.status}`}>
            {statusText(base, notebook)}
          </span>
        )}
      </div>
      <p className="nb-headline">
        {base.record.defenses
          ? `Held ${base.record.held} of ${base.record.defenses}`
          : heldText(base.record).replace(/^./, (letter) => letter.toUpperCase())}
        <small>
          {[
            lost ? `${lost} trophies a defense` : null,
            base.record.days
              ? `${base.record.days} ${base.record.days === 1 ? "day" : "days"}`
              : null,
            accounts > 1 ? "all accounts" : null,
            notebook.asOfDay ? `as of Day ${notebook.asOfDay}` : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </small>
      </p>
      <div className="nb-actions">
        <CopyToSeason notebook={notebook} base={base} saver={saver} />
        {detail?.link ? (
          <a
            className="button"
            href={detail.link}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open in game
          </a>
        ) : null}
        {editable ? (
          <button
            type="button"
            className="button secondary"
            onClick={() => setOpen("edit")}
          >
            Edit
          </button>
        ) : null}
      </div>
      {/* Every number on the page itself, one section each; the best and
       * worst armies go in a section after Stars. */}
      <section className="nb-base-part" aria-labelledby="nb-stars-title">
        <h2 id="nb-stars-title">Stars</h2>
        <Stars record={base.record} />
      </section>
      <section className="nb-base-part" aria-labelledby="nb-each-title">
        <h2 id="nb-each-title">Each account</h2>
        {detail?.players.length ? (
          <ul className="nb-per-account">
            {detail.players.map((item) => {
              const player = notebook.players.find((one) => one.tag === item.tag);
              const each = averageLost(item.record);
              const runs = player?.runs.filter((run) => run.base === base.id) ?? [];
              const first = runs[0]?.from;
              const last = runs[runs.length - 1]?.to;
              return (
                <li key={item.tag}>
                  <b>{player ? playerName(player) : item.tag}</b>
                  <span>
                    {heldText(item.record)}
                    {each ? ` · ${each} a defense` : ""} · {item.record.days}{" "}
                    {item.record.days === 1 ? "day" : "days"}
                  </span>
                  {player && first && last ? (
                    <small>
                      {baseRuns(player, base.id)} ·{" "}
                      {first === last
                        ? dayDate(notebook.season.start, first)
                        : `${dayDate(notebook.season.start, first)} – ${dayDate(notebook.season.start, last)}`}
                    </small>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="nb-hint">No account has run it on a counted day yet.</p>
        )}
        <p className="nb-hint">{recordLine(notebook)}</p>
      </section>
      <NoteBox
        season={notebook.season.id}
        base={base}
        note={detail?.note ?? ""}
        saver={saver}
        readOnly={!editable}
      />
      {open === "status" ? (
        <StatusSheet
          notebook={notebook}
          base={base}
          saver={saver}
          onClose={() => setOpen(null)}
        />
      ) : null}
      {open === "edit" ? (
        <EditSheet
          base={base}
          home={notebookPath(notebook.season)}
          link={detail?.link ?? null}
          saver={saver}
          onClose={() => setOpen(null)}
        />
      ) : null}
    </>
  );
}

function Stars({ record }: { record: BaseRecord }) {
  return (
    <dl className="nb-stars">
      {[3, 2, 1, 0].map((stars) => (
        <div key={stars}>
          <dt>{stars}★</dt>
          <dd>{record.stars[stars]}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The base's note, saved as the Clasher types, when the box loses focus,
 * and when the page closes. */
function NoteBox({
  season,
  base,
  note,
  saver,
  readOnly,
}: {
  /** The Season of the notebook the note is in. */
  season: string;
  base: NotebookBase;
  note: string;
  saver: Saver;
  readOnly: boolean;
}) {
  const [text, setText] = useState(note);
  const [saved, setSaved] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  // Typed but not yet sent.
  const waiting = useRef<string | null>(null);
  const flush = useRef(() => {});
  flush.current = () => {
    window.clearTimeout(timer.current);
    const value = waiting.current;
    if (value === null) return;
    waiting.current = null;
    // Called only once the save succeeded.
    saver.save({ action: "edit", season, base: base.id, note: value }, () => {
      if (waiting.current === null) setSaved(true);
      return null;
    });
  };
  useEffect(() => () => flush.current(), []);
  return (
    <div className="form-field nb-note">
      <label htmlFor="nb-note">
        Note <span className="nb-saved">{saved ? "Saved" : ""}</span>
      </label>
      <textarea
        id="nb-note"
        value={text}
        maxLength={1000}
        rows={3}
        readOnly={readOnly}
        placeholder={readOnly ? "" : "Clan Castle troops, traps, anything"}
        onChange={(event) => {
          waiting.current = event.target.value;
          setText(event.target.value);
          setSaved(false);
          window.clearTimeout(timer.current);
          timer.current = window.setTimeout(() => flush.current(), NOTE_WAIT_MS);
        }}
        onBlur={() => flush.current()}
      />
    </div>
  );
}

/** Rename a base, change its base link, or delete it. */
function EditSheet({
  base,
  home,
  link,
  saver,
  onClose,
}: {
  base: NotebookBase;
  /** The notebook to go back to once the base is deleted. */
  home: string;
  link: string | null;
  saver: Saver;
  onClose: () => void;
}) {
  const [name, setName] = useState(base.name);
  const [newLink, setNewLink] = useState(link ?? "");
  const [deleting, setDeleting] = useState(false);
  const navigate = useNavigate();
  return (
    <Sheet title="Edit base" onClose={onClose}>
      <form
        className="nb-form"
        onSubmit={(event) => {
          event.preventDefault();
          saver.save(
            { action: "edit", base: base.id, name: name.trim(), link: newLink.trim() },
            () => ({ text: "Saved.", actions: [] }),
          );
          onClose();
        }}
      >
        <div className="form-field">
          <label htmlFor="nb-edit-name">Name</label>
          <input
            id="nb-edit-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={60}
            required
            autoComplete="off"
          />
        </div>
        <div className="form-field">
          <label htmlFor="nb-edit-link">Base link</label>
          <input
            id="nb-edit-link"
            value={newLink}
            onChange={(event) => setNewLink(event.target.value)}
            inputMode="url"
            autoComplete="off"
            placeholder="https://link.clashofclans.com/…"
          />
        </div>
        <button type="submit" className="button" disabled={!name.trim()}>
          Save
        </button>
      </form>
      <div className="nb-delete">
        {deleting ? (
          <>
            <p>Delete {base.name}? The days it ran stay, with no base.</p>
            <button
              type="button"
              className="button danger-button"
              onClick={() => {
                saver.save({ action: "delete", base: base.id }, () => ({
                  text: `${base.name} deleted.`,
                  actions: [],
                }));
                void navigate(home);
              }}
            >
              Delete base
            </button>
          </>
        ) : (
          <button type="button" className="nb-chip" onClick={() => setDeleting(true)}>
            Delete this base…
          </button>
        )}
      </div>
    </Sheet>
  );
}
