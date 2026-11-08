import type { FrontPageData } from "../components/FrontPage";
import type { OwnerChoices } from "../components/ViewAs";
import type { LinkedPlayerCard } from "./account-contracts";
import type { PlayerPage, WebsiteErrorResponse } from "./contracts";
import type { DashboardSettings } from "./dashboard";
import type { CardReads } from "./dashboard-cards";

export type DashboardLoaderData =
  | ({ kind: "signed-out" } & FrontPageData)
  | { kind: "error"; error: WebsiteErrorResponse }
  | {
      kind: "signed-in";
      players: LinkedPlayerCard[];
      playersUnavailable: boolean;
      selectedTag: string | null;
      settings: DashboardSettings;
      /** Each card's read, sent on as it finishes. */
      cards: CardReads;
      /** The Reset that ends the Legend day the cards were read for. */
      dayEndsMs: number;
      /** The selected account's player page, when it left Legend I this Season. */
      leftPlayer: PlayerPage | null;
      idempotencyKey: string;
      /** The signed-in account is the site owner, who may View as any player. */
      canViewAs?: boolean;
      /**
       * The player the owner views the dashboard as, read only, which is then
       * `players`' only one; "not-found" when no player has the tag asked for.
       */
      viewAs?: { tag: string; name: string | null } | "not-found" | null;
      /** The owner's own pin and Discord switches; null for anyone else. */
      ownerChoices?: OwnerChoices | null;
    };

export interface DashboardActionData {
  saved: boolean;
  error: string | null;
  idempotencyKey: string;
  /** The settings as saved, which the page shows without reading itself again. */
  settings: DashboardSettings | null;
}
