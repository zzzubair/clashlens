import { useState, type ReactNode } from "react";

import { Sheet, type Saver } from "./NotebookParts";
import {
  dayDate,
  dayText,
  isLegendDay,
  playerName,
  type CopyAccount,
  type Notebook,
  type NotebookBase,
  type NotebookChange,
  type NotebookPlayer,
} from "../lib/notebook";

// A run counts for this many Legend days after it was last confirmed.
const CHECK_DAYS = 7;

/** A choice in the copy sheet: with a game link it opens the game on the
 * base in a new tab, as the tap that saves, so phones hand it to the game. */
function CopyOption({
  link,
  onPick,
  children,
}: {
  link: string | null;
  onPick: () => void;
  children: ReactNode;
}) {
  return link ? (
    <a
      className="nb-row"
      href={link}
      target="_blank"
      rel="noopener noreferrer"
      onClick={onPick}
    >
      {children}
    </a>
  ) : (
    <button type="button" className="nb-row" onClick={onPick}>
      {children}
    </button>
  );
}

/** "Copy base": which account will run it, from tomorrow or already today,
 * or just looking; then the game opens on the base. */
export function CopySheet({
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
  const [already, setAlready] = useState(false);
  const { today, tomorrow } = notebook.season;
  const link = notebook.base?.link ?? null;
  const accounts = notebook.base?.copy ?? [];
  const day = already ? today : tomorrow;
  const shown = accounts.filter((account) =>
    already ? account.today : account.tomorrow,
  );
  const inMine = notebook.base?.shelves.includes("mine") ?? false;
  const when = already ? `today (Day ${today})` : `from tomorrow (${dayText(tomorrow)})`;
  function run(account: CopyAccount) {
    // Picks go in your own notebook, adding the base there first.
    const change: NotebookChange = inMine
      ? { action: "pick", tag: account.tag, day, base: base.id, crew: null }
      : { action: "add", base: base.id, tag: account.tag, day, crew: null };
    saver.save(change, () => ({
      text: `${base.name} runs on ${playerName(account)} ${already ? "from today" : "from tomorrow"}.`,
      actions: [],
    }));
    onClose();
  }
  return (
    <Sheet title={`Copy ${base.name}`} onClose={onClose}>
      <p className="nb-ask">Which account will run it?</p>
      <ul className="nb-list nb-pick-list">
        {shown.map((account) => (
          <li key={account.tag}>
            <CopyOption link={link} onPick={() => run(account)}>
              <span className="nb-row-text">
                <b>{playerName(account)}</b>
                <small>{when}</small>
              </span>
            </CopyOption>
          </li>
        ))}
        <li>
          <CopyOption link={link} onPick={onClose}>
            <span className="nb-row-text">
              <b>Just looking</b>
              <small>Your notebook stays as it is</small>
            </span>
          </CopyOption>
        </li>
      </ul>
      {!already && accounts.some((account) => account.today) ? (
        <button type="button" className="nb-chip" onClick={() => setAlready(true)}>
          It's already my base today (Day {today}) ›
        </button>
      ) : null}
      <p className="nb-hint">
        {shown.length === 0
          ? `None of your accounts plays a Legend day ${already ? "today" : "tomorrow"}. `
          : ""}
        {link
          ? "Then Clash of Clans opens on the base, so you can copy it there."
          : "This base has no game link yet, so the game won't open."}
      </p>
    </Sheet>
  );
}

/** "Still on Ring on Main?": a run not confirmed for CHECK_DAYS Legend
 * days waits, counting for nothing, until the Clasher answers. */
export function StillOn({
  notebook,
  player,
  saver,
  onChanged,
}: {
  notebook: Notebook;
  player: NotebookPlayer;
  saver: Saver;
  onChanged: () => void;
}) {
  const waiting = player.waiting;
  if (!waiting || !notebook.season.current) return null;
  const name =
    notebook.bases.find((base) => base.id === waiting.base)?.name ?? "that base";
  const first = waiting.seen + CHECK_DAYS + 1;
  return (
    <div className="nb-still" role="group" aria-label="Still on it?">
      <p>
        <b>
          Still on {name} on {playerName(player)}?
        </b>{" "}
        {first >= 1 ? `Its days from ${dayText(first)}` : "Its recent days"} don't count
        until you say.
      </p>
      <div className="nb-sheet-foot">
        <button
          type="button"
          className="button"
          onClick={() =>
            saver.save({ action: "confirm", tag: player.tag }, () => ({
              text: `${name}'s days on ${playerName(player)} count again.`,
              actions: [],
            }))
          }
        >
          Yes
        </button>
        <button type="button" className="button secondary" onClick={onChanged}>
          No, I changed
        </button>
      </div>
    </div>
  );
}

/** After "No, I changed": the day they changed, from the day after the run
 * was last confirmed to today. Only the days before it count. */
export function ChangedSheet({
  notebook,
  player,
  saver,
  onClose,
}: {
  notebook: Notebook;
  player: NotebookPlayer;
  saver: Saver;
  onClose: () => void;
}) {
  const { start, today } = notebook.season;
  const from = Math.max(1, (player.waiting?.seen ?? 0) + 1);
  const days = [];
  for (let day = today; day >= from; day -= 1)
    if (isLegendDay(player, day)) days.push(day);
  return (
    <Sheet title="Which day did you change?" onClose={onClose}>
      <p className="nb-ask">The first day {playerName(player)} ran another base</p>
      <ul className="nb-list nb-pick-list">
        {days.map((day) => (
          <li key={day}>
            <button
              type="button"
              className="nb-row"
              onClick={() => {
                saver.save(
                  { action: "pick", tag: player.tag, day, base: null, crew: null },
                  () => ({
                    text: `${playerName(player)} has no base from ${dayText(day)}. Pick the one you run now.`,
                    actions: [],
                  }),
                );
                onClose();
              }}
            >
              <span className="nb-row-text">
                <b>{dayText(day)}</b>
                <small>{dayDate(start, day)}</small>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </Sheet>
  );
}
