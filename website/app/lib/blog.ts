import type { MetaDescriptor } from "react-router";

export interface BlogPostSummary {
  /** The file name without `.md`; the post lives at `/blog/<slug>`. */
  slug: string;
  title: string;
  /** Publication day, `YYYY-MM-DD`. */
  date: string;
  /** One line shown in the list, link previews and the feed. */
  summary: string;
  author: string | null;
  /** A site path such as `/images/blog/x.png`, or an https URL. */
  cover: string | null;
  coverAlt: string;
}

export interface BlogPost extends BlogPostSummary {
  /** Rendered Markdown with all raw HTML removed. */
  html: string;
}

const blogDateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "long",
  year: "numeric",
  timeZone: "UTC",
});

export function formatBlogDate(date: string): string {
  return blogDateFormatter.format(new Date(`${date}T00:00:00Z`));
}

/** Makes a site path absolute so link previews outside the site can load it. */
export function absoluteBlogUrl(pathOrUrl: string, origin: string): string {
  return new URL(pathOrUrl, origin).href;
}

interface PageMeta {
  title: string;
  description: string;
  /** Absolute page address. */
  url: string;
  origin: string;
  type: "website" | "article";
  image?: string | null;
  imageAlt?: string;
}

interface BlogPageMeta extends PageMeta {
  publishedDate?: string;
}

/** Page title plus the Open Graph and Twitter tags Discord reads for link previews. */
export function pageMeta(page: PageMeta): MetaDescriptor[] {
  const image = page.image ? absoluteBlogUrl(page.image, page.origin) : null;
  return [
    { title: `${page.title} · Clash Lens` },
    { name: "description", content: page.description },
    { property: "og:site_name", content: "Clash Lens" },
    { property: "og:type", content: page.type },
    { property: "og:title", content: page.title },
    { property: "og:description", content: page.description },
    { property: "og:url", content: page.url },
    ...(image
      ? [
          { property: "og:image", content: image },
          ...(page.imageAlt
            ? [{ property: "og:image:alt", content: page.imageAlt }]
            : []),
        ]
      : []),
    { name: "twitter:card", content: image ? "summary_large_image" : "summary" },
    { name: "twitter:title", content: page.title },
    { name: "twitter:description", content: page.description },
    ...(image ? [{ name: "twitter:image", content: image }] : []),
    { tagName: "link", rel: "canonical", href: page.url },
  ];
}

/** The shared preview tags plus the blog's publish date and feed link. */
export function blogMeta(page: BlogPageMeta): MetaDescriptor[] {
  return [
    ...pageMeta(page),
    ...(page.publishedDate
      ? [{ property: "article:published_time", content: page.publishedDate }]
      : []),
    {
      tagName: "link",
      rel: "alternate",
      type: "application/rss+xml",
      title: "Clash Lens Blog",
      href: `${page.origin}/blog/rss.xml`,
    },
  ];
}
