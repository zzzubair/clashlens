import { Link } from "react-router";
import { DashboardIcon } from "./DashboardIcon";
import { AccountButtons } from "./NeedsAccount";

// An example Legend day, drawn small: what a dashboard shows, not anyone's data.
const ATTACKS = [3, 3, 2, 3, 3, 1, null, null] as const;
const DEFENSES = [1, 2, 0, 3, 1, null, null, null] as const;
const RIVALS = [
  { rank: 1186, name: "Sky Lord", trophies: 5248, up: true },
  { rank: 1188, name: "You", trophies: 5244, you: true },
  { rank: 1189, name: "Nightfall", trophies: 5240, up: false },
] as const;
// Net trophies of 28 Legend days: gains, losses and the days still to come.
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
  20,
  null,
  null,
  null,
  null,
  null,
] as const;

/**
 * What the dashboard holds, shown to a visitor who is not signed in: a small
 * example dashboard, with the way in under it.
 */
export function DashboardTeaser({ loginAvailable }: { loginAvailable: boolean }) {
  return (
    <section className="front-teaser" aria-labelledby="front-teaser-title">
      <Board link={loginAvailable}>
        <span className="teaser-example">Example</span>
        <div className="teaser-card teaser-day">
          <span className="teaser-label">Legend day 23</span>
          <span className="teaser-score">
            <strong className="score">5,244</strong>
            <span className="teaser-net score">+34</span>
          </span>
          <Slots stars={ATTACKS} kind="attack" />
          <Slots stars={DEFENSES} kind="defense" />
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
        </div>
        <div className="teaser-card teaser-clock">
          <svg viewBox="0 0 64 64">
            <circle cx="32" cy="32" r="27" />
            <circle
              className="teaser-clock-left"
              cx="32"
              cy="32"
              r="27"
              pathLength="100"
            />
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
          {HEAT.map((net, day) => (
            <i
              key={day}
              data-net={net === null ? "later" : net >= 0 ? "up" : "down"}
              style={net === null ? undefined : { opacity: 0.35 + Math.abs(net) / 60 }}
            />
          ))}
        </div>
      </Board>
      <div className="teaser-cta">
        <h2 id="front-teaser-title">Your Legend day, live</h2>
        <p>Rank, rivals, Reset clock and goal. Free.</p>
        {loginAvailable ? (
          <AccountButtons returnPath="/" />
        ) : (
          <p role="status">Sign-in is not available right now.</p>
        )}
      </div>
    </section>
  );
}

// The example itself, a second way into Create account for a tap or click;
// keyboards and screen readers use the buttons below it.
function Board({ link, children }: { link: boolean; children: React.ReactNode }) {
  return link ? (
    <Link
      className="teaser-board"
      to="/signup?returnPath=%2F"
      tabIndex={-1}
      aria-hidden="true"
    >
      {children}
    </Link>
  ) : (
    <div className="teaser-board" aria-hidden="true">
      {children}
    </div>
  );
}

function Slots({
  stars,
  kind,
}: {
  stars: readonly (number | null)[];
  kind: "attack" | "defense";
}) {
  return (
    <span className={`teaser-slots teaser-${kind}`}>
      <DashboardIcon name={kind === "attack" ? "swords" : "shield"} />
      {stars.map((star, index) => (
        <i key={index} data-stars={star ?? "none"} />
      ))}
    </span>
  );
}
