import { Suspense } from "react";
import { Await } from "react-router";

import type { PastSeasonFinish } from "../lib/contracts";

const endFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});
const monthFormatter = new Intl.DateTimeFormat("en-GB", {
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

// Past Season finishes reported by ClashKing. They stream in after the rest
// of the page and show nothing when ClashKing has none or is slow or down.
export function PastSeasons({
  finishes,
}: {
  finishes: Promise<PastSeasonFinish[] | null> | undefined;
}) {
  if (finishes === undefined) return null;
  return (
    <Suspense fallback={null}>
      <Await resolve={finishes} errorElement={null}>
        {(resolved) =>
          resolved && resolved.length > 0 ? <PastSeasonList finishes={resolved} /> : null
        }
      </Await>
    </Suspense>
  );
}

const SHOWN_FIRST = 10;

export function PastSeasonList({ finishes }: { finishes: PastSeasonFinish[] }) {
  const older = finishes.slice(SHOWN_FIRST);
  return (
    <section className="data-section past-seasons" aria-labelledby="past-seasons-title">
      <div className="section-heading">
        <h2 id="past-seasons-title">Past Seasons</h2>
      </div>
      <p className="past-seasons-source">
        Source:{" "}
        <a href="https://clashk.ing" rel="noopener">
          ClashKing
        </a>
      </p>
      <p className="section-note">
        Final trophies and global rank from earlier Legend seasons, as recorded by
        ClashKing. They are not Clash Lens tracking and are never added to the daily log
        or totals. Dates show when each Season ended; older seasons ran by calendar month.
      </p>
      <FinishTable finishes={finishes.slice(0, SHOWN_FIRST)} label="Past Seasons" />
      {older.length > 0 ? (
        <details className="top-space">
          <summary>Show {older.length} older seasons</summary>
          <FinishTable finishes={older} label="Older past Seasons" />
        </details>
      ) : null}
    </section>
  );
}

function FinishTable({
  finishes,
  label,
}: {
  finishes: PastSeasonFinish[];
  label: string;
}) {
  return (
    <div className="table-wrap" tabIndex={0} role="region" aria-label={`${label} table`}>
      <table className="data-table" aria-label={label}>
        <thead>
          <tr>
            <th scope="col">Season</th>
            <th scope="col">Final trophies</th>
            <th scope="col">Global rank</th>
          </tr>
        </thead>
        <tbody>
          {finishes.map((finish) => (
            <tr key={finish.seasonId}>
              <th scope="row">{pastSeasonLabel(finish)}</th>
              <td>{finish.trophies.toLocaleString("en-GB")}</td>
              <td>
                {finish.globalRank === null
                  ? "Not recorded"
                  : `#${finish.globalRank.toLocaleString("en-GB")}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function pastSeasonLabel(finish: PastSeasonFinish): string {
  if (finish.seasonEnd !== null) {
    return endFormatter.format(new Date(finish.seasonEnd)).replace("Sept", "Sep");
  }
  return monthFormatter
    .format(new Date(`${finish.seasonId}-01T00:00:00Z`))
    .replace("Sept", "Sep");
}
