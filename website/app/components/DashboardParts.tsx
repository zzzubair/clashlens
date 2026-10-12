import type { ReactNode } from "react";

/** A signed number rounded to `digits` places; "–" when unknown. */
export function signedOr(value: number | null, digits = 0): string {
  if (value === null || !Number.isFinite(value)) return "–";
  const rounded = Number(value.toFixed(digits));
  const text = Math.abs(rounded).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return rounded > 0 ? `+${text}` : rounded < 0 ? `−${text}` : text;
}

export function count(value: number | null): string {
  return value === null ? "–" : value.toLocaleString("en-US");
}

export function percent(share: number | null): string {
  return share === null ? "–" : `${Math.round(share * 100)}%`;
}

export function tone(value: number | null): string {
  return value === null || value === 0 ? "" : value > 0 ? "positive" : "negative";
}

/** "updated 3 min ago", or nothing until the browser knows the time. */
export function updatedAgo(
  observedAtMs: number | null,
  nowMs: number | null,
): string | null {
  if (observedAtMs === null || nowMs === null) return null;
  const minutes = Math.max(0, Math.floor((nowMs - observedAtMs) / 60_000));
  if (minutes < 1) return "updated just now";
  if (minutes < 120) return `updated ${minutes} min ago`;
  return `updated ${Math.floor(minutes / 60)} h ago`;
}

/** "Last 7 days", or how many days the Season has had so far. */
export function lastDaysLabel(asked: number, span: number): string {
  if (span < asked)
    return `Last ${span} day${span === 1 ? "" : "s"} · only ${span} so far this Season`;
  return `Last ${asked} days`;
}

/** The card's title row; `meta` sits at the right end. */
export function CardHead({ title, meta }: { title: ReactNode; meta?: ReactNode }) {
  return (
    <header className="dash-card-head">
      <h2>{title}</h2>
      {meta ? <span className="dash-card-meta">{meta}</span> : null}
    </header>
  );
}

/** A choice flipped while looking at the card, such as 3 days or 7 days. */
export function Switch<Value extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly (readonly [Value, string])[];
  value: Value;
  onChange: (value: Value) => void;
}) {
  return (
    <span className="dash-switch" role="group" aria-label={label}>
      {options.map(([option, text]) => (
        <button
          key={option}
          type="button"
          aria-pressed={option === value}
          onClick={() => onChange(option)}
        >
          {text}
        </button>
      ))}
    </span>
  );
}

/** A name cut with "…" when long, shown in full on hover. */
export function Name({ children }: { children: string }) {
  return (
    <bdi className="dash-name" title={children}>
      {children}
    </bdi>
  );
}

/** Shown in place of a card's numbers when there are none yet. */
export function CardNote({ children }: { children: ReactNode }) {
  return <p className="dash-card-note">{children}</p>;
}
