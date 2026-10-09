import { useNavigate, useFetcher } from "react-router";

import type { BaseStrength, LegendsHeld, OpponentRow } from "../lib/dashboard";
import { BASE_STRENGTH_MIN_DEFENSES, baseStrength } from "../lib/dashboard";
import { canonicalPlayerPath } from "../lib/player-tag";
import type { DashboardActionData } from "../routes/dashboard";
import { signed, starText, timeFormatter } from "./LegendClock";

const STRENGTH_LABELS: Record<BaseStrength, string> = {
  hard: "Hard",
  average: "Average",
  easy: "Easy",
  early: "Too early",
};
const STRENGTH_ORDER: BaseStrength[] = ["hard", "average", "easy", "early"];

function heldShare(defenses: OpponentRow["defenses"]): number {
  if (defenses.length === 0) return 0;
  return defenses.filter((defense) => defense.stars < 3).length / defenses.length;
}

function SaveButton({
  tag,
  saved,
  idempotencyKey,
}: {
  tag: string;
  saved: boolean;
  idempotencyKey: string;
}) {
  const fetcher = useFetcher<DashboardActionData>();
  const done = saved || fetcher.data?.saved === true;
  return (
    <>
      <button
        type="button"
        className="button button-secondary dash-small-button"
        aria-pressed={done}
        disabled={done || fetcher.state !== "idle"}
        onClick={(event) => {
          event.stopPropagation();
          fetcher.submit(
            {
              intent: "save-player",
              tag,
              idempotencyKey: fetcher.data?.idempotencyKey ?? idempotencyKey,
            },
            { method: "post" },
          );
        }}
      >
        {done ? "✓ In saved players" : "Add to saved players"}
      </button>
      {fetcher.state === "idle" && fetcher.data?.error ? (
        <span className="dash-save-error" role="alert">
          {fetcher.data.error}
        </span>
      ) : null}
    </>
  );
}

/**
 * Card 3: one row per attack today, hardest bases first. Strength compares
 * the base's held share today with today's held share across tracked Legend
 * players. Colours follow the attacker: green is good for you, red is bad.
 */
export function OpponentsCard({
  rows,
  legends,
  savedTags,
  saveKeys,
  timeZone,
}: {
  rows: OpponentRow[];
  legends: LegendsHeld | null;
  savedTags: string[];
  saveKeys: Record<string, string>;
  timeZone: string;
}) {
  const navigate = useNavigate();
  const format = timeFormatter(timeZone);
  const sorted = rows
    .map((row) => ({ row, strength: baseStrength(row.defenses, legends) }))
    .sort(
      (a, b) =>
        STRENGTH_ORDER.indexOf(a.strength) - STRENGTH_ORDER.indexOf(b.strength) ||
        heldShare(b.row.defenses) - heldShare(a.row.defenses),
    );
  return (
    <div className="opponents">
      <div className="opponents-head" aria-hidden="true">
        <span>Player · trophies at Reset</span>
        <span>Your hit</span>
        <span>Their defenses today</span>
        <span>Base strength</span>
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
                held {held} of {row.defenses.length}
                {row.defenses.length < BASE_STRENGTH_MIN_DEFENSES
                  ? ` · needs ${BASE_STRENGTH_MIN_DEFENSES}+ defenses`
                  : ` · ${Math.round(heldShare(row.defenses) * 100)}%`}
              </span>
            </div>
            <div className="opponents-action">
              <SaveButton
                tag={row.tag}
                saved={savedTags.includes(row.tag)}
                idempotencyKey={saveKeys[row.tag] ?? ""}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
