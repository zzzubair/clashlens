import { createReadStream } from "node:fs";
import { readdir, stat } from "node:fs/promises";
import { createServer } from "node:http";
import type { IncomingMessage, RequestListener, ServerResponse } from "node:http";
import { createRequire } from "node:module";
import { extname, join, relative, resolve } from "node:path";
import { pipeline } from "node:stream";
import { fileURLToPath } from "node:url";
import { constants } from "node:zlib";
import { createRequestListener } from "@react-router/node";
import type { RequestListenerOptions } from "@react-router/node";
import type { ServerBuild } from "react-router";

const serverRequire = createRequire(
  import.meta.resolve("@react-router/serve/package.json"),
);
const compression = serverRequire("compression") as (options: {
  threshold: number;
  flush: number;
  brotli: { flush: number };
}) => (request: IncomingMessage, response: ServerResponse, next: () => void) => void;
const compress = compression({
  threshold: 0,
  flush: constants.Z_SYNC_FLUSH,
  brotli: { flush: constants.BROTLI_OPERATION_FLUSH },
});

const CONTENT_TYPES: Record<string, string> = {
  ".css": "text/css",
  ".js": "text/javascript",
  ".json": "application/json",
  ".webmanifest": "application/manifest+json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".avif": "image/avif",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".txt": "text/plain",
  ".html": "text/html",
};

// The live site. Search engines may index pages only there, and only once it
// is the configured public origin; then every preview.clashlens.net address
// moves to the same address on it. While the public origin is anything else,
// such as the preview, every host stays out of search engines.
const LIVE_ORIGIN = new URL("https://clashlens.net");
const PREVIEW_HOST = "preview.clashlens.net";
// Private, signed-in or data-only addresses, which have nothing to search for.
const LIVE_ROBOTS = `User-agent: *
Allow: /
Disallow: /account
Disallow: /auth/
Disallow: /crews
Disallow: /o/
Disallow: /resources/
Sitemap: ${LIVE_ORIGIN.origin}/sitemap.xml
`;
const CLOSED_ROBOTS = "User-agent: *\nDisallow: /\n";

export async function createWebsiteServer(
  build: ServerBuild,
  clientDirectory: string,
  publicOrigin = process.env.CLASHLENS_PUBLIC_ORIGIN,
) {
  // Read like the website config does, so "https://clashlens.net/" counts too.
  const live = URL.parse(publicOrigin?.trim() ?? "")?.origin === LIVE_ORIGIN.origin;
  // Only files found in the built public directory can be served. No request
  // pathname is ever resolved against the filesystem, and symlinks are excluded.
  const files = new Map<string, string>();
  const directory = resolve(clientDirectory);
  for (const item of await readdir(directory, { recursive: true, withFileTypes: true })) {
    const path = join(item.parentPath, item.name);
    const name = relative(directory, path);
    if (item.isFile() && !name.split("/").some((part) => part.startsWith("."))) {
      files.set(`/${name}`, path);
    }
  }
  const entry = build.entry.module as typeof build.entry.module & {
    getLoadContext: NonNullable<RequestListenerOptions["getLoadContext"]>;
  };
  if (typeof entry.getLoadContext !== "function")
    throw new Error("Missing socket context adapter");
  const listener = createRequestListener({ build, getLoadContext: entry.getLoadContext });
  const handleRequest: RequestListener = async (request, response) => {
    // No other site may show these pages inside a frame, so it cannot overlay
    // them to trick a click. X-Frame-Options covers older browsers.
    response.setHeader("Content-Security-Policy", "frame-ancestors 'none'");
    response.setHeader("X-Frame-Options", "DENY");
    let url: URL;
    let pathname: string;
    try {
      url = new URL(request.url ?? "/", "http://localhost");
      pathname = decodeURIComponent(url.pathname);
    } catch {
      response.writeHead(400).end();
      return;
    }
    const host = request.headers.host?.toLowerCase();
    if (live && host === PREVIEW_HOST) {
      // An hour's caching keeps a rollback to the preview quick for browsers.
      response.setHeader("Cache-Control", "max-age=3600");
      response.writeHead(301, {
        // A path is never read as another host: it always follows the origin.
        Location: `${LIVE_ORIGIN.origin}${url.pathname}${url.search}`,
      });
      response.end();
      return;
    }
    const indexable = live && host === LIVE_ORIGIN.host;
    if (!indexable) response.setHeader("X-Robots-Tag", "noindex, nofollow");
    if (pathname === "/robots.txt" && ["GET", "HEAD"].includes(request.method ?? "")) {
      response.setHeader("Content-Type", "text/plain; charset=utf-8");
      response.setHeader("Cache-Control", "public, max-age=3600");
      response.end(
        request.method === "HEAD" ? undefined : indexable ? LIVE_ROBOTS : CLOSED_ROBOTS,
      );
      return;
    }
    // The OBS overlay's preview, which has nothing to click, shows inside its
    // own account page.
    if (pathname === "/account/obs-overlay/preview") {
      response.setHeader("Content-Security-Policy", "frame-ancestors 'self'");
      response.setHeader("X-Frame-Options", "SAMEORIGIN");
    }
    const file = files.get(pathname);
    if (!file || !["GET", "HEAD"].includes(request.method ?? "")) {
      listener(request, response);
      return;
    }
    try {
      const metadata = await stat(file);
      const etag = `W/"${metadata.size}-${metadata.mtimeMs}"`;
      const contentType = CONTENT_TYPES[extname(file)] ?? "application/octet-stream";
      response.setHeader("Content-Type", contentType);
      response.setHeader("X-Content-Type-Options", "nosniff");
      response.setHeader(
        "Cache-Control",
        pathname.startsWith("/assets/")
          ? "public, max-age=31536000, immutable"
          : "public, max-age=3600",
      );
      response.setHeader("ETag", etag);
      response.setHeader("Content-Length", metadata.size);
      if (request.headers["if-none-match"] === etag) {
        response.writeHead(304).end();
        return;
      }
      if (request.method === "HEAD") {
        response.end();
        return;
      }
      const onError = (error: NodeJS.ErrnoException | null) => {
        if (error) response.destroy();
      };
      pipeline(createReadStream(file), response, onError);
    } catch {
      response.writeHead(500).end();
    }
  };
  return createServer((request, response) =>
    compress(request, response, () => handleRequest(request, response)),
  );
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.env.NODE_ENV ??= "production";
  const buildPath = "./build/server/index.js";
  const build = await import(buildPath);
  const server = await createWebsiteServer(build, "./build/client");
  server.listen(Number(process.env.PORT ?? 3000), process.env.HOST ?? "0.0.0.0");
  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.once(signal, () => server.close());
  }
}
