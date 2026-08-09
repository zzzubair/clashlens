import { index, route } from "@react-router/dev/routes";
import type { RouteConfig } from "@react-router/dev/routes";

export default [
  index("routes/home.tsx"),
  route("players/:tag", "routes/player.tsx"),
  route("resources/players/search", "routes/player-search.ts"),
  route("resources/players/:tag/refresh", "routes/refresh.ts"),
  route("leaderboards/tracked", "routes/tracked-leaderboard.tsx"),
  route("login", "routes/login.tsx"),
  route("auth/google", "routes/auth.google.ts"),
  route("auth/google/callback", "routes/auth.google.callback.tsx"),
  route("logout", "routes/logout.ts"),
  route("account", "routes/account.tsx"),
  route("account/setup", "routes/account.setup.tsx"),
  route("account/profile", "routes/account.profile.tsx"),
  route("account/saved-players", "routes/account.saved-players.tsx"),
  route("account/verify-player", "routes/account.verify-player.tsx"),
  route("account/groups", "routes/account.groups.tsx"),
  route("users/:username", "routes/users.$username.tsx"),
  route("healthz", "routes/healthz.ts"),
] satisfies RouteConfig;
