import {
  data,
  Link,
  useActionData,
  useLoaderData,
  useSearchParams,
  type ShouldRevalidateFunctionArgs,
} from "react-router";

import { type BackHandle } from "../components/BackLink";
import {
  boardCaption,
  BoardRows,
  boardTitle,
  crewCount,
  crewSearch,
  DEFENSE_BOARDS,
  DefenseOrder,
  emptyBoardText,
  PeriodSwitch,
  PlacesMeter,
  RoleChip,
} from "../components/CrewBoards";
import { CrewInvite } from "../components/CrewForms";
import { CrewDay, sinceReset } from "../components/CrewDay";
import { ErrorNotice } from "../components/ErrorNotice";
import { Crest } from "../components/GameIcons";
import { SavedLine } from "../components/SavedLine";
import {
  BOARDS,
  boardFromSlug,
  crewPeriod,
  NOW_BOARDS,
  type BoardKey,
  type CrewBoards,
  type CrewPeriod,
  type CrewView,
  type InviteLink,
} from "../lib/crew-contracts";
import type { Route } from "./+types/crews.$crewId";
import "../crews.css";
import "../crew-day.css";

const NO_STORE = { "Cache-Control": "no-store" };
/** Each card shows the top of its board; the board's own page shows the rest. */
const CARD_ROWS = 5;

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
        <Crest name={crew.name} />
        <div className="crew-title">
          <h1 id="crew-title">{crew.name}</h1>
          {myRole === null ? (
            <p className="crew-meta">By invite only</p>
          ) : myRole === "member" ? null : (
            <p className="crew-meta">
              <RoleChip role={myRole} />
            </p>
          )}
        </div>
        <div className="crew-places">
          {/* Accounts fill a crew's room; the Clashers who own them may hold
              several each. The meter says how full, with its count beside it. */}
          <p>{crewCount(crew.used, clashers)}</p>
          <span className="crew-room">
            <PlacesMeter used={crew.used} size={crew.size} />
            <span aria-hidden="true">
              {crew.used}/{crew.size}
            </span>
            <span className="sr-only">
              Room for {crew.size} accounts, {open} free
            </span>
          </span>
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
        <CrewDay live={boards.boards.live.rows} />
      </section>

      {/* The dock sticks under the top bar; its bar fills in only once stuck
          (crews.css), so the header's glow fades out behind the switch. */}
      <div className="crew-tabs-dock">
        <div className="crew-tabs-bar">
          <nav aria-label="Boards" className="leaderboard-view-switch crew-tabs">
            {TABS.map(({ label, boards: keys }) => {
              const current = keys.includes(shown);
              return (
                <Link
                  key={label}
                  className="button secondary"
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
        </div>
      </div>

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
  // Live rows say what each account has won or lost since the last Reset.
  const today = board === "live" ? sinceReset(boards.boards.live.rows) : undefined;
  // Boards with rows share their row on a wider screen (crews.css); an empty
  // one shrinks to a line under them. Both defender boards hold the same days.
  const filled = (
    fixed ? NOW_BOARDS : (["attackers", "best_defenders", "streaks"] as const)
  ).filter((key) => boards.boards[key].rows.length > 0).length;
  const span = {
    "--span-wide": 6 / Math.max(1, filled),
    "--span-mid": filled === 1 || (filled === 3 && board === "streaks") ? 2 : 1,
  } as React.CSSProperties;
  const path = `/crews/${crew.crewId}/boards/${BOARDS[board].slug}${
    fixed || period === "season" ? "" : `?period=${period}`
  }`;
  return (
    <section
      className={`crew-panel crew-board crew-board-${BOARDS[board].slug}`}
      aria-labelledby={`board-${board}`}
      data-current={current ? "" : undefined}
      data-empty={rows.length === 0 ? "" : undefined}
      style={rows.length === 0 ? undefined : span}
    >
      {/* The title is the one way to a board's own page, with every account
          and who is missing. */}
      <div className="crew-board-head">
        <div>
          <h3 id={`board-${board}`}>
            <Link className="crew-board-open" to={path}>
              {boardTitle(board)}
              <span className="sr-only">
                {rows.length > CARD_ROWS ? `, all ${rows.length}` : ", in full"}
              </span>
            </Link>
          </h3>
          <p>{boardCaption(board, period)}</p>
        </div>
        {order}
      </div>
      {rows.length > 0 ? (
        <BoardRows
          rows={rows}
          limit={CARD_ROWS}
          board={board}
          period={period}
          backLabel={crew.name}
          today={today}
        />
      ) : (
        <div className="crew-empty">
          <b>{emptyBoardText(board, period, boards.dayNumber)}</b>
          {missing.length > 0 ? (
            <Link to={`${path}#not-on-board`}>
              See why<span className="sr-only"> no one is on {boardTitle(board)}</span>
            </Link>
          ) : null}
        </div>
      )}
      <div className="crew-board-foot">
        {/* The board's own page has every account, as Show all does on the
            dashboard's lists. */}
        {rows.length > CARD_ROWS ? (
          <Link className="show-all" to={path}>
            Show all {rows.length}
            <span className="sr-only"> on {boardTitle(board)}</span>
          </Link>
        ) : null}
        {missing.length > 0 && rows.length > 0 ? (
          <span>
            {missing.length} {missing.length === 1 ? "account has" : "accounts have"} no
            data here.{" "}
            <Link to={`${path}#not-on-board`}>
              See who<span className="sr-only"> is not on {boardTitle(board)}</span>
            </Link>
          </span>
        ) : null}
      </div>
    </section>
  );
}
