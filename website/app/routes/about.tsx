import { useLoaderData } from "react-router";

import { DISCORD_INVITE_URL } from "../lib/discord";
import { Markdown } from "../lib/markdown";
import "../about.css";

/**
 * GET /about — what Clash Lens is and who it is for. The words live in
 * app/content/about.md so they can be edited without touching code. The Fan
 * Content Policy notice is in the site footer on every page, this one included.
 */
export async function loader() {
  const { default: source } = await import("../content/about.md?raw");
  return { source: source.replaceAll("DISCORD_INVITE_URL", DISCORD_INVITE_URL) };
}

export default function AboutRoute() {
  const { source } = useLoaderData<typeof loader>();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell about-page">
      <Markdown source={source} />
    </main>
  );
}
