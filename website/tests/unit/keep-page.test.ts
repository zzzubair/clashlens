import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createMemoryRouter,
  data,
  UNSAFE_FrameworkContext,
  type MiddlewareFunction,
} from "react-router";
import { expect, it } from "vitest";

import {
  LOGGED_OUT,
  keepPageOnLostConnection,
  rememberShownPage,
  usePreloadInlineAnswerCode,
} from "../../app/lib/keep-page";

const PAGE = "/players/%23LY2QQ9L9Q";
const GROUPS = "/account/groups";
const UNAVAILABLE = {
  error: {
    code: "unavailable",
    message: "Saved data is still available, but the live service is unavailable.",
  },
};

// A page whose next read fails the way a phone's does after waking.
async function openPage(
  failure: () => unknown,
  path = PAGE,
  middleware: MiddlewareFunction[] = [keepPageOnLostConnection as MiddlewareFunction],
) {
  const read = (data: unknown) => {
    let reads = 0;
    return () => {
      reads += 1;
      if (reads > 1) throw failure();
      return data;
    };
  };
  const router = createMemoryRouter(
    [
      {
        id: "root",
        path: "/",
        middleware,
        loader: read({ loggedIn: true, accountLabel: "A" }),
        children: [
          { id: "player", path: "players/:tag", loader: read({ trophies: 5_014 }) },
          {
            id: "groups",
            path: "account/groups",
            loader: read({ groups: ["A's group"] }),
          },
          {
            id: "refresh",
            path: "resources/players/:tag/refresh",
            // The website's data read hands a failed submission back wrapped.
            action: () => {
              throw data(failure());
            },
          },
          {
            id: "search",
            path: "resources/players/search",
            loader: () => {
              throw failure();
            },
          },
          { id: "home", index: true, loader: () => ({ home: true }) },
        ],
      },
    ],
    { initialEntries: [path] },
  );
  await new Promise<void>((resolve) => {
    if (router.state.initialized) resolve();
    else router.subscribe((state) => state.initialized && resolve());
  });
  rememberShownPage(path);
  return router;
}

// What a page's fetcher would show once its request settles.
async function fetched(
  router: ReturnType<typeof createMemoryRouter>,
  key: string,
  start: () => Promise<void>,
) {
  let data: unknown;
  const stop = router.subscribe((state) => {
    const fetcher = state.fetchers.get(key);
    if (fetcher?.state === "idle") data = fetcher.data;
  });
  await start();
  stop();
  return data;
}

it("showed the error page when a background re-read lost the connection", async () => {
  const router = await openPage(() => new TypeError("Load failed"), PAGE, []);
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
  const router = await openPage(failure);
  await router.revalidate();
  expect(router.state.errors).toBeNull();
  expect(router.state.loaderData.player).toEqual({ trophies: 5_014 });
  expect(router.state.loaderData.root).toEqual(LOGGED_OUT);
});

it("still shows a real page error from a re-read", async () => {
  const router = await openPage(() => new Error("bad data"));
  await router.revalidate();
  expect(router.state.errors).toEqual({ root: new Error("bad data") });
});

it("does not keep old data for a page that was never shown", async () => {
  const router = await openPage(() => new TypeError("Load failed"));
  await router.navigate("/");
  rememberShownPage("/");
  await router.navigate(PAGE);
  expect(router.state.errors).toEqual({ root: new TypeError("Load failed") });
});

it.each([
  [
    "a sign-in check refuses",
    () => ({ status: 503, statusText: "", internal: false, data: "" }),
  ],
  ["the phone is offline", () => new TypeError("Load failed")],
])(
  "never keeps an account page when its re-read fails because %s",
  async (_, failure) => {
    const router = await openPage(failure, GROUPS);
    await router.revalidate();
    expect(router.state.errors).toEqual({ root: failure() });
    expect(router.state.loaderData.groups).toBeUndefined();
  },
);

it("shows Refresh as unavailable when its request loses the connection", async () => {
  const router = await openPage(() => new TypeError("Load failed"));
  const formData = new FormData();
  formData.set("idempotencyKey", "key");
  const answer = await fetched(router, "refresh", () =>
    router.fetch("refresh", "player", "/resources/players/%23LY2QQ9L9Q/refresh", {
      formMethod: "post",
      formData,
    }),
  );
  expect(answer).toEqual(UNAVAILABLE);
  expect(router.state.errors).toBeNull();
  expect(router.state.loaderData.player).toEqual({ trophies: 5_014 });
  expect(router.state.loaderData.root).toEqual(LOGGED_OUT);
});

it("shows search as unavailable when its request loses the connection", async () => {
  const router = await openPage(() => new TypeError("Load failed"));
  const answer = await fetched(router, "search", () =>
    router.fetch("search", "player", "/resources/players/search?q=+Zub+"),
  );
  expect(answer).toEqual({
    query: "Zub",
    search: null,
    error: UNAVAILABLE,
  });
  expect(router.state.errors).toBeNull();
});

// Without this code already downloaded, a first search or Refresh made offline
// makes React Router reload the page into the browser's offline screen.
it("has the page download the code for search and Refresh up front", () => {
  const route = (module: string, imports: string[] = []) => ({ module, imports });
  const manifest = {
    routes: {
      "routes/player": route("/assets/player.js"),
      "routes/player-search": route("/assets/player-search.js", ["/assets/shared.js"]),
      "routes/refresh": route("/assets/refresh.js"),
    },
  };
  function Page() {
    usePreloadInlineAnswerCode();
    return createElement("html", null, createElement("head"), createElement("body"));
  }
  const html = renderToString(
    createElement(
      UNSAFE_FrameworkContext.Provider,
      { value: { manifest } as never },
      createElement(Page),
    ),
  );
  const preloaded = [...html.matchAll(/<link rel="modulepreload" href="([^"]+)"/g)].map(
    (match) => match[1],
  );
  expect(preloaded.sort()).toEqual([
    "/assets/player-search.js",
    "/assets/refresh.js",
    "/assets/shared.js",
  ]);
});

it("shows the error again, not an empty page, on Back after a failed profile", async () => {
  const router = await openPage(() => new TypeError("Load failed"));
  await router.navigate("/players/%232PP");
  expect(router.state.errors).toEqual({ root: new TypeError("Load failed") });
  await router.navigate(-1);
  expect(router.state.location.pathname).toBe(PAGE);
  expect(router.state.errors).toEqual({ root: new TypeError("Load failed") });
});
