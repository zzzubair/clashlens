import type { CSSProperties } from "react";
import { Link } from "react-router";

import type { HeatCell } from "../lib/dashboard-cards";
import { ShieldIcon, Stars, SwordIcon } from "./GameIcons";
import { signedNumber as signed } from "../lib/number-format";
import { HeatFoot, HeatKey, SeasonHeatGrid } from "./SeasonHeatGrid";

const ATTACKS = [
  [3, 40],
  [3, 40],
  [2, 32],
  [3, 40],
  [3, 40],
  [1, 16],
] as const;
const DEFENSES = [
  [1, -16],
  [2, -32],
  [0, 0],
  [3, -40],
  [1, -16],
] as const;
const SLOTS = 8;
const PLACES = { lastReset: 1288, now: 1188, projected: 1140, best: 1105, worst: 1190 };
const NETS = [
  72, -18, 110, 45, -61, 88, -27, 124, -23, 96, 40, -8, 143, 57, 80, -47, 35, 101, -12,
  66, 118, -33,
];
const RANKED = [...NETS].sort((a, b) => a - b);
const HEAT: HeatCell[] = Array.from({ length: 28 }, (_, index) => {
  const day = index + 1;
  const value = NETS[index];
  if (value === undefined)
    return {
      day,
      kind: day === NETS.length + 1 ? "today" : "future",
      value: null,
      shade: null,
    };
  return {
    day,
    kind: "played",
    value,
    shade: RANKED.indexOf(value) / (RANKED.length - 1),
  };
});

const place = (rank: number) => `#${rank.toLocaleString("en-GB")}`;

export function DashboardTeaser({ loginAvailable }: { loginAvailable: boolean }) {
  return (
    <section className="teaser-cta" aria-labelledby="front-teaser-title">
      <div className="teaser-caption">
        <h2 id="front-teaser-title">Your Legend day, live</h2>
        {loginAvailable ? (
          <Link className="button button-primary" to="/signup?returnPath=%2F">
            Create account
          </Link>
        ) : (
          <p role="status">Sign-in is not available right now.</p>
        )}
      </div>
      <TeaserBoard link={loginAvailable} />
    </section>
  );
}

export function TeaserBoard({ link }: { link: boolean }) {
  return (
    <Board link={link}>
      <span className="teaser-example">Example dashboard</span>
      <div className="teaser-card teaser-day">
        <span className="teaser-label">Legend day {NETS.length + 1}</span>
        <span className="teaser-score">
          <strong className="score">5,244</strong>
          <span className="teaser-net score">
            +104<small> today</small>
          </span>
        </span>
        <span className="teaser-places">
          <span className="teaser-ranks">
            <span>
              <small>Last Reset</small>
              {place(PLACES.lastReset)}
            </span>
            <span className="teaser-now">
              <small>Now</small>
              <b className="rank-mark">{place(PLACES.now)}</b>
            </span>
            <span className="teaser-next">
              <small>
                <i className="teaser-diamond" />
                Projected place
              </small>
              {place(PLACES.projected)}
            </span>
          </span>
          <span className="teaser-range">
            <span className="teaser-range-name">Next Reset range</span>
            <small>best </small>
            {place(PLACES.best)}
            <i
              style={
                {
                  "--at": `${(100 * (PLACES.projected - PLACES.best)) / (PLACES.worst - PLACES.best)}%`,
                } as CSSProperties
              }
            />
            <small>worst </small>
            {place(PLACES.worst)}
          </span>
        </span>
        <Battles battles={ATTACKS} kind="attack" />
        <Battles battles={DEFENSES} kind="defense" />
      </div>
      <div className="teaser-card teaser-heat">
        <SeasonHeatGrid cells={HEAT} label="An example Season" loss />
        <HeatFoot readout={null}>
          <HeatKey />
        </HeatFoot>
      </div>
    </Board>
  );
}

function Board({ link, children }: { link: boolean; children: React.ReactNode }) {
  return link ? (
    <Link
      className="front-teaser teaser-board"
      to="/signup?returnPath=%2F"
      tabIndex={-1}
      aria-hidden="true"
    >
      {children}
    </Link>
  ) : (
    <div className="front-teaser teaser-board" aria-hidden="true">
      {children}
    </div>
  );
}

function Battles({
  battles,
  kind,
}: {
  battles: readonly (readonly [number, number])[];
  kind: "attack" | "defense";
}) {
  const total = battles.reduce((sum, [, trophies]) => sum + trophies, 0);
  return (
    <span className={`teaser-battles teaser-${kind}s`}>
      <span className="teaser-battles-head">
        {kind === "attack" ? <SwordIcon /> : <ShieldIcon />}
        <span className="teaser-battles-name">
          {kind === "attack" ? "Attacks" : "Defenses"}
        </span>
        {battles.length}/{SLOTS}
        <b className="score">{signed(total)}</b>
      </span>
      <span className="teaser-tiles">
        {Array.from({ length: SLOTS }, (_, index) => {
          const battle = battles[index];
          return battle ? (
            <span key={index} data-stars={battle[0]}>
              <Stars count={battle[0]} />
              {signed(battle[1])}
            </span>
          ) : (
            <span key={index} className="teaser-tile-empty" />
          );
        })}
      </span>
    </span>
  );
}
