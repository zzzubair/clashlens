import { useEffect, useRef, useState } from "react";
import { useFetcher, useRouteLoaderData } from "react-router";

import type { OpponentRow } from "../lib/dashboard";
import { attackedName, playerName, type Notebook } from "../lib/notebook";
import type { RootLoaderData } from "../root";
import {
  canSet,
  Choice,
  dayOptions,
  LEGEND_ONLY,
  SaveNotice,
  Sheet,
  statusNews,
  useNotebookSave,
} from "./NotebookParts";
import "../notebook.css";

/** Whether Bases you attacked offers Save: the Notebook switch is on. */
export function useNotebookOn(): boolean {
  return useRouteLoaderData<RootLoaderData>("root")?.notebookEnabled === true;
}

/**
 * Save on Bases you attacked: one sheet for the base tapped, and the toast
 * with Undo, which stays after the sheet closes. `tag` is the dashboard's
 * account, `day` the Legend day the card shows. The sheet reads the
 * Notebook when it opens, to offer Active on that account only on its
 * Legend days.
 */
export function SaveAttacked({
  tag,
  day,
  row,
  onClose,
}: {
  tag: string;
  day: number;
  /** The base tapped; null while no sheet is open. */
  row: OpponentRow | null;
  onClose: () => void;
}) {
  const reader = useFetcher<{ notebook: Notebook | null }>();
  const notebook = reader.data?.notebook ?? null;
  const saver = useNotebookSave(notebook);
  const { load } = reader;
  const open = row !== null;
  useEffect(() => {
    if (open) void load("/notebook");
  }, [open, load]);
  return (
    <>
      {row ? (
        <SaveSheet
          key={`${row.tag}-${day}`}
          tag={tag}
          day={day}
          row={row}
          notebook={notebook}
          failed={reader.state === "idle" && reader.data !== undefined && !notebook}
          saver={saver}
          onClose={onClose}
        />
      ) : null}
      <SaveNotice saver={saver} />
    </>
  );
}

function SaveSheet({
  tag,
  day,
  row,
  notebook,
  failed,
  saver,
  onClose,
}: {
  tag: string;
  day: number;
  row: OpponentRow;
  notebook: Notebook | null;
  failed: boolean;
  saver: ReturnType<typeof useNotebookSave>;
  onClose: () => void;
}) {
  const [name, setName] = useState(attackedName(day, row.resetTrophies));
  const [status, setStatus] = useState<"to_try" | "active">("to_try");
  const [picked, setDay] = useState<number | null>(null);
  const input = useRef<HTMLInputElement>(null);
  // The name opens selected, so typing replaces it.
  useEffect(() => input.current?.select(), []);
  const player = notebook?.players.find((item) => item.tag === tag);
  const canRun = Boolean(notebook && player && canSet(notebook, player));
  const options = notebook && player ? dayOptions(notebook, player) : [];
  // Tomorrow unless they pick today: a new layout is up from the next Reset.
  const from =
    options.find(([option]) => option === picked)?.[0] ??
    options[options.length - 1]?.[0];
  const held = row.defenses.filter((defense) => defense.stars < 3).length;
  const who = player ? playerName(player) : "";
  return (
    <Sheet title="Save to Notebook" onClose={onClose}>
      <form
        className="nb-form"
        onSubmit={(event) => {
          event.preventDefault();
          const active = status === "active" && canRun && from !== undefined;
          const clean = name.trim();
          saver.save(
            {
              action: "add",
              name: clean,
              status: active ? "active" : "to_try",
              ...(active ? { tag, day: from } : {}),
              attacked: { tag, opponent: row.tag, day },
            },
            (saved, next) => ({
              text: [
                active
                  ? `${clean} is Active on ${who} from Day ${from}.`
                  : `${clean} saved as To try.`,
                statusNews(saved, next, saved.base ?? undefined),
              ].join(" "),
              actions: [],
            }),
          );
          onClose();
        }}
      >
        <p className="nb-them-line">
          held {held} of {row.defenses.length} for them on Day {day}
        </p>
        <div className="form-field">
          <label htmlFor="nb-save-name">Name</label>
          <input
            ref={input}
            id="nb-save-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={60}
            required
            autoFocus
            autoComplete="off"
          />
        </div>
        {failed ? (
          <p className="nb-hint">
            Your Notebook couldn't be read. Try again in a minute.
          </p>
        ) : !notebook ? (
          <p className="nb-hint">Loading your Notebook…</p>
        ) : canRun ? (
          <fieldset className="nb-fieldset">
            <legend>Status</legend>
            <Choice
              label="Status"
              value={status}
              onChange={setStatus}
              options={[
                ["to_try", "To try"],
                ["active", `Active on ${who}`],
              ]}
            />
            {status === "active" && from !== undefined ? (
              <Choice label="From" value={from} onChange={setDay} options={options} />
            ) : null}
          </fieldset>
        ) : (
          <p className="nb-hint">{LEGEND_ONLY} Saved as To try.</p>
        )}
        <button
          type="submit"
          className="button"
          disabled={!notebook || !name.trim() || saver.busy}
        >
          Save
        </button>
      </form>
    </Sheet>
  );
}
