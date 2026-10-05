// One side of a Season's recorded battles. Trophies are totals moved, never
// signed; null anywhere means that part is unknown.
export interface SummarySide {
  count: number | null;
  stars: Array<number | null>;
  unknown: number | null;
  trophies: number | null;
  perDay: number | null;
}

const count = (value: number | null) =>
  value === null ? "Unknown" : value.toLocaleString("en-GB");
const signed = (value: number | null, sign: 1 | -1, digits = 0) =>
  value === null
    ? "Unavailable"
    : value === 0
      ? "0"
      : `${sign > 0 ? "+" : "-"}${value.toLocaleString("en-GB", {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        })}`;
// Words such as "Not available yet" take a smaller size than numbers.
const words = (value: string) => (/[a-z]/i.test(value) ? "summary-words" : undefined);
export const per = (total: number | null, by: number | null) =>
  total === null || !by ? null : total / by;

// The one Season box: rank and trophies first, then hit rate, battles by
// stars and averages. Without battles it shows only the headline. An ended
// Season's final rank is its standout number.
export function SeasonSummary({
  title,
  controls,
  rank,
  finalRank = false,
  trophies,
  attack,
  defense,
  children,
}: {
  title: string;
  controls?: React.ReactNode;
  rank: [label: string, value: string];
  finalRank?: boolean;
  trophies: [label: string, value: string];
  attack?: SummarySide;
  defense?: SummarySide;
  children?: React.ReactNode;
}) {
  const total = attack?.count ?? null;
  const triples = attack?.stars[3] ?? null;
  const unknown = [
    [attack?.unknown, "attack"],
    [defense?.unknown, "defense"],
  ].flatMap(([value, side]) =>
    typeof value === "number" && value > 0
      ? [`${count(value)} ${side}${value === 1 ? "" : "s"}`]
      : [],
  );
  return (
    <section
      className="data-section season-summary"
      aria-labelledby="season-summary-title"
    >
      <div className="season-summary-head">
        <h2 id="season-summary-title">{title}</h2>
        {controls}
      </div>
      <dl className="season-summary-headline">
        {[rank, trophies].map(([label, value]) => (
          <div
            key={label}
            className={finalRank && label === rank[0] ? "season-summary-rank" : undefined}
          >
            <dt>{label}</dt>
            <dd className={words(value)}>{value}</dd>
          </div>
        ))}
        {attack ? (
          <div>
            <dt>Hit rate</dt>
            <dd className={triples === null || !total ? "summary-words" : undefined}>
              {triples === null || !total
                ? "Unavailable"
                : `${((100 * triples) / total).toFixed(1)}%`}
              {triples === null || !total ? null : (
                <small>
                  {count(triples)} of {count(total)} attacks
                </small>
              )}
            </dd>
          </div>
        ) : null}
      </dl>
      {attack && defense ? (
        <>
          <div
            className="season-summary-table"
            tabIndex={0}
            role="region"
            aria-label="Battles by stars"
          >
            <table>
              <thead>
                <tr>
                  <td />
                  <th scope="col">Total</th>
                  {[3, 2, 1, 0].map((stars) => (
                    <th scope="col" key={stars}>
                      <span aria-hidden="true">{stars}★</span>
                      <span className="sr-only">{stars}-star</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(
                  [
                    ["Attacks", attack],
                    ["Defenses", defense],
                  ] as const
                ).map(([label, side]) => (
                  <tr key={label}>
                    <th scope="row">{label}</th>
                    <td>{count(side.count)}</td>
                    {[3, 2, 1, 0].map((stars) => (
                      <td key={stars}>{count(side.stars[stars] ?? null)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {unknown.length > 0 ? (
            <p className="section-note">Stars unknown for {unknown.join(" and ")}.</p>
          ) : null}
          <dl className="season-summary-averages">
            {(
              [
                ["Offense per day", signed(attack.perDay, 1)],
                ["Defense per day", signed(defense.perDay, -1)],
                ["Per attack", signed(per(attack.trophies, attack.count), 1, 1)],
                ["Per defense", signed(per(defense.trophies, defense.count), -1, 1)],
                ["Trophies lost", signed(defense.trophies, -1)],
              ] as const
            ).map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </>
      ) : null}
      {children}
    </section>
  );
}
