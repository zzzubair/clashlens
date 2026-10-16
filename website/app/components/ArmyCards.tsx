import { useRef, useState } from "react";

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
import { CardHead, CardNote, Name, Switch } from "./DashboardParts";
import { signed } from "./LegendClock";

/** Armies on defense lists this many army types. */
const DEFENSE_ROWS = 5;
/** Armies on offense lists this many before "Other armies". */
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

/** The numbers for the chosen days: the page's first read, then the card's own. */
function useArmies(lens: ArmyLens, { tag, armies: initial }: ArmiesData) {
  // The chosen days and their numbers: null when they could not be read,
  // undefined while they load.
  const [chosen, setChosen] = useState<{ days: ArmyDays; armies?: PlayerArmies | null }>({
    days: initial.days,
    armies: initial,
  });
  // What was on screen when the chosen days were asked for.
  const [shown, setShown] = useState<PlayerArmies | null>(initial);
  const asked = useRef(0);
  const loading = chosen.armies === undefined;
  return {
    days: chosen.days,
    // While the chosen days load, the numbers already shown stay, faded.
    armies: loading ? shown : (chosen.armies ?? null),
    loading,
    choose(days: ArmyDays) {
      const ask = ++asked.current;
      if (!loading) setShown(chosen.armies ?? null);
      if (days === initial.days) {
        setChosen({ days, armies: initial });
        return;
      }
      setChosen({ days });
      // Not a router fetcher: its failed read, a dropped connection or a 502
      // from the server in front, would replace the whole page with an error.
      void fetch(armiesPath(tag, lens, days), { headers: { Accept: "application/json" } })
        .then((response) => response.json() as Promise<ArmiesResponse>)
        .then(({ armies }) => armies ?? null)
        .catch(() => null)
        .then((armies) => {
          if (ask === asked.current) setChosen({ days, armies });
        });
    },
  };
}

/** "3 defenses", "1 attack". */
function count(value: number, noun: string): string {
  return `${value.toLocaleString("en-US")} ${noun}${value === 1 ? "" : "s"}`;
}

function percent(value: number | null): string {
  return value === null ? "–" : `${value}%`;
}

/** Every tracked player's hit rate with this army type, on Armies on offense. */
function legendsHit(army: ArmyRow): number | null {
  return army.legends ? hitPercent(army.legends) : null;
}

function lost(outcome: ArmyOutcome | null): string {
  const average = outcome ? averageTrophies(outcome) : null;
  return average === null ? "–" : signed(-average);
}

/** The user's number, then every tracked player's in grey: "29% / 50%". */
function WithLegends({ own, legends }: { own: string; legends: string }) {
  return (
    <span>
      {own} <span className="dash-muted army-legends">/ {legends}</span>
    </span>
  );
}

/** What the card says instead of numbers, or null when it has numbers to show. */
function emptyText(armies: PlayerArmies | null, noun: string): string | null {
  if (!armies) return "Army numbers aren't available right now.";
  if (armies.coveredDays.length === 0)
    return "No finished Legend days with army data yet.";
  if (armies.total.battles === 0) return `No ${noun}s on these days.`;
  return null;
}

/** 3 days / 7 days, then Season on Armies on offense or Custom 1–28 days on defense. */
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

/**
 * Armies on defense: the army types that cost the user the most trophies
 * over the chosen finished days, and every defense together, each beside
 * every tracked player's defenses against that army.
 */
export function ArmiesOnDefenseCard({ data }: { data: ArmiesData }) {
  const { days, armies, loading, choose } = useArmies("defense", data);
  const empty = emptyText(armies, "defense");
  const row = (
    name: string,
    outcome: ArmyOutcome,
    legends: ArmyOutcome | null,
    all = false,
  ) => (
    <div className={`army-defense-row${all ? " is-all" : ""}`} key={name}>
      {all ? <span>{name}</span> : <Name>{name}</Name>}
      <span>
        {percent(
          armies?.total.battles
            ? Math.round((100 * outcome.battles) / armies.total.battles)
            : null,
        )}
      </span>
      <WithLegends
        own={percent(heldPercent(outcome))}
        legends={percent(legends ? heldPercent(legends) : null)}
      />
      <span className="negative">
        <WithLegends own={lost(outcome)} legends={lost(legends)} />
      </span>
    </div>
  );
  return (
    <div className={`army-card${loading ? " is-loading" : ""}`} aria-busy={loading}>
      <CardHead
        title="Armies on defense"
        meta={<DaySwitch days={days} choose={choose} custom />}
      />
      {empty || !armies ? (
        <CardNote>{empty}</CardNote>
      ) : (
        <>
          <p className="dash-muted dash-card-sub">
            {`${armyWindowLabel(armies)} · ${count(armies.total.battles, "defense")}`}
          </p>
          <div className="army-defense-row army-head" aria-hidden="true">
            <span>Army type</span>
            <span>Share</span>
            <span>
              Held <span className="army-legends-head">/ Legends avg</span>
            </span>
            <span>
              Avg lost <span className="army-legends-head">/ Legends avg</span>
            </span>
          </div>
          {armies.rows
            .slice(0, DEFENSE_ROWS)
            .map((army) => row(army.name, army, army.legends as ArmyOutcome | null))}
          {row("All defenses", armies.total, armies.legendsTotal, true)}
        </>
      )}
    </div>
  );
}

/**
 * Armies on offense: the user's hit rate (share of attacks tripled) with
 * each army type over the last 3 or 7 finished days or this Season's, beside
 * everyone's with that army. A one-army player sees the two numbers big.
 */
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
        title="Armies on offense"
        meta={<DaySwitch days={days} choose={choose} custom={false} />}
      />
      {empty || !armies || !total ? (
        <CardNote>{empty}</CardNote>
      ) : (
        <>
          <p className="dash-muted dash-card-sub">
            {`${armyWindowLabel(armies)} · ${count(total.battles, "attack")}`}
          </p>
          {only ? (
            <OneArmy army={only} />
          ) : (
            <>
              <div className="army-offense-row army-head" aria-hidden="true">
                <span>Army type</span>
                <span>You</span>
                <span>Legends</span>
              </div>
              {shown.map((army) => (
                <div className="army-offense-row" key={army.typeId}>
                  <Name>{army.name}</Name>
                  <b>{percent(hitPercent(army))}</b>
                  <span className="dash-muted">{percent(legendsHit(army))}</span>
                </div>
              ))}
              {rest && rest.battles > 0 ? (
                <div className="army-offense-row">
                  <span>Other armies</span>
                  <b>{percent(hitPercent(rest))}</b>
                  <span className="dash-muted">–</span>
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
          <span className="dash-label">You</span>
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
