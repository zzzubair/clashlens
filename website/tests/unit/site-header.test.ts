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

it("names the tabs most useful first, with no Home tab", async () => {
  const header = await renderHeader({
    ...SIGNED_OUT,
    dashboardEnabled: true,
    crewsEnabled: true,
  });
  const wide = /<nav class="primary-nav"[\s\S]*?<\/nav>/.exec(header)![0];
  const tabs = ["Dashboard", "Rankings", "Analytics", "Crews", "Watchlists", "Blog"];
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
  expect(header).not.toContain("Log out");
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

it("on phones puts the four most useful tabs and More in the bottom bar", async () => {
  const features = { dashboardEnabled: true, crewsEnabled: true };
  const signedOut = await renderHeader({ ...SIGNED_OUT, ...features });
  const more = moreOf(signedOut);
  const bar = labelsOf(bottomOf(signedOut).replace(more, ""));
  expect(bar).toEqual(["Home", "Rankings", "Analytics", "Crews"]);
  expect(labelsOf(more)).toEqual([
    "More",
    "Watchlists",
    "Blog",
    "Join Discord, opens in a new tab",
  ]);
  expect(more).toContain(DISCORD_INVITE_URL);

  const signedIn = await renderHeader({
    ...SIGNED_OUT,
    ...features,
    loggedIn: true,
    accountLabel: "Lens Scout",
  });
  expect(labelsOf(bottomOf(signedIn))[0]).toBe("Dashboard");
  // With the Dashboard switched off, / is the front page for everyone.
  expect(
    labelsOf(bottomOf(await renderHeader({ ...SIGNED_OUT, loggedIn: true }))),
  ).toEqual([
    "Home",
    "Rankings",
    "Analytics",
    "More",
    "Watchlists",
    "Blog",
    "Join Discord, opens in a new tab",
  ]);
});

it("keeps one theme switch, in the top bar, on every screen size", async () => {
  const header = await renderHeader(SIGNED_OUT);
  expect(header.match(/class="theme-toggle"/g)).toHaveLength(1);
  expect(bottomOf(header)).not.toContain("theme-toggle");
});

it("marks More while one of its pages is open", async () => {
  const toggleOf = (header: string) => /<summary [^>]*>/.exec(moreOf(header))![0];
  expect(toggleOf(await renderHeader(SIGNED_OUT, "/blog"))).toContain(
    'aria-current="page"',
  );
  expect(toggleOf(await renderHeader(SIGNED_OUT, "/analytics/armies"))).not.toContain(
    "aria-current",
  );
});

it("signed in puts your profile, tools, Settings and Log out behind the account name", async () => {
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
  expect(panel).toContain(">Log out</button>");
  expect(nav.replace(panel, "")).not.toContain("Log out");
  expect(nav).not.toContain(">Sign in<");
});
