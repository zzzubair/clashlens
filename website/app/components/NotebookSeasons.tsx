import { Link, useNavigate } from "react-router";

import {
  momentText,
  notebookPath,
  seasonLabel,
  type Notebook,
  type NotebookBase,
} from "../lib/notebook";
import type { Saver } from "./NotebookParts";

/** The Season under the page title: a switcher when the Clasher has a past
 * notebook, otherwise just its dates. */
export function SeasonSwitcher({ notebook }: { notebook: Notebook }) {
  const navigate = useNavigate();
  const { season, seasons } = notebook;
  const day = season.current ? ` · Day ${season.today} of 28` : "";
  if (seasons.length < 2)
    return (
      <p className="nb-season">
        {seasonLabel(season.start, season.end)}
        {day}
      </p>
    );
  return (
    <form className="nb-season nb-season-pick" method="get" action="/notebook">
      <label htmlFor="nb-season" className="sr-only">
        Season
      </label>
      <select
        id="nb-season"
        name="season"
        value={season.id}
        onChange={(event) =>
          void navigate(
            notebookPath({
              id: event.target.value,
              current: event.target.value === seasons[0]!.id,
            }),
          )
        }
      >
        {seasons.map((item, index) => (
          <option key={item.id} value={item.id}>
            {seasonLabel(item.start, item.end)}
            {index === 0 ? " · this Season" : ""}
          </option>
        ))}
      </select>
      <noscript>
        <button type="submit" className="button secondary">
          Open
        </button>
      </noscript>
      {day ? <span>{day}</span> : null}
    </form>
  );
}

/** What a past Season's notebook still takes: its days, notes and names
 * until the end of the Season after it, then nothing. On the Notebook page
 * it also says how to copy a base, with a way back to this Season. */
export function PastSeasonNotice({
  notebook,
  onBase = false,
}: {
  notebook: Notebook;
  onBase?: boolean;
}) {
  const { season } = notebook;
  if (season.current) return null;
  return (
    <div className="nb-past" role="note">
      <p>
        {season.editable
          ? `This Season has ended. You can fix its days, notes and names until ${momentText(season.editableUntil)}; then it's read-only.`
          : "This Season's notebook is read-only."}
        {onBase ? "" : " Open a base to copy it to this Season."}
      </p>
      {onBase ? null : <Link to={notebookPath()}>Back to this Season</Link>}
    </div>
  );
}

/** On a past Season's base: Copy to this Season, a new base there saved as
 * To try, or a link to the one already there. */
export function CopyToSeason({
  notebook,
  base,
  saver,
}: {
  notebook: Notebook;
  base: NotebookBase;
  saver: Saver;
}) {
  if (notebook.season.current) return null;
  const there = notebook.base?.thisSeason;
  if (there)
    return (
      <Link className="button secondary" to={`/notebook/bases/${there}`}>
        In this Season
      </Link>
    );
  return (
    <button
      type="button"
      className="button"
      disabled={saver.busy}
      onClick={() =>
        saver.save({ action: "copy", season: notebook.season.id, base: base.id }, () => ({
          text: `${base.name} copied to this Season as To try.`,
          actions: [],
        }))
      }
    >
      Copy to this Season
    </button>
  );
}
