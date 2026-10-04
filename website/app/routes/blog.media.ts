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
 * Only plain file names found by listing media/ are served, so no address can
 * reach a file outside that folder. A file no published post uses is served
 * only to the signed-in site owner, decided by a read of the blog folder taken
 * after the served file last changed.
 */
export async function loader({ params, request }: Route.LoaderArgs) {
  const { MEDIA_NAME, blogDirectory, blogFolder, isBlogOwner } =
    await import("../server/blog.server");
  const directory = blogDirectory();
  const contentType = CONTENT_TYPES[extname(params.file).toLowerCase()];
  if (!directory || !contentType || !MEDIA_NAME.test(params.file)) {
    throw new Response(null, { status: 404 });
  }
  const path = join(directory, "media", params.file);
  const metadata = await stat(path).catch(() => null);
  if (!metadata) throw new Response(null, { status: 404 });
  const folder = await blogFolder(Date.now(), metadata.mtimeMs);
  const isPublic = folder.publicMedia.has(params.file);
  if (!folder.media.has(params.file) || (!isPublic && !(await isBlogOwner(request)))) {
    throw new Response(null, { status: 404 });
  }
  const etag = `W/"${metadata.size}-${metadata.mtimeMs}"`;
  let body: Buffer;
  try {
    body = await readFile(path);
  } catch {
    throw new Response(null, { status: 404 });
  }
  const headers = {
    "Content-Type": contentType,
    "X-Content-Type-Options": "nosniff",
    // A sync can replace a file under the same name, so browsers recheck after
    // 5 minutes. Owner-only files are never stored.
    "Cache-Control": isPublic ? "public, max-age=300" : "no-store",
    ETag: etag,
  };
  if (request.headers.get("If-None-Match") === etag) {
    return new Response(null, { status: 304, headers });
  }
  return new Response(new Uint8Array(body), { headers });
}
