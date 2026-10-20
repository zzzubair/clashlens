import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  data,
  StaticRouterProvider,
  useRouteError,
} from "react-router";
import { expect, it } from "vitest";

import { ErrorBoundary } from "../../app/root";

async function renderFailure(path: string, status: number, payload: unknown = null) {
  const handler = createStaticHandler([
    {
      id: "root",
      path: "/",
      ErrorBoundary: () =>
        createElement(ErrorBoundary, { error: useRouteError() } as never),
      children: [
        {
          path: "*",
          loader: () => {
            throw data(payload, { status });
          },
          Component: () => createElement("p", null, "Your saved players"),
        },
      ],
    },
  ]);
  const context = await handler.query(new Request(`https://clashlens.example${path}`));
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  );
}

it("offers to reload the same page when a signed-in page fails to load", async () => {
  const html = await renderFailure("/account/saved-players?tab=list", 503);
  expect(html).toContain("The page could not be loaded");
  expect(html).toContain('href="/account/saved-players?tab=list">Try again</a>');
  expect(html).toContain('href="/">Go to Clash Lens</a>');
  expect(html).not.toContain("Your saved players");
});

it("does not offer a retry for a page that does not exist", async () => {
  const html = await renderFailure("/missing", 404);
  expect(html).toContain("Page not found");
  expect(html).not.toContain("Try again");
  expect(html).toContain('href="/">Go to Clash Lens</a>');
});

it("explains in place why a signed-out page needs an account", async () => {
  const html = await renderFailure("/crews", 401, {
    needsAccount: true,
    returnPath: "/crews",
  });
  expect(html).toContain("<h1>Crews</h1>");
  expect(html).toContain("You need a Clash Lens account to make or join one.");
  expect(html).toContain('href="/signup?returnPath=%2Fcrews"');
  expect(html).toContain('href="/login?returnPath=%2Fcrews"');
  expect(html).not.toContain("The page could not be loaded");

  const account = await renderFailure("/account/profile", 401, {
    needsAccount: true,
    returnPath: "/account/profile",
  });
  expect(account).toContain("<h1>Your account</h1>");
});
