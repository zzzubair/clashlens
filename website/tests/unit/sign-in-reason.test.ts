import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { expect, it } from "vitest";

import { SignInReason } from "../../app/components/SignInReason";

const render = (returnPath: string) =>
  renderToString(createElement(SignInReason, { returnPath })).replaceAll("<!-- -->", "");

it.each([
  [
    "/account/watchlists",
    "<strong>Watchlists.</strong> Private lists of players, side by side. Sign in to keep them.",
  ],
  [
    "/account/watchlists/add/%232PP",
    "<strong>Watchlists.</strong> Private lists of players, side by side.",
  ],
  [
    "/crews?tab=boards",
    "<strong>Crews.</strong> Invite-only groups with shared Legend boards. Sign in to make or join one.",
  ],
])("says in one line why %s asks a visitor to sign in", (returnPath, text) => {
  expect(render(returnPath)).toContain(text);
});

it.each(["/account", "/", "/players/%232PP", "/crews/join/abc123"])(
  "says nothing extra when sign-in came from %s",
  (returnPath) => {
    expect(render(returnPath)).toBe("");
  },
);
