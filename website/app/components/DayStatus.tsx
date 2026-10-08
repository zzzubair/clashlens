// A small dot for an ended day's status: filled when Verified, hollow when
// Calculated, gold when Uncertain. Hover shows the status; opening the day
// explains it.
const MARK_CLASS: Record<string, string> = {
  Verified: "day-mark day-mark-verified",
  Calculated: "day-mark",
};

export function DayMark({ status }: { status: string }) {
  return (
    <span className={MARK_CLASS[status] ?? "day-mark day-mark-gap"} title={status}>
      <span className="sr-only">{status}</span>
    </span>
  );
}

const STATUS_TEXT: Record<string, string> = {
  Verified:
    "A reading from the game matched this day's start, battles and automatic defense loss. ",
  Calculated:
    "Added up from the recorded battles. A reading from the game has not confirmed every part of this number yet. ",
};

export function DayStatusNote({
  status,
  reasons,
}: {
  status: string;
  reasons: string[];
}) {
  return (
    <p className="section-note">
      <strong>{status}.</strong> {STATUS_TEXT[status] ?? null}
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
