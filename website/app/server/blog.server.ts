/**
 * Server-only blog posts, read at request time from a checkout of the private
 * blog repo on the server. `CLASHLENS_BLOG_DIR` names that folder: one
 * Markdown file per post in `posts/<slug>.md`, starting with front matter, and
 * the images and data files posts use in `media/`. With no folder configured
 * the blog is empty. A post with `draft: true` is seen only by the site owner,
 * whose sign-in is named by `CLASHLENS_BLOG_OWNER` as `<provider>:<subject>`.
 *
 * Raw HTML in a post is removed, never rendered: only Markdown formatting
 * reaches the page. Markdown links and images keep markdown-it's own URL
 * check, which refuses `javascript:` and similar addresses.
 */

import { readdir, readFile } from "node:fs/promises";
import { join, resolve } from "node:path";

import MarkdownIt from "markdown-it";

import type { BlogPost, BlogPostSummary } from "../lib/blog";

/** How long one read of the blog folder is reused before the next request rereads it. */
export const BLOG_CACHE_MS = 60_000;
/** A file name in media/: no folders, no leading dot, nothing a URL needs escaped. */
export const MEDIA_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
/** Where media/ is served; posts refer to it as `../media/<file>`. */
const MEDIA_URL = "/blog/media/";
const SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const FIELDS = new Set([
  "title",
  "date",
  "summary",
  "author",
  "cover",
  "coverAlt",
  "draft",
]);

export class BlogPostError extends Error {
  constructor(file: string, reason: string) {
    super(`blog post ${file}: ${reason}`);
    this.name = "BlogPostError";
  }
}

const markdown = new MarkdownIt({ html: true });
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

interface RenderEnv {
  /** File names in media/. */
  media: ReadonlySet<string>;
  /** Collects the media/ files the rendered post uses. */
  used: Set<string>;
}

// Posts refer to media as `../media/<file>`, relative to the post in the blog
// repo. On the site that folder is served at /blog/media/.
markdown.core.ruler.push("blog_media", (state) => {
  for (const block of state.tokens) {
    for (const token of block.children ?? []) {
      const name =
        token.type === "image" ? "src" : token.type === "link_open" ? "href" : null;
      if (!name) continue;
      const file = /^\.\.\/media\/([^/?#]+)$/.exec(String(token.attrGet(name)));
      if (file && MEDIA_NAME.test(file[1])) {
        token.attrSet(name, MEDIA_URL + file[1]);
      }
      useMedia((state.env as unknown as RenderEnv).used, String(token.attrGet(name)));
    }
  }
});
// A chart x.png with a sibling x-dark.png in media/ renders as both images;
// blog.css shows only the one matching the site theme. Both load lazily, so
// the browser skips fetching the hidden one.
const renderImage = markdown.renderer.rules.image!;
markdown.renderer.rules.image = (tokens, index, options, env, self) => {
  const token = tokens[index];
  const src = String(token.attrGet("src"));
  const dark = src.startsWith(MEDIA_URL) && src.replace(/(\.[A-Za-z0-9]+)$/, "-dark$1");
  const { media, used } = env as unknown as RenderEnv;
  if (!dark || dark === src || !media.has(dark.slice(MEDIA_URL.length))) {
    return renderImage(tokens, index, options, env, self);
  }
  useMedia(used, dark);
  token.attrSet("class", "blog-img-light");
  token.attrSet("loading", "lazy");
  const light = renderImage(tokens, index, options, env, self);
  token.attrSet("class", "blog-img-dark");
  token.attrSet("src", dark);
  return light + renderImage(tokens, index, options, env, self);
};

/**
 * Renders a post body; `media` lists media/ so each chart can find its dark
 * version, and `used` collects the media/ files the body shows or links to.
 */
export function renderBlogMarkdown(
  source: string,
  media: ReadonlySet<string> = new Set(),
  used: Set<string> = new Set(),
): string {
  return markdown.render(source, { media, used } satisfies RenderEnv);
}

/**
 * Parses one post file, adding the media/ files it uses, cover included, to
 * `used`. Throws BlogPostError for anything a reader would trip on.
 */
export function parseBlogPost(
  file: string,
  source: string,
  media: ReadonlySet<string> = new Set(),
  used: Set<string> = new Set(),
): BlogPost {
  const slug = (file.split("/").pop() ?? "").replace(/\.md$/, "");
  if (!SLUG.test(slug)) {
    throw new BlogPostError(file, "file name must be lowercase words joined by hyphens");
  }
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(source);
  if (!match) throw new BlogPostError(file, "missing front matter between --- lines");
  const fields: Record<string, string> = {};
  for (const line of match[1].split(/\r?\n/)) {
    if (line.trim() === "" || line.trim().startsWith("#")) continue;
    const separator = line.indexOf(":");
    const key = line.slice(0, separator).trim();
    if (separator < 0 || !FIELDS.has(key)) {
      throw new BlogPostError(file, `unknown front matter line "${line}"`);
    }
    if (key in fields) throw new BlogPostError(file, `"${key}" appears twice`);
    fields[key] = frontMatterValue(line.slice(separator + 1).trim());
  }
  for (const key of ["title", "date", "summary"]) {
    if (!fields[key]) throw new BlogPostError(file, `"${key}" is required`);
  }
  if (!DATE.test(fields.date) || !isRealDate(fields.date)) {
    throw new BlogPostError(file, "date must be a real date written YYYY-MM-DD");
  }
  if (fields.cover !== undefined && MEDIA_NAME.test(fields.cover)) {
    fields.cover = MEDIA_URL + fields.cover;
  } else if (fields.cover !== undefined && !isValidCover(fields.cover)) {
    throw new BlogPostError(
      file,
      "cover must be a file in media/, a site path starting with / or an https URL",
    );
  }
  if (fields.draft !== undefined && !["true", "false"].includes(fields.draft)) {
    throw new BlogPostError(file, "draft must be true or false");
  }
  const html = renderBlogMarkdown(source.slice(match[0].length), media, used);
  if (fields.cover) useMedia(used, fields.cover);
  return {
    slug,
    title: fields.title,
    date: fields.date,
    summary: fields.summary,
    author: fields.author || null,
    cover: fields.cover || null,
    coverAlt: fields.coverAlt ?? "",
    draft: fields.draft === "true",
    html,
  };
}

/**
 * Parses every source and returns the posts newest first, adding the media/
 * files published posts use to `publicMedia`. A post that fails to parse is
 * logged and left out, so one bad file cannot take the blog down.
 */
export function loadBlogPosts(
  sources: Record<string, string>,
  media: ReadonlySet<string> = new Set(),
  publicMedia: Set<string> = new Set(),
): BlogPost[] {
  const posts: BlogPost[] = [];
  for (const [file, source] of Object.entries(sources)) {
    try {
      const used = new Set<string>();
      const post = parseBlogPost(file, source, media, used);
      posts.push(post);
      if (!post.draft) used.forEach((name) => publicMedia.add(name));
    } catch (error) {
      if (!(error instanceof BlogPostError)) throw error;
      console.error(error.message);
    }
  }
  return posts.sort(
    (a, b) => b.date.localeCompare(a.date) || a.title.localeCompare(b.title),
  );
}

export interface BlogFolder {
  /** Every post, drafts included, newest first. */
  posts: BlogPost[];
  /** File names in media/ that may be served. */
  media: ReadonlySet<string>;
  /** The media/ files a published post uses; the rest are for the owner only. */
  publicMedia: ReadonlySet<string>;
}

/**
 * Reads posts/ and media/ from a blog checkout. A missing folder is an empty
 * blog, and a post a sync removes while it is being read is left out.
 */
export async function readBlogFolder(directory: string | null): Promise<BlogFolder> {
  if (directory === null) return { posts: [], media: new Set(), publicMedia: new Set() };
  const media = new Set(
    (await listFiles(join(directory, "media"))).filter((name) => MEDIA_NAME.test(name)),
  );
  const sources: Record<string, string> = {};
  for (const name of await listFiles(join(directory, "posts"))) {
    // posts/_template.md and other underscore files are not posts.
    if (name.endsWith(".md") && !name.startsWith("_")) {
      const source = await ifMissing(
        readFile(join(directory, "posts", name), "utf8"),
        null,
      );
      if (source !== null) sources[name] = source;
    }
  }
  const publicMedia = new Set<string>();
  return { posts: loadBlogPosts(sources, media, publicMedia), media, publicMedia };
}

let cached:
  { directory: string | null; readAt: number; folder: Promise<BlogFolder> } | undefined;

/** The configured blog checkout, or null when this site has no blog folder. */
export function blogDirectory(): string | null {
  const directory = process.env.CLASHLENS_BLOG_DIR?.trim();
  return directory ? resolve(directory) : null;
}

/** The blog folder, reread at most once per BLOG_CACHE_MS so a sync shows up quickly. */
export function blogFolder(now = Date.now()): Promise<BlogFolder> {
  const directory = blogDirectory();
  if (
    cached === undefined ||
    cached.directory !== directory ||
    now - cached.readAt >= BLOG_CACHE_MS
  ) {
    const entry = { directory, readAt: now, folder: readBlogFolder(directory) };
    cached = entry;
    // A failed read is retried on the next request instead of being kept.
    entry.folder.catch(() => {
      if (cached === entry) cached = undefined;
    });
  }
  return cached.folder;
}

/** Posts everyone may see, newest first. Drafts are never included. */
export async function publishedBlogPosts(): Promise<BlogPost[]> {
  return (await blogFolder()).posts.filter((post) => !post.draft);
}

/** Posts this request may see: drafts too when the site owner is signed in. */
export async function visibleBlogPosts(request: Request): Promise<BlogPost[]> {
  const { posts } = await blogFolder();
  if (!posts.some((post) => post.draft) || (await isBlogOwner(request))) return posts;
  return posts.filter((post) => !post.draft);
}

/**
 * True when the request carries a valid sign-in for an identity listed in
 * CLASHLENS_BLOG_OWNER: comma-separated `<provider>:<subject>` values such as
 * `google:1234`. The site has no other owner or admin role.
 */
export async function isBlogOwner(request: Request): Promise<boolean> {
  const owners = (process.env.CLASHLENS_BLOG_OWNER ?? "")
    .split(",")
    .map((owner) => owner.trim())
    .filter(Boolean);
  if (owners.length === 0) return false;
  try {
    const { getWebsiteConfig } = await import("./config.server");
    const { readLoginIdentity } = await import("./actions.server");
    const identity = await readLoginIdentity(request, getWebsiteConfig());
    return (
      identity !== null &&
      owners.includes(`${identity.provider}:${identity.providerSubject}`)
    );
  } catch {
    return false;
  }
}

/** A post without its body, for the list page. */
export function summarizeBlogPost(post: BlogPost): BlogPostSummary {
  const { slug, title, date, summary, author, cover, coverAlt, draft } = post;
  return { slug, title, date, summary, author, cover, coverAlt, draft };
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

/** A front matter value without its quotes or a trailing `# comment`. */
function frontMatterValue(value: string): string {
  const quoted = /^"(.*?)"(?:\s+#.*)?$/.exec(value) ?? /^'(.*?)'(?:\s+#.*)?$/.exec(value);
  return quoted ? quoted[1] : value.replace(/\s+#.*$/, "");
}

/** Regular files directly in a folder; a missing folder has none. */
async function listFiles(directory: string): Promise<string[]> {
  const entries = await ifMissing(readdir(directory, { withFileTypes: true }), []);
  return entries.filter((entry) => entry.isFile()).map((entry) => entry.name);
}

/** The result of reading a file or folder, or `missing` when it does not exist. */
async function ifMissing<T, M>(read: Promise<T>, missing: M): Promise<T | M> {
  try {
    return await read;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return missing;
    throw error;
  }
}

/** Records the media/ file a /blog/media/ address points at. */
function useMedia(used: Set<string>, url: string): void {
  if (url.startsWith(MEDIA_URL)) used.add(url.slice(MEDIA_URL.length));
}

/** A path that stays on this site, or an https URL, that both parse as addresses. */
function isValidCover(value: string): boolean {
  if (/\s/.test(value) || /^\/[/\\]/.test(value)) return false;
  const base = "https://site.invalid";
  try {
    const url = new URL(value, base);
    return value.startsWith("/")
      ? url.origin === base
      : value.startsWith("https://") && url.protocol === "https:";
  } catch {
    return false;
  }
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
