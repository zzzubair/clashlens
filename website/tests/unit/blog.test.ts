import { mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  readLoginIdentity: vi.fn(),
}));

vi.mock("node:fs/promises", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:fs/promises")>();
  return { ...actual, readFile: vi.fn(actual.readFile) };
});
vi.mock("../../app/server/actions.server", () => ({
  readLoginIdentity: mocks.readLoginIdentity,
}));
vi.mock("../../app/server/config.server", () => ({
  getWebsiteConfig: () => ({ publicOrigin: new URL("https://clashlens.example") }),
}));

import { absoluteBlogUrl, blogMeta } from "../../app/lib/blog";
import BlogPostRoute, {
  loader as postLoader,
  meta as postMeta,
} from "../../app/routes/blog.$slug";
import BlogIndex, { loader as indexLoader } from "../../app/routes/blog";
import { loader as mediaLoader } from "../../app/routes/blog.media";
import { loader as feedLoader } from "../../app/routes/blog.rss";
import {
  BLOG_CACHE_MS,
  BlogPostError,
  blogFeed,
  blogFolder,
  loadBlogPosts,
  parseBlogPost,
  readBlogFolder,
  renderBlogMarkdown,
  visibleBlogPosts,
} from "../../app/server/blog.server";

// A test-only blog folder laid out like the private blog repo: posts/ and media/.
const FIXTURE_DIR = resolve(import.meta.dirname, "../fixtures/blog");
const fixturePosts = (await readBlogFolder(FIXTURE_DIR)).posts;
const ORIGIN = "https://clashlens.example";
const OWNER = { provider: "google", providerSubject: "owner-subject" };

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

function signIn(identity: typeof OWNER | null) {
  mocks.readLoginIdentity.mockResolvedValue(identity);
}

beforeEach(() => {
  vi.stubEnv("CLASHLENS_BLOG_DIR", FIXTURE_DIR);
  vi.stubEnv("CLASHLENS_BLOG_OWNER", "discord:123, google:owner-subject");
  signIn(null);
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("blog posts", () => {
  it("reads every post in posts/, drafts included, newest first", () => {
    expect(fixturePosts.map((post) => [post.date, post.slug])).toEqual([
      ["2026-09-30", "draft-preview"],
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
      draft: false,
    });
    expect(fixture("draft-preview")).toMatchObject({
      title: "Draft preview",
      draft: true,
    });
  });

  it("skips the template, other files, and the media folder's hidden or odd names", async () => {
    const folder = await readBlogFolder(FIXTURE_DIR);
    expect(folder.posts.map((post) => post.slug)).not.toContain("_template");
    expect([...folder.media].sort()).toEqual([
      "badge.png",
      "gap-chart-dark.png",
      "gap-chart.csv",
      "gap-chart.png",
    ]);
    const empty = { posts: [], media: new Set(), publicMedia: new Set() };
    expect(await readBlogFolder(null)).toEqual(empty);
    expect(await readBlogFolder(join(FIXTURE_DIR, "missing"))).toEqual(empty);
  });

  it("leaves out a post a sync removes between listing and reading", async () => {
    const directory = await mkdtemp(join(tmpdir(), "blog-sync-"));
    try {
      await mkdir(join(directory, "posts"));
      for (const slug of ["kept", "gone"]) {
        await writeFile(
          join(directory, "posts", `${slug}.md`),
          "---\ntitle: T\ndate: 2026-01-01\nsummary: S\n---\n",
        );
      }
      const read = vi.mocked(readFile);
      const realRead = read.getMockImplementation()!;
      read.mockImplementationOnce(async (...args) => {
        await rm(join(directory, "posts", "gone.md"));
        return realRead(...args);
      });
      const folder = await readBlogFolder(directory);
      expect(folder.posts.map((post) => post.slug)).toEqual(["kept"]);
    } finally {
      await rm(directory, { recursive: true });
    }
  });

  it("leaves out a broken post and logs why", () => {
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const posts = loadBlogPosts({
      "good.md": "---\ntitle: T\ndate: 2026-01-01\nsummary: S\n---\n",
      "bad.md": "---\ntitle: T\n---\n",
    });
    expect(posts.map((post) => post.slug)).toEqual(["good"]);
    expect(log).toHaveBeenCalledWith('blog post bad.md: "date" is required');
    log.mockRestore();
  });

  it("allows comment lines and trailing comments in front matter", () => {
    const post = parseBlogPost(
      "notes.md",
      [
        "---",
        '# author: "Your name"    # optional',
        'title: "Gems # and gold"   # quoted, so the # stays',
        "coverAlt: 'Badge' # rename to 'Shield'",
        'author: "T" # rename to "U"',
        "date: 2026-10-04          # YYYY-MM-DD",
        "summary: Plain text # comment",
        "cover: badge.png",
        "draft: false",
        "---",
      ].join("\n"),
    );
    expect(post).toMatchObject({
      title: "Gems # and gold",
      date: "2026-10-04",
      summary: "Plain text",
      author: "T",
      cover: "/blog/media/badge.png",
      coverAlt: "Badge",
      draft: false,
    });
  });

  it("rereads the folder once the cache expires", async () => {
    const directory = await mkdtemp(join(tmpdir(), "blog-cache-"));
    try {
      await mkdir(join(directory, "posts"));
      const write = (slug: string) =>
        writeFile(
          join(directory, "posts", `${slug}.md`),
          "---\ntitle: T\ndate: 2026-01-01\nsummary: S\n---\n",
        );
      await write("first");
      vi.stubEnv("CLASHLENS_BLOG_DIR", directory);
      const now = Date.now();
      expect((await blogFolder(now)).posts.map((post) => post.slug)).toEqual(["first"]);
      await write("second");
      expect((await blogFolder(now + BLOG_CACHE_MS - 1)).posts).toHaveLength(1);
      expect((await blogFolder(now + BLOG_CACHE_MS)).posts).toHaveLength(2);
    } finally {
      await rm(directory, { recursive: true });
    }
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

  it("pairs a chart with its -dark sibling and serves media/ links from /blog/media", () => {
    const html = fixture("how-matchmaking-works").html;
    expect(html).toContain(
      '<img src="/blog/media/gap-chart.png" alt="Trophy gap by band" class="blog-img-light" loading="lazy">' +
        '<img src="/blog/media/gap-chart-dark.png" alt="Trophy gap by band" class="blog-img-dark" loading="lazy">',
    );
    expect(html).toContain('<img src="/blog/media/badge.png" alt="A plain badge">');
    expect(html).toContain('<a href="/blog/media/gap-chart.csv">Data</a>');
    expect(html.match(/<img/g)).toHaveLength(4);
  });

  it("pairs only files found in media/", () => {
    const media = new Set(["x.png", "x-dark.png"]);
    expect(renderBlogMarkdown("![A](../media/x-dark.png)", media)).toBe(
      '<p><img src="/blog/media/x-dark.png" alt="A"></p>\n',
    );
    expect(renderBlogMarkdown("![A](media/x.png)", media)).toBe(
      '<p><img src="media/x.png" alt="A"></p>\n',
    );
    expect(renderBlogMarkdown("![A](../media/x.png)")).toBe(
      '<p><img src="/blog/media/x.png" alt="A"></p>\n',
    );
    expect(renderBlogMarkdown("![A](/images/x.png)", media)).toBe(
      '<p><img src="/images/x.png" alt="A"></p>\n',
    );
    expect(renderBlogMarkdown("![A](../media/../posts/x.png)", media)).toBe(
      '<p><img src="../media/../posts/x.png" alt="A"></p>\n',
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
      "draft.md",
      "---\ntitle: T\ndate: 2026-01-01\nsummary: S\ndraft: yes\n---\n",
      "draft must be true or false",
    ],
  ])("rejects %s", (file, source, reason) => {
    expect(() => parseBlogPost(file, source)).toThrow(BlogPostError);
    expect(() => parseBlogPost(file, source)).toThrow(reason);
  });

  it.each([
    "//evil.example/x.png",
    "/\\evil.example/x.png",
    "/\\[broken",
    "//site.invalid/cover.png",
    "/\\site.invalid/cover.png",
    "https://images.example:bad/cover.png",
    "http://images.example/cover.png",
    "images/cover.png",
  ])("rejects the cover address %s", (cover) => {
    const source = `---\ntitle: T\ndate: 2026-01-01\nsummary: S\ncover: ${cover}\n---\n`;
    expect(() => parseBlogPost("cover.md", source)).toThrow(BlogPostError);
    expect(() => parseBlogPost("cover.md", source)).toThrow("cover must be");
  });

  it.each([
    ["/images/blog/cover.png", `${ORIGIN}/images/blog/cover.png`],
    ["https://images.example/cover.png", "https://images.example/cover.png"],
    ["cover.png", `${ORIGIN}/blog/media/cover.png`],
  ])("accepts the cover address %s", (cover, absolute) => {
    const post = parseBlogPost(
      "cover.md",
      `---\ntitle: T\ndate: 2026-01-01\nsummary: S\ncover: ${cover}\n---\n`,
    );
    expect(absoluteBlogUrl(post.cover!, ORIGIN)).toBe(absolute);
    const tags = blogMeta({
      title: post.title,
      description: post.summary,
      url: `${ORIGIN}/blog/cover`,
      origin: ORIGIN,
      type: "article",
      image: post.cover,
    });
    expect(tags).toContainEqual({ property: "og:image", content: absolute });
  });

  it("keeps punctuation as written", () => {
    expect(renderBlogMarkdown(`"Quotes" -- it's (c) 2026...`)).toBe(
      "<p>&quot;Quotes&quot; -- it's (c) 2026...</p>\n",
    );
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
    expect(html).not.toContain("Draft");
    expect(html).toContain('<time dateTime="2026-09-20">20 September 2026</time>');
    expect(html).toContain("What 40,000 recorded attacks say");
    expect(html).not.toContain("First post coming soon");
    expect(html).toContain('href="/blog/rss.xml"');
  });

  it("shows a friendly empty state when nothing is published", async () => {
    vi.stubEnv("CLASHLENS_BLOG_DIR", "");
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

describe("blog drafts", () => {
  const slugs = async () =>
    (await visibleBlogPosts(new Request(`${ORIGIN}/blog`))).map((post) => post.slug);

  it("hides drafts from visitors, other accounts and when no owner is set", async () => {
    expect(await slugs()).not.toContain("draft-preview");
    signIn({ provider: "google", providerSubject: "someone-else" });
    expect(await slugs()).not.toContain("draft-preview");
    signIn(OWNER);
    vi.stubEnv("CLASHLENS_BLOG_OWNER", "");
    expect(await slugs()).not.toContain("draft-preview");
  });

  it("hides drafts when the sign-in cannot be checked", async () => {
    mocks.readLoginIdentity.mockRejectedValue(new Error("private API down"));
    expect(await slugs()).not.toContain("draft-preview");
  });

  it("shows drafts to the signed-in owner", async () => {
    signIn(OWNER);
    expect(await slugs()).toContain("draft-preview");
  });

  it("returns 404 for a draft's address unless the owner is signed in", async () => {
    expect((await render("/blog/draft-preview")).status).toBe(404);
    signIn(OWNER);
    const { html, status } = await render("/blog/draft-preview");
    expect(status).toBe(200);
    expect(html).toContain("30 September 2026</time> · Draft");
    expect((await render("/blog")).html).toContain(
      '<a href="/blog/draft-preview" data-discover="true">Draft preview</a></h2><p class="blog-date"><time dateTime="2026-09-30">30 September 2026</time> · Draft</p>',
    );
  });

  it("tells search engines not to index a draft", async () => {
    signIn(OWNER);
    const loaderData = await postLoader({
      params: { slug: "draft-preview" },
      request: new Request(`${ORIGIN}/blog/draft-preview`),
    } as Parameters<typeof postLoader>[0]);
    const tags = postMeta({ loaderData } as Parameters<typeof postMeta>[0]);
    expect(tags).toContainEqual({ name: "robots", content: "noindex" });
    const published = await postLoader({
      params: { slug: "how-matchmaking-works" },
      request: new Request(`${ORIGIN}/blog/how-matchmaking-works`),
    } as Parameters<typeof postLoader>[0]);
    expect(
      postMeta({ loaderData: published } as Parameters<typeof postMeta>[0]),
    ).not.toContainEqual({ name: "robots", content: "noindex" });
  });

  it("never puts a draft in the feed, even for the owner", async () => {
    signIn(OWNER);
    const response = (await feedLoader({
      request: new Request(`${ORIGIN}/blog/rss.xml`),
    } as Parameters<typeof feedLoader>[0])) as Response;
    expect(await response.text()).not.toContain("draft-preview");
  });
});

describe("blog media", () => {
  async function get(file: string, headers: HeadersInit = {}) {
    try {
      return (await mediaLoader({
        params: { file },
        request: new Request(`${ORIGIN}/blog/media/${encodeURIComponent(file)}`, {
          headers,
        }),
      } as Parameters<typeof mediaLoader>[0])) as Response;
    } catch (thrown) {
      if (thrown instanceof Response) return thrown;
      throw thrown;
    }
  }

  it("serves a chart with its type and caching headers", async () => {
    const response = await get("gap-chart-dark.png");
    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Type")).toBe("image/png");
    expect(response.headers.get("Cache-Control")).toBe("public, max-age=300");
    expect(response.headers.get("X-Content-Type-Options")).toBe("nosniff");
    expect(new Uint8Array(await response.arrayBuffer()).slice(1, 4)).toEqual(
      new TextEncoder().encode("PNG"),
    );
    const etag = response.headers.get("ETag")!;
    expect(etag).toMatch(/^W\/"\d+-[\d.]+"$/);
    expect((await get("gap-chart-dark.png", { "If-None-Match": etag })).status).toBe(304);
    expect((await get("gap-chart.csv")).headers.get("Content-Type")).toBe(
      "text/csv; charset=utf-8",
    );
  });

  it("serves media only drafts use to the signed-in owner alone, never cached", async () => {
    const directory = await mkdtemp(join(tmpdir(), "blog-draft-media-"));
    const post = (draft: boolean, cover: string, body: string) =>
      `---\ntitle: T\ndate: 2026-01-01\nsummary: S\ncover: ${cover}\ndraft: ${draft}\n---\n${body}\n`;
    try {
      await mkdir(join(directory, "posts"));
      await mkdir(join(directory, "media"));
      for (const name of [
        "shared.png",
        "cover.png",
        "secret.png",
        "secret-dark.png",
        "secret.csv",
        "secret-cover.png",
        "unused.png",
      ]) {
        await writeFile(join(directory, "media", name), name);
      }
      await writeFile(
        join(directory, "posts", "published.md"),
        post(false, "cover.png", "![A](../media/shared.png)"),
      );
      await writeFile(
        join(directory, "posts", "draft.md"),
        post(
          true,
          "secret-cover.png",
          "![A](../media/shared.png) ![B](../media/secret.png) [Data](../media/secret.csv)",
        ),
      );
      vi.stubEnv("CLASHLENS_BLOG_DIR", directory);
      for (const name of ["shared.png", "cover.png"]) {
        const response = await get(name);
        expect(response.status).toBe(200);
        expect(response.headers.get("Cache-Control")).toBe("public, max-age=300");
      }
      const ownerOnly = [
        "secret.png",
        "secret-dark.png",
        "secret.csv",
        "secret-cover.png",
        "unused.png",
      ];
      signIn({ provider: "google", providerSubject: "someone-else" });
      for (const name of ownerOnly) expect((await get(name)).status).toBe(404);
      signIn(OWNER);
      for (const name of ownerOnly) {
        const response = await get(name);
        expect(response.status).toBe(200);
        expect(response.headers.get("Cache-Control")).toBe("no-store");
        expect(await response.text()).toBe(name);
      }
    } finally {
      await rm(directory, { recursive: true });
    }
  });

  it.each([
    "../posts/how-matchmaking-works.md",
    "..",
    "../../package.json",
    "/etc/passwd",
    "gap-chart.png/..",
    "missing.png",
    "_template.md",
    ".gitkeep",
  ])("refuses %s", async (file) => {
    expect((await get(file)).status).toBe(404);
  });

  it("refuses files without a known type and serves nothing without a folder", async () => {
    const directory = await mkdtemp(join(tmpdir(), "blog-media-"));
    try {
      await mkdir(join(directory, "media"));
      await writeFile(join(directory, "media", "page.html"), "<script></script>");
      await writeFile(join(directory, "secret.png"), "outside media/");
      await symlink(join(directory, "secret.png"), join(directory, "media", "link.png"));
      vi.stubEnv("CLASHLENS_BLOG_DIR", directory);
      expect((await get("page.html")).status).toBe(404);
      expect((await get("link.png")).status).toBe(404);
      vi.stubEnv("CLASHLENS_BLOG_DIR", "");
      expect((await get("gap-chart.png")).status).toBe(404);
    } finally {
      await rm(directory, { recursive: true });
    }
  });
});
