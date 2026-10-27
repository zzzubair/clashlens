import {
  data,
  Link,
  useActionData,
  useLoaderData,
  useSearchParams,
  type ShouldRevalidateFunctionArgs,
} from "react-router";

import { type BackHandle, useBackState } from "../components/BackLink";
import {
  BoardRows,
  crewSearch,
  DEFENSE_BOARDS,
  DefenseOrder,
  emptyBoardText,
  PeriodSwitch,
  PlacesMeter,
  RoleChip,
} from "../components/CrewBoards";
import { CrewInvite } from "../components/CrewForms";
import { ErrorNotice } from "../components/ErrorNotice";
import { SavedLine } from "../components/SavedLine";
import { TrophyMark } from "../components/LeaderboardShared";
import {
  BOARDS,
  boardDescription,
  boardFromSlug,
  crewPeriod,
  NOW_BOARDS,
  type BoardKey,
  type CrewBoards,
  type CrewPeriod,
  type CrewView,
  type InviteLink,
} from "../lib/crew-contracts";
import { canonicalPlayerPath } from "../lib/player-tag";
import type { Route } from "./+types/crews.$crewId";
import "../crews.css";

const NO_STORE = { "Cache-Control": "no-store" };
/** Each card shows the top of its board; the board's own page shows the rest. */
const CARD_ROWS = 5;
/** The players list shows this many, then Show all. */
const PLAYER_ROWS = 10;

/** A phone shows one board at a time, picked from these tabs. */
const TABS: readonly { label: string; boards: readonly BoardKey[] }[] = [
  { label: "Live", boards: ["live"] },
  { label: "Reset", boards: ["top"] },
  { label: "Attack", boards: ["attackers"] },
  { label: "Defense", boards: DEFENSE_BOARDS },
  { label: "Streaks", boards: ["streaks"] },
];

/** Back leads members to their crews; anyone else back where they came from. */
export const handle: BackHandle = {
  back: (match) =>
    (match.loaderData as { crew?: CrewView | null } | undefined)?.crew?.myRole
      ? { to: "/crews", label: "Crews" }
      : null,
};

/**
 * GET /crews/:crewId — one crew's boards and accounts, for anyone. Only its
 * members see Invite, Members and Edit; `?saved=joined`, after an invite, tells a
 * member they joined. `?board=` picks the board a phone shows, and whether
 * defenders read best or worst first.
 */
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

/** Every board is already loaded, so picking another one reads nothing new. */
export function shouldRevalidate({
  currentUrl,
  nextUrl,
  formMethod,
  defaultShouldRevalidate,
}: ShouldRevalidateFunctionArgs) {
  const rest = (url: URL) => {
    const params = new URLSearchParams(url.search);
    params.delete("board");
    return `${url.pathname}?${params.toString()}`;
  };
  if (!formMethod && rest(currentUrl) === rest(nextUrl)) return false;
  return defaultShouldRevalidate;
}

export function meta({ loaderData: loaded }: Route.MetaArgs) {
  return [
    { title: loaded?.crew ? `${loaded.crew.name} · Clash Lens` : "Crew · Clash Lens" },
  ];
}

export default function CrewRoute() {
  const { crew, boards, period, error, idempotencyKey } = useLoaderData<typeof loader>();
  const answer = useActionData<typeof action>();
  const [searchParams] = useSearchParams();
  // Set by joining from an invite link.
  const joined = searchParams.get("saved") === "joined";
  const shown = boardFromSlug(searchParams.get("board") ?? undefined) ?? "live";
  if (crew === null || boards === null) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
        <h1>Crew unavailable</h1>
        {error ? <ErrorNotice error={error} /> : null}
      </main>
    );
  }
  const open = Math.max(0, crew.size - crew.used);
  const { clashers, myRole } = crew;
  const defense = shown === "worst_defenders" ? shown : "best_defenders";
  const card = (board: BoardKey) => (
    <BoardCard
      board={board}
      current={board === shown}
      crew={crew}
      boards={boards}
      period={period}
    />
  );
  return (
    <main id="main-content" tabIndex={-1} className="page-shell crew-page">
      {joined && myRole !== null ? <SavedLine>You joined {crew.name}.</SavedLine> : null}
      <section className="crew-panel crew-head" aria-labelledby="crew-title">
        <span className="crew-icon" aria-hidden="true">
          {Array.from(crew.name)[0]}
        </span>
        <div className="crew-title">
          <h1 id="crew-title">{crew.name}</h1>
          {myRole === null ? (
            <p className="crew-meta">Joining is by invite only.</p>
          ) : clashers === null ? null : (
            <p className="crew-meta">
              {clashers} {clashers === 1 ? "clasher" : "clashers"}
              <RoleChip role={myRole} />
            </p>
          )}
        </div>
        <div className="crew-places">
          <p>
            <span>
              {crew.used} of {crew.size} places
            </span>
            {open === 0 ? <b className="crew-full">Full</b> : <span>{open} open</span>}
          </p>
          <PlacesMeter used={crew.used} size={crew.size} />
        </div>
        {myRole === null ? null : (
          <div className="crew-actions">
            {open > 0 ? (
              <CrewInvite
                crewName={crew.name}
                canEdit={myRole !== "member"}
                idempotencyKey={idempotencyKey}
                answer={answer ?? null}
              />
            ) : null}
            <Link className="button secondary" to={`/crews/${crew.crewId}/members`}>
              Members
            </Link>
            {myRole === "member" ? null : (
              <Link className="button secondary" to={`/crews/${crew.crewId}/settings`}>
                Edit
              </Link>
            )}
          </div>
        )}
      </section>

      <nav aria-label="Boards" className="tab-row crew-tabs">
        {TABS.map(({ label, boards: keys }) => {
          const current = keys.includes(shown);
          return (
            <Link
              key={label}
              to={{ search: crewSearch(period, current ? shown : keys[0]) }}
              aria-current={current ? "page" : undefined}
              replace
              preventScrollReset
              onClick={showBoardTop}
            >
              {label}
            </Link>
          );
        })}
      </nav>

      {/* A phone shows only the picked board (crews.css); wider screens show them all. */}
      <div className="crew-boards" id="crew-boards">
        {/* The live boards speak for themselves; the heading only keeps the outline whole. */}
        <h2 className="sr-only">Right now</h2>
        {card("live")}
        {card("top")}
        <div
          className="crew-period-bar"
          data-current={NOW_BOARDS.includes(shown) ? undefined : ""}
        >
          <h2>Averages and streaks</h2>
          <PeriodSwitch period={period} board={shown} />
        </div>
        {card("attackers")}
        <BoardCard
          board={defense}
          current={defense === shown}
          crew={crew}
          boards={boards}
          period={period}
          order={
            <DefenseOrder
              board={defense}
              to={(key) => ({ search: crewSearch(period, key) })}
            />
          }
        />
        {card("streaks")}
        <CrewAccounts crew={crew} />
      </div>
    </main>
  );
}

/**
 * A tab tapped while scrolled down the page, the tabs stuck at the top,
 * brings the new board's top up under them rather than leaving its middle.
 */
function showBoardTop(event: React.MouseEvent<HTMLAnchorElement>) {
  const tabs = event.currentTarget.parentElement?.getBoundingClientRect();
  const boards = document.getElementById("crew-boards")?.getBoundingClientRect();
  if (tabs && boards && boards.top < tabs.bottom) {
    window.scrollBy({ top: boards.top - tabs.bottom });
  }
}

/** Every Clash of Clans account in the crew, most trophies first. */
function CrewAccounts({ crew }: { crew: CrewView }) {
  const backState = useBackState(crew.name);
  if (crew.players.length === 0) return null;
  const item = (player: CrewView["players"][number]) => (
    <li key={player.tag}>
      <Link to={canonicalPlayerPath(player.tag)} state={backState}>
        {player.name ?? player.tag}
      </Link>
      <span>
        {player.tag}
        {player.trophies === null ? null : (
          <>
            {" · "}
            <TrophyMark />
            {player.trophies.toLocaleString("en-GB")}
          </>
        )}
      </span>
    </li>
  );
  const rest = crew.players.slice(PLAYER_ROWS);
  return (
    <section
      className="crew-panel crew-board crew-players"
      aria-labelledby="crew-accounts-title"
    >
      <div className="crew-board-head">
        <h2 id="crew-accounts-title">Players ({crew.players.length})</h2>
      </div>
      <ul className="crew-player-list">{crew.players.slice(0, PLAYER_ROWS).map(item)}</ul>
      <div className="crew-board-foot">
        {rest.length > 0 ? (
          <details className="crew-more">
            <summary className="button secondary">Show all {crew.players.length}</summary>
            <ul className="crew-player-list">{rest.map(item)}</ul>
          </details>
        ) : null}
      </div>
    </section>
  );
}

/**
 * One board's top rows. Cards side by side share one height, and their
 * header, rows and footer start on the same lines (crews.css), so a card
 * always has exactly these three parts.
 */
function BoardCard({
  board,
  current,
  crew,
  boards,
  period,
  order,
}: {
  board: BoardKey;
  /** The board a phone shows. */
  current: boolean;
  crew: CrewView;
  boards: CrewBoards;
  period: CrewPeriod;
  /** Defenders' Best and Worst switch. */
  order?: React.ReactNode;
}) {
  const { rows, missing } = boards.boards[board];
  const fixed = NOW_BOARDS.includes(board);
  const path = `/crews/${crew.crewId}/boards/${BOARDS[board].slug}${
    fixed || period === "season" ? "" : `?period=${period}`
  }`;
  return (
    <section
      className={`crew-panel crew-board crew-board-${BOARDS[board].slug}`}
      aria-labelledby={`board-${board}`}
      data-current={current ? "" : undefined}
    >
      <div className="crew-board-head">
        <div>
          <h3 id={`board-${board}`}>{BOARDS[board].title}</h3>
          <p>{boardDescription(board, period)}</p>
        </div>
        {order}
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
