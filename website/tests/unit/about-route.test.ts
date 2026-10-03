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

const FAN_CONTENT_NOTICE =
  "This material is unofficial and is not endorsed by Supercell. For more information see Supercell's Fan Content Policy: www.supercell.com/fan-content-policy.";

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
  expect(html).toContain("<h2>What Clash Lens is</h2>");
  expect(html).toContain("<h2>Thank you</h2>");
  expect(html).toContain("<h2>Fan Content Policy</h2>");
  expect(text(html)).toContain("Legend League tracker");
  expect(html).toContain(
    '<a href="https://developer.clashofclans.com/">Clash of Clans API</a>',
  );
  expect(html).toContain('<a href="https://www.postgresql.org/">PostgreSQL</a>');
  expect(html).toContain(
    '<a href="https://www.cloudflare.com/products/tunnel/">Cloudflare Tunnel</a>',
  );
  expect(html).toContain('<a href="https://www.supercell.com/fan-content-policy">');
  // The owner's intro placeholder is a comment in the file, not page text.
  expect(html).not.toContain("PLACEHOLDER");
  for (const [, href] of html.matchAll(/href="([^"]*)"/g)) {
    expect(href).toMatch(/^https:\/\//);
  }
});

it("keeps the Fan Content Policy notice word for word", async () => {
  const html = await renderAbout();
  const notice = /<p>(This material is unofficial[\s\S]*?)<\/p>/.exec(html)?.[1];

  expect(text(notice ?? "")).toBe(FAN_CONTENT_NOTICE);
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
});
