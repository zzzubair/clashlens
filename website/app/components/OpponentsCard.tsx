import { Link, useNavigate } from "react-router";

import type { BaseStrength, BasesMode, LegendsHeld, OpponentRow } from "../lib/dashboard";
import {
  BASE_STRENGTH_MIN_DEFENSES,
  baseStrength,
  trophiesGivenUp,
} from "../lib/dashboard";
import { addToGroupPath, canonicalPlayerPath } from "../lib/player-tag";
import { CardHead, CardNote, updatedAgo } from "./DashboardParts";
import { signed, starText, timeFormatter } from "./LegendClock";

const STRENGTH_LABELS: Record<BaseStrength, string> = {
  hard: "Hard",
  average: "Average",
  easy: "Easy",
  early: "Too early",
};
const STRENGTH_ORDER: BaseStrength[] = ["hard", "average", "easy", "early"];
/** A Legend day has 8 attacks. */
const ATTACKS = 8;

function heldShare(defenses: OpponentRow["defenses"]): number {
  if (defenses.length === 0) return 0;
  return defenses.filter((defense) => defense.stars < 3).length / defenses.length;
}

/** Trophies given up a defense today, rounded, as a negative number. */
function lostEach(trophies: number, defenses: number): string {
  return signed(-Math.round(trophies / defenses));
}

/**
 * Card 3: one row per attack today, hardest bases first. Strength compares
 * the base's held share today (Held % mode), or the trophies it gave up a
 * defense (Trophies mode), with the same across tracked Legend players
 * today. Colours follow the attacker: green is good for you, red is bad.
 */
export function OpponentsCard({
  rows,
  legends,
  timeZone,
  mode = "held",
  nowMs = null,
}: {
  rows: OpponentRow[];
  legends: LegendsHeld | null;
  timeZone: string;
  mode?: BasesMode;
  nowMs?: number | null;
}) {
  const navigate = useNavigate();
  const format = timeFormatter(timeZone);
  // Hardest first within each strength: held most, or gave up least a defense.
  const hardness = (defenses: OpponentRow["defenses"]) =>
    mode === "held"
      ? heldShare(defenses)
      : defenses.length && -trophiesGivenUp(defenses) / defenses.length;
  const sorted = rows
    .map((row) => ({ row, strength: baseStrength(row.defenses, legends, mode) }))
    .sort(
      (a, b) =>
        STRENGTH_ORDER.indexOf(a.strength) - STRENGTH_ORDER.indexOf(b.strength) ||
        hardness(b.row.defenses) - hardness(a.row.defenses),
    );
  // The oldest opponent read, so the card never looks newer than it is.
  const observed = rows.flatMap((row) => row.observedAtMs ?? []);
  const head = (
    <CardHead
      title={
        <>
          {"Bases you attacked "}
          <small className="dash-muted">{`· today · ${rows.length} of ${ATTACKS}`}</small>
        </>
      }
      meta={observed.length ? updatedAgo(Math.min(...observed), nowMs) : null}
    />
  );
  if (rows.length === 0)
    return (
      <>
        {head}
        <CardNote>No attacks yet today.</CardNote>
      </>
    );
  return (
    <>
      {head}
      <div className="opponents">
        <div className="opponents-head" aria-hidden="true">
          <span>Player · trophies at Reset</span>
          <span>Your hit</span>
          <span>Their defenses today</span>
          <span>
            Base strength
            {legends?.defenses
              ? ` · Legends ${
                  mode === "held"
                    ? `${Math.round((100 * legends.held) / legends.defenses)}% held`
                    : `${lostEach(legends.trophies, legends.defenses)} a defense`
                }`
              : ""}
          </span>
          <span />
        </div>
        {sorted.map(({ row, strength }, index) => {
          const held = row.defenses.filter((defense) => defense.stars < 3).length;
          const open = () => void navigate(canonicalPlayerPath(row.tag));
          return (
            <div
              key={`${row.tag}-${index}`}
              className="opponents-row"
              role="link"
              tabIndex={0}
              aria-label={`${row.name ?? row.tag}: open player page`}
              onClick={open}
              onKeyDown={(event) => {
                if (event.key === "Enter" && event.target === event.currentTarget) open();
              }}
            >
              <div className="opponents-name">
                <bdi className="dash-name" title={row.name ?? row.tag}>
                  {row.name ?? row.tag}
                </bdi>
                <span className="dash-muted">
                  {row.resetTrophies === null
                    ? "–"
                    : row.resetTrophies.toLocaleString("en-US")}
                </span>
              </div>
              <div>
                <span className="dash-stars">{starText(row.hit.stars)}</span>{" "}
                {row.hit.destruction}%{" "}
                <b className={row.hit.trophyChange > 0 ? "positive" : ""}>
                  {signed(row.hit.trophyChange)}
                </b>
                <span className="dash-muted"> · {format.format(row.hit.at)}</span>
              </div>
              <ol
                className="opponents-blocks"
                aria-label={`held ${held} of ${row.defenses.length}`}
              >
                {row.defenses.map((defense, at) => (
                  <li
                    key={at}
                    className={`opponents-block ${defense.stars === 3 ? "is-tripled" : "is-held"}${defense.yours ? " is-yours" : ""}`}
                    title={`${defense.stars === 3 ? "tripled" : "held"}${defense.yours ? " · your attack" : ""}`}
                  />
                ))}
              </ol>
              <div className="opponents-strength">
                <span className={`dash-strength dash-strength-${strength}`}>
                  {STRENGTH_LABELS[strength]}
                </span>
                <span className="dash-muted">
                  {row.defenses.length < BASE_STRENGTH_MIN_DEFENSES
                    ? `held ${held} of ${row.defenses.length} · needs ${BASE_STRENGTH_MIN_DEFENSES}+ defenses`
                    : mode === "held"
                      ? `held ${held} of ${row.defenses.length} · ${Math.round(heldShare(row.defenses) * 100)}%`
                      : `gave up ${lostEach(trophiesGivenUp(row.defenses), row.defenses.length)} a defense`}
                </span>
              </div>
              <div className="opponents-action">
                <Link
                  className="button button-secondary dash-small-button"
                  to={addToGroupPath(row.tag)}
                  onClick={(event) => event.stopPropagation()}
                >
                  Add to group
                </Link>
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}
