import type { AnalyticsSettings } from "./analytics";
import type { UpdateStatus } from "./contracts";

export interface RootLoaderData {
  loggedIn: boolean;
  accountLabel: string | null;
  accountUsername: string | null;
  logoutIdempotencyKey: string | null;
  loginKey: string | null;
  updateStatus: UpdateStatus | null;
  dashboardEnabled?: boolean;
  crewsEnabled?: boolean;
  analytics?: AnalyticsSettings | null;
  /** The site's public origin, for each page's canonical address; null when unreadable. */
  origin?: string | null;
}
