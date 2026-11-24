import { CARDS } from "../lib/dashboard";
import { useEffect, useReducer, useRef, useState } from "react";

import type { ArmiesData } from "../lib/dashboard-cards";
import type {
  ArmyDays,
  ArmyLens,
  ArmyOutcome,
  ArmyRow,
  PlayerArmies,
} from "../lib/dashboard-armies";
import {
  ARMY_MAX_DAYS,
  armyWindowLabel,
  averageTrophies,
  heldPercent,
  hitPercent,
} from "../lib/dashboard-armies";
import { UnitArt } from "./ArmyPortraits";
import { CardHead, CardNote, InfoTip, Name, Switch } from "./DashboardParts";
import { TrophyIcon } from "./GameIcons";
import { signedNumber as signed } from "../lib/number-format";

/**
 * The days chosen on an Armies card and their numbers:
 *
 * - `ready`: the chosen days' numbers; null when they could not be read.
 * - `loading`: the chosen days are being read; the numbers on screen when
 *   they were asked for stay, faded.
 *
 * Events: `choose` (the page's own days come back at once, with no read) and
 * `answered`. Each choice is a new ask; only the newest ask's answer lands.
 * A failed read shows as could not be read; choosing again retries. The card
 * owns it, and drops it when the page goes.
 */
export type ArmyDaysRead =
  | { phase: "ready"; ask: number; days: ArmyDays; armies: PlayerArmies | null }
  | { phase: "loading"; ask: number; days: ArmyDays; shown: PlayerArmies | null };

export type ArmyDaysEvent =
  | { type: "choose"; ask: number; days: ArmyDays; initial: PlayerArmies }
  | { type: "answered"; ask: number; days: ArmyDays; armies: PlayerArmies | null };

export function armyDaysStart(initial: PlayerArmies): ArmyDaysRead {
  return { phase: "ready", ask: 0, days: initial.days, armies: initial };
}

export function armyDaysStep(state: ArmyDaysRead, event: ArmyDaysEvent): ArmyDaysRead {
  if (event.type === "answered") {
    return event.ask === state.ask
      ? { phase: "ready", ask: event.ask, days: event.days, armies: event.armies }
      : state;
  }
  if (event.days === event.initial.days) {
    return { phase: "ready", ask: event.ask, days: event.days, armies: event.initial };
  }
  const shown = state.phase === "ready" ? state.armies : state.shown;
  return { phase: "loading", ask: event.ask, days: event.days, shown };
}

export function armyDaysShown(state: ArmyDaysRead): PlayerArmies | null {
  return state.phase === "ready" ? state.armies : state.shown;
}

const DEFENSE_ROWS = 5;
const PHONE_DEFENSE_ROWS = 3;
const HELD_MARGIN = 5;
const LOST_MARGIN = 3;
const RULE = `Green or red when ${HELD_MARGIN}+ pts or ${LOST_MARGIN}+ trophies from the Legend average.`;
const OFFENSE_ROWS = 3;
const DAY_OPTIONS = [
  ["3", "3 days"],
  ["7", "7 days"],
] as const;
const OFFENSE_DAY_OPTIONS = [...DAY_OPTIONS, ["season", "Season"]] as const;
const DEFENSE_DAY_OPTIONS = [...DAY_OPTIONS, ["custom", "Custom"]] as const;

export interface ArmiesResponse {
  armies: PlayerArmies | null;
}

export function armiesPath(tag: string, lens: ArmyLens, days: ArmyDays): string {
  return `/resources/players/${encodeURIComponent(tag)}/armies?lens=${lens}&days=${days}`;
}

function useArmies(lens: ArmyLens, { tag, armies: initial }: ArmiesData) {
  const [read, dispatch] = useReducer(armyDaysStep, initial, armyDaysStart);
  const asked = useRef(0);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  return {
    days: read.days,
    armies: armyDaysShown(read),
    loading: read.phase === "loading",
    choose(days: ArmyDays) {
      const ask = ++asked.current;
      controller.current?.abort();
      dispatch({ type: "choose", ask, days, initial });
      if (days === initial.days) return;
      // Not a router fetcher: its failed read, a dropped connection or a 502
      // from the server in front, would replace the whole page with an error.
      const next = new AbortController();
      controller.current = next;
      const timeout = setTimeout(() => next.abort(), 15_000);
      void fetch(armiesPath(tag, lens, days), {
        headers: { Accept: "application/json" },
        signal: next.signal,
      })
        .then((response) => {
          if (!response.ok) throw new Error(String(response.status));
          return response.json() as Promise<ArmiesResponse>;
        })
        .then(({ armies }) => armies ?? null)
        .catch(() => null)
        .finally(() => clearTimeout(timeout))
        .then((armies) => dispatch({ type: "answered", ask, days, armies }));
    },
  };
}

function count(value: number, noun: string): string {
  return `${value.toLocaleString("en-US")} ${noun}${value === 1 ? "" : "s"}`;
}

function percent(value: number | null): string {
  return value === null ? "–" : `${value}%`;
}

function legendsHit(army: ArmyRow): number | null {
  return army.legends ? hitPercent(army.legends) : null;
}

function beyond(a: number | null, b: number | null, margin: number): string {
  if (a === null || b === null || Math.abs(a - b) < margin) return "";
  return a > b ? " is-better" : " is-worse";
}

function difference(a: number | null, b: number | null): number | null {
  return a === null || b === null ? null : Math.round(a - b);
}

function lost(outcome: ArmyOutcome | null): string {
  const average = outcome ? averageTrophies(outcome) : null;
  return average === null ? "–" : signed(-average);
}

function Bar({ own }: { own: number | null }) {
  return (
    <span className="army-bar" aria-hidden="true">
      <i style={{ width: `${Math.min(100, own ?? 0)}%` }} />
    </span>
  );
}

function pointsDiff(diff: number | null): [string, string] {
  if (diff === null) return ["–", "no Legends average yet"];
  if (diff === 0) return ["0 pts", "same as the Legends average"];
  const points = `${Math.abs(diff)} point${Math.abs(diff) === 1 ? "" : "s"}`;
  return [
    `${signed(diff)} pts`,
    `${points} ${diff > 0 ? "above" : "below"} the Legends average`,
  ];
}

function lostDiff(fewer: number | null): [string, string] {
  if (fewer === null) return ["–", "no Legends average yet"];
  if (fewer === 0) return ["0", "same as the Legends average"];
  const word = fewer > 0 ? "fewer" : "more";
  return [signed(fewer), `${Math.abs(fewer)} ${word} lost than the Legends average`];
}

function WithLegends({
  label,
  own,
  unit,
  diff,
  tone,
}: {
  label: string;
  own: string;
  unit?: React.ReactNode;
  diff: [string, string];
  tone: string;
}) {
  return (
    <span className="army-stat">
      <span className="army-own" aria-hidden="true">
        {own}
        {unit}
      </span>
      <span className={`army-diff${tone}`} aria-hidden="true">
        {diff[0]}
      </span>
      <span className="sr-only">{`${label} ${own}, ${diff[1]}`}</span>
    </span>
  );
}

function WindowLine({ armies, noun }: { armies: PlayerArmies; noun: string }) {
  const plain =
    !armies.season &&
    (armies.days === 3 || armies.days === 7) &&
    armies.coveredDays.length === armies.days;
  return (
    <p className={plain ? "sr-only" : "dash-muted dash-card-sub"}>
      {`${armyWindowLabel(armies)} · ${count(armies.total.battles, noun)}`}
    </p>
  );
}

function emptyText(armies: PlayerArmies | null, noun: string): string | null {
  if (!armies) return "Army numbers aren't available right now.";
  if (armies.coveredDays.length === 0)
    return "No finished Legend days with army data yet.";
  if (armies.total.battles === 0) return `No ${noun}s on these days.`;
  return null;
}

function DaySwitch({
  days,
  choose,
  custom,
}: {
  days: ArmyDays;
  choose: (days: ArmyDays) => void;
  custom: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(days === "season" ? 7 : days);
  const value = days === 3 || days === 7 || days === "season" ? String(days) : "custom";
  return (
    <span className="army-switch">
      <Switch
        label="Days"
        options={custom ? DEFENSE_DAY_OPTIONS : OFFENSE_DAY_OPTIONS}
        value={value}
        onChange={(option) => {
          if (option === "custom") {
            if (days !== "season") setDraft(days);
            setOpen(!open);
          } else {
            setOpen(false);
            choose(option === "season" ? option : Number(option));
          }
        }}
      />
      {open ? (
        <span className="army-custom">
          <label>
            <b>{`Last ${draft} day${draft === 1 ? "" : "s"}`}</b>
            <input
              type="range"
              min={1}
              max={ARMY_MAX_DAYS}
              value={draft}
              aria-label="Number of days"
              onChange={(event) => setDraft(Number(event.currentTarget.value))}
            />
          </label>
          <span className="dash-muted army-custom-ends">
            <span>1 day</span>
            <span>{ARMY_MAX_DAYS} days</span>
          </span>
          <button
            type="button"
            className="button button-primary dash-small-button"
            onClick={() => {
              setOpen(false);
              choose(draft);
            }}
          >
            Show
          </button>
        </span>
      ) : null}
    </span>
  );
}

export function ArmiesOnDefenseCard({ data }: { data: ArmiesData }) {
  const { days, armies, loading, choose } = useArmies("defense", data);
  const [allShown, setAllShown] = useState(false);
  const listed = armies?.rows.slice(0, DEFENSE_ROWS) ?? [];
  const empty = emptyText(armies, "defense");
  const mostBattles = Math.max(0, ...listed.map((army) => army.battles));
  const row = (
    name: string,
    outcome: ArmyOutcome,
    legends: ArmyOutcome | null,
    all = false,
    typeId: number | null = null,
    extra = false,
  ) => {
    const share = armies?.total.battles
      ? Math.round((100 * outcome.battles) / armies.total.battles)
      : null;
    return (
      <div
        className={`army-defense-row${all ? " is-all" : ""}${extra ? " is-extra" : ""}`}
        key={name}
      >
        {all ? (
          <span>{name}</span>
        ) : (
          <span className="army-type">
            {typeId === null ? null : <UnitArt unitKey={`army-type:${typeId}`} />}
            <Name>{name}</Name>
          </span>
        )}
        <span className="army-share">
          {all ? null : (
            <>
              <Bar own={(100 * outcome.battles) / Math.max(1, mostBattles)} />
              <span>
                {percent(share)}
                <span className="army-share-of"> of defenses</span>
              </span>
            </>
          )}
        </span>
        <WithLegends
          label="Held"
          own={percent(heldPercent(outcome))}
          diff={pointsDiff(
            difference(heldPercent(outcome), legends && heldPercent(legends)),
          )}
          tone={beyond(
            heldPercent(outcome),
            legends && heldPercent(legends),
            HELD_MARGIN,
          )}
        />
        <WithLegends
          label="Trophies per defense"
          own={lost(outcome)}
          unit={<TrophyIcon />}
          diff={lostDiff(
            difference(legends && averageTrophies(legends), averageTrophies(outcome)),
          )}
          tone={beyond(
            legends && averageTrophies(legends),
            averageTrophies(outcome),
            LOST_MARGIN,
          )}
        />
      </div>
    );
  };
  return (
    <div
      className={`army-card${loading ? " is-loading" : ""}${allShown ? " army-all" : ""}`}
      aria-busy={loading}
    >
      <CardHead
        icon={CARDS["armies-defense"].icon}
        title="Armies on defense"
        info={
          empty || !armies ? null : (
            <InfoTip label="What the columns mean">
              <p>Usage: your share of defenses against each army.</p>
              <p>
                Held: kept the attacker under 3 stars. Per defense: trophies lost a
                defense.
              </p>
              <p>
                Differences are against all tracked Legend players&apos; defenses against
                that army; + is better. {RULE}
              </p>
            </InfoTip>
          )
        }
        meta={
          <>
            {empty || !armies ? null : (
              <span className="dash-card-ref">vs Legend avg</span>
            )}
            <DaySwitch days={days} choose={choose} custom />
          </>
        }
      />
      {empty || !armies ? (
        <>
          <CardNote>{empty}</CardNote>
          {!armies ? (
            <button
              type="button"
              className="button button-secondary dash-small-button"
              disabled={loading}
              onClick={() => choose(days)}
            >
              Try again
            </button>
          ) : null}
        </>
      ) : (
        <>
          <WindowLine armies={armies} noun="defense" />
          <div className="army-defense-row army-head" aria-hidden="true">
            <span>
              Army<span className="army-head-usage"> · usage</span>
            </span>
            <span>Usage</span>
            <span>Held</span>
            <span>Per defense</span>
          </div>
          {listed.map((army, index) =>
            row(
              army.name,
              army,
              army.legends as ArmyOutcome | null,
              false,
              army.typeId,
              index >= PHONE_DEFENSE_ROWS,
            ),
          )}
          {row(
            `All ${count(armies.total.battles, "defense")}`,
            armies.total,
            armies.legendsTotal,
            true,
          )}
          {listed.length > PHONE_DEFENSE_ROWS ? (
            <button
              type="button"
              className="show-all opponents-more"
              aria-expanded={allShown}
              onClick={() => setAllShown(!allShown)}
            >
              {allShown ? "Show fewer" : `Show all ${listed.length}`}
            </button>
          ) : null}
        </>
      )}
    </div>
  );
}

export function ArmiesOnOffenseCard({ data }: { data: ArmiesData }) {
  const { days, armies, loading, choose } = useArmies("offense", data);
  const empty = emptyText(armies, "attack");
  const total = armies?.total;
  const only =
    armies &&
    total &&
    armies.rows.length === 1 &&
    armies.rows[0].battles === total.battles
      ? armies.rows[0]
      : null;
  const shown = armies?.rows.slice(0, OFFENSE_ROWS) ?? [];
  const rest = total
    ? shown.reduce(
        (left, army) => ({
          battles: left.battles - army.battles,
          threeStars: left.threeStars - army.threeStars,
          trophies: 0,
        }),
        { battles: total.battles, threeStars: total.threeStars, trophies: 0 },
      )
    : null;
  return (
    <div className={`army-card${loading ? " is-loading" : ""}`} aria-busy={loading}>
      <CardHead
        icon={CARDS["armies-offense"].icon}
        title="Armies on offense"
        info={
          empty || !armies ? null : (
            <InfoTip label="What the columns mean">
              <p>
                Your hit rate: share of your attacks that scored 3 stars with each army.
              </p>
              <p>
                Legends: the 3-star rate of all tracked Legend players using that army.
              </p>
            </InfoTip>
          )
        }
        meta={<DaySwitch days={days} choose={choose} custom={false} />}
      />
      {empty || !armies || !total ? (
        <>
          <CardNote>{empty}</CardNote>
          {!armies ? (
            <button
              type="button"
              className="button button-secondary dash-small-button"
              disabled={loading}
              onClick={() => choose(days)}
            >
              Try again
            </button>
          ) : null}
        </>
      ) : (
        <>
          <WindowLine armies={armies} noun="attack" />
          {only ? (
            <OneArmy army={only} />
          ) : (
            <>
              <div className="army-offense-row army-head" aria-hidden="true">
                <span>Army type</span>
                <span>Your hit rate</span>
                <span>Legends</span>
              </div>
              {shown.map((army) => (
                <div className="army-offense-row" key={army.typeId}>
                  <span className="army-type">
                    <UnitArt unitKey={`army-type:${army.typeId}`} />
                    <Name>{army.name}</Name>
                  </span>
                  <b>{percent(hitPercent(army))}</b>
                  <span className="dash-muted">{percent(legendsHit(army))}</span>
                </div>
              ))}
              {rest && rest.battles > 0 ? (
                <div className="army-offense-row">
                  <span>Other armies</span>
                  <b>{percent(hitPercent(rest))}</b>
                  <span className="dash-muted">mixed</span>
                </div>
              ) : null}
            </>
          )}
        </>
      )}
    </div>
  );
}

function OneArmy({ army }: { army: ArmyRow }) {
  return (
    <div className="army-one">
      <div className="army-one-name">
        <Name>{army.name}</Name>
      </div>
      <div className="army-one-big">
        <div>
          <span className="dash-label">Your hit rate</span>
          <b>{percent(hitPercent(army))}</b>
        </div>
        <div>
          <span className="dash-label">Legends</span>
          <b className="dash-muted">{percent(legendsHit(army))}</b>
        </div>
      </div>
    </div>
  );
}
