import { data, Link, useLoaderData } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { BaseTile, Stars } from "../components/NotebookParts";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { averageLost, heldText, seasonLabel } from "../lib/notebook";
import { isShareLinkId, type SharedBase } from "../lib/notebook-share";
import type { Route } from "./+types/b.$link";
import "../notebook.css";
import "../notebook-share.css";

const HEADERS = { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" };

export function meta({ loaderData: loaded }: Route.MetaArgs) {
  return [
    {
      title: loaded?.shared
        ? `${loaded.shared.name} · Shared base · Clash Lens`
        : "Shared base · Clash Lens",
    },
    { name: "robots", content: "noindex" },
  ];
}

/** GET /b/:link — one shared base, for anyone with its unlisted link: its
 * name, live record and, as its Clasher chooses, its in-game link and their
 * player names. Never its base ID or note. A stopped link shows nothing. */
export async function loader({ params }: Route.LoaderArgs) {
  const { isNotebookEnabled } = await import("../server/config.server");
  if (!isNotebookEnabled()) throw data(null, { status: 404 });
  const none = { shared: null, error: null };
  if (!isShareLinkId(params.link)) return data(none, { status: 404, headers: HEADERS });
  try {
    const { readSharedBase } = await import("../services/notebook-share.server");
    const shared: SharedBase | null = await readSharedBase(params.link);
    if (shared === null) return data(none, { status: 404, headers: HEADERS });
    return data({ shared, error: null }, { headers: HEADERS });
  } catch (cause) {
    const { safeWebsiteError } = await import("../server/errors.server");
    const error: WebsiteErrorResponse = safeWebsiteError(cause);
    return data({ shared: null, error }, { status: 503, headers: HEADERS });
  }
}

export function headers() {
  return HEADERS;
}

export default function SharedBaseRoute() {
  const { shared, error } = useLoaderData<typeof loader>();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell nb-page">
      {shared ? (
        <SharedPage shared={shared} />
      ) : (
        <>
          <h1>Shared base</h1>
          {error ? (
            <ErrorNotice error={error} />
          ) : (
            <p>
              This link doesn't open a base. Whoever shared it may have stopped sharing.
            </p>
          )}
        </>
      )}
    </main>
  );
}

function SharedPage({ shared }: { shared: SharedBase }) {
  const { record } = shared;
  const lost = averageLost(record);
  return (
    <>
      {/* The base's picture goes here once bases have pictures. */}
      <BaseTile name={shared.name} size="lg" />
      <div className="nb-base-title">
        <h1>{shared.name}</h1>
        {shared.link ? (
          <a
            className="button"
            href={shared.link}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open in game
          </a>
        ) : null}
      </div>
      {record.defenses ? (
        <dl className="nb-shared-stats">
          <div>
            <dt>held</dt>
            <dd>
              {record.held}/{record.defenses}
            </dd>
          </div>
          <div>
            <dt>a defense</dt>
            <dd>{lost}</dd>
          </div>
          <div>
            <dt>{record.days === 1 ? "day" : "days"}</dt>
            <dd>{record.days}</dd>
          </div>
        </dl>
      ) : (
        <p className="nb-headline">
          {heldText(record).replace(/^./, (letter) => letter.toUpperCase())}
        </p>
      )}
      <Stars record={record} />
      {/* The base's best and worst armies go here. */}
      {shared.players?.length ? (
        <section className="nb-base-part" aria-labelledby="nb-shared-each">
          <h2 id="nb-shared-each">Each account</h2>
          <ul className="nb-per-account">
            {shared.players.map((player, index) => {
              const each = averageLost(player.record);
              return (
                <li key={index}>
                  <b>{player.name}</b>
                  <span>
                    {heldText(player.record)}
                    {each ? ` · ${each} a defense` : ""} · {player.record.days}{" "}
                    {player.record.days === 1 ? "day" : "days"}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}
      <p className="nb-hint nb-shared-foot">
        Recorded by Clash Lens · Season{" "}
        {seasonLabel(shared.season.start, shared.season.end)}
        {shared.asOfDay ? ` · as of Day ${shared.asOfDay}` : ""}. Held means the attacker
        didn't get 3 stars, on Legend days only.
      </p>
      <Link className="button secondary" to="/notebook">
        Track your own bases
      </Link>
    </>
  );
}
