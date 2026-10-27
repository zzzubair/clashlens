// One side of a Season's recorded battles. Averages are trophies moved, never
// signed; null anywhere means that part is unknown.
export interface SummarySide {
  count: number | null;
  stars: Array<number | null>;
  unknown: number | null;
  perDay: number | null;
  perBattle: number | null;
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
// Words such as "Not published yet" take a smaller size than numbers.
const words = (value: string) => (/[a-z]/i.test(value) ? "summary-words" : undefined);
export const per = (total: number | null, by: number | null) =>
  total === null || !by ? null : total / by;
// Grid track sizes for a row's 3-, 2-, 1- and 0-star segments. A segment
// with any battles stays wide enough for its count.
const shares = (stars: Array<number | null>) =>
  Object.fromEntries(
    [3, 2, 1, 0].map((star) => [
      `--s${star}`,
      stars[star] ? `minmax(2.25rem, ${stars[star]}fr)` : "0fr",
    ]),
  ) as React.CSSProperties;
// A segment under a fifth of the row shows its count without the star label.
const narrow = (stars: Array<number | null>, star: number) => {
  const total = stars.reduce<number>((sum, n) => sum + (n ?? 0), 0);
  return total > 0 && (stars[star] ?? 0) / total < 0.2;
};

// The one Season box: rank and trophies first, then hit rate, battles by
// stars and averages. Meta is one short line of dates and coverage. Without battles it shows only the headline. An ended
// Season's final rank is its standout number.
export function SeasonSummary({
  title,
  meta,
  controls,
  rank,
  finalRank = false,
  trophies,
  attack,
  defense,
  children,
}: {
  title: string;
  meta?: string;
  controls?: React.ReactNode;
  rank: [label: string, value: string];
  finalRank?: boolean;
  trophies: [label: string, value: string, mark?: React.ReactNode];
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
        <div>
          <h2 id="season-summary-title">{title}</h2>
          {meta ? (
            <p className="season-summary-meta" aria-live="polite">
              {meta}
            </p>
          ) : null}
        </div>
        {controls}
      </div>
      <dl className="season-summary-headline">
        {[rank, trophies].map(([label, value, mark]) => (
          <div
            key={label}
            className={finalRank && label === rank[0] ? "season-summary-rank" : undefined}
          >
            <dt>{label}</dt>
            <dd className={words(value)}>
              {value}
              {mark}
            </dd>
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
                  {count(triples)} of {count(total)}
                </small>
              )}
              {triples === null || !total ? null : (
                <span className="meter" aria-hidden="true">
                  <span
                    style={
                      { "--value": `${(100 * triples) / total}%` } as React.CSSProperties
                    }
                  />
                </span>
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
                      <span aria-hidden="true">
                        {stars}
                        <span className="summary-star">★</span>
                      </span>
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
                  // Each star count is a bar segment as wide as its share.
                  <tr key={label} style={shares(side.stars)}>
                    <th scope="row">{label}</th>
                    <td>{count(side.count)}</td>
                    {[3, 2, 1, 0].map((stars) => (
                      <td
                        key={stars}
                        data-stars={stars}
                        data-empty={side.stars[stars] ? undefined : ""}
                        data-narrow={narrow(side.stars, stars) ? "" : undefined}
                      >
                        {count(side.stars[stars] ?? null)}
                      </td>
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
                [
                  "Offense per day",
                  signed(attack.perDay, 1),
                  "Averaged over the finished Legend days with at least one recorded attack",
                ],
                [
                  "Defense per day",
                  signed(defense.perDay, -1),
                  "Averaged over the finished Legend days with at least one recorded defense",
                ],
                ["Per attack", signed(attack.perBattle, 1, 1)],
                ["Per defense", signed(defense.perBattle, -1, 1)],
              ] as const
            ).map(([label, value, hint]) => (
              <div key={label}>
                <dt title={hint}>{label}</dt>
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
