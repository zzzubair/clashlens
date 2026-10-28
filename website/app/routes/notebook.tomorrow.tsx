import { useState } from "react";
import { data, Link, redirect, useLoaderData, useOutletContext } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { NotebookPages, type Saver } from "../components/NotebookParts";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { playerName, type Notebook, type NotebookChange } from "../lib/notebook";
import {
  holdsText,
  nearText,
  rangeText,
  type ReachableBase,
  type ScoredBase,
  type Tomorrow,
} from "../lib/notebook-tomorrow";
import type { Route } from "./+types/notebook.tomorrow";
import "../notebook.css";

const NO_STORE = { "Cache-Control": "no-store" };
const RANKED = 24;
const number = new Intl.NumberFormat("en-GB");

export function meta() {
  return [{ title: "Tomorrow · Clash Lens" }];
}

/** GET /notebook/tomorrow — the Notebook's first screen: every base the
 * signed-in Clasher can reach, ranked for one of their game accounts
 * (`?tag=`, or their first Legend account) by holds of 8 tomorrow. It reads
 * their notebook too, so "Run it from tomorrow" saves like the Notebook
 * does. While CLASHLENS_NOTEBOOK_ENABLED is off it doesn't exist. */
export async function loader({ request }: Route.LoaderArgs) {
  const { isNotebookEnabled } = await import("../server/config.server");
  if (!isNotebookEnabled()) throw data(null, { status: 404 });
  const tag = new URL(request.url).searchParams.get("tag") ?? undefined;
  if (tag !== undefined && tag.length > 16) throw data(null, { status: 404 });
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const { PythonApiError } = await import("../services/python.server");
  try {
    const { getNotebook, getTomorrow } = await import("../services/notebook.server");
    const notebook: Notebook = await getNotebook(identity);
    const tomorrow: Tomorrow | null = notebook.players.length
      ? await getTomorrow(identity, tag)
      : null;
    return data({ notebook, tomorrow, error: null }, { headers: NO_STORE });
  } catch (cause) {
    if (cause instanceof PythonApiError && cause.status === 404)
      throw data(null, { status: 404 });
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(cause)) {
      const { accountSetupPath } = await import("../server/return-path.server");
      const url = new URL(request.url);
      throw redirect(accountSetupPath(url.pathname, url));
    }
    const { safeWebsiteError } = await import("../server/errors.server");
    const error: WebsiteErrorResponse = safeWebsiteError(cause);
    return data(
      { notebook: null, tomorrow: null, error },
      { status: 503, headers: NO_STORE },
    );
  }
}

export function headers() {
  return NO_STORE;
}

export default function TomorrowRoute() {
  const { notebook, tomorrow, error } = useLoaderData<typeof loader>();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell nb-page">
      <header className="nb-title">
        <h1>Tomorrow</h1>
      </header>
      <NotebookPages here="tomorrow" />
      {error ? <ErrorNotice error={error} /> : null}
      {notebook && !tomorrow ? (
        <p className="nb-empty">
          Link your Clash of Clans account to see which base to run tomorrow.{" "}
          <Link to="/account/verify-player">Link a player</Link>
        </p>
      ) : null}
      {notebook && tomorrow ? (
        <TomorrowPage notebook={notebook} tomorrow={tomorrow} />
      ) : null}
    </main>
  );
}

function TomorrowPage({
  notebook,
  tomorrow,
}: {
  notebook: Notebook;
  tomorrow: Tomorrow;
}) {
  const saver = useOutletContext<Saver>();
  const name = playerName(tomorrow.player);
  const ranked = tomorrow.bases.filter((base) => base.ranked);
  const guesses = tomorrow.bases.filter((base) => !base.ranked);

  function run(base: ReachableBase) {
    // Picks go in your own notebook, adding the base there first.
    const day = notebook.season.tomorrow;
    const change: NotebookChange = base.mine
      ? { action: "pick", tag: tomorrow.player.tag, day, base: base.id, crew: null }
      : { action: "add", base: base.id, tag: tomorrow.player.tag, day, crew: null };
    saver.save(change, () => ({
      text: `${base.name} runs on ${name} from tomorrow.`,
      actions: [],
    }));
  }

  return (
    <>
      <p className="lede">
        What base should {name} run tomorrow? Every base you can reach, from your notebook
        and every Crew you're in, best first.
      </p>
      {tomorrow.players.length > 1 ? (
        <nav className="nb-seg" aria-label="Account">
          {tomorrow.players.map((player) => (
            <Link
              key={player.tag}
              to={`?${new URLSearchParams({ tag: player.tag }).toString()}`}
              aria-current={player.tag === tomorrow.player.tag ? "page" : undefined}
            >
              {playerName(player)}
            </Link>
          ))}
        </nav>
      ) : null}
      <Meet tomorrow={tomorrow} />
      {tomorrow.trophies ? (
        <section className="nb-section" aria-labelledby="nb-ranked-title">
          <h2 id="nb-ranked-title">
            {ranked.length
              ? `Ranked: ${RANKED}+ defenses near you`
              : `No base has ${RANKED} defenses near you yet`}
          </h2>
          {ranked.map((base, index) => (
            <BaseCard
              key={base.id}
              base={base}
              place={index + 1}
              best={index === 0 ? name : null}
              busy={saver.busy}
              onRun={() => run(base)}
            />
          ))}
          {guesses.length ? (
            <>
              <h2>Early guesses: under {RANKED} defenses near you</h2>
              <p className="nb-hint">
                Mostly the average base near you, nudged by the few results there are.
                They move up once results come in.
              </p>
              {guesses.map((base, index) => (
                <BaseCard
                  key={base.id}
                  base={base}
                  place={ranked.length + index + 1}
                  best={null}
                  busy={saver.busy}
                  onRun={() => run(base)}
                />
              ))}
            </>
          ) : null}
          {tomorrow.untried.length ? (
            <>
              <h2>{tomorrow.meta.length ? "Not run yet" : "Not ranked yet"}</h2>
              <ul className="nb-per-account">
                {tomorrow.untried.map((base) => (
                  <li key={base.id}>
                    <b>{base.name}</b>
                    <span>{base.where.join(", ")}</span>
                    <small>
                      {tomorrow.meta.length
                        ? "No results yet, so no prediction. Someone has to try it first."
                        : "Nothing to rank against until armies near you are counted."}
                    </small>
                    <RunButton
                      running={base.running}
                      busy={saver.busy}
                      onRun={() => run(base)}
                    />
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          {tomorrow.bases.length + tomorrow.untried.length === 0 ? (
            <p className="nb-empty">
              No bases yet. Add one to <Link to="/notebook">your notebook</Link>, or join
              a Crew that has some.
            </p>
          ) : null}
        </section>
      ) : null}
      {tomorrow.trophies ? (
        <section className="nb-tm-card" aria-labelledby="nb-why-holds">
          <h2 id="nb-why-holds">Why holds, not trophies lost</h2>
          <p>
            Two bases can both lose 270 in a day: one held 2 of 8 (6 triples), the other 5
            of 8 (3 triples and cheap holds). The second is the better base. So the list
            ranks by holds of 8, counted defense by defense over many days, and shows
            trophies lost second.
          </p>
        </section>
      ) : null}
    </>
  );
}

/** Who the account will meet: its Reset trophies, about, how far from them
 * defenses count, and the armies attacking players there. */
function Meet({ tomorrow }: { tomorrow: Tomorrow }) {
  const { trophies, meta, width, gap } = tomorrow;
  const name = playerName(tomorrow.player);
  const when = `${tomorrow.weekday}, Day ${tomorrow.day}`;
  if (!trophies)
    return (
      <section className="nb-tm-card" aria-labelledby="nb-meet-title">
        <h2 id="nb-meet-title">Who you will meet</h2>
        <p className="nb-hint">
          There are no trophies for {name} yet, so nothing to rank against. Check back
          once Clash Lens has read their profile.
        </p>
      </section>
    );
  const top = meta[0]?.share ?? 1;
  return (
    <section className="nb-tm-card" aria-labelledby="nb-meet-title">
      <h2 id="nb-meet-title">Who you will meet</h2>
      <p className="nb-headline">
        About {number.format(trophies.about)} at the Reset
        <small>{when}</small>
      </p>
      <p className="nb-hint">
        {tomorrow.day === 1
          ? "A new Season starts, so everyone starts on 5,000."
          : `Now ${number.format(trophies.now)}, with ${trophies.attacksLeft} ${trophies.attacksLeft === 1 ? "attack" : "attacks"} and ${trophies.defensesLeft} ${trophies.defensesLeft === 1 ? "defense" : "defenses"} left today. "About" is the middle of 200 imagined finishes of today, the same ones behind Projected place.`}
      </p>
      {width !== null && gap !== null ? (
        <p className="nb-hint">
          At your trophies, attackers and targets are usually {gap} trophies apart, so
          this counts defenses on players who started within about ±{width} of you.
        </p>
      ) : null}
      <h3>Armies they used, last 7 days</h3>
      {meta.length ? (
        <ul className="nb-tm-bars">
          {meta.map((army) => (
            <li key={army.name}>
              <span>{army.name}</span>
              <span className="nb-tm-track" aria-hidden="true">
                <i style={{ width: `${Math.round((100 * army.share) / top)}%` }} />
              </span>
              <b>{Math.round(100 * army.share)}%</b>
            </li>
          ))}
        </ul>
      ) : (
        <p className="nb-hint">No counted defenses near you in the last 7 days yet.</p>
      )}
    </section>
  );
}

function BaseCard({
  base,
  place,
  best,
  busy,
  onRun,
}: {
  base: ScoredBase;
  place: number;
  best: string | null;
  busy: boolean;
  onRun: () => void;
}) {
  const [why, setWhy] = useState(false);
  const whyId = `nb-why-${base.id}`;
  return (
    <article className={best ? "nb-tm-card nb-tm-best" : "nb-tm-card"}>
      {best ? <p className="nb-tm-kicker">Best pick for {best}</p> : null}
      <div className="nb-tm-head">
        <h3>
          {place}. {base.name}
        </h3>
        <span className="nb-status">
          {base.ranked ? `${base.near} near you` : "Early guess"}
        </span>
      </div>
      <p className="nb-hint">
        {base.where.join(", ")} · {nearText(base)}
      </p>
      <p className="nb-headline">
        {holdsText(base)}
        <small>{rangeText(base)}</small>
      </p>
      {base.widened ? (
        <p className="nb-hint">
          Under {RANKED} defenses close to you, so this looks as wide as ±{base.width}.
          Players that far away meet somewhat different attackers.
        </p>
      ) : null}
      {base.recent === 0 ? (
        <p className="nb-hint">
          Nobody ran it near your trophies in the last 7 days, so this rests on older
          results and may miss this week's armies.
        </p>
      ) : null}
      <div className="nb-actions">
        <button
          type="button"
          className="button secondary"
          aria-expanded={why}
          aria-controls={whyId}
          onClick={() => setWhy(!why)}
        >
          {why ? "Hide why" : "Why?"}
        </button>
        <RunButton running={base.running} busy={busy} onRun={onRun} />
      </div>
      {why ? (
        <div id={whyId}>
          <ul className="nb-per-account">
            {base.why.map((row) => (
              <li key={row.name}>
                <b>{row.name}</b>
                <span>
                  {Math.round(100 * row.share)}% of attacks · holds{" "}
                  {Math.round(100 * row.holds)}%
                </span>
                <small>
                  Tripled {row.tripled} of {row.defenses} near you
                </small>
              </li>
            ))}
          </ul>
          <p className="nb-hint">
            A defense counts fully when its defender started on your trophies, and less
            the further away they were. The last 7 days count fully, the 2 weeks before
            about a third, older a tenth. Each army also gets 10 pretend defenses at the
            average base's rate near you, so one lucky day can't top the list. Holds of 8
            is 8 × each army's share × its hold chance, added up.
          </p>
        </div>
      ) : null}
    </article>
  );
}

function RunButton({
  running,
  busy,
  onRun,
}: {
  running: boolean;
  busy: boolean;
  onRun: () => void;
}) {
  return running ? (
    <button type="button" className="button" disabled>
      ✓ Running from tomorrow
    </button>
  ) : (
    <button type="button" className="button" disabled={busy} onClick={onRun}>
      Run it from tomorrow
    </button>
  );
}
