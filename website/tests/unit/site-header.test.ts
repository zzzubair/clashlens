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

async function renderHeader(data: RootLoaderData) {
  const handler = createStaticHandler([
    { id: "root", path: "/", loader: () => data, Component: App },
  ]);
  const context = await handler.query(new Request("https://clashlens.example/"));
  if (context instanceof Response) throw new Error("unexpected response");
  const html = renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  );
  return /<nav class="site-nav"[\s\S]*?<\/nav>/.exec(html)![0];
}

it("signed out keeps the sign-in control and offers Join Discord in a new tab", async () => {
  const nav = await renderHeader(SIGNED_OUT);

  expect(nav).toContain('href="/login" data-discover="true">Account</a>');
  expect(nav).toContain(
    `href="${DISCORD_INVITE_URL}" target="_blank" rel="noopener noreferrer">`,
  );
  expect(nav).toContain("Join </span>Discord");
  expect(nav).toContain("opens in a new tab");
  expect(nav).not.toContain("account-menu");
});

it("signed in puts Account and Log out behind the closed account name", async () => {
  const nav = await renderHeader({
    ...SIGNED_OUT,
    loggedIn: true,
    accountLabel: "Lens Scout",
    accountUsername: "lens_scout",
    logoutIdempotencyKey: "logout-key",
  });

  expect(nav).toMatch(
    /<button type="button" class="nav-link nav-account" aria-expanded="false" aria-controls="account-menu-panel">[\s\S]*Lens Scout/,
  );
  const panel = /<ul id="account-menu-panel"[\s\S]*?<\/ul>/.exec(nav)![0];
  expect(panel).toContain("hidden");
  expect(panel).toContain('href="/users/lens_scout" data-discover="true">Account</a>');
  expect(panel).toContain('action="/logout"');
  expect(panel).toContain('value="logout-key"');
  expect(panel).toContain(">Log out</button>");
  expect(nav.replace(panel, "")).not.toContain("Log out");
  expect(nav.indexOf("account-menu-panel")).toBeLessThan(nav.indexOf(DISCORD_INVITE_URL));
});
