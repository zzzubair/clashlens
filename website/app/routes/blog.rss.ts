import type { Route } from "./+types/blog.rss";

/** GET /blog/rss.xml — an RSS 2.0 feed of every published post; drafts never appear. */
export async function loader({ request }: Route.LoaderArgs) {
  const { blogFeed, blogOrigin, publishedBlogPosts } =
    await import("../server/blog.server");
  return new Response(blogFeed(await publishedBlogPosts(), await blogOrigin(request)), {
    headers: {
      "Content-Type": "application/rss+xml; charset=utf-8",
      "Cache-Control": "public, max-age=300",
    },
  });
}
