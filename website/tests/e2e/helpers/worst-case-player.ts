import type { Page } from "@playwright/test";

import type { PlayerPage } from "../../../app/lib/contracts";
import {
  WORST_SEASON_SUMMARY,
  WORST_SEASONS,
  worstCasePlayer,
} from "../../fixtures/player-worst-case";
import { decodePageData, encodePageData } from "./player-page";

// Sends Refresh submissions with an invalid key, so the server refuses them
// before they spend either per-visitor allowance.
export async function refuseRefreshes(page: Page, allowNext = () => false) {
  const submissions: string[] = [];
  await page.route("**/resources/players/*/refresh*", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    submissions.push(route.request().postData() ?? "");
    if (allowNext()) return route.continue();
    await route.continue({ postData: "idempotencyKey=invalid" });
  });
  return submissions;
}

// Opens #2PP from search with worst-case saved data and one past Season.
// `servesSeason` returning false leaves that read of the past Season unanswered;
// `edit` changes the player data first.
export async function openWorstCasePlayer(
  page: Page,
  servesSeason = () => true,
  edit: (player: PlayerPage) => void = () => {},
) {
  // Day 6 of a Season, so every worst-case day belongs to the current Season.
  const now = Date.parse("2026-10-10T12:00:00Z");
  await page.clock.install({ time: now });
  await page.goto("/blog");
  const saved = await page.request.get("/players/%232PP.data");
  const dataType = saved.headers()["content-type"];
  const decoded = decodePageData(await saved.text());
  const data = Object.values(decoded).find((route) => route.data?.player)!.data!;
  const player = worstCasePlayer("#2PP", now);
  edit(player);
  Object.assign(data, { player, seasons: WORST_SEASONS });
  const current = encodePageData(decoded);
  Object.assign(data, {
    selectedSeason: WORST_SEASON_SUMMARY.seasonId,
    historical: { ...WORST_SEASON_SUMMARY, tag: "#2PP" },
  });
  const season = encodePageData(decoded);
  await page.route("**/players/%232PP.data*", (route) => {
    const past = new URL(route.request().url()).searchParams.has("season");
    if (past && !servesSeason()) return;
    return route.fulfill({ contentType: dataType, body: past ? season : current });
  });

  // Client navigation reads the replaced page data.
  await page.getByRole("button", { name: "Search players" }).click();
  await page.getByRole("searchbox").fill("#2PP");
  await page.getByRole("searchbox").press("Enter");
}
