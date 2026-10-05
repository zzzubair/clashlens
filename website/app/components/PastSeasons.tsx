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

// Official and ClashKing finishes stream in after the rest of the page.
// An unavailable history response leaves the rest of the page usable.
// Visitors without JavaScript never see them; that is accepted for this section.
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

export function PastSeasonList({ finishes }: { finishes: PastSeasonFinish[] }) {
  return (
    <section className="data-section past-seasons" aria-labelledby="past-seasons-title">
      <div className="section-heading">
        <h2 id="past-seasons-title">Past Seasons</h2>
      </div>
      <p className="section-note">
        Older history from{" "}
        <a href="https://clashk.ing" rel="noopener">
          ClashKing
        </a>
        .
      </p>
      <div
        className="table-wrap top-space"
        tabIndex={0}
        role="region"
        aria-label="Past Seasons table"
      >
        <table className="data-table" aria-label="Past Seasons">
          <thead>
            <tr>
              <th scope="col">Season ended</th>
              <th scope="col">Global rank</th>
              <th scope="col">Final trophies</th>
            </tr>
          </thead>
          <tbody>
            {finishes.map((finish) => (
              <tr key={finish.seasonId}>
                <th scope="row">{pastSeasonLabel(finish)}</th>
                {finish.globalRank === null ? (
                  <td>Not recorded</td>
                ) : (
                  <td className="past-season-rank">
                    {`#${finish.globalRank.toLocaleString("en-GB")}`}
                  </td>
                )}
                <td>{finish.trophies?.toLocaleString("en-GB") ?? "Not recorded"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
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
