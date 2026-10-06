import { createMemoryRouter, type MiddlewareFunction } from "react-router";
import { expect, it } from "vitest";

import { keepPageOnLostConnection, rememberShownPage } from "../../app/lib/keep-page";

const PAGE = "/players/%23LY2QQ9L9Q";

// A profile page whose next read fails the way a phone's does after waking.
async function openProfile(
  failure: () => unknown,
  middleware: MiddlewareFunction[] = [keepPageOnLostConnection as MiddlewareFunction],
) {
  let reads = 0;
  const router = createMemoryRouter(
    [
      {
        id: "root",
        path: "/",
        middleware,
        loader: () => ({ loggedIn: false }),
        children: [
          {
            id: "player",
            path: "players/:tag",
            loader: () => {
              reads += 1;
              if (reads > 1) throw failure();
              return { trophies: 5_014 };
            },
          },
          { id: "home", index: true, loader: () => ({ home: true }) },
        ],
      },
    ],
    { initialEntries: [PAGE] },
  );
  await new Promise<void>((resolve) => {
    if (router.state.initialized) resolve();
    else router.subscribe((state) => state.initialized && resolve());
  });
  rememberShownPage(
    PAGE,
    new Map(
      router.state.matches.map(({ route }) => [
        route.id,
        router.state.loaderData[route.id],
      ]),
    ),
  );
  return router;
}

it("showed the error page when a background re-read lost the connection", async () => {
  const router = await openProfile(() => new TypeError("Load failed"), []);
  await router.revalidate();
  expect(router.state.errors).toEqual({ root: new TypeError("Load failed") });
});

it.each([
  ["the phone is offline", () => new TypeError("Load failed")],
  // The browser's data read turns a proxy's error page into this shape.
  [
    "a proxy answers instead",
    () => ({ status: 502, statusText: "Bad Gateway", internal: false, data: "" }),
  ],
  ["the answer is cut off", () => new Error("Unable to decode turbo-stream response")],
])("keeps the shown profile when a re-read fails because %s", async (_, failure) => {
  const router = await openProfile(failure);
  await router.revalidate();
  expect(router.state.errors).toBeNull();
  expect(router.state.loaderData.player).toEqual({ trophies: 5_014 });
});

it("still shows a real page error from a re-read", async () => {
  const router = await openProfile(() => new Error("bad data"));
  await router.revalidate();
  expect(router.state.errors).toEqual({ root: new Error("bad data") });
});

it("does not stand in old data for a page that was never shown", async () => {
  const router = await openProfile(() => new TypeError("Load failed"));
  await router.navigate("/");
  rememberShownPage("/", new Map([["home", { home: true }]]));
  await router.navigate(PAGE);
  expect(router.state.errors).toEqual({ root: new TypeError("Load failed") });
});
