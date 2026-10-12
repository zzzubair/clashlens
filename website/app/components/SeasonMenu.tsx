import { Form, useLocation, useNavigate } from "react-router";
import type { SummarizedSeasonRef } from "../lib/contracts";
import { canonicalPlayerPath } from "../lib/player-tag";

// A player's Season menu: the current Season and each past Season with Clash
// Lens days, in the same bold style as the Analytics filters.
export function SeasonMenu({
  tag,
  seasons,
  selectedSeason,
  currentAvailable = true,
  label,
}: {
  tag: string;
  seasons: SummarizedSeasonRef[];
  selectedSeason: string | null;
  currentAvailable?: boolean;
  label: (seasonId: string) => string;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const path = canonicalPlayerPath(tag);
  // Older finishes without Clash Lens days are in the separate Older history table.
  const past = seasons
    .filter((season) => season.source === "tracked_summary")
    .map((season) => season.seasonId);
  // A selected past Season always keeps its way back, even if the list failed.
  if (selectedSeason !== null && !past.includes(selectedSeason))
    past.push(selectedSeason);
  if (past.length === 0) return null;
  return (
    <Form method="get" action={path} className="season-menu">
      <label className="filter-field">
        Season
        <select
          name="season"
          value={selectedSeason ?? ""}
          onChange={(event) => {
            const season = event.currentTarget.value;
            void navigate(
              season ? `${path}?season=${encodeURIComponent(season)}` : path,
              {
                state: location.state,
                replace: true,
              },
            );
          }}
        >
          {currentAvailable || selectedSeason !== null ? (
            <option value="">Current Season</option>
          ) : (
            <option value="" disabled>
              Choose a Season
            </option>
          )}
          {past.map((season) => (
            <option key={season} value={season}>
              {label(season)}
            </option>
          ))}
        </select>
      </label>
      <noscript>
        <button type="submit">Show Season</button>
      </noscript>
    </Form>
  );
}
