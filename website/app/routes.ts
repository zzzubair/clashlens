import { index, route } from "@react-router/dev/routes";
import type { RouteConfig } from "@react-router/dev/routes";

export default [
  index("routes/home.tsx"),
  route("players/:tag", "routes/player.tsx"),
  route("resources/players/search", "routes/player-search.ts"),
  route("resources/players/:tag/refresh", "routes/refresh.ts"),
  route("leaderboards/tracked", "routes/tracked-leaderboard.tsx"),
  route("healthz", "routes/healthz.ts"),
] satisfies RouteConfig;
