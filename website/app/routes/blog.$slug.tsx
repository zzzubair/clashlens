import { Link, data, useLoaderData } from "react-router";

import { blogMeta, formatBlogDate, type BlogPost } from "../lib/blog";
import type { Route } from "./+types/blog.$slug";
import "../blog.css";

export interface BlogPostLoaderData {
  post: BlogPost;
  origin: string;
}

/** GET /blog/:slug — one post; unknown slugs, and drafts unless the owner is signed in, are a 404. */
export async function loader({
  params,
  request,
}: Route.LoaderArgs): Promise<BlogPostLoaderData> {
  const { blogOrigin, visibleBlogPosts } = await import("../server/blog.server");
  const post = (await visibleBlogPosts(request)).find(
    (candidate) => candidate.slug === params.slug,
  );
  if (!post) throw data(null, { status: 404 });
  return { post, origin: await blogOrigin(request) };
}

export function meta({ loaderData }: Route.MetaArgs) {
  if (!loaderData) return [{ title: "Page not found · Clash Lens" }];
  const { post, origin } = loaderData;
  const tags = blogMeta({
    title: post.title,
    description: post.summary,
    url: `${origin}/blog/${post.slug}`,
    origin,
    type: "article",
    image: post.cover,
    imageAlt: post.coverAlt,
    publishedDate: post.date,
  });
  return post.draft ? [...tags, { name: "robots", content: "noindex" }] : tags;
}

export default function BlogPostRoute() {
  const { post } = useLoaderData<typeof loader>();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell blog-page">
      <article className="blog-article" aria-labelledby="blog-post-title">
        <header className="blog-article-header">
          <p className="eyebrow">
            <Link to="/blog">Blog</Link>
          </p>
          <h1 id="blog-post-title">{post.title}</h1>
          <p className="blog-date">
            <time dateTime={post.date}>{formatBlogDate(post.date)}</time>
            {post.author ? <> · {post.author}</> : null}
            {post.draft ? <> · Draft</> : null}
          </p>
        </header>
        {post.cover ? (
          <img className="blog-cover" src={post.cover} alt={post.coverAlt} />
        ) : null}
        {/* Rendered on the server from the blog folder's Markdown with raw HTML removed. */}
        <div className="blog-body" dangerouslySetInnerHTML={{ __html: post.html }} />
      </article>
    </main>
  );
}
