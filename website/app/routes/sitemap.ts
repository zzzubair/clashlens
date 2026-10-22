import type { Route } from "./+types/sitemap";

/** GET /sitemap.xml — the public pages search engines should read; drafts never appear. */
export async function loader({ request }: Route.LoaderArgs) {
  const { blogOrigin, publishedBlogPosts, siteMap } =
    await import("../server/blog.server");
  return new Response(siteMap(await publishedBlogPosts(), await blogOrigin(request)), {
    headers: {
      "Content-Type": "application/xml; charset=utf-8",
      "Cache-Control": "public, max-age=300",
    },
  });
}
