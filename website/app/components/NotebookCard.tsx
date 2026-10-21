import { Link } from "react-router";

import type { NotebookToday } from "../lib/dashboard-cards";
import { heldText } from "../lib/notebook";
import { CardHead } from "./DashboardParts";
import { BaseTile } from "./NotebookParts";

/** The Notebook with this account's Change sheet open. */
export function changePath(tag: string): string {
  return `/notebook?${new URLSearchParams({ change: tag })}`;
}

/**
 * "Base: Ring ›" under Armies on defense, opening the base the account runs
 * today; with none, "Which base? ›" opens the account's Change sheet.
 */
export function BaseLine({ today }: { today: NotebookToday }) {
  return (
    <Link
      className="dash-nb-link"
      to={today.base ? `/notebook/bases/${today.base.id}` : changePath(today.tag)}
    >
      {today.base ? (
        <>
          Base: <bdi className="dash-nb-name">{today.base.name}</bdi> ›
        </>
      ) : (
        "Which base? ›"
      )}
    </Link>
  );
}

/** The Notebook card: the base the account runs today and how it held. */
export function NotebookCard({ data }: { data: NotebookToday }) {
  const { base, next } = data;
  const upNext = next ? (
    <span className="dash-muted">
      → <bdi>{next.name ?? "no base"}</bdi> from Day {next.day}
    </span>
  ) : null;
  return (
    <>
      <CardHead
        title="Notebook"
        meta={
          <Link className="dash-nb-link" to="/notebook">
            Open ›
          </Link>
        }
      />
      {base ? (
        <Link className="dash-nb-today" to={`/notebook/bases/${base.id}`}>
          <BaseTile name={base.name} />
          <span className="dash-nb-text">
            <bdi className="dash-nb-base">{base.name}</bdi>
            <span className="dash-muted">
              since Day {base.since} · {heldText(base.record)}
            </span>
            {upNext}
          </span>
        </Link>
      ) : (
        <div className="dash-nb-today">
          <BaseTile name={null} />
          <span className="dash-nb-text">
            <span className="dash-card-note">No base today</span>
            {upNext}
          </span>
        </div>
      )}
      <Link className="dash-nb-link dash-nb-change" to={changePath(data.tag)}>
        {base ? "Change base ›" : "Which base? ›"}
      </Link>
    </>
  );
}
