import { data, Link, redirect, useLoaderData } from "react-router";

import { RoleChip } from "../components/CrewBoards";
import { ErrorNotice } from "../components/ErrorNotice";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { MAX_CREWS, type CrewList } from "../lib/crew-contracts";
import type { Route } from "./+types/crews";
import "../crews.css";

const NO_STORE = { "Cache-Control": "no-store" };

export function meta() {
  return [{ title: "Crews · Clash Lens" }];
}

/** GET /crews — the signed-in account's crews. */
export async function loader({ request }: Route.LoaderArgs) {
  const { isCrewsEnabled } = await import("../server/config.server");
  if (!isCrewsEnabled()) throw data(null, { status: 404 });
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  try {
    const { listCrews } = await import("../services/crews.server");
    const list: CrewList = await listCrews(identity);
    return data({ list, error: null }, { headers: NO_STORE });
  } catch (cause) {
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (isAccountNotFoundError(cause)) {
      const { accountSetupPath } = await import("../server/return-path.server");
      const url = new URL(request.url);
      throw redirect(accountSetupPath(url.pathname, url));
    }
    const { safeWebsiteError } = await import("../server/errors.server");
    const error: WebsiteErrorResponse = safeWebsiteError(cause);
    return data({ list: null, error }, { status: 503, headers: NO_STORE });
  }
}

export function headers() {
  return NO_STORE;
}

export default function CrewsRoute() {
  const { list, error } = useLoaderData<typeof loader>();
  const crews = list?.crews ?? [];
  const max = list?.maxCrews ?? MAX_CREWS;
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell crew-page">
      <h1>Crews</h1>
      {error ? (
        <ErrorNotice error={error} />
      ) : (
        <p className="crew-create">
          {crews.length < max ? (
            <Link className="button button-primary" to="/crews/new">
              Create a crew
            </Link>
          ) : (
            <span className="button button-primary" aria-disabled="true">
              Create a crew
            </span>
          )}
          <span>
            {crews.length} of {max} crews
          </span>
        </p>
      )}
      {crews.length > 0 ? (
        <ul className="crew-list">
          {crews.map((crew) => (
            <li key={crew.crewId}>
              <Link className="crew-panel crew-list-item" to={`/crews/${crew.crewId}`}>
                <span className="crew-icon" aria-hidden="true">
                  {Array.from(crew.name)[0]}
                </span>
                <span className="crew-list-name">
                  <b>{crew.name}</b>
                  <span>
                    {crew.used} of {crew.size} places
                  </span>
                </span>
                <RoleChip role={crew.role} />
              </Link>
            </li>
          ))}
        </ul>
      ) : null}
    </main>
  );
}
