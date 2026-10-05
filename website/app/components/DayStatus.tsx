// A small dot for an ended day's status: hollow when provisional, filled when
// evidence is missing. Hover shows the status; opening the day explains it.
export function DayMark({ status }: { status: string }) {
  return (
    <span
      className={status === "Provisional result" ? "day-mark" : "day-mark day-mark-gap"}
      title={status}
    >
      <span className="sr-only">{status}</span>
    </span>
  );
}

export function DayStatusNote({
  status,
  reasons,
}: {
  status: string;
  reasons: string[];
}) {
  return (
    <p className="section-note">
      <strong>{status}.</strong>{" "}
      {status === "Provisional result"
        ? "Not yet proven to include the automatic defense loss at Reset. "
        : null}
      {reasons.join(" ")}
    </p>
  );
}

// A provisional end-of-day count keeps its number and gains a hollow dot.
export function provisional(value: string, state: string | null) {
  return value === "Unknown" || state === "accepted" ? (
    value
  ) : (
    <>
      {value}
      <span className="day-mark" title="Provisional">
        <span className="sr-only"> (provisional)</span>
      </span>
    </>
  );
}
