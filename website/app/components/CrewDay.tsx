import type { BoardRow } from "../lib/crew-contracts";

function trophyRows(rows: BoardRow[]) {
  return rows.flatMap((row) => (row.kind === "trophies" ? [row] : []));
}

/**
 * Each Live account's trophies won or lost today so far, from its Legend
 * day's settled start, as the dashboard's Legend day card counts them, so an
 * automatic defense loss the Reset reading was taken before is never today's.
 * Accounts whose day isn't known are left out.
 */
export function sinceReset(live: BoardRow[]): Map<string, number> {
  return new Map(
    trophyRows(live).flatMap((row) =>
      row.todayChange === null ? [] : [[row.tag, row.todayChange] as const],
    ),
  );
}

/**
 * The crew's Legend day so far, at the foot of its header: the trophies the
 * crew has won or lost together since the last Reset, then one bar split into
 * the accounts up, level and down, its counts at its ends on the same line.
 * It reads only the Live board the page already has; with no account's day
 * known yet, it doesn't show.
 */
export function CrewDay({ live }: { live: BoardRow[] }) {
  const moves = [...sinceReset(live).values()];
  if (moves.length === 0) return null;
  const up = moves.filter((move) => move > 0).length;
  const down = moves.filter((move) => move < 0).length;
  const level = moves.length - up - down;
  const total = moves.reduce((sum, move) => sum + move, 0);
  const tone = total > 0 ? "crew-day-up" : total < 0 ? "crew-day-down" : undefined;
  return (
    <section className="crew-day" aria-labelledby="crew-day-title">
      <h2 id="crew-day-title">Since the last Reset</h2>
      <p className="crew-day-total">
        Crew total{" "}
        <b className={tone}>
          {total > 0 ? "+" : total < 0 ? "−" : ""}
          {Math.abs(total).toLocaleString("en-US")}
        </b>
        <span className="sr-only"> trophies</span>
      </p>
      {/* A count of none stays grey, so only real moves take a colour. */}
      <p className="crew-day-counts">
        <span className={up ? "crew-day-up" : undefined}>{up} up</span>
        <span className="crew-day-bar" aria-hidden="true">
          {[
            ["up", up],
            ["level", level],
            ["down", down],
          ].map(([move, count]) =>
            count ? (
              <span
                key={move}
                data-move={move}
                style={{ flexGrow: count } as React.CSSProperties}
              />
            ) : null,
          )}
        </span>
        <span>
          {level > 0 ? `${level} level · ` : null}
          <span className={down ? "crew-day-down" : undefined}>{down} down</span>
        </span>
      </p>
    </section>
  );
}
