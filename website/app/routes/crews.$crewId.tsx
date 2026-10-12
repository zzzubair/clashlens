import { data, Link, useActionData, useLoaderData } from "react-router";

import { type BackHandle } from "../components/BackLink";
import {
  BoardRows,
  emptyBoardText,
  PeriodSwitch,
  RoleChip,
} from "../components/CrewBoards";
import { CrewInvite } from "../components/CrewForms";
import { ErrorNotice } from "../components/ErrorNotice";
import {
  BOARD_KEYS,
  BOARDS,
  boardDescription,
  crewPeriod,
  NOW_BOARDS,
  type BoardKey,
  type Crew,
  type CrewBoards,
  type CrewPeriod,
  type InviteLink,
} from "../lib/crew-contracts";
import type { Route } from "./+types/crews.$crewId";
import "../crews.css";

const NO_STORE = { "Cache-Control": "no-store" };
/** Each card shows the top of its board; the board's own page shows the rest. */
const CARD_ROWS = 5;

export const handle: BackHandle = { back: { to: "/crews", label: "Crews" } };

/** GET /crews/:crewId — one crew's boards, for its members only. */
export async function loader({ request, params }: Route.LoaderArgs) {
  const period = crewPeriod(new URL(request.url).searchParams.get("period"));
  const { loadCrewPage } = await import("../services/crews.server");
  const page = await loadCrewPage(request, params.crewId, period);
  const { freshIdempotencyKey } = await import("../server/actions.server");
  return data(
    { ...page, period, idempotencyKey: freshIdempotencyKey() },
    { status: page.error ? 503 : 200, headers: NO_STORE },
  );
}

/** POST /crews/:crewId — Invite: the clasher's live link, or a new one. */
export async function action({ request, params }: Route.ActionArgs) {
  const { crewFormAction, makeInvite } = await import("../services/crews.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  return crewFormAction(
    request,
    params.crewId,
    ["invite"],
    async ({ identity, crewId, fields, key }) => {
      const fresh = fields["new"] === "1";
      const made = await makeInvite(identity, crewId, fresh, key(fresh ? "new" : "same"));
      const link = new URL(`/crews/join/${made.code}`, getWebsiteConfig().publicOrigin);
      const invite: InviteLink = { ...made, link: link.href };
      return { invite };
    },
  );
}

export function headers() {
  return NO_STORE;
}

export function meta({ loaderData: loaded }: Route.MetaArgs) {
  return [
    { title: loaded?.crew ? `${loaded.crew.name} · Clash Lens` : "Crew · Clash Lens" },
  ];
}

export default function CrewRoute() {
  const { crew, boards, period, error, idempotencyKey } = useLoaderData<typeof loader>();
  const answer = useActionData<typeof action>();
  if (crew === null || boards === null) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
        <h1>Crew unavailable</h1>
        {error ? <ErrorNotice error={error} /> : null}
      </main>
    );
  }
  const open = Math.max(0, crew.size - crew.used);
  const clashers = crew.members.length;
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
      <section className="crew-panel crew-head" aria-labelledby="crew-title">
        <h1 id="crew-title">{crew.name}</h1>
        <p className="crew-meta">
          {clashers} {clashers === 1 ? "clasher" : "clashers"}
          <RoleChip role={crew.myRole} />
        </p>
        <div className="crew-bar" aria-hidden="true">
          <i style={{ width: `${Math.min(100, (crew.used / crew.size) * 100)}%` }} />
        </div>
        <p className="crew-places">
          <span>
            {crew.used} of {crew.size} places
          </span>
          {open === 0 ? <b className="crew-full">Full</b> : <span>{open} open</span>}
        </p>
        <div className="crew-actions">
          {open > 0 ? (
            <CrewInvite
              crewName={crew.name}
              idempotencyKey={idempotencyKey}
              answer={answer ?? null}
            />
          ) : null}
          <Link className="button secondary" to={`/crews/${crew.crewId}/members`}>
            Members
          </Link>
          {crew.myRole === "member" ? null : (
            <Link className="button secondary" to={`/crews/${crew.crewId}/settings`}>
              Edit
            </Link>
          )}
        </div>
      </section>

      <h2 className="crew-section-title">Right now</h2>
      <div className="crew-boards">
        {NOW_BOARDS.map((key) => (
          <BoardCard key={key} board={key} crew={crew} boards={boards} period={period} />
        ))}
      </div>

      <h2 className="crew-section-title">Averages and streaks</h2>
      <PeriodSwitch period={period} />
      <div className="crew-boards">
        {BOARD_KEYS.filter((key) => !NOW_BOARDS.includes(key)).map((key) => (
          <BoardCard key={key} board={key} crew={crew} boards={boards} period={period} />
        ))}
      </div>
    </main>
  );
}

/**
 * One board's top rows. Cards side by side share one height, and their
 * header, rows and footer start on the same lines (crews.css), so a card
 * always has exactly these three parts.
 */
function BoardCard({
  board,
  crew,
  boards,
  period,
}: {
  board: BoardKey;
  crew: Crew;
  boards: CrewBoards;
  period: CrewPeriod;
}) {
  const { rows, missing } = boards.boards[board];
  const fixed = NOW_BOARDS.includes(board);
  const path = `/crews/${crew.crewId}/boards/${BOARDS[board].slug}${
    fixed || period === "season" ? "" : `?period=${period}`
  }`;
  return (
    <section className="crew-panel crew-board" aria-labelledby={`board-${board}`}>
      <div className="crew-board-head">
        <h3 id={`board-${board}`}>{BOARDS[board].title}</h3>
        <p>{boardDescription(board, period)}</p>
      </div>
      {rows.length > 0 ? (
        <BoardRows
          rows={rows.slice(0, CARD_ROWS)}
          board={board}
          period={period}
          backLabel={crew.name}
        />
      ) : (
        <p className="crew-empty">{emptyBoardText(board, period, boards.dayNumber)}</p>
      )}
      <div className="crew-board-foot">
        {missing.length > 0 ? (
          <span>
            {missing.length} {missing.length === 1 ? "account has" : "accounts have"} no
            data here.{" "}
            <Link to={`${path}#not-on-board`}>
              Who<span className="sr-only"> is not on {BOARDS[board].title}</span>?
            </Link>
          </span>
        ) : (
          <span />
        )}
        {rows.length > CARD_ROWS ? (
          <Link className="button secondary" to={path}>
            See all {rows.length}
            <span className="sr-only"> on {BOARDS[board].title}</span>
          </Link>
        ) : null}
      </div>
    </section>
  );
}
