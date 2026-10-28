import { Link } from "react-router";

import type { PlayerCrew } from "../lib/crew-contracts";
import { useBackState } from "./BackLink";

// None, or an unavailable answer, shows nothing.
export function PlayerCrews({
  crews,
  name,
}: {
  crews: PlayerCrew[] | null | undefined;
  name: string;
}) {
  const backState = useBackState(name);
  if (!crews || crews.length === 0) return null;
  return (
    <span className="player-crews">
      <span className="sr-only">{crews.length === 1 ? "Crew" : "Crews"}: </span>
      {crews.map((crew, index) => (
        <span key={crew.crewId}>
          {index > 0 ? " · " : null}
          {/* The Crew's crest, as on its page, sets it apart from the clan. */}
          <Link to={`/crews/${crew.crewId}`} state={backState}>
            <span className="player-crew-crest" aria-hidden="true">
              {Array.from(crew.name)[0]}
            </span>
            <bdi>{crew.name}</bdi>
          </Link>
        </span>
      ))}
    </span>
  );
}
