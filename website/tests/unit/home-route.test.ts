import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { expect, it } from "vitest";

import Home from "../../app/routes/home";

it("keeps the exact player tag link when another player's name matches the tag", async () => {
  const handler = createStaticHandler([
    {
      path: "/",
      Component: Home,
      loader: () => ({
        leaderboard: null,
        query: "#2PP",
        error: null,
        search: {
          exactTag: "#2PP",
          users: [],
          results: [{ tag: "#2PY", name: "#2PP", clan: "Test clan", trophies: 5000 }],
        },
      }),
    },
  ]);
  const context = await handler.query(new Request("https://clashlens.example/?q=%232PP"));
  if (context instanceof Response) throw new Error("unexpected response");
  const html = renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  );
  expect(html).toContain('href="/players/%232PP"');
  expect(html).toContain("Open player profile");
  expect(html).not.toContain('href="/players/%232PY"');
});
