import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { expect, it } from "vitest";

import { Markdown } from "../../app/lib/markdown";
import About, { loader } from "../../app/routes/about";

async function renderAbout() {
  const handler = createStaticHandler([{ path: "/about", Component: About, loader }]);
  const context = await handler.query(new Request("https://clashlens.example/about"));
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
}

function text(html: string) {
  return html
    .replace(/<[^>]+>/g, " ")
    .replaceAll("&#x27;", "'")
    .replace(/\s+/g, " ")
    .replace(/ ([.,])/g, "$1")
    .trim();
}

it("renders the about page from its Markdown file", async () => {
  const html = await renderAbout();

  expect(html).toContain("<h1>About Clash Lens</h1>");
  expect(html).toContain("<h2>Data for everyone</h2>");
  expect(html).toContain("<h2>Thanks</h2>");
  expect(text(html)).toContain(
    "Clash Lens puts Legend League data in everyone's hands, and makes it easy to understand and use.",
  );
  expect(html).toContain("<li>Compare yourself with your friends, side by side.</li>");
  expect(html).toContain('<a href="https://discord.gg/792KJQTtRf">Join the Discord</a>');
  for (const [, href] of html.matchAll(/href="([^"]*)"/g)) {
    expect(href).toMatch(/^https:\/\//);
  }
});

it("renders Markdown paragraphs, lists and https links only", () => {
  const html = renderToString(
    createElement(Markdown, {
      source: [
        "Intro line one",
        "continues here.",
        "",
        "- Plain item",
        "- [Site](https://clashlens.example/) and [bad](javascript:void) and [also bad](/about)",
        "",
        "<script>alert(1)</script>",
        "<!-- editor note -->",
      ].join("\n"),
    }),
  ).replaceAll("<!-- -->", "");

  expect(html).toContain("<p>Intro line one continues here.</p>");
  expect(html).toContain("<li>Plain item</li>");
  expect(html).toContain(
    '<a href="https://clashlens.example/">Site</a> and bad and also bad',
  );
  expect(html).not.toContain("javascript:");
  expect(html).not.toContain("<script>");
  expect(html).toContain("&lt;script&gt;");
  expect(html).not.toContain("editor note");
});
