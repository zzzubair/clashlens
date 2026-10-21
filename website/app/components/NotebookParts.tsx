import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { Link, useFetcher } from "react-router";

import {
  basesNear,
  baseInitials,
  baseLine,
  baseOnDay,
  dayDate,
  daysText,
  heldText,
  isLegendDay,
  libraryView,
  matchName,
  playerName,
  runOn,
  sameUntil,
  SORT_LABEL,
  STATUS_LABEL,
  stripBlocks,
  type BasePicture,
  type BaseStatus,
  type LibraryFilter,
  type LibrarySort,
  type Notebook,
  type NotebookBase,
  type NotebookChange,
  type NotebookPlayer,
  type NotebookSaved,
} from "../lib/notebook";
import { PictureButton, PictureImage } from "./NotebookPictures";

const SAVE = "/notebook/save";
export const LEGEND_ONLY = "Bases can be set on Legend days only.";
// Rows drawn at first, and added each time the list's end scrolls into view.
const PAGE_ROWS = 30;
const TOAST_MS = 8_000;

const ERRORS: Record<string, string> = {
  invalid_name: "Give the base a name of up to 60 characters.",
  invalid_base_link:
    "That isn't a base link from the game. Copy it from the layout's share button.",
  invalid_note: "Notes can be up to 1,000 characters.",
  invalid_characters: "Names and notes can't contain invisible characters.",
  invalid_day: "That day can't be changed any more. Refresh the page.",
  player_required: "Pick which account.",
  player_not_verified: "That account is no longer linked to you.",
  base_not_found: "That base was deleted. Refresh the page.",
  runs_until_season_end: "It runs until the Season ends.",
  not_legend_day: LEGEND_ONLY,
  invalid: "That didn't save. Refresh the page and try again.",
  unavailable: "Couldn't save just now. Try again in a moment.",
};

/** What the page says when a save is refused with `code`. */
export const saveError = (code: string): string => ERRORS[code] ?? ERRORS.unavailable!;

export interface Toast {
  text: string;
  actions: Array<{ label: string; change: NotebookChange; describe?: Describe }>;
}

type Describe = (saved: NotebookSaved, notebook: Notebook) => Toast | null;

/** Saves through /notebook/save, then words the result in a toast with Undo
 * once the page has read the notebook again. `notebook` is the page's, or
 * null on a page without one, such as a base just deleted. */
export function useNotebookSave(notebook: Notebook | null) {
  const fetcher = useFetcher<{ saved?: NotebookSaved; error?: string }>({
    key: "notebook",
  });
  const latest = useRef(notebook);
  if (notebook) latest.current = notebook;
  const describe = useRef<Describe | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const [error, setError] = useState<string | null>(null);
  const handled = useRef<unknown>(null);

  useEffect(() => {
    if (fetcher.state !== "idle" || !fetcher.data || handled.current === fetcher.data)
      return;
    handled.current = fetcher.data;
    if (fetcher.data.error) {
      setError(saveError(fetcher.data.error));
      return;
    }
    const saved = fetcher.data.saved;
    if (!saved) return;
    setError(null);
    const shown = (latest.current && describe.current?.(saved, latest.current)) ?? null;
    setToast(
      shown && saved.undo
        ? {
            ...shown,
            actions: [
              ...shown.actions,
              { label: "Undo", change: { action: "undo", undo: saved.undo } },
            ],
          }
        : shown,
    );
  }, [fetcher.state, fetcher.data]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), TOAST_MS);
    return () => window.clearTimeout(timer);
  }, [toast]);

  function save(change: NotebookChange, words?: Describe) {
    describe.current = words ?? null;
    setToast(null);
    void fetcher.submit(
      { change: JSON.stringify(change) },
      { method: "post", action: SAVE },
    );
  }

  return {
    save,
    busy: fetcher.state !== "idle",
    toast,
    error,
    closeToast: () => setToast(null),
    clearError: () => setError(null),
  };
}

export type Saver = ReturnType<typeof useNotebookSave>;

/** The toast and any save error, fixed to the bottom of the screen. */
export function SaveNotice({ saver }: { saver: Saver }) {
  return (
    <div className="nb-notice" aria-live="polite">
      {saver.error ? (
        <div className="nb-toast nb-toast-error" role="alert">
          <span>{saver.error}</span>
          <button type="button" className="nb-chip" onClick={saver.clearError}>
            OK
          </button>
        </div>
      ) : saver.toast ? (
        <div className="nb-toast" role="status">
          <span>{saver.toast.text}</span>
          <span className="nb-toast-actions">
            {saver.toast.actions.map((item) => (
              <button
                key={item.label}
                type="button"
                className="nb-chip"
                onClick={() => saver.save(item.change, item.describe ?? undoneWords)}
              >
                {item.label}
              </button>
            ))}
          </span>
        </div>
      ) : null}
    </div>
  );
}

const undoneWords: Describe = () => ({ text: "Undone.", actions: [] });

/** "Ring is now Retired." for each base a change moved, other than `except`. */
export function statusNews(
  saved: NotebookSaved,
  notebook: Notebook,
  except?: string,
): string {
  const names = new Map(notebook.bases.map((base) => [base.id, base.name]));
  return saved.changed
    .filter((item) => item.base !== except && names.has(item.base))
    .map((item) => `${names.get(item.base)} is now ${STATUS_LABEL[item.status]}.`)
    .join(" ");
}

/** A base's tile: its picture's small copy, or its initials without one. */
export function BaseTile({
  name,
  picture,
  size = "sm",
}: {
  name: string | null;
  picture?: BasePicture | null;
  size?: "sm" | "lg";
}) {
  return (
    <span
      className={`nb-tile nb-tile-${size}${name ? "" : " nb-tile-empty"}`}
      aria-hidden="true"
    >
      {picture ? (
        <PictureImage src={picture.small} alt="" loading="lazy" decoding="async" />
      ) : name ? (
        baseInitials(name)
      ) : (
        ""
      )}
    </span>
  );
}

/** A modal sheet: closes on its ✕, Escape, or a tap outside it. */
export function Sheet({
  title,
  onClose,
  children,
  head,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  head?: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (!element.open) element.showModal();
    const closed = () => close.current();
    element.addEventListener("close", closed);
    return () => element.removeEventListener("close", closed);
  }, []);
  return (
    <dialog
      ref={dialog}
      className="nb-sheet"
      aria-label={title}
      onClick={(event) => {
        if (event.target === event.currentTarget) dialog.current?.close();
      }}
    >
      <div className="nb-sheet-body">
        <header className="nb-sheet-head">
          <h2>{title}</h2>
          {head}
          <button
            type="button"
            className="nb-close"
            aria-label="Close"
            onClick={() => dialog.current?.close()}
          >
            ✕
          </button>
        </header>
        {children}
      </div>
    </dialog>
  );
}

/** Two to four choices in a row, one pressed. */
export function Choice<T extends string | number>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: Array<[T, string]>;
  onChange: (value: T) => void;
}) {
  return (
    <div className="nb-seg" role="group" aria-label={label}>
      {options.map(([option, text]) => (
        <button
          key={String(option)}
          type="button"
          aria-pressed={option === value}
          onClick={() => onChange(option)}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

/** Today or tomorrow, as the Change and status sheets offer them; with
 * ``player``, only those that are the player's Legend days. */
export function dayOptions(
  notebook: Notebook,
  player?: NotebookPlayer,
): Array<[number, string]> {
  const { today, tomorrow } = notebook.season;
  const days: Array<[number, string]> = [[today, `Day ${today} · today`]];
  if (tomorrow > today) days.push([tomorrow, `Day ${tomorrow} · tomorrow`]);
  return player ? days.filter(([day]) => isLegendDay(player, day)) : days;
}

/** Whether a base can be set on the player today or tomorrow. */
export function canSet(notebook: Notebook, player: NotebookPlayer): boolean {
  return dayOptions(notebook, player).length > 0;
}

/** Who runs a base, as "Active · Main, Alt from Day 6". */
export function statusText(base: NotebookBase, notebook: Notebook): string {
  if (base.status !== "active") return STATUS_LABEL[base.status];
  const on = notebook.players.flatMap((player) =>
    player.current === base.id
      ? [playerName(player)]
      : player.next?.base === base.id
        ? [`${playerName(player)} from Day ${player.next.day}`]
        : [],
  );
  return on.length ? `Active · ${on.join(", ")}` : "Active";
}

function Highlight({ name, query }: { name: string; query: string }) {
  const parts = matchName(name, query);
  if (!parts || !parts[1]) return <>{name}</>;
  return (
    <>
      {parts[0]}
      <mark>{parts[1]}</mark>
      {parts[2]}
    </>
  );
}

/** Draws rows 30 at a time, adding 30 more as the list's end scrolls near. */
function useRowLimit(resetOn: unknown) {
  const [limit, setLimit] = useState(PAGE_ROWS);
  const end = useRef<HTMLLIElement>(null);
  useEffect(() => setLimit(PAGE_ROWS), [resetOn]);
  useEffect(() => {
    const element = end.current;
    if (!element || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting))
          setLimit((value) => value + PAGE_ROWS);
      },
      { rootMargin: "400px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  });
  return { limit, end };
}

/** The search box every base list shares. */
function SearchBox({
  value,
  onChange,
  total,
}: {
  value: string;
  onChange: (value: string) => void;
  total: number;
}) {
  return (
    <div className="nb-search">
      <input
        type="search"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={`Search ${total} ${total === 1 ? "base" : "bases"}`}
        aria-label="Search bases by name"
        autoComplete="off"
        enterKeyHint="search"
      />
    </div>
  );
}

/** The Library: search, status filter with counts, sort, and a row per base
 * that opens its page. */
export function Library({ notebook, onNew }: { notebook: Notebook; onNew: () => void }) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<LibraryFilter>("all");
  const [sort, setSort] = useState<LibrarySort>("recent");
  const { rows, counts } = useMemo(
    () => libraryView(notebook.bases, query, filter, sort),
    [notebook.bases, query, filter, sort],
  );
  const { limit, end } = useRowLimit(`${query}|${filter}|${sort}`);
  const total = notebook.bases.length;
  return (
    <section className="nb-section" aria-labelledby="nb-library">
      <div className="nb-section-head">
        <h2 id="nb-library">
          Library{" "}
          <span className="nb-count">
            · {total} {total === 1 ? "base" : "bases"}
          </span>
        </h2>
        <button type="button" className="button nb-add" onClick={onNew}>
          + New base
        </button>
      </div>
      {total === 0 ? (
        <p className="nb-empty">
          No bases yet. Add the base you run now from your account above, or save one you
          plan to try with + New base.
        </p>
      ) : (
        <>
          <div className="nb-tools">
            <SearchBox value={query} onChange={setQuery} total={total} />
            <label className="nb-sort">
              <span className="sr-only">Sort</span>
              <select
                value={sort}
                onChange={(event) => setSort(event.target.value as LibrarySort)}
                aria-label="Sort bases"
              >
                {(Object.keys(SORT_LABEL) as LibrarySort[]).map((key) => (
                  <option key={key} value={key}>
                    {SORT_LABEL[key]}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <Choice
            label="Show bases"
            value={filter}
            onChange={setFilter}
            options={(["all", "active", "to_try", "retired"] as LibraryFilter[]).map(
              (key) => [
                key,
                `${key === "all" ? "All" : STATUS_LABEL[key]} ${counts[key]}`,
              ],
            )}
          />
          <ul className="nb-list">
            {rows.slice(0, limit).map((base) => (
              <li key={base.id}>
                <Link to={`/notebook/bases/${base.id}`} className="nb-row">
                  <BaseTile name={base.name} picture={base.picture} />
                  <span className="nb-row-text">
                    <b>
                      <Highlight name={base.name} query={query} />
                    </b>
                    <small>{baseLine(base, notebook.season.today)}</small>
                  </span>
                  <span className={`nb-status nb-status-${base.status}`}>
                    {statusText(base, notebook)}
                  </span>
                </Link>
                {/* Tapping the picture opens it; the rest of the row, the base. */}
                {base.picture ? (
                  <PictureButton base={base} picture={base.picture} size="cover" />
                ) : null}
              </li>
            ))}
            {rows.length > limit ? (
              <li ref={end} className="nb-more">
                {rows.length - limit} more as you scroll
              </li>
            ) : null}
          </ul>
          {rows.length === 0 ? (
            <p className="nb-empty">No base matches. Clear the search or pick All.</p>
          ) : null}
        </>
      )}
    </section>
  );
}

/** A tappable list of bases with search, for the Change and fix-a-day
 * sheets: tapping a row is the save. */
function PickList({
  notebook,
  onPick,
  top,
  autoFocus,
}: {
  notebook: Notebook;
  onPick: (base: NotebookBase) => void;
  top?: ReactNode;
  autoFocus?: boolean;
}) {
  const [query, setQuery] = useState("");
  const { rows } = useMemo(
    () => libraryView(notebook.bases, query, "all", "recent"),
    [notebook.bases, query],
  );
  const { limit, end } = useRowLimit(query);
  const input = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (autoFocus) input.current?.querySelector("input")?.focus();
  }, [autoFocus]);
  return (
    <>
      <div ref={input}>
        <SearchBox value={query} onChange={setQuery} total={notebook.bases.length} />
      </div>
      <ul className="nb-list nb-pick-list">
        {top}
        {rows.slice(0, limit).map((base) => (
          <li key={base.id}>
            <button type="button" className="nb-row" onClick={() => onPick(base)}>
              <BaseTile name={base.name} picture={base.picture} />
              <span className="nb-row-text">
                <b>
                  <Highlight name={base.name} query={query} />
                </b>
                <small>
                  {statusText(base, notebook)} · {baseLine(base, notebook.season.today)}
                </small>
              </span>
              <span className="nb-pick">Pick</span>
            </button>
            {base.picture ? (
              <PictureButton base={base} picture={base.picture} size="cover" />
            ) : null}
          </li>
        ))}
        {rows.length > limit ? (
          <li ref={end} className="nb-more">
            {rows.length - limit} more as you scroll
          </li>
        ) : null}
      </ul>
    </>
  );
}

const nameOf = (notebook: Notebook, code: string | null) =>
  notebook.bases.find((base) => base.id === code)?.name ?? "No base";

/** Change: which base one account runs from today or tomorrow. */
export function ChangeSheet({
  notebook,
  player,
  saver,
  onClose,
  onNew,
}: {
  notebook: Notebook;
  player: NotebookPlayer;
  saver: Saver;
  onClose: () => void;
  onNew: (day: number) => void;
}) {
  const options = dayOptions(notebook, player);
  const [day, setDay] = useState(
    options[options.length - 1]?.[0] ?? notebook.season.today,
  );
  const who = playerName(player);
  return (
    <Sheet
      title={`${who} from`}
      onClose={onClose}
      head={<Choice label="From" value={day} options={options} onChange={setDay} />}
    >
      <PickList
        notebook={notebook}
        top={
          <li>
            <button
              type="button"
              className="nb-row nb-row-new"
              onClick={() => onNew(day)}
            >
              <span className="nb-tile nb-tile-sm nb-tile-add" aria-hidden="true">
                +
              </span>
              <span className="nb-row-text">
                <b>New base</b>
                <small>
                  Active on {who} from Day {day}
                </small>
              </span>
            </button>
          </li>
        }
        onPick={(base) => {
          saver.save(
            { action: "pick", tag: player.tag, day, base: base.id },
            (saved, next) => ({
              text: [
                `${base.name} is Active on ${who} from Day ${day}.`,
                statusNews(saved, next, base.id),
              ].join(" "),
              actions: [],
            }),
          );
          onClose();
        }}
      />
    </Sheet>
  );
}

/** Fix a past day: which base was up on that day, for one account only. */
export function FixDaySheet({
  notebook,
  tag,
  day,
  saver,
  onClose,
}: {
  notebook: Notebook;
  tag: string;
  day: number;
  saver: Saver;
  onClose: () => void;
}) {
  const [searching, setSearching] = useState(false);
  const player = notebook.players.find((item) => item.tag === tag)!;
  const said = baseOnDay(player, day);
  const near = basesNear(player, day);
  const who = playerName(player);

  function pick(base: string | null) {
    // What ran the next day before this change, for "Only Day N".
    const before = baseOnDay(player, day + 1);
    saver.save({ action: "pick", tag: player.tag, day, base }, (saved, next) => {
      const now = next.players.find((item) => item.tag === player.tag);
      const to = now ? sameUntil(now, day, next.season.today) : day;
      const name = nameOf(next, base);
      const only =
        to > day && before !== base
          ? [
              {
                label: `Only Day ${day}`,
                change: {
                  action: "pick",
                  tag: player.tag,
                  day: day + 1,
                  base: before,
                } as const,
                describe: () => ({
                  text: `${name} on ${who}, Day ${day} only.`,
                  actions: [],
                }),
              },
            ]
          : [];
      return {
        text: [
          `${base ? name : "No base"} on ${who}, ${daysText(day, to)}.`,
          statusNews(saved, next, base ?? undefined),
        ].join(" "),
        actions: only,
      };
    });
    onClose();
  }

  return (
    <Sheet title={`Day ${day} · ${who}`} onClose={onClose}>
      <p className="nb-ask">Which base was up on {who}?</p>
      {searching ? (
        <PickList notebook={notebook} onPick={(base) => pick(base.id)} autoFocus />
      ) : (
        <>
          <ul className="nb-list nb-pick-list">
            {near.map((code) => {
              const base = notebook.bases.find((item) => item.id === code);
              if (!base) return null;
              const runs = player.runs.filter((run) => run.base === code);
              return (
                <li key={code}>
                  <button
                    type="button"
                    className="nb-row"
                    aria-current={code === said ? "true" : undefined}
                    onClick={() => (code === said ? onClose() : pick(code))}
                  >
                    <BaseTile name={base.name} picture={base.picture} />
                    <span className="nb-row-text">
                      <b>{base.name}</b>
                      <small>
                        {code === said
                          ? "what you said"
                          : `ran ${runs.map((run) => daysText(run.from, run.to)).join(", ")}`}
                      </small>
                    </span>
                    <span className="nb-pick">{code === said ? "✓" : "This one"}</span>
                  </button>
                  {base.picture ? (
                    <PictureButton base={base} picture={base.picture} size="cover" />
                  ) : null}
                </li>
              );
            })}
          </ul>
          <div className="nb-sheet-foot">
            <button type="button" className="nb-chip" onClick={() => setSearching(true)}>
              Search all {notebook.bases.length} bases
            </button>
            {said ? (
              <button type="button" className="nb-chip" onClick={() => pick(null)}>
                No base that day
              </button>
            ) : null}
          </div>
        </>
      )}
    </Sheet>
  );
}

export interface NewBasePreset {
  status: "to_try" | "active";
  tag?: string;
  day?: number;
}

/** A new base: a name, an optional base link, and To try or Active. The
 * likely status is ticked from where the Clasher started. */
export function NewBaseSheet({
  notebook,
  preset,
  saver,
  onClose,
}: {
  notebook: Notebook;
  preset: NewBasePreset;
  saver: Saver;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const [link, setLink] = useState("");
  const [status, setStatus] = useState<"to_try" | "active">(preset.status);
  // Only accounts playing Legend days can run it: the account where they
  // started, or the only one; otherwise they choose.
  const runners = notebook.players.filter((item) => canSet(notebook, item));
  const [tag, setTag] = useState(
    preset.tag ?? (runners.length === 1 ? runners[0]!.tag : ""),
  );
  const runner = runners.find((item) => item.tag === tag);
  const options = runner ? dayOptions(notebook, runner) : dayOptions(notebook);
  const [picked, setDay] = useState(preset.day);
  const day =
    options.find(([option]) => option === picked)?.[0] ??
    options[options.length - 1]?.[0] ??
    notebook.season.today;
  const canRun = runners.length > 0;
  const known = notebook.players.find((item) => item.tag === preset.tag);
  return (
    <Sheet
      title={known ? `New base on ${playerName(known)}` : "New base"}
      onClose={onClose}
    >
      <form
        className="nb-form"
        onSubmit={(event) => {
          event.preventDefault();
          const active = status === "active" && canRun;
          const who = playerName(
            notebook.players.find((item) => item.tag === tag) ?? { tag, name: null },
          );
          const clean = name.trim();
          saver.save(
            {
              action: "add",
              name: clean,
              ...(link.trim() ? { link: link.trim() } : {}),
              status: active ? "active" : "to_try",
              ...(active ? { tag, day } : {}),
            },
            (saved, next) => ({
              text: [
                active
                  ? `${clean} is Active on ${who} from Day ${day}.`
                  : `${clean} saved as To try.`,
                statusNews(saved, next, saved.base ?? undefined),
              ].join(" "),
              actions: [],
            }),
          );
          onClose();
        }}
      >
        <div className="form-field">
          <label htmlFor="nb-new-name">Name</label>
          <input
            id="nb-new-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={60}
            required
            autoFocus
            autoComplete="off"
            placeholder="like Ring v2"
          />
        </div>
        <div className="form-field">
          <label htmlFor="nb-new-link">Base link (optional)</label>
          <input
            id="nb-new-link"
            value={link}
            onChange={(event) => setLink(event.target.value)}
            inputMode="url"
            autoComplete="off"
            placeholder="https://link.clashofclans.com/…"
          />
        </div>
        {canRun ? (
          <fieldset className="nb-fieldset">
            <legend>Status</legend>
            <Choice
              label="Status"
              value={status}
              onChange={setStatus}
              options={[
                ["to_try", "To try"],
                ["active", "Active"],
              ]}
            />
            {status === "active" ? (
              <div className="nb-active-on">
                {/* Ask for the account only when where they started didn't say. */}
                {runners.length > 1 && !known ? (
                  <Choice
                    label="Account"
                    value={tag}
                    onChange={setTag}
                    options={runners.map((item) => [item.tag, playerName(item)])}
                  />
                ) : null}
                <Choice label="From" value={day} onChange={setDay} options={options} />
              </div>
            ) : (
              <p className="nb-hint">To try: saved for later.</p>
            )}
          </fieldset>
        ) : (
          <p className="nb-hint">{LEGEND_ONLY} Saved as To try.</p>
        )}
        <button
          type="submit"
          className="button"
          disabled={!name.trim() || (status === "active" && canRun && !runner)}
        >
          Save
        </button>
      </form>
    </Sheet>
  );
}

/** Set a base To try, Active (on which account, from when) or Retired. */
export function StatusSheet({
  notebook,
  base,
  saver,
  onClose,
}: {
  notebook: Notebook;
  base: NotebookBase;
  saver: Saver;
  onClose: () => void;
}) {
  const options = dayOptions(notebook);
  const [day, setDay] = useState(options[options.length - 1]![0]);
  const [choosing, setChoosing] = useState(false);
  // Accounts that play the chosen day in Legend League.
  const runners = notebook.players.filter((player) => isLegendDay(player, day));
  const { today, tomorrow } = notebook.season;
  const upToday = notebook.players.some((player) => player.current === base.id);
  const running =
    upToday || notebook.players.some((player) => player.next?.base === base.id);

  function set(status: BaseStatus, player?: NotebookPlayer) {
    const who = player ? playerName(player) : "";
    saver.save(
      {
        action: "status",
        base: base.id,
        status,
        ...(player ? { tag: player.tag, day } : {}),
      },
      (saved, next) => ({
        text: [
          status === "active"
            ? `${base.name} is Active on ${who} from Day ${day}.`
            : `${base.name} is ${STATUS_LABEL[status]}${upToday ? ` from Day ${tomorrow}` : ""}.`,
          statusNews(saved, next, base.id),
        ].join(" "),
        actions: [],
      }),
    );
    onClose();
  }

  return (
    <Sheet title={base.name} onClose={onClose}>
      <Choice
        label="Status"
        value={choosing ? "active" : base.status}
        onChange={(status) => {
          if (status !== "active") return set(status);
          if (notebook.players.length === 1 && runners.length === 1)
            return set("active", runners[0]);
          setChoosing(true);
        }}
        options={[
          ["to_try", "To try"],
          ["active", "Active"],
          ["retired", "Retired"],
        ]}
      />
      {notebook.players.length > 0 ? (
        <div className="nb-from">
          <span>Active from</span>
          <Choice label="Active from" value={day} onChange={setDay} options={options} />
        </div>
      ) : (
        <p className="nb-hint">Link a player to run a base.</p>
      )}
      {running ? (
        <p className="nb-hint">
          {tomorrow > today
            ? `To try or Retired stops it from Day ${tomorrow} · tomorrow.`
            : "It runs until the Season ends."}
        </p>
      ) : null}
      {choosing ? (
        <>
          <p className="nb-ask">Active on which account?</p>
          <ul className="nb-list nb-pick-list">
            {runners.map((player) => {
              const current = notebook.bases.find((item) => item.id === player.current);
              return (
                <li key={player.tag}>
                  <button
                    type="button"
                    className="nb-row"
                    onClick={() => set("active", player)}
                  >
                    <BaseTile
                      name={player.current ? nameOf(notebook, player.current) : null}
                      picture={current?.picture}
                    />
                    <span className="nb-row-text">
                      <b>{playerName(player)}</b>
                      <small>
                        {player.current
                          ? `now ${nameOf(notebook, player.current)}${
                              player.current === base.id
                                ? ""
                                : ` · it stops on Day ${day}`
                            }`
                          : "no base yet"}
                      </small>
                    </span>
                    <span className="nb-pick">Pick</span>
                  </button>
                  {current?.picture ? (
                    <PictureButton
                      base={current}
                      picture={current.picture}
                      size="cover"
                    />
                  ) : null}
                </li>
              );
            })}
          </ul>
          {runners.length < notebook.players.length ? (
            <p className="nb-hint">{LEGEND_ONLY}</p>
          ) : null}
        </>
      ) : null}
    </Sheet>
  );
}

/** One account's Season on one line: a block per run, today's in orange,
 * earlier ones tan, days to come outlined. A picture only; days are fixed
 * from the Days list. */
export function RunStrip({
  notebook,
  player,
}: {
  notebook: Notebook;
  player: NotebookPlayer;
}) {
  const { today } = notebook.season;
  const blocks = stripBlocks(player, today);
  const who = playerName(player);
  const summary = blocks
    .filter((block) => block.base)
    .map((block) => `${nameOf(notebook, block.base)} ${daysText(block.from, block.to)}`)
    .join(", ");
  return (
    <div className="nb-strip-wrap">
      <div
        className="nb-strip"
        role="img"
        aria-label={`${who}'s Season: ${summary || "no base picked yet"}. Today is Day ${today}.`}
      >
        {blocks.map((block) => {
          const length = block.to - block.from + 1;
          const name = nameOf(notebook, block.base);
          return (
            <span
              key={block.from}
              className={`nb-run nb-run-${block.kind}`}
              style={
                {
                  gridColumn: `${block.from} / ${block.to + 1}`,
                  "--nb-days": length,
                } as CSSProperties
              }
            >
              {/* Long runs show the name, short ones the tile's letters, one
               * day nothing: the Days list names every day. */}
              {block.base
                ? length >= 4
                  ? name
                  : length >= 2
                    ? baseInitials(name)
                    : ""
                : ""}
            </span>
          );
        })}
      </div>
      <div className="nb-axis" aria-hidden="true">
        <span>Day 1</span>
        <span>{today} · today</span>
        <span>28</span>
      </div>
    </div>
  );
}

/** An account's row: today's base and its run's record, Change and Days,
 * and the run strip. Bases are set only on the account's Legend days. */
export function AccountRow({
  notebook,
  player,
  onChange,
  onAdd,
  onDays,
}: {
  notebook: Notebook;
  player: NotebookPlayer;
  onChange: () => void;
  onAdd: () => void;
  onDays: () => void;
}) {
  const { today } = notebook.season;
  const run = runOn(player, today);
  const base = notebook.bases.find((item) => item.id === run?.base);
  const next = player.next;
  const settable = canSet(notebook, player);
  const fixable = player.legendDays.some((day) => day <= today);
  // Back in Legend League with no base: picking one is the next step.
  const pickNext = settable && !run && !next?.base && player.runs.length > 0;
  return (
    <article className="nb-account">
      <div className="nb-account-head">
        {base?.picture ? (
          <PictureButton base={base} picture={base.picture} />
        ) : (
          <BaseTile name={base?.name ?? null} />
        )}
        <div className="nb-account-text">
          <h3>
            {playerName(player)}
            {player.league ? <small> · {player.league}</small> : null}
          </h3>
          <p>
            {base && run ? (
              <>
                <Link to={`/notebook/bases/${base.id}`}>{base.name}</Link> · since Day{" "}
                {run.from} · {heldText(run.record)}
              </>
            ) : (
              "No base today"
            )}
            {next ? (
              <span className="nb-next">
                {" "}
                → {nameOf(notebook, next.base)} from Day {next.day}
              </span>
            ) : null}
          </p>
        </div>
      </div>
      <RunStrip notebook={notebook} player={player} />
      {settable || fixable ? (
        <div className="nb-account-actions">
          {settable && !player.runs.length && !player.next?.base ? (
            <button type="button" className="button" onClick={onAdd}>
              Add your base
            </button>
          ) : null}
          {settable ? (
            <button
              type="button"
              className={pickNext ? "button" : "button secondary"}
              onClick={onChange}
            >
              {pickNext ? "Pick a base" : "Change"}
            </button>
          ) : null}
          {fixable ? (
            <button type="button" className="button secondary" onClick={onDays}>
              Days
            </button>
          ) : null}
        </div>
      ) : null}
      {!settable ? <p className="nb-hint">{LEGEND_ONLY}</p> : null}
    </article>
  );
}

/** Every day of one account's Season so far, newest first, one full-width
 * row each; a Legend day opens the fix-a-day sheet. */
export function DaysSheet({
  notebook,
  player,
  onDay,
  onClose,
}: {
  notebook: Notebook;
  player: NotebookPlayer;
  onDay: (day: number) => void;
  onClose: () => void;
}) {
  const { today, start } = notebook.season;
  return (
    <Sheet title={`${playerName(player)} · days`} onClose={onClose}>
      <ul className="nb-list nb-days">
        {Array.from({ length: today }, (_, index) => today - index).map((day) => {
          const legend = isLegendDay(player, day);
          const code = baseOnDay(player, day);
          return (
            <li key={day}>
              <button
                type="button"
                className="nb-row nb-day-row"
                disabled={!legend}
                onClick={() => onDay(day)}
              >
                <span className="nb-row-text">
                  <b>
                    Day {day}
                    {day === today ? " · today" : ""}
                  </b>
                  <small>{dayDate(start, day)}</small>
                </span>
                <span className="nb-day-base">
                  {legend
                    ? code
                      ? nameOf(notebook, code)
                      : "No base"
                    : "Not a Legend day"}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </Sheet>
  );
}

/** How fresh the records are, for the line under the page title. */
export function recordLine(notebook: Notebook): string {
  return notebook.asOfDay
    ? `Defense records as of Day ${notebook.asOfDay}; a day's defenses count about 30 minutes after its Reset.`
    : "Defense records start once a Legend day this Season is counted, about 30 minutes after its Reset.";
}
