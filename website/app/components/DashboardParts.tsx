import { type ReactNode } from "react";

import { DashboardIcon, type IconName } from "./DashboardIcon";

export function count(value: number | null): string {
  return value === null ? "–" : value.toLocaleString("en-US");
}

export function percent(share: number | null): string {
  return share === null ? "–" : `${Math.round(share * 100)}%`;
}

export function tone(value: number | null): string {
  return value === null || value === 0 ? "" : value > 0 ? "positive" : "negative";
}

export function lastDaysLabel(asked: number | "season", span: number): string {
  const days = `${span} day${span === 1 ? "" : "s"}`;
  if (asked === "season") return `Whole Season · ${days}`;
  if (span < asked) return `Last ${days} · only ${span} so far this Season`;
  return `Last ${asked} days`;
}

export function CardHead({
  title,
  info,
  meta,
  icon,
}: {
  title: ReactNode;
  info?: ReactNode;
  meta?: ReactNode;
  icon?: IconName;
}) {
  return (
    <header className="dash-card-head">
      <h2>
        {icon ? <DashboardIcon name={icon} /> : null}
        {title}
      </h2>
      {info}
      {meta ? <span className="dash-card-meta">{meta}</span> : null}
    </header>
  );
}

/**
 * A card's one ⓘ in its header: what its short column names mean, opened
 * with a tap or a click; its name is said aloud and is its tooltip.
 */
export function InfoTip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <details className="dash-info">
      <summary title={label}>
        <span aria-hidden="true">ⓘ</span>
        <span className="sr-only">{label}</span>
      </summary>
      <div className="dash-info-body">{children}</div>
    </details>
  );
}

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

export function CardNote({ children }: { children: ReactNode }) {
  return <p className="dash-card-note">{children}</p>;
}
