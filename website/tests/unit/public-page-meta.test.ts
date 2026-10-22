import { describe, expect, it } from "vitest";

import { meta as analyticsMeta } from "../../app/routes/army-analytics";
import { meta as frontPageMeta } from "../../app/routes/dashboard";
import { meta as privacyMeta } from "../../app/routes/privacy";
import { meta as rankingsMeta } from "../../app/routes/tracked-leaderboard";
import { meta as rootMeta } from "../../app/root";

const ORIGIN = "https://clashlens.net";
const matches = (origin: string | null) => [{ id: "root", loaderData: { origin } }];

type AnyMeta = (args: never) => unknown;
function tags(meta: AnyMeta, args: Record<string, unknown>) {
  return (meta as (args: unknown) => Record<string, string>[])({
    location: { pathname: "/", search: "" },
    matches: matches(ORIGIN),
    ...args,
  });
}
const canonical = (found: Record<string, string>[]) =>
  found.find((tag) => tag.rel === "canonical")?.href;
const value = (found: Record<string, string>[], key: string, name: string) =>
  found.find((tag) => tag[key] === name)?.content;

describe("public page tags", () => {
  it("gives each public page a title, description and canonical address", () => {
    const pages = [
      [tags(frontPageMeta, { loaderData: { kind: "signed-out" } }), `${ORIGIN}/`],
      [
        tags(analyticsMeta, { location: { search: "?category=troops" } }),
        `${ORIGIN}/analytics/armies`,
      ],
      [tags(privacyMeta, {}), `${ORIGIN}/privacy`],
      [
        tags(rankingsMeta, {
          location: { search: "?q=bob&page=3&view=live&player=2PP" },
        }),
        `${ORIGIN}/leaderboards/tracked?view=live&page=3`,
      ],
      [
        tags(rankingsMeta, {
          location: { search: "?page=2&day=4&season=2026-10&view=daily" },
        }),
        `${ORIGIN}/leaderboards/tracked?view=daily&season=2026-10&day=4&page=2`,
      ],
    ] as const;
    for (const [found, address] of pages) {
      expect(found.find((tag) => "title" in tag)?.title).toMatch(/ · Clash Lens$/);
      expect(value(found, "name", "description")).toBeTruthy();
      expect(canonical(found)).toBe(address);
      expect(value(found, "property", "og:url")).toBe(address);
    }
    expect(tags(frontPageMeta, { loaderData: { kind: "signed-out" } })[0]).toEqual({
      title: "Clash of Clans Legend League tracker · Clash Lens",
    });
  });

  it("keeps the title and description when the site's address is unreadable", () => {
    const found = tags(privacyMeta, { matches: matches(null) });
    expect(found).toEqual([
      { title: "Privacy · Clash Lens" },
      {
        name: "description",
        content:
          "What Clash Lens counts about visits if you accept, and how to change your choice.",
      },
    ]);
  });

  it("names a signed-in dashboard without a canonical address", () => {
    expect(tags(frontPageMeta, { loaderData: { kind: "dashboard" } })).toEqual([
      { title: "Dashboard · Clash Lens" },
    ]);
  });
});

describe("not-found page", () => {
  it("is titled Page not found and kept out of search engines", () => {
    const notFound = { status: 404, statusText: "Not Found", internal: true, data: "" };
    expect(tags(rootMeta, { error: notFound })).toEqual([
      { title: "Page not found · Clash Lens" },
      { name: "robots", content: "noindex" },
    ]);
    expect(tags(rootMeta, { error: undefined })[0]).toEqual({ title: "Clash Lens" });
  });
});
