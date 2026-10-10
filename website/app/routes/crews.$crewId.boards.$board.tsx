import { data, Link, useLoaderData, type UIMatch } from "react-router";

import { type BackHandle, useBackState } from "../components/BackLink";
import { BoardRows, emptyBoardText, PeriodSwitch } from "../components/CrewBoards";
import { ErrorNotice } from "../components/ErrorNotice";
import {
  BOARDS,
  boardDescription,
  boardFromSlug,
  crewPeriod,
  missingReason,
  NOW_BOARDS,
} from "../lib/crew-contracts";
import { canonicalPlayerPath } from "../lib/player-tag";
import type { Route } from "./+types/crews.$crewId.boards.$board";
import "../crews.css";

const NO_STORE = { "Cache-Control": "no-store" };

/** Back leads to the crew, on the same period. */
export const handle: BackHandle = {
  back: (match: UIMatch) => {
    const loaded = match.loaderData as
      Awaited<ReturnType<typeof loader>>["data"] | undefined;
    const crew = loaded?.crew;
    if (!crew) return null;
    const period = loaded.period === "season" ? "" : `?period=${loaded.period}`;
    return { to: `/crews/${crew.crewId}${period}`, label: crew.name };
  },
};

/** GET /crews/:crewId/boards/:board — one crew board in full, and who is not on it. */
export async function loader({ request, params }: Route.LoaderArgs) {
  const board = boardFromSlug(params.board);
  if (board === null) throw data(null, { status: 404 });
  const period = crewPeriod(new URL(request.url).searchParams.get("period"));
  const { loadCrewPage } = await import("../services/crews.server");
  const page = await loadCrewPage(request, params.crewId, period);
  return data(
    { ...page, board, period },
    { status: page.error ? 503 : 200, headers: NO_STORE },
  );
}

export function headers() {
  return NO_STORE;
}

export function meta({ loaderData: loaded }: Route.MetaArgs) {
  return [
    {
      title: loaded?.crew
        ? `${BOARDS[loaded.board].title} · ${loaded.crew.name} · Clash Lens`
        : "Crew · Clash Lens",
    },
  ];
}

export default function CrewBoardRoute() {
  const { crew, boards, board, period, error } = useLoaderData<typeof loader>();
  const title = BOARDS[board].title;
  const backState = useBackState(title);
  if (crew === null || boards === null) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
        <h1>{title}</h1>
        {error ? <ErrorNotice error={error} /> : null}
      </main>
    );
  }
  const { rows, missing } = boards.boards[board];
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
      <p className="eyebrow">{crew.name}</p>
      <h1>{title}</h1>
      <p className="crew-lede">{boardDescription(board, period)}</p>
      {NOW_BOARDS.includes(board) ? null : <PeriodSwitch period={period} />}
      <section className="crew-panel crew-board crew-board-full" aria-label={title}>
        {rows.length > 0 ? (
          <BoardRows rows={rows} board={board} period={period} backLabel={title} />
        ) : (
          <p className="crew-empty">{emptyBoardText(board, period, boards.dayNumber)}</p>
        )}
      </section>
      {missing.length > 0 ? (
        <section
          className="crew-panel crew-missing"
          id="not-on-board"
          aria-labelledby="not-on-board-title"
        >
          <h2 id="not-on-board-title">Not on this board ({missing.length})</h2>
          <ul>
            {missing.map((account) => (
              <li key={account.tag}>
                <Link to={canonicalPlayerPath(account.tag)} state={backState}>
                  {account.name ?? account.tag}
                </Link>
                <span>
                  {account.tag} · {missingReason(account.reason)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </main>
  );
}
