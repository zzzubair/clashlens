import type { Route } from "./+types/blog.rss";

/** GET /blog/rss.xml — an RSS 2.0 feed of every published post; drafts never appear. */
export async function loader({ request }: Route.LoaderArgs) {
  const { blogFeed, feedOrigin, publishedBlogPosts } =
    await import("../server/blog.server");
  const { origin, cacheControl } = await feedOrigin(request);
  return new Response(blogFeed(await publishedBlogPosts(), origin), {
    headers: {
      "Content-Type": "application/rss+xml; charset=utf-8",
      "Cache-Control": cacheControl,
    },
  });
}
