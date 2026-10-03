/**
 * Server-only blog posts. Each post is one Markdown file in `website/blog`,
 * named `<slug>.md`, that starts with front matter (see `blog/README.md`).
 * Posts are bundled into the server build, so publishing one is a commit.
 *
 * Raw HTML in a post is removed, never rendered: only Markdown formatting
 * reaches the page. Markdown links and images keep markdown-it's own URL
 * check, which refuses `javascript:` and similar addresses.
 */

import MarkdownIt from "markdown-it";

import type { BlogPost, BlogPostSummary } from "../lib/blog";

const POST_SOURCES = import.meta.glob<string>(
  ["../../blog/*.md", "!../../blog/README.md"],
  {
    query: "?raw",
    import: "default",
    eager: true,
  },
);

const SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const FIELDS = new Set(["title", "date", "summary", "author", "cover", "coverAlt"]);

export class BlogPostError extends Error {
  constructor(file: string, reason: string) {
    super(`blog post ${file}: ${reason}`);
    this.name = "BlogPostError";
  }
}

const markdown = new MarkdownIt({ html: true, typographer: true });
// Recognize raw HTML so it can be dropped instead of shown as escaped text.
markdown.renderer.rules.html_block = () => "";
markdown.renderer.rules.html_inline = () => "";
// The page title is the only h1, so post headings start at h2.
markdown.renderer.rules.heading_open = (tokens, index, options, _env, self) => {
  const token = tokens[index];
  token.tag = `h${Math.min(Number(token.tag.slice(1)) + 1, 6)}`;
  return self.renderToken(tokens, index, options);
};
markdown.renderer.rules.heading_close = (tokens, index, options, _env, self) => {
  const token = tokens[index];
  token.tag = `h${Math.min(Number(token.tag.slice(1)) + 1, 6)}`;
  return self.renderToken(tokens, index, options);
};
// Wide tables and code scroll inside the article instead of widening the page.
// tabindex lets keyboard users focus them to scroll.
markdown.renderer.rules.table_open = () =>
  '<div class="blog-table" tabindex="0">\n<table>\n';
markdown.renderer.rules.table_close = () => "</table>\n</div>\n";
for (const rule of ["fence", "code_block"] as const) {
  const render = markdown.renderer.rules[rule]!;
  markdown.renderer.rules[rule] = (...args) =>
    render(...args).replace(/^<pre>/, '<pre tabindex="0">');
}

export function renderBlogMarkdown(source: string): string {
  return markdown.render(source);
}

/** Parses one post file. Throws BlogPostError for anything a reader would trip on. */
export function parseBlogPost(file: string, source: string): BlogPost {
  const slug = (file.split("/").pop() ?? "").replace(/\.md$/, "");
  if (!SLUG.test(slug)) {
    throw new BlogPostError(file, "file name must be lowercase words joined by hyphens");
  }
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(source);
  if (!match) throw new BlogPostError(file, "missing front matter between --- lines");
  const fields: Record<string, string> = {};
  for (const line of match[1].split(/\r?\n/)) {
    if (line.trim() === "") continue;
    const separator = line.indexOf(":");
    const key = line.slice(0, separator).trim();
    if (separator < 0 || !FIELDS.has(key)) {
      throw new BlogPostError(file, `unknown front matter line "${line}"`);
    }
    if (key in fields) throw new BlogPostError(file, `"${key}" appears twice`);
    fields[key] = unquote(line.slice(separator + 1).trim());
  }
  for (const key of ["title", "date", "summary"]) {
    if (!fields[key]) throw new BlogPostError(file, `"${key}" is required`);
  }
  if (!DATE.test(fields.date) || !isRealDate(fields.date)) {
    throw new BlogPostError(file, "date must be a real date written YYYY-MM-DD");
  }
  if (fields.cover !== undefined && !/^(\/(?!\/)|https:\/\/)\S+$/.test(fields.cover)) {
    throw new BlogPostError(
      file,
      "cover must be a site path starting with / or an https URL",
    );
  }
  return {
    slug,
    title: fields.title,
    date: fields.date,
    summary: fields.summary,
    author: fields.author || null,
    cover: fields.cover || null,
    coverAlt: fields.coverAlt ?? "",
    html: renderBlogMarkdown(source.slice(match[0].length)),
  };
}

/** Parses every source and returns the posts newest first. */
export function loadBlogPosts(sources: Record<string, string>): BlogPost[] {
  const posts = Object.entries(sources).map(([file, source]) =>
    parseBlogPost(file, source),
  );
  const slugs = new Set<string>();
  for (const post of posts) {
    if (slugs.has(post.slug)) throw new BlogPostError(post.slug, "duplicate slug");
    slugs.add(post.slug);
  }
  return posts.sort(
    (a, b) => b.date.localeCompare(a.date) || a.title.localeCompare(b.title),
  );
}

let publishedPosts: BlogPost[] | undefined;

/** The committed posts, parsed once per process, newest first. */
export function publishedBlogPosts(): BlogPost[] {
  publishedPosts ??= loadBlogPosts(POST_SOURCES);
  return publishedPosts;
}

/** A post without its body, for the list page. */
export function summarizeBlogPost(post: BlogPost): BlogPostSummary {
  const { slug, title, date, summary, author, cover, coverAlt } = post;
  return { slug, title, date, summary, author, cover, coverAlt };
}

/** The site's public origin for absolute links in previews and the feed. */
export async function blogOrigin(request: Request): Promise<string> {
  try {
    const { getWebsiteConfig } = await import("./config.server");
    return getWebsiteConfig().publicOrigin.origin;
  } catch {
    return new URL(request.url).origin;
  }
}

export function blogFeed(posts: BlogPostSummary[], origin: string): string {
  const items = posts.map((post) => {
    const link = `${origin}/blog/${post.slug}`;
    return [
      "    <item>",
      `      <title>${escapeXml(post.title)}</title>`,
      `      <link>${escapeXml(link)}</link>`,
      `      <guid isPermaLink="true">${escapeXml(link)}</guid>`,
      `      <pubDate>${new Date(`${post.date}T00:00:00Z`).toUTCString()}</pubDate>`,
      `      <description>${escapeXml(post.summary)}</description>`,
      post.author ? `      <dc:creator>${escapeXml(post.author)}</dc:creator>` : null,
      "    </item>",
    ]
      .filter((line) => line !== null)
      .join("\n");
  });
  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/">',
    "  <channel>",
    "    <title>Clash Lens Blog</title>",
    `    <link>${escapeXml(`${origin}/blog`)}</link>`,
    "    <description>What Clash Lens data shows about Legend League.</description>",
    "    <language>en</language>",
    `    <atom:link href="${escapeXml(`${origin}/blog/rss.xml`)}" rel="self" type="application/rss+xml"/>`,
    ...items,
    "  </channel>",
    "</rss>",
    "",
  ].join("\n");
}

function unquote(value: string): string {
  const quoted = /^"(.*)"$/.exec(value) ?? /^'(.*)'$/.exec(value);
  return quoted ? quoted[1] : value;
}

function isRealDate(value: string): boolean {
  const date = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(date.getTime()) && date.toISOString().startsWith(value);
}

function escapeXml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
}
