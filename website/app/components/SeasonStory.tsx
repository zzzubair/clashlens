import { runLines, seasonDayDate, type SeasonShape } from "../lib/season-shape";

const KIND_WORDS = {
  legend: "Legend I",
  waiting: "Legend I, not signed up yet",
  outside: "Not in Legend I",
  unrecorded: "League not known",
  ahead: "Still to come",
} as const;

/**
 * A partial Season at a glance: one cell per Season day, and one line per
 * stretch in Legend I saying how it began and ended. Shown only when the
 * Season so far was not all Legend I from Day 1.
 */
export function SeasonStory({
  shape,
  title = "This Season in Legend I",
}: {
  shape: SeasonShape<unknown>;
  title?: string;
}) {
  if (!shape.partial) return null;
  const shown = new Set(shape.kinds);
  return (
    <section className="season-story" aria-labelledby="season-story-title">
      <h2 id="season-story-title">{title}</h2>
      <ul className="season-story-runs">
        {runLines(shape).map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
      <ol className="season-strip" aria-label="Each day of the Season">
        {shape.kinds.map((kind, index) => (
          <li
            key={index}
            className={`season-strip-${kind}${index + 1 === shape.today ? " season-strip-today" : ""}`}
            title={`Day ${index + 1}, ${seasonDayDate(shape, index + 1)}: ${KIND_WORDS[kind]}`}
          >
            <span className="sr-only">
              Day {index + 1}: {KIND_WORDS[kind]}
            </span>
          </li>
        ))}
      </ol>
      <p className="season-strip-ends" aria-hidden="true">
        <span>Day 1</span>
        <span>Day 28</span>
      </p>
      <ul className="season-strip-key" aria-hidden="true">
        {(["legend", "waiting", "outside", "unrecorded", "ahead"] as const)
          .filter((kind) => shown.has(kind))
          .map((kind) => (
            <li key={kind} className={`season-strip-${kind}`}>
              {KIND_WORDS[kind]}
            </li>
          ))}
      </ul>
    </section>
  );
}
