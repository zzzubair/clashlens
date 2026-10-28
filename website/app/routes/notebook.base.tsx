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
  basePath,
  baseRuns,
  bookPath,
  canManage,
  CREW_ID,
  dayDate,
  heldText,
  noDataText,
  playerName,
  type BaseRecord,
  type Notebook,
  type NotebookBase,
} from "../lib/notebook";
import type { Route } from "./+types/notebook.base";
import "../notebook.css";

const NO_STORE = { "Cache-Control": "no-store" };
// A note saves this long after the last key press.
const NOTE_WAIT_MS = 800;
const SAVE = "/notebook/save";

export const handle: BackHandle = {
  back: (match) => {
    const crew = (match.loaderData as { notebook?: Notebook | null } | undefined)
      ?.notebook?.crew;
    return crew
      ? { to: bookPath({ crew }), label: `${crew.name} bases` }
      : { to: "/notebook", label: "Notebook" };
  },
};

export function meta({ loaderData: loaded }: Route.MetaArgs) {
  const name = loaded?.notebook?.bases.find((base) => base.id === loaded.code)?.name;
  return [{ title: `${name ?? "Base"} · Notebook · Clash Lens` }];
}

/** GET /notebook/bases/:code — one base in the Clasher's notebook: its
 * record this Season from everyone who ran it, each of the Clasher's
 * accounts on it, its note and link. GET /crews/:crewId/bases/:code — the
 * same base in a Crew's notebook, for its members only. The base ID is for
 * support, so the page never shows it. */
export async function loader({ request, params }: Route.LoaderArgs) {
  const { isNotebookEnabled } = await import("../server/config.server");
  if (!isNotebookEnabled()) throw data(null, { status: 404 });
  const code = params.code;
  const crew = (params as { crewId?: string }).crewId;
  if (!BASE_ID.test(code) || (crew !== undefined && !CREW_ID.test(crew)))
    throw data(null, { status: 404 });
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { PythonApiError } = await import("../services/python.server");
  try {
    const { getNotebook } = await import("../services/notebook.server");
    const notebook: Notebook = await getNotebook(identity, { base: code, crew });
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
        <BasePage notebook={notebook} base={base} />
      ) : (
        <>
          <h1>Base</h1>
          {error ? (
            <ErrorNotice error={error} />
          ) : (
            <p>
              That base isn't in this notebook. <Link to="/notebook">Notebook</Link>
            </p>
          )}
        </>
      )}
    </main>
  );
}

function BasePage({ notebook, base }: { notebook: Notebook; base: NotebookBase }) {
  const saver = useOutletContext<Saver>();
  const [open, setOpen] = useState<"status" | "edit" | "crews" | null>(null);
  const [removing, setRemoving] = useState(false);
  const navigate = useNavigate();
  const detail = notebook.base;
  const lost = averageLost(base.record);
  const running = base.status === "running";
  const { crew } = notebook;
  const manage = canManage(notebook, base);
  const shelves = detail?.shelves ?? [];
  // Crews you can add bases to: you're their owner, an admin or an Uploader.
  const uploads = notebook.crews.filter((item) => item.role !== "member");
  return (
    <>
      <BaseTile name={base.name} size="lg" />
      <div className="nb-base-title">
        <h1>{base.name}</h1>
        {manage ? (
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
            base.ranBy
              ? `${base.ranBy} ${base.ranBy === 1 ? "account" : "accounts"} ran it`
              : null,
            base.record.days
              ? `${base.record.days} ${base.record.days === 1 ? "day" : "days"}`
              : null,
            noDataText(base.record),
            notebook.asOfDay ? `as of Day ${notebook.asOfDay}` : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </small>
      </p>
      <div className="nb-actions">
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
        {base.mine ? (
          <button
            type="button"
            className="button secondary"
            onClick={() => setOpen("edit")}
          >
            Edit
          </button>
        ) : null}
        {crew && !shelves.includes("mine") ? (
          <button
            type="button"
            className="button secondary"
            onClick={() =>
              saver.save({ action: "add", base: base.id, crew: null }, () => ({
                text: `${base.name} added to your notebook.`,
                actions: [],
              }))
            }
          >
            Add to my notebook
          </button>
        ) : null}
        {crew && shelves.includes("mine") ? (
          <Link className="button secondary" to={basePath({ crew: null }, base.id)}>
            In your notebook ›
          </Link>
        ) : null}
        {!crew && uploads.length ? (
          <button
            type="button"
            className="button secondary"
            onClick={() => setOpen("crews")}
          >
            Add to Crew
          </button>
        ) : null}
      </div>
      <p className="nb-hint">
        {crew
          ? "This Season, from everyone who ran it."
          : "This Season, from everyone who ran it. The rows below are your accounts."}
      </p>
      {/* Every number on the page itself, one section each; the best and
       * worst armies go in a section after Stars. */}
      <section className="nb-base-part" aria-labelledby="nb-stars-title">
        <h2 id="nb-stars-title">Stars</h2>
        <Stars record={base.record} />
      </section>
      <section className="nb-base-part" aria-labelledby="nb-each-title" hidden={!!crew}>
        <h2 id="nb-each-title">Your accounts</h2>
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
                    {item.record.noData ? ` · ${noDataText(item.record)}` : ""}
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
          <p className="nb-hint">
            None of your accounts has run it on a counted day yet.
          </p>
        )}
        <p className="nb-hint">{recordLine(notebook)}</p>
      </section>
      {base.mine ? (
        <NoteBox key={base.id} base={base} note={detail?.note ?? ""} />
      ) : detail?.note ? (
        <p className="nb-note-text">{detail.note}</p>
      ) : null}
      {manage ? (
        <div className="nb-delete">
          {running ? (
            <p className="nb-hint">
              {crew
                ? `Someone in ${crew.name} runs it, so it stays until they pick another base.`
                : "One of your accounts runs it, so it stays in your notebook until you pick another base."}
            </p>
          ) : removing ? (
            <>
              <p>
                Remove {base.name} from {crew ? crew.name : "your notebook"}? Its results
                stay.
              </p>
              <button
                type="button"
                className="button danger-button"
                onClick={() => {
                  saver.save({ action: "remove", base: base.id }, () => ({
                    text: `${base.name} removed from ${crew ? crew.name : "your notebook"}.`,
                    actions: [],
                  }));
                  void navigate(bookPath(notebook));
                }}
              >
                Remove
              </button>
            </>
          ) : (
            <button type="button" className="nb-chip" onClick={() => setRemoving(true)}>
              {crew ? `Remove from ${crew.name}…` : "Remove from my notebook…"}
            </button>
          )}
        </div>
      ) : null}
      {open === "status" ? (
        <StatusSheet
          notebook={notebook}
          base={base}
          saver={saver}
          onClose={() => setOpen(null)}
        />
      ) : null}
      {open === "crews" ? (
        <Sheet title={`Add ${base.name} to a Crew`} onClose={() => setOpen(null)}>
          <ul className="nb-list nb-pick-list">
            {uploads.map((item) =>
              shelves.includes(item.id) ? (
                <li key={item.id}>
                  <Link className="nb-row" to={basePath({ crew: item }, base.id)}>
                    <span className="nb-row-text">
                      <b>{item.name}</b>
                      <small>In it already</small>
                    </span>
                    <span className="nb-pick">Open</span>
                  </Link>
                </li>
              ) : (
                <li key={item.id}>
                  <button
                    type="button"
                    className="nb-row"
                    onClick={() => {
                      saver.save({ action: "add", base: base.id, crew: item.id }, () => ({
                        text: `${base.name} added to ${item.name}.`,
                        actions: [],
                      }));
                      setOpen(null);
                    }}
                  >
                    <span className="nb-row-text">
                      <b>{item.name}</b>
                      <small>Everyone in it can copy the base</small>
                    </span>
                    <span className="nb-pick">Add</span>
                  </button>
                </li>
              ),
            )}
          </ul>
        </Sheet>
      ) : null}
      {open === "edit" ? (
        <EditSheet
          base={base}
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

// Note saves go one after another, and carry on after the page closes, so
// an older text can never land after a newer one.
let noteSaves: Promise<unknown> = Promise.resolve();

function sendNote(base: string, note: string): Promise<boolean> {
  const body = new URLSearchParams({
    change: JSON.stringify({ action: "edit", base, note }),
  });
  const sent = noteSaves.then(() =>
    fetch(SAVE, { method: "POST", body }).then(
      (response) => response.ok,
      () => false,
    ),
  );
  noteSaves = sent;
  return sent;
}

/** The base's note, saved as the Clasher types, when the box loses focus,
 * and when the page closes. */
function NoteBox({ base, note }: { base: NotebookBase; note: string }) {
  const [text, setText] = useState(note);
  const [saved, setSaved] = useState<"" | "Saved" | "Not saved">("");
  const timer = useRef<number | undefined>(undefined);
  // Typed but not yet sent.
  const waiting = useRef<string | null>(null);
  const open = useRef(true);
  const flush = useRef(() => {});
  flush.current = () => {
    window.clearTimeout(timer.current);
    const value = waiting.current;
    if (value === null) return;
    waiting.current = null;
    void sendNote(base.id, value).then((ok) => {
      if (open.current && waiting.current === null) setSaved(ok ? "Saved" : "Not saved");
    });
  };
  useEffect(() => {
    open.current = true;
    return () => {
      open.current = false;
      flush.current();
    };
  }, []);
  return (
    <div className="form-field nb-note">
      <label htmlFor="nb-note">
        Note <span className="nb-saved">{saved}</span>
      </label>
      <textarea
        id="nb-note"
        value={text}
        maxLength={1000}
        rows={3}
        placeholder="Clan Castle troops, traps, anything"
        onChange={(event) => {
          waiting.current = event.target.value;
          setText(event.target.value);
          setSaved("");
          window.clearTimeout(timer.current);
          timer.current = window.setTimeout(() => flush.current(), NOTE_WAIT_MS);
        }}
        onBlur={() => flush.current()}
      />
    </div>
  );
}

/** Rename a base or change its base link: only its maker can. */
function EditSheet({
  base,
  link,
  saver,
  onClose,
}: {
  base: NotebookBase;
  link: string | null;
  saver: Saver;
  onClose: () => void;
}) {
  const [name, setName] = useState(base.name);
  const [newLink, setNewLink] = useState(link ?? "");
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
    </Sheet>
  );
}
