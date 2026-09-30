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

export async function createWebsiteServer(build: ServerBuild, clientDirectory: string) {
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
    let pathname: string;
    try {
      pathname = decodeURIComponent(
        new URL(request.url ?? "/", "http://localhost").pathname,
      );
    } catch {
      response.writeHead(400).end();
      return;
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
