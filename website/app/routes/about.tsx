import { useLoaderData } from "react-router";

import { Markdown } from "../lib/markdown";
import "../about.css";

/**
 * GET /about — what Clash Lens is, the projects and services it is built on,
 * and the Fan Content Policy notice. The words live in app/content/about.md
 * so they can be edited without touching code.
 */
export async function loader() {
  const { default: source } = await import("../content/about.md?raw");
  return { source };
}

export default function AboutRoute() {
  const { source } = useLoaderData<typeof loader>();
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell about-page">
      <Markdown source={source} />
    </main>
  );
}
