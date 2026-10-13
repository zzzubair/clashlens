import { Link, useNavigate } from "react-router";

import type { BaseStrength, BasesMode, OpponentRow } from "../lib/dashboard";
import {
  BASE_STRENGTH_MIN_DEFENSES,
  LEGENDS_DEFENSE_LOSS,
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
  unavailable: "–",
  early: "Too early",
};
const STRENGTH_ORDER: BaseStrength[] = [
  "hard",
  "average",
  "easy",
  "unavailable",
  "early",
];
/** A Legend day has 8 attacks. */
const ATTACKS = 8;

function heldShare(defenses: OpponentRow["defenses"]): number {
  if (defenses.length === 0) return 0;
  return defenses.filter((defense) => defense.stars < 3).length / defenses.length;
}

/**
 * Card 3: one row per attack today, hardest bases first. Held % mode compares
 * the base's held share today with today's among tracked Legend players at
 * similar trophies, and shows "–" when there are too few of them. Trophies
 * mode compares the trophies it gave up a defense with the fixed Legends 32
 * for every base, whatever its trophies, and shows the total it lost today.
 * Colours follow the attacker: green is good for you, red is bad.
 */
export function OpponentsCard({
  rows,
  timeZone,
  mode = "held",
  nowMs = null,
  comparedAtMs = null,
}: {
  rows: OpponentRow[] | null;
  timeZone: string;
  mode?: BasesMode;
  nowMs?: number | null;
  /** When Held % mode's comparisons were read. */
  comparedAtMs?: number | null;
}) {
  const navigate = useNavigate();
  const format = timeFormatter(timeZone);
  // The oldest opponent or comparison read, so the card never looks newer than it is.
  const observed = (rows ?? []).flatMap((row) => row.observedAtMs ?? []);
  if (rows?.length && mode === "held" && comparedAtMs !== null)
    observed.push(comparedAtMs);
  const head = (
    <CardHead
      title={
        <>
          {"Bases you attacked "}
          <small className="dash-muted">
            {rows ? `· today · ${rows.length} of ${ATTACKS}` : "· today"}
          </small>
        </>
      }
      meta={observed.length ? updatedAgo(Math.min(...observed), nowMs) : null}
    />
  );
  if (!rows?.length)
    return (
      <>
        {head}
        <CardNote>
          {rows ? "No attacks yet today." : "Today's battles aren't saved yet."}
        </CardNote>
      </>
    );
  // Hardest first within each strength: held most, or gave up least a defense.
  const hardness = (defenses: OpponentRow["defenses"]) =>
    mode === "held"
      ? heldShare(defenses)
      : defenses.length && -trophiesGivenUp(defenses) / defenses.length;
  const sorted = rows
    .map((row) => ({ row, strength: baseStrength(row.defenses, row.legends, mode) }))
    .sort(
      (a, b) =>
        STRENGTH_ORDER.indexOf(a.strength) - STRENGTH_ORDER.indexOf(b.strength) ||
        hardness(b.row.defenses) - hardness(a.row.defenses),
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
            {mode === "trophies"
              ? ` · vs all Legends ${signed(-LEGENDS_DEFENSE_LOSS)} a defense`
              : " · vs Legends at similar trophies today"}
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
                      ? `held ${held} of ${row.defenses.length} · ${
                          row.legends?.defenses
                            ? `${Math.round(heldShare(row.defenses) * 100)}% vs ${Math.round((100 * row.legends.held) / row.legends.defenses)}%`
                            : "no comparison yet"
                        }`
                      : `lost ${signed(-trophiesGivenUp(row.defenses))} in ${row.defenses.length} defenses`}
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
