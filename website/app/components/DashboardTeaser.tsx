import { Link } from "react-router";
import { DashboardIcon } from "./DashboardIcon";
import { signed, starText } from "./LegendClock";

// An example Legend day, drawn small: what a dashboard shows, not anyone's data.
// Each battle is its stars and trophies; the day's net is their sum.
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
const RIVALS = [
  { rank: 1186, name: "Sky Lord", trophies: 5248, up: true },
  { rank: 1188, name: "You", trophies: 5244, you: true },
  { rank: 1189, name: "Nightfall", trophies: 5240, up: false },
] as const;
// Net trophies of 28 Legend days: gains, losses, today (day 23) and the days
// still to come.
const HEAT = [
  24,
  -8,
  40,
  12,
  -30,
  18,
  6,
  32,
  -12,
  26,
  8,
  -4,
  38,
  14,
  22,
  -20,
  10,
  30,
  -6,
  16,
  34,
  -14,
  null,
  null,
  null,
  null,
  null,
  null,
] as const;
const TODAY = 22;

// Each played day's place in the example Season, worst (0) to best (4), as
// the real heat map colours a Season against itself.
const PLAYED: number[] = HEAT.filter((net) => net !== null).sort((a, b) => a - b);
function heatStep(net: number): number {
  return Math.min(4, Math.floor((5 * PLAYED.indexOf(net)) / PLAYED.length));
}

/**
 * What the dashboard holds, shown to a visitor who is not signed in: three
 * short points, the way in and the example board, in the order a phone reads
 * them, so Create account shows on its first screen. Sign in stays in the
 * header.
 */
export function DashboardTeaser({ loginAvailable }: { loginAvailable: boolean }) {
  return (
    <section className="teaser-cta" aria-labelledby="front-teaser-title">
      <h2 id="front-teaser-title">Your Legend day, live</h2>
      <ul className="teaser-points">
        <li>
          <DashboardIcon name="swords" />
          Every battle
        </li>
        <li>
          <DashboardIcon name="target" />
          Next Reset rank
        </li>
        <li>
          <DashboardIcon name="grid" />
          Season heat map
        </li>
      </ul>
      {loginAvailable ? (
        <Link className="button button-primary" to="/signup?returnPath=%2F">
          Create account
        </Link>
      ) : (
        <p role="status">Sign-in is not available right now.</p>
      )}
      <TeaserBoard link={loginAvailable} />
    </section>
  );
}

/** A small example dashboard: what an account shows, not anyone's data. */
export function TeaserBoard({ link }: { link: boolean }) {
  return (
    <Board link={link}>
      <span className="teaser-example">Example</span>
      <div className="teaser-card teaser-day">
        <span className="teaser-label">Legend day 23</span>
        <span className="teaser-score">
          <strong className="score">5,244</strong>
          <span className="teaser-net score">+104</span>
        </span>
        <span className="teaser-ranks">
          <span>
            <small>Last Reset</small>#1,288
          </span>
          <i />
          <span className="teaser-now">
            <small>Now</small>
            <b className="rank-mark">#1,188</b>
          </span>
          <i />
          <span className="teaser-next">
            <small>Next Reset</small>#1,140
            <small>estimate</small>
          </span>
        </span>
        <Battles battles={ATTACKS} kind="attack" />
        <Battles battles={DEFENSES} kind="defense" />
      </div>
      <div className="teaser-card teaser-clock">
        <svg viewBox="0 0 64 64">
          <circle cx="32" cy="32" r="27" />
          <circle className="teaser-clock-left" cx="32" cy="32" r="27" pathLength="100" />
        </svg>
        <span>
          <strong className="score">9h 12m</strong>
          <small>to Reset</small>
        </span>
      </div>
      <div className="teaser-card teaser-rivals">
        {RIVALS.map((rival) => (
          <span key={rival.rank} className={"you" in rival ? "teaser-you" : undefined}>
            <b className="score">#{rival.rank.toLocaleString("en-GB")}</b>
            <span>{rival.name}</span>
            <strong className="score">{rival.trophies.toLocaleString("en-GB")}</strong>
          </span>
        ))}
      </div>
      <div className="teaser-card teaser-heat">
        <span className="teaser-label">Season heat map</span>
        {HEAT.map((net, day) => (
          <i
            key={day}
            className={day === TODAY ? "teaser-today" : undefined}
            style={
              net === null ? undefined : { background: `var(--cl-heat-${heatStep(net)})` }
            }
          />
        ))}
      </div>
    </Board>
  );
}

// The example itself, a second way into Create account for a tap or click;
// keyboards and screen readers use the buttons beside it. Without a link (sign-in
// is off, or it already sits on the sign-in page) it is only a picture.
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

// A row of the day's battles as the dashboard's Legend day card draws them:
// stars over trophies in a tile, the slots still to play dashed.
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
        <DashboardIcon name={kind === "attack" ? "swords" : "shield"} />
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
              <small>{starText(battle[0])}</small>
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
