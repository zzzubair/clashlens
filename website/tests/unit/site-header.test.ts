import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { expect, it } from "vitest";

import { DISCORD_INVITE_URL } from "../../app/lib/discord";
import App, { type RootLoaderData } from "../../app/root";

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
  // The sign-in page itself is where that link goes, so it has none.
  expect(await renderHeader(SIGNED_OUT, "/login?returnPath=%2Fcrews")).not.toContain(
    ">Sign in</a>",
  );
});

it("on phones shows everyone the same four tabs, and the rest and the account in More", async () => {
  const features = { dashboardEnabled: true, crewsEnabled: true };
  const tabs = ["Home", "Rankings", "Watchlists", "Crews"];
  const pages = ["More", "Analytics", "Blog", "Join Discord, opens in a new tab"];
  const signedOut = await renderHeader({ ...SIGNED_OUT, ...features }, "/blog");
  const more = moreOf(signedOut);
  expect(labelsOf(bottomOf(signedOut).replace(more, ""))).toEqual(tabs);
  expect(labelsOf(more)).toEqual([...pages, "Sign in"]);
  expect(more).toContain(DISCORD_INVITE_URL);
  expect(more).toContain('href="/login?returnPath=%2Fblog"');
  expect(more).not.toContain("Sign out");

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
  expect(labelsOf(signedInMore)).toEqual([...pages, "Your profile", "Settings"]);
  expect(signedInMore).toContain("Signed in as <bdi>Lens Scout</bdi>");
  expect(signedInMore).toContain('href="/users/lens_scout"');
  expect(signedInMore).toMatch(
    /<form [^>]*action="\/logout"[^>]*>[\s\S]*value="logout-key"[\s\S]*Sign out<\/button>/,
  );
  expect(signedInMore).not.toContain(">Sign in<");
  // The dashboard is Home in both bars.
  expect(tabsOf(/<nav class="primary-nav"[\s\S]*?<\/nav>/.exec(signedIn)![0])[0]).toBe(
    "Home",
  );
  // With the Dashboard and Crews switched off, / is the front page for
  // everyone and the bar keeps its other tabs.
  expect(
    labelsOf(bottomOf(await renderHeader({ ...SIGNED_OUT, loggedIn: true }))),
  ).toEqual(["Home", "Rankings", "Watchlists", ...pages, "Your profile", "Settings"]);
});

it("keeps one theme switch, in the top bar, on every screen size", async () => {
  const header = await renderHeader(SIGNED_OUT);
  expect(header.match(/class="theme-toggle"/g)).toHaveLength(1);
  expect(bottomOf(header)).not.toContain("theme-toggle");
});

it("marks More while one of its pages is open, and no tab on a player's page", async () => {
  const toggleOf = (header: string) => /<summary [^>]*>/.exec(moreOf(header))![0];
  for (const path of ["/blog", "/analytics/armies"])
    expect(toggleOf(await renderHeader(SIGNED_OUT, path))).toContain(
      'aria-current="page"',
    );
  expect(toggleOf(await renderHeader(SIGNED_OUT, "/account/watchlists"))).not.toContain(
    "aria-current",
  );
  const signedIn = { ...SIGNED_OUT, loggedIn: true, accountUsername: "lens_scout" };
  for (const path of ["/account/settings", "/users/lens_scout"])
    expect(toggleOf(await renderHeader(signedIn, path))).toContain('aria-current="page"');
  // While a busy moment leaves the name unread, the profile is /account, and
  // a watchlist lights only Watchlists.
  const unread = { ...signedIn, accountUsername: null };
  const watchlist = await renderHeader(unread, "/account/watchlists/g1");
  expect(toggleOf(watchlist)).not.toContain("aria-current");
  expect(bottomOf(watchlist).match(/<a [^>]*aria-current="page"[^>]*>/g)).toEqual([
    expect.stringContaining('href="/account/watchlists"'),
  ]);
  expect(toggleOf(await renderHeader(unread, "/account"))).toContain(
    'aria-current="page"',
  );

  // A player's page lights no tab, in either bar.
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
  // Sign out lives in the account menu and, on phones, in More.
  expect(nav.replace(panel, "").replace(moreOf(nav), "")).not.toContain("Sign out");
  expect(nav).not.toContain(">Sign in<");
});
