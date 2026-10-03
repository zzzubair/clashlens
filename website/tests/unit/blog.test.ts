import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  publishedBlogPosts: vi.fn(),
}));

vi.mock("../../app/server/blog.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/blog.server")>();
  return { ...actual, publishedBlogPosts: mocks.publishedBlogPosts };
});

import { blogMeta } from "../../app/lib/blog";
import BlogPostRoute, {
  loader as postLoader,
  meta as postMeta,
} from "../../app/routes/blog.$slug";
import BlogIndex, { loader as indexLoader } from "../../app/routes/blog";
import { loader as feedLoader } from "../../app/routes/blog.rss";
import {
  BlogPostError,
  blogFeed,
  loadBlogPosts,
  parseBlogPost,
} from "../../app/server/blog.server";

// Test-only posts; none of these are published on the site.
const FIXTURES = import.meta.glob<string>("../fixtures/blog/*.md", {
  query: "?raw",
  import: "default",
  eager: true,
});
const fixturePosts = loadBlogPosts(FIXTURES);
const ORIGIN = "https://clashlens.example";

function fixture(slug: string) {
  return fixturePosts.find((post) => post.slug === slug)!;
}

async function render(path: string) {
  const handler = createStaticHandler([
    { path: "/blog", Component: BlogIndex, loader: indexLoader },
    {
      path: "/blog/:slug",
      Component: BlogPostRoute,
      loader: (args) => postLoader(args as Parameters<typeof postLoader>[0]),
    },
  ]);
  const context = await handler.query(new Request(`${ORIGIN}${path}`));
  if (context instanceof Response) throw new Error("unexpected response");
  const html = renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
  return { html, status: context.statusCode };
}

beforeEach(() => {
  mocks.publishedBlogPosts.mockReset();
  mocks.publishedBlogPosts.mockReturnValue(fixturePosts);
});

describe("blog posts", () => {
  it("lists posts newest first", () => {
    expect(fixturePosts.map((post) => [post.date, post.slug])).toEqual([
      ["2026-09-28", "meta-after-balance-changes"],
      ["2026-09-20", "how-matchmaking-works"],
      ["2026-09-01", "unsafe-html"],
    ]);
  });

  it("reads front matter, including quoted values and optional fields", () => {
    expect(fixture("meta-after-balance-changes")).toMatchObject({
      title: "The meta after the September balance changes",
      author: null,
      cover: null,
      coverAlt: "",
    });
    expect(fixture("how-matchmaking-works")).toMatchObject({
      title: "How Legend League matchmaking works",
      author: "Clash Lens",
      cover: "/images/legend-league.webp",
      coverAlt: "The Legend League badge",
    });
  });

  it("renders headings below the page title, lists, links, images, tables, quotes and code", () => {
    const html = fixture("how-matchmaking-works").html;
    expect(html).not.toContain("<h1");
    expect(html).toContain("<h2>Who you get matched against</h2>");
    expect(html).toContain("<h3>What the numbers show</h3>");
    expect(html).toContain("<strong>similar trophy count</strong>");
    expect(html).toContain('<a href="/leaderboards/tracked">live rankings</a>');
    expect(html).toMatch(/<ol>\s*<li>Most attacks/);
    expect(html).toMatch(/<ul>\s*<li>Defenses/);
    expect(html).toMatch(/<div class="blog-table" tabindex="0">\s*<table>\s*<thead>/);
    expect(html).toContain('<th style="text-align:right">Share of attacks</th>');
    expect(html).toContain("<blockquote>");
    expect(html).toContain(
      '<pre tabindex="0"><code class="language-text">gap = |attacker',
    );
    expect(html).toContain(
      '<img src="/images/legend-league.webp" alt="The Legend League badge">',
    );
  });

  it("strips raw HTML and refuses script links", () => {
    const html = fixture("unsafe-html").html;
    for (const unsafe of [
      "<script",
      '<img src="x"',
      "onerror",
      "onclick",
      "<iframe",
      "<b",
    ]) {
      expect(html).not.toContain(unsafe);
    }
    // Markdown refuses to turn script and HTML data addresses into links or images.
    expect(html).not.toMatch(/(href|src)="(javascript|data):/);
    expect(html).toContain("Inline  image and bold tag.");
  });

  it.each([
    ["Bad_Name.md", "---\ntitle: T\ndate: 2026-01-01\nsummary: S\n---\n", "file name"],
    ["no-front-matter.md", "# Hello\n", "missing front matter"],
    [
      "missing-title.md",
      "---\ndate: 2026-01-01\nsummary: S\n---\n",
      '"title" is required',
    ],
    ["bad-date.md", "---\ntitle: T\ndate: 2026-02-30\nsummary: S\n---\n", "real date"],
    [
      "typo.md",
      "---\ntitle: T\ndate: 2026-01-01\nsumary: S\n---\n",
      "unknown front matter",
    ],
    ["twice.md", "---\ntitle: T\ntitle: U\ndate: 2026-01-01\nsummary: S\n---\n", "twice"],
    [
      "cover.md",
      "---\ntitle: T\ndate: 2026-01-01\nsummary: S\ncover: //evil.example/x.png\n---\n",
      "cover",
    ],
  ])("rejects %s", (file, source, reason) => {
    expect(() => parseBlogPost(file, source)).toThrow(BlogPostError);
    expect(() => parseBlogPost(file, source)).toThrow(reason);
  });

  it("rejects two posts with the same slug", () => {
    const source = "---\ntitle: T\ndate: 2026-01-01\nsummary: S\n---\n";
    expect(() => loadBlogPosts({ "a/same.md": source, "b/same.md": source })).toThrow(
      "duplicate slug",
    );
  });

  it("parses every committed post", async () => {
    const actual = await vi.importActual<typeof import("../../app/server/blog.server")>(
      "../../app/server/blog.server",
    );
    expect(() => actual.publishedBlogPosts()).not.toThrow();
  });
});

describe("blog pages", () => {
  it("shows each post's title, date and summary, newest first", async () => {
    const { html, status } = await render("/blog");
    expect(status).toBe(200);
    const titles = [...html.matchAll(/<h2><a href="\/blog\/([a-z-]+)"/g)].map(
      (match) => match[1],
    );
    expect(titles).toEqual([
      "meta-after-balance-changes",
      "how-matchmaking-works",
      "unsafe-html",
    ]);
    expect(html).toContain('<time dateTime="2026-09-20">20 September 2026</time>');
    expect(html).toContain("What 40,000 recorded attacks say");
    expect(html).not.toContain("First post coming soon");
    expect(html).toContain('href="/blog/rss.xml"');
  });

  it("shows a friendly empty state when nothing is published", async () => {
    mocks.publishedBlogPosts.mockReturnValue([]);
    const { html, status } = await render("/blog");
    expect(status).toBe(200);
    expect(html).toContain("First post coming soon");
    expect(html).not.toContain('class="blog-list"');
  });

  it("shows a post with its date, author, cover and body", async () => {
    const { html, status } = await render("/blog/how-matchmaking-works");
    expect(status).toBe(200);
    expect(html).toContain(
      '<h1 id="blog-post-title">How Legend League matchmaking works</h1>',
    );
    expect(html).toContain("20 September 2026</time> · Clash Lens");
    expect(html).toContain(
      '<img class="blog-cover" src="/images/legend-league.webp" alt="The Legend League badge"/>',
    );
    expect(html).toContain("<h2>Who you get matched against</h2>");
  });

  it("returns 404 for a slug with no post", async () => {
    const { status } = await render("/blog/no-such-post");
    expect(status).toBe(404);
  });

  it("gives Discord a title, description and absolute cover image", async () => {
    const loaderData = await postLoader({
      params: { slug: "how-matchmaking-works" },
      request: new Request(`${ORIGIN}/blog/how-matchmaking-works`),
    } as Parameters<typeof postLoader>[0]);
    const tags = postMeta({ loaderData } as Parameters<typeof postMeta>[0]);
    expect(tags).toEqual(
      expect.arrayContaining([
        { title: "How Legend League matchmaking works · Clash Lens" },
        { name: "description", content: fixture("how-matchmaking-works").summary },
        { property: "og:type", content: "article" },
        { property: "og:title", content: "How Legend League matchmaking works" },
        { property: "og:url", content: `${ORIGIN}/blog/how-matchmaking-works` },
        { property: "og:image", content: `${ORIGIN}/images/legend-league.webp` },
        { name: "twitter:card", content: "summary_large_image" },
        { name: "twitter:image", content: `${ORIGIN}/images/legend-league.webp` },
      ]),
    );
  });

  it("uses a small preview card when a post has no cover", () => {
    const tags = blogMeta({
      title: "Blog",
      description: "D",
      url: `${ORIGIN}/blog`,
      origin: ORIGIN,
      type: "website",
    });
    expect(tags).toContainEqual({ name: "twitter:card", content: "summary" });
    expect(tags.some((tag) => "property" in tag && tag.property === "og:image")).toBe(
      false,
    );
  });
});

describe("blog feed", () => {
  it("lists every post newest first with absolute links", async () => {
    const response = (await feedLoader({
      request: new Request(`${ORIGIN}/blog/rss.xml`),
    } as Parameters<typeof feedLoader>[0])) as Response;
    expect(response.headers.get("Content-Type")).toBe(
      "application/rss+xml; charset=utf-8",
    );
    const xml = await response.text();
    expect(xml).toMatch(/^<\?xml version="1.0" encoding="UTF-8"\?>\n<rss version="2.0"/);
    expect([...xml.matchAll(/<link>([^<]+)<\/link>/g)].map((match) => match[1])).toEqual([
      `${ORIGIN}/blog`,
      `${ORIGIN}/blog/meta-after-balance-changes`,
      `${ORIGIN}/blog/how-matchmaking-works`,
      `${ORIGIN}/blog/unsafe-html`,
    ]);
    expect(xml).toContain("<pubDate>Sun, 20 Sep 2026 00:00:00 GMT</pubDate>");
    expect(xml).toContain("<dc:creator>Clash Lens</dc:creator>");
    expect(xml).toContain(`<atom:link href="${ORIGIN}/blog/rss.xml" rel="self"`);
  });

  it("escapes text and stays valid with no posts", () => {
    const [post] = loadBlogPosts({
      "x/odd.md":
        '---\ntitle: Gems & <Gold>\ndate: 2026-01-01\nsummary: "Quotes" & more\n---\n',
    });
    const xml = blogFeed([post], ORIGIN);
    expect(xml).toContain("<title>Gems &amp; &lt;Gold&gt;</title>");
    expect(xml).toContain("<description>&quot;Quotes&quot; &amp; more</description>");
    expect(blogFeed([], ORIGIN)).not.toContain("<item>");
  });
});
