import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { expect, it } from "vitest";

import { DISCORD_INVITE_URL } from "../../app/lib/discord";
import type { RootLoaderData } from "../../app/lib/root-data";
import App from "../../app/root";

const SIGNED_OUT: RootLoaderData = {
  loggedIn: false,
  accountLabel: null,
  accountUsername: null,
  logoutIdempotencyKey: null,
  loginKey: null,
  updateStatus: null,
};

async function renderHeader(data: RootLoaderData, path = "/") {
  const handler = createStaticHandler([
    { id: "root", path: "*", loader: () => data, Component: App },
  ]);
  const context = await handler.query(new Request(`https://clashlens.example${path}`));
  if (context instanceof Response) throw new Error("unexpected response");
  const html = renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  );
  return /<header class="site-header"[\s\S]*?<\/header>/.exec(html)![0];
}

const tabsOf = (nav: string) =>
  [...nav.matchAll(/<a [^>]*>([^<]+)<\/a>/g)].map((match) => match[1]);
// Each link's or toggle's words, icons left out.
const labelsOf = (html: string) =>
  [...html.matchAll(/<(a|summary) [^>]*>([\s\S]*?)<\/\1>/g)].map((match) =>
    match[2].replace(/<[^>]*>/g, ""),
  );
const bottomOf = (header: string) =>
  /<nav class="bottom-tabs"[\s\S]*?<\/nav>/.exec(header)![0];
const moreOf = (header: string) =>
  /<details class="site-menu"[\s\S]*?<\/details>/.exec(header)![0];

it("names the tabs most useful first, the front page or dashboard Home", async () => {
  const header = await renderHeader({
    ...SIGNED_OUT,
    dashboardEnabled: true,
    crewsEnabled: true,
  });
  const wide = /<nav class="primary-nav"[\s\S]*?<\/nav>/.exec(header)![0];
  const tabs = ["Home", "Rankings", "Analytics", "Crews", "Watchlists", "Blog"];
  expect(tabsOf(wide)).toEqual(tabs);
  expect(wide).toContain('href="/account/watchlists"');
  expect(header).toContain('aria-label="Clash Lens"');

  const switchedOff = await renderHeader(SIGNED_OUT);
  expect(tabsOf(/<nav class="primary-nav"[\s\S]*?<\/nav>/.exec(switchedOff)![0])).toEqual(
    ["Rankings", "Analytics", "Watchlists", "Blog"],
  );
});

it("signed out offers Sign in back to this page and Join Discord in a new tab", async () => {
  const header = await renderHeader(SIGNED_OUT, "/players/%232PP");

  expect(header).toContain(
    'href="/login?returnPath=%2Fplayers%2F%25232PP" data-discover="true">Sign in</a>',
  );
  expect(header).toContain(
    `href="${DISCORD_INVITE_URL}" target="_blank" rel="noopener noreferrer">`,
  );
  expect(header).toContain(">Join Discord<");
  expect(header).toContain("opens in a new tab");
  expect(header).not.toContain("account-menu");
  expect(header).not.toContain("Sign out");
});

it("on a sign-in page, Sign in passes on where the Clasher was going", async () => {
  expect(await renderHeader(SIGNED_OUT, "/signup")).toContain(
    'href="/login" data-discover="true">Sign in</a>',
  );
  expect(
    await renderHeader(SIGNED_OUT, "/signup?returnPath=%2Fcrews%2Fjoin%2Fabc"),
  ).toContain(
    'href="/login?returnPath=%2Fcrews%2Fjoin%2Fabc" data-discover="true">Sign in</a>',
  );
  expect(await renderHeader(SIGNED_OUT, "/login?returnPath=%2Fcrews")).not.toContain(
    ">Sign in</a>",
  );
});

it("on phones shows everyone the same four tabs and the rest in More, the account only on top", async () => {
  const features = { dashboardEnabled: true, crewsEnabled: true };
  const tabs = ["Rankings", "Analytics", "Home", "Crews"];
  const pages = ["More", "Watchlists", "Blog", "Join Discord, opens in a new tab"];
  const signedOut = await renderHeader({ ...SIGNED_OUT, ...features }, "/blog");
  const more = moreOf(signedOut);
  expect(labelsOf(bottomOf(signedOut).replace(more, ""))).toEqual(tabs);
  expect(labelsOf(more)).toEqual(pages);
  expect(more).toContain(DISCORD_INVITE_URL);
  expect(signedOut.replace(more, "")).toContain('href="/login?returnPath=%2Fblog"');

  const signedIn = await renderHeader({
    ...SIGNED_OUT,
    ...features,
    loggedIn: true,
    accountLabel: "Lens Scout",
    accountUsername: "lens_scout",
    logoutIdempotencyKey: "logout-key",
  });
  const signedInMore = moreOf(signedIn);
  expect(labelsOf(bottomOf(signedIn).replace(signedInMore, ""))).toEqual(tabs);
  expect(labelsOf(signedInMore)).toEqual(pages);
  for (const word of ["Lens Scout", "/users/lens_scout", "/logout", "Sign"])
    expect(signedInMore).not.toContain(word);
  expect(tabsOf(/<nav class="primary-nav"[\s\S]*?<\/nav>/.exec(signedIn)![0])[0]).toBe(
    "Home",
  );
  // With the Dashboard and Crews switched off, / is the front page for
  // everyone and the bar keeps its other tabs.
  expect(
    labelsOf(bottomOf(await renderHeader({ ...SIGNED_OUT, loggedIn: true }))),
  ).toEqual(["Rankings", "Analytics", "Home", ...pages]);
});

it("keeps one theme switch, in the top bar, on every screen size", async () => {
  const header = await renderHeader(SIGNED_OUT);
  expect(header.match(/class="theme-toggle"/g)).toHaveLength(1);
  expect(bottomOf(header)).not.toContain("theme-toggle");
});

it("marks More while one of its pages is open, and no tab on a player's page", async () => {
  const toggleOf = (header: string) => /<summary [^>]*>/.exec(moreOf(header))![0];
  for (const path of ["/blog", "/account/watchlists"])
    expect(toggleOf(await renderHeader(SIGNED_OUT, path))).toContain(
      'aria-current="page"',
    );
  expect(toggleOf(await renderHeader(SIGNED_OUT, "/analytics/armies"))).not.toContain(
    "aria-current",
  );
  const signedIn = { ...SIGNED_OUT, loggedIn: true, accountUsername: "lens_scout" };
  // The account's pages live in the top bar's account menu, so they light
  // nothing in the bottom bar.
  for (const path of ["/account/settings", "/users/lens_scout", "/account"])
    expect(bottomOf(await renderHeader(signedIn, path))).not.toContain("aria-current");
  const watchlist = await renderHeader(signedIn, "/account/watchlists/g1");
  expect(toggleOf(watchlist)).toContain('aria-current="page"');
  expect(bottomOf(watchlist).match(/<a [^>]*aria-current="page"[^>]*>/g)).toEqual([
    expect.stringContaining('href="/account/watchlists"'),
  ]);

  const player = await renderHeader(
    { ...signedIn, dashboardEnabled: true, crewsEnabled: true },
    "/players/%232PP",
  );
  expect(player).not.toContain("aria-current");
  // Its Rankings tabs look as they do on a page that isn't Rankings.
  const rankingsTabs = (header: string) =>
    header.match(/<a [^>]*href="\/leaderboards\/tracked[^"]*"[^>]*>/g);
  const blog = await renderHeader(
    { ...signedIn, dashboardEnabled: true, crewsEnabled: true },
    "/blog",
  );
  expect(rankingsTabs(player)).toEqual(rankingsTabs(blog));
});

it("signed in puts your profile, tools, Settings and Sign out behind the account name", async () => {
  const nav = await renderHeader({
    ...SIGNED_OUT,
    loggedIn: true,
    accountLabel: "Lens Scout",
    accountUsername: "lens_scout",
    logoutIdempotencyKey: "logout-key",
  });

  expect(nav).toMatch(
    /<details class="account-menu"><summary class="nav-link nav-account">[\s\S]*Lens Scout[\s\S]*<\/summary>/,
  );
  const panel = /<ul class="account-menu-panel"[\s\S]*?<\/ul>/.exec(nav)![0];
  const links = [...panel.matchAll(/href="([^"]+)" data-discover="true">([^<]+)</g)];
  expect(links.map(([, href, label]) => [href, label])).toEqual([
    ["/users/lens_scout", "Your profile"],
    ["/account/discord-alerts", "Discord alerts"],
    ["/account/obs-overlay", "Stream overlay"],
    ["/account/settings", "Settings"],
  ]);
  expect(panel).toContain('action="/logout"');
  expect(panel).toContain('value="logout-key"');
  expect(panel).toContain(">Sign out</button>");
  expect(nav.replace(panel, "")).not.toContain("Sign out");
  expect(nav).not.toContain(">Sign in<");
});
