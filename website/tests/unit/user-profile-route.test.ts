import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { expect, it } from "vitest";

import UserRoute from "../../app/routes/users.$username";

const data = {
  user: {
    username: "nova88",
    displayName: "Nova",
    verifiedPlayers: [{ tag: "#2PP", name: "Nova" }],
  },
  notFound: false,
  error: null,
};

async function renderProfile(search: string) {
  const handler = createStaticHandler([
    { path: "/users/:username", Component: UserRoute, loader: () => data },
  ]);
  const context = await handler.query(
    new Request(`https://clashlens.example/users/nova88${search}`),
  );
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
}

it("confirms a just-linked player without knowing who is signed in", async () => {
  const html = await renderProfile("?linked=%232PP");
  expect(html).toContain("Linked #2PP");
  expect(html).not.toContain("Edit profile");
  expect(await renderProfile("?linked=%232PY")).not.toContain("Linked #2PY");
});
