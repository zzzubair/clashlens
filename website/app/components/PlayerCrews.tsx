import { Link } from "react-router";

import type { PlayerCrew } from "../lib/crew-contracts";
import { useBackState } from "./BackLink";
import { Crest } from "./GameIcons";

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
      <span className="player-crews-label">{crews.length === 1 ? "Crew" : "Crews"}</span>
      {crews.map((crew, index) => (
        <span key={crew.crewId}>
          {index > 0 ? " · " : null}
          <Link to={`/crews/${crew.crewId}`} state={backState}>
            <Crest name={crew.name} />
            <bdi>{crew.name}</bdi>
          </Link>
        </span>
      ))}
    </span>
  );
}
