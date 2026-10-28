import { useState } from "react";
import { Link } from "react-router";

import { Choice, Sheet } from "./NotebookParts";
import {
  averageLost,
  heldText,
  playerName,
  WINDOWS,
  type BaseRecord,
  type BaseWindow,
  type Notebook,
  type NotebookBase,
  type WhoRow,
} from "../lib/notebook";

const BANDS = ["Under 5,400", "5,400–5,799", "5,800+"];
const number = new Intl.NumberFormat("en-GB");

/** "39%", a share of `total`, or "–" with none. */
function share(count: number, total: number): string {
  return total ? `${Math.round((100 * count) / total)}%` : "–";
}

/** The base's numbers from everyone who ran it, in the window the Clasher
 * picks: stars, holds, armies, starting trophies, and Who ran it. No
 * verdicts: each number shows with its count. */
export function BaseNumbers({
  notebook,
  base,
}: {
  notebook: Notebook;
  base: NotebookBase;
}) {
  const [span, setSpan] = useState<BaseWindow>("season");
  const [whoOpen, setWhoOpen] = useState(false);
  const detail = notebook.base;
  const numbers = detail?.windows?.[span];
  const record = numbers?.record ?? base.record;
  const label = WINDOWS.find(([key]) => key === span)?.[1] ?? "";
  const who = (detail?.who.rows ?? []).filter((row) => row.windows.includes(span));
  const total = detail?.who.total[span] ?? 0;
  return (
    <>
      <section className="nb-base-part" aria-labelledby="nb-stars-title">
        <h2 id="nb-stars-title">Stars</h2>
        {detail?.copies !== null && detail?.copies !== undefined ? (
          <p className="nb-hint">
            {detail.copies} copied · {detail.windows?.all.ranBy ?? base.ranBy} ran it
          </p>
        ) : null}
        {detail?.windows ? (
          <Choice label="Which days" value={span} options={WINDOWS} onChange={setSpan} />
        ) : null}
        <p className="nb-hint">
          {label}, from everyone who ran it
          {numbers
            ? ` · ${numbers.ranBy} ${numbers.ranBy === 1 ? "account" : "accounts"}`
            : ""}
          .
        </p>
        <dl className="nb-stars">
          {[3, 2, 1, 0].map((stars) => (
            <div key={stars}>
              <dt>{stars}★</dt>
              <dd>{record.stars[stars]}</dd>
              <small>{share(record.stars[stars], record.defenses)}</small>
            </div>
          ))}
        </dl>
        <p className="nb-hint">{heldLine(record)}</p>
        {detail?.windows ? (
          <button type="button" className="nb-chip" onClick={() => setWhoOpen(true)}>
            Who ran it ({total}) ›
          </button>
        ) : null}
      </section>
      {numbers ? (
        <>
          <section className="nb-base-part" aria-labelledby="nb-armies-title">
            <h2 id="nb-armies-title">3-starred by army</h2>
            {numbers.armies === null ? (
              <p className="nb-hint">
                Armies are being sorted again after a rules update.
              </p>
            ) : numbers.armies.length ? (
              <ul className="nb-per-account">
                {numbers.armies.map((army) => (
                  <li key={army.name}>
                    <b>{army.name}</b>
                    <span>
                      {army.threeStar} of {army.defenses}
                      {` · −${Math.round(army.trophiesLost / army.defenses)} a defense`}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="nb-hint">No counted defenses in these days.</p>
            )}
          </section>
          <section className="nb-base-part" aria-labelledby="nb-bands-title">
            <h2 id="nb-bands-title">By starting trophies</h2>
            <ul className="nb-per-account">
              {numbers.bands.map((band, index) => (
                <li key={BANDS[index]}>
                  <b>{BANDS[index]}</b>
                  <span>
                    {heldText(band)}
                    {averageLost(band) ? ` · ${averageLost(band)} a defense` : ""}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        </>
      ) : null}
      {whoOpen ? (
        <WhoRanIt
          name={base.name}
          label={label}
          rows={who}
          capped={total > who.length}
          crew={notebook.crew !== null}
          onClose={() => setWhoOpen(false)}
        />
      ) : null}
    </>
  );
}

/** "Held 61% (130 of 212) · 18 trophies lost a defense". */
function heldLine(record: BaseRecord): string {
  if (!record.defenses) return heldText(record).replace(/^./, (c) => c.toUpperCase());
  const lost = record.trophiesLost / record.defenses;
  return `Held ${share(record.held, record.defenses)} (${record.held} of ${record.defenses}) · ${lost.toFixed(1)} trophies lost a defense`;
}

/** Who ran it: one row per account per Legend day, newest first, for the
 * people in this notebook; each opens that day on the player's page. */
export function WhoRanIt({
  name,
  label,
  rows,
  capped,
  crew,
  onClose,
}: {
  name: string;
  label: string;
  rows: WhoRow[];
  /** Only the newest days came, not all of them. */
  capped: boolean;
  crew: boolean;
  onClose: () => void;
}) {
  return (
    <Sheet title={`Who ran ${name}`} onClose={onClose}>
      <p className="nb-ask">
        {label}, {crew ? "people in this Crew" : "your accounts"}
      </p>
      {rows.length ? (
        <ul className="nb-list nb-pick-list">
          {rows.map((row) => (
            <li key={`${row.tag} ${row.date}`}>
              <Link
                className="nb-row"
                to={`/players/${encodeURIComponent(row.tag)}?day=${row.date}`}
              >
                <span className="nb-row-text">
                  <b>
                    {[
                      row.owner,
                      playerName(row),
                      `Day ${row.day}`,
                      row.live ? "live" : null,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </b>
                  <small>{whoLine(row)}</small>
                </span>
                <span aria-hidden="true">›</span>
              </Link>
            </li>
          ))}
        </ul>
      ) : (
        <p className="nb-hint">
          {crew ? "Nobody in this Crew" : "None of your accounts"} ran it in these days.
        </p>
      )}
      {capped ? (
        <p className="nb-hint">Only the newest {rows.length} days are shown.</p>
      ) : null}
    </Sheet>
  );
}

/** "Started 5,312 · 8 defenses · −164 · held 5", "so far" for today. */
function whoLine(row: WhoRow): string {
  const start = row.start === null ? null : `Started ${number.format(row.start)}`;
  if (!row.defenses)
    return [
      start,
      row.defenses === 0
        ? row.live
          ? "no defenses yet"
          : "0 defenses"
        : row.live
          ? "no data yet"
          : "no data",
    ]
      .filter(Boolean)
      .join(" · ");
  return [
    start,
    `${row.defenses} ${row.defenses === 1 ? "defense" : "defenses"}`,
    `−${row.trophiesLost ?? 0}${row.live ? " so far" : ""}`,
    `held ${row.held ?? 0}`,
  ]
    .filter(Boolean)
    .join(" · ");
}
