import { Link, useLoaderData } from "react-router";

import { blogMeta, formatBlogDate, type BlogPostSummary } from "../lib/blog";
import type { Route } from "./+types/blog";
import "../blog.css";

export interface BlogIndexLoaderData {
  posts: BlogPostSummary[];
  origin: string;
}

/** GET /blog — every committed post, newest first. */
export async function loader({
  request,
}: Route.LoaderArgs): Promise<BlogIndexLoaderData> {
  const { blogOrigin, publishedBlogPosts, summarizeBlogPost } =
    await import("../server/blog.server");
  return {
    posts: publishedBlogPosts().map(summarizeBlogPost),
    origin: await blogOrigin(request),
  };
}

export function meta({ loaderData }: Route.MetaArgs) {
  if (!loaderData) return [{ title: "Blog · Clash Lens" }];
  return blogMeta({
    title: "Blog",
    description: "What Clash Lens data shows about Legend League.",
    url: `${loaderData.origin}/blog`,
    origin: loaderData.origin,
    type: "website",
  });
}

export default function BlogIndex() {
  const { posts } = useLoaderData<typeof loader>();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell blog-page">
      <section className="hero" aria-labelledby="blog-title">
        <h1 id="blog-title">Blog</h1>
        <p className="hero-copy">What Clash Lens data shows about Legend League.</p>
      </section>
      {posts.length === 0 ? (
        <div className="empty-state">
          <h2>First post coming soon</h2>
          <p>We're writing up what the data shows. Check back shortly.</p>
        </div>
      ) : (
        <ol className="blog-list">
          {posts.map((post) => (
            <li key={post.slug}>
              <article>
                <h2>
                  <Link to={`/blog/${post.slug}`}>{post.title}</Link>
                </h2>
                <p className="blog-date">
                  <time dateTime={post.date}>{formatBlogDate(post.date)}</time>
                </p>
                <p>{post.summary}</p>
              </article>
            </li>
          ))}
        </ol>
      )}
      <p className="blog-feed-link">
        <a href="/blog/rss.xml">RSS feed</a>
      </p>
    </main>
  );
}
