import { readFile, stat } from "node:fs/promises";
import { extname, join } from "node:path";

import type { Route } from "./+types/blog.media";

const CONTENT_TYPES: Record<string, string> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".avif": "image/avif",
  ".csv": "text/csv; charset=utf-8",
};

/**
 * GET /blog/media/:file — one image or data file from the blog folder's media/.
 * Only names found by listing media/ are served, never a path built from the
 * request alone, so no address can reach a file outside that folder.
 */
export async function loader({ params, request }: Route.LoaderArgs) {
  const { blogDirectory, blogFolder } = await import("../server/blog.server");
  const directory = blogDirectory();
  const contentType = CONTENT_TYPES[extname(params.file).toLowerCase()];
  if (!directory || !contentType || !(await blogFolder()).media.has(params.file)) {
    throw new Response(null, { status: 404 });
  }
  const path = join(directory, "media", params.file);
  let body: Buffer;
  let etag: string;
  try {
    const metadata = await stat(path);
    etag = `W/"${metadata.size}-${metadata.mtimeMs}"`;
    body = await readFile(path);
  } catch {
    throw new Response(null, { status: 404 });
  }
  const headers = {
    "Content-Type": contentType,
    "X-Content-Type-Options": "nosniff",
    // A sync can replace a file under the same name, so browsers recheck after 5 minutes.
    "Cache-Control": "public, max-age=300",
    ETag: etag,
  };
  if (request.headers.get("If-None-Match") === etag) {
    return new Response(null, { status: 304, headers });
  }
  return new Response(new Uint8Array(body), { headers });
}
