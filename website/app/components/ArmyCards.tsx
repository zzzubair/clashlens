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
import { UnitArt } from "./ArmyPortraits";
import { CardHead, CardNote, InfoTip, Name, Switch } from "./DashboardParts";
import { TrophyIcon } from "./GameIcons";
import { signed } from "./LegendClock";

/** Armies on defense lists this many army types. */
const DEFENSE_ROWS = 5;
/** A phone shows this many army types before Show all. */
const PHONE_DEFENSE_ROWS = 3;
/** Not tripled is coloured only when this many points or more from the Legends... */
const HELD_MARGIN = 5;
/** ...and Avg lost only when this many trophies or more. */
const LOST_MARGIN = 3;
/** The colouring rule in words, the same as Analytics'. */
const RULE = `Green or red when ${HELD_MARGIN}+ pts or ${LOST_MARGIN}+ trophies from the Legend average.`;
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

/**
 * " is-better" when a is at least `margin` above b, " is-worse" when at least
 * `margin` below, and "" when closer or unknown, so a gap of a point or two
 * stays uncoloured. Both are whole numbers, as shown.
 */
function beyond(a: number | null, b: number | null, margin: number): string {
  if (a === null || b === null || Math.abs(a - b) < margin) return "";
  return a > b ? " is-better" : " is-worse";
}

/** a − b, rounded; null when either is unknown. */
function difference(a: number | null, b: number | null): number | null {
  return a === null || b === null ? null : Math.round(a - b);
}

function lost(outcome: ArmyOutcome | null): string {
  const average = outcome ? averageTrophies(outcome) : null;
  return average === null ? "–" : signed(-average);
}

/** An army's share as an ember bar on a fixed scale, 0–100. */
function Bar({ own }: { own: number | null }) {
  return (
    <span className="army-bar" aria-hidden="true">
      <i style={{ width: `${Math.min(100, own ?? 0)}%` }} />
    </span>
  );
}

/** Not tripled's difference: "+10 pts", said "10 points above". */
function pointsDiff(diff: number | null): [string, string] {
  if (diff === null) return ["–", "no Legends average yet"];
  if (diff === 0) return ["0 pts", "same as the Legends average"];
  const points = `${Math.abs(diff)} point${Math.abs(diff) === 1 ? "" : "s"}`;
  return [
    `${signed(diff)} pts`,
    `${points} ${diff > 0 ? "above" : "below"} the Legends average`,
  ];
}

/**
 * Trophies per defense's difference in the value's own sign, the value less the
 * Legends': −27 against −32 is "+5", 5 fewer lost; −33 against −32 is "−1".
 */
function lostDiff(fewer: number | null): [string, string] {
  if (fewer === null) return ["–", "no Legends average yet"];
  if (fewer === 0) return ["0", "same as the Legends average"];
  const word = fewer > 0 ? "fewer" : "more";
  return [signed(fewer), `${Math.abs(fewer)} ${word} lost than the Legends average`];
}

/**
 * One comparison: the user's number with its unit, then one signed
 * difference from the Legends with its own, which the column heading names.
 * Only the difference is coloured, by the rule the header's ⓘ states.
 * Screen readers hear it as a sentence.
 */
function WithLegends({
  label,
  own,
  unit,
  diff,
  tone,
}: {
  label: string;
  own: string;
  /** A unit drawn after the number, such as the trophy for trophies. */
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

/**
 * The days and battles the numbers cover: said aloud always, and shown only
 * when the switch doesn't already say it, such as Custom days or days
 * without army data.
 */
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
  // A phone shows the first few armies until asked for every one.
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
        {/* Bars against the most common army, so the gaps between armies show. */}
        <span className="army-share">
          {all ? null : (
            <>
              <Bar own={(100 * outcome.battles) / Math.max(1, mostBattles)} />
              <span>
                {percent(share)}
                {/* Said aloud. */}
                <span className="army-share-of"> of defenses</span>
              </span>
            </>
          )}
        </span>
        <WithLegends
          label="Not tripled"
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
        {/* Trophies lost read as negatives, so fewer lost is above the Legends. */}
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
        title="Armies on defense"
        // The short headings in full, and the one colouring rule.
        info={
          empty || !armies ? null : (
            <InfoTip label="What the columns mean">
              <p>Usage: your share of defenses against each army.</p>
              <p>
                Not tripled: kept the attacker under 3 stars. Per defense: trophies lost a
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
            {/* The one reference every difference is against, said once. */}
            {empty || !armies ? null : (
              <span className="dash-card-ref">vs Legend avg</span>
            )}
            <DaySwitch days={days} choose={choose} custom />
          </>
        }
      />
      {empty || !armies ? (
        <CardNote>{empty}</CardNote>
      ) : (
        <>
          <WindowLine armies={armies} noun="defense" />
          {/* One word or two a column; a narrow card puts Usage with Army. */}
          <div className="army-defense-row army-head" aria-hidden="true">
            <span>
              Army<span className="army-head-usage"> · usage</span>
            </span>
            <span>Usage</span>
            <span>Not tripled</span>
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
