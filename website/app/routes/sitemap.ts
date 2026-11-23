import type { Route } from "./+types/sitemap";

export async function loader({ request }: Route.LoaderArgs) {
  const { feedOrigin, publishedBlogPosts, siteMap } =
    await import("../server/blog.server");
  const { origin, cacheControl } = await feedOrigin(request);
  return new Response(siteMap(await publishedBlogPosts(), origin), {
    headers: {
      "Content-Type": "application/xml; charset=utf-8",
      "Cache-Control": cacheControl,
    },
  });
}
