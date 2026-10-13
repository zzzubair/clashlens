import { Suspense } from "react";
import { Await, Link } from "react-router";

import type { PlayerCrew } from "../lib/crew-contracts";
import { useBackState } from "./BackLink";

// The crews stream in after the rest of the page; none, or an unavailable
// answer, shows nothing.
export function PlayerCrews({
  crews,
  name,
}: {
  crews: Promise<PlayerCrew[] | null> | undefined;
  name: string;
}) {
  const backState = useBackState(name);
  if (crews === undefined) return null;
  return (
    <Suspense fallback={null}>
      <Await resolve={crews} errorElement={null}>
        {(resolved) =>
          resolved && resolved.length > 0 ? (
            <p className="player-crews">
              {resolved.length === 1 ? "Crew" : "Crews"}:{" "}
              {resolved.map((crew, index) => (
                <span key={crew.crewId}>
                  {index > 0 ? " · " : null}
                  <Link to={`/crews/${crew.crewId}`} state={backState}>
                    <bdi>{crew.name}</bdi>
                  </Link>
                </span>
              ))}
            </p>
          ) : null
        }
      </Await>
    </Suspense>
  );
}
