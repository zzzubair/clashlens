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

it("names the tabs most useful first, with no Home tab", async () => {
  const header = await renderHeader({
    ...SIGNED_OUT,
    dashboardEnabled: true,
    crewsEnabled: true,
  });
  const wide = /<nav class="primary-nav"[\s\S]*?<\/nav>/.exec(header)![0];
  const menu = /<nav class="site-menu-tabs"[\s\S]*?<\/nav>/.exec(header)![0];
  const tabs = ["Dashboard", "Rankings", "Analytics", "Crews", "Watchlists", "Blog"];
  expect(tabsOf(wide)).toEqual(tabs);
  expect(tabsOf(menu)).toEqual(tabs);
  expect(wide).toContain('href="/account/watchlists"');
  expect(header).toContain('aria-label="Clash Lens"');
  expect(header).not.toContain(">Home<");

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
  expect(header).toContain("Join </span>Discord");
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
});

it("folds the tabs, theme and Discord into one Menu", async () => {
  const header = await renderHeader(SIGNED_OUT);
  const menu = /<details class="site-menu"[\s\S]*?<\/details>/.exec(header)![0];
  expect(menu).toContain("<span>Menu</span>");
  expect(menu).toContain("theme-toggle");
  expect(menu).toContain(DISCORD_INVITE_URL);
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
