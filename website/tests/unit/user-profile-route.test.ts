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
    verifiedPlayers: [
      {
        tag: "#2PP",
        name: "Nova",
        clan: "Night Owls",
        state: "tracking",
        reason: null,
        trophies: 5300,
        seasonResetPending: false,
        rank: 1234,
        today: { net: 28, attacks: 5, defenses: 3 },
      },
      {
        tag: "#8PY",
        name: "Second",
        clan: null,
        state: "tracking",
        reason: null,
        trophies: 5100,
        seasonResetPending: false,
        rank: null,
        today: { net: null, attacks: 2, defenses: 0 },
      },
      {
        tag: "#2PQ",
        name: "Quiet",
        clan: null,
        state: "tracking",
        reason: "no_legend_battles",
        trophies: null,
        seasonResetPending: false,
        rank: null,
        today: null,
      },
      {
        tag: "#9PY",
        name: "Demoted",
        clan: null,
        state: "not_in_legend",
        reason: null,
        trophies: 4900,
        seasonResetPending: false,
        rank: null,
        today: { net: -40, attacks: 0, defenses: 1 },
      },
    ],
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

it("shows each linked account as one card linking to its player page", async () => {
  const html = await renderProfile("");
  const cards = html.match(/<a class="linked-player-card"[\s\S]*?<\/a>/g) ?? [];
  expect(cards).toHaveLength(4);
  const [tracked, unknownNet, season0, demoted] = cards;
  expect(tracked).toContain('href="/players/%232PP"');
  expect(tracked).toContain(
    'aria-labelledby="linked-player-2PP-name linked-player-2PP-tag"',
  );
  for (const text of ["Nova", "#2PP", "Night Owls", "5,300", "#1,234", "+28", " so far"])
    expect(tracked).toContain(text);
  expect(tracked).toContain("5/8 attacks · 3/8 defenses");
  // Not every battle so far is recorded, so the net is not claimed.
  expect(unknownNet).toContain("Unranked");
  expect(unknownNet).toContain("Unknown");
  expect(unknownNet).not.toContain("so far");
  expect(unknownNet).toContain("2/8 attacks · 0/8 defenses");
  expect(tracked).not.toContain("linked-player-note");
  // The same words as the player page, with nothing valid to count.
  expect(season0).toContain(
    "Quiet is in Legend League but hasn&#x27;t played a Legend League battle this Season.",
  );
  for (const text of ["Trophies", "Unknown", "Unranked", "Not available yet"])
    expect(season0).toContain(text);
  // A player who left Legend I keeps their saved trophies and today's battles.
  expect(demoted).toContain("This player is not in Legend I.");
  for (const text of ["4,900", "Unranked", "-40", "0/8 attacks · 1/8 defenses"])
    expect(demoted).toContain(text);
  expect(demoted).toContain(
    'aria-describedby="linked-player-9PY-details linked-player-9PY-note"',
  );
});
