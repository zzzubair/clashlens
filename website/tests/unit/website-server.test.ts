import { copyFile, mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { request as httpRequest } from "node:http";
import type { Server } from "node:http";
import { join } from "node:path";
import { gunzipSync, gzipSync } from "node:zlib";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ServerBuild } from "react-router";
import { createWebsiteServer } from "../../server";
import { createClientAddressContext } from "../../app/server/client-address.server";
import { clearPublicRefreshLimits } from "../../app/server/abuse.server";
import { action } from "../../app/routes/refresh";

vi.mock("../../app/services/python.server", () => ({
  createPythonClient: () => ({ requestRefresh: async () => ({ state: "queued" }) }),
}));
vi.mock("../../app/server/config.server", () => ({
  getWebsiteConfig: () => ({ publicOrigin: new URL("https://clashlens.example") }),
}));

let directory: string;
let build: ServerBuild;
let server: Server;
let port: number;
let releaseStream: () => void;
const pageText = "Player page content. ".repeat(200);
const routeData = "Player route data. ".repeat(200);
// Browsers request these paths on their own, whether or not a page links them.
const icons = {
  "/favicon.ico": 32,
  "/apple-touch-icon.png": 180,
  "/apple-touch-icon-precomposed.png": 180,
  "/apple-touch-icon-120x120-precomposed.png": 120,
};

beforeEach(async () => {
  clearPublicRefreshLimits();
  directory = await mkdtemp(join(process.cwd(), "node_modules/.website-server-test-"));
  await mkdir(join(directory, "assets"));
  await writeFile(join(directory, "assets", "app.js"), "window.example = true;");
  await writeFile(join(directory, "site.webmanifest"), '{"name":"Clash Lens"}');
  await writeFile(join(directory, ".secret"), "must not be served");
  for (const icon of Object.keys(icons))
    await copyFile(join("public", icon), join(directory, icon));
  const streamFinished = new Promise<void>((resolve) => {
    releaseStream = resolve;
  });
  build = {
    entry: {
      module: {
        default: (_request: Request, status: number, headers: Headers) => {
          if (status === 404) return new Response("Not found", { status });
          headers.set("Content-Type", "text/html");
          headers.set("Vary", "Origin");
          return new Response(
            new ReadableStream({
              start(controller) {
                controller.enqueue(new TextEncoder().encode(pageText));
                controller.close();
              },
            }),
            { status, headers },
          );
        },
        getLoadContext: createClientAddressContext({
          CLASHLENS_TRUSTED_PROXY_IP: "127.0.0.2",
        }),
      },
    },
    routes: {
      root: {
        id: "root",
        path: "",
        module: { default: () => null },
      },
      player: {
        id: "player",
        parentId: "root",
        path: "players/:tag",
        module: { default: () => null, loader: () => ({ result: routeData }) },
      },
      response: {
        id: "response",
        parentId: "root",
        path: "responses/:kind",
        module: {
          loader: ({ params }: { params: { kind: string } }) => {
            if (params.kind === "stream") {
              return new Response(
                new ReadableStream({
                  async start(controller) {
                    controller.enqueue(new TextEncoder().encode("first chunk"));
                    await streamFinished;
                    controller.enqueue(new TextEncoder().encode("last chunk"));
                    controller.close();
                  },
                }),
                { headers: { "Content-Type": "text/plain" } },
              );
            }
            return new Response(
              params.kind === "encoded" ? gzipSync(pageText) : pageText,
              {
                headers: {
                  "Content-Type": params.kind === "binary" ? "image/png" : "text/plain",
                  ...(params.kind === "encoded" ? { "Content-Encoding": "gzip" } : {}),
                  ...(params.kind === "untransformed"
                    ? { "Cache-Control": "no-transform" }
                    : {}),
                  ...(params.kind === "length"
                    ? { "Content-Length": String(Buffer.byteLength(pageText)) }
                    : {}),
                },
              },
            );
          },
        },
      },
      refresh: {
        id: "refresh",
        parentId: "root",
        path: "resources/players/:tag/refresh",
        module: { action },
      },
    },
    assets: { version: "test", routes: {}, entry: { module: "" }, url: "" },
    future: {},
    ssr: true,
    isSpaMode: false,
    prerender: [],
    publicPath: "/",
    assetsBuildDirectory: directory,
    routeDiscovery: { mode: "initial", manifestPath: "/__manifest" },
  } as unknown as ServerBuild;
  await startServer(undefined);
});

async function startServer(publicOrigin: string | undefined) {
  if (server) await new Promise((resolve) => server.close(resolve));
  server = await createWebsiteServer(build, directory, publicOrigin);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Missing test port");
  port = address.port;
}

afterEach(async () => {
  releaseStream?.();
  if (server)
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  if (directory) await rm(directory, { recursive: true, force: true });
});

function getResponse(path: string, encoding = "gzip", method = "GET", host?: string) {
  return new Promise<{
    status: number | undefined;
    headers: import("node:http").IncomingHttpHeaders;
    body: Buffer;
  }>((resolve, reject) => {
    const request = httpRequest(
      {
        hostname: "127.0.0.1",
        port,
        path,
        method,
        headers: { "Accept-Encoding": encoding, ...(host ? { Host: host } : {}) },
      },
      (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.on("error", reject);
        response.on("end", () =>
          resolve({
            status: response.statusCode,
            headers: response.headers,
            body: Buffer.concat(chunks),
          }),
        );
      },
    );
    request.on("error", reject);
    request.end();
  });
}

it.each(["/players/%232PP", "/players/%232PP.data"])(
  "compresses %s and respects refused compression and HEAD",
  async (path) => {
    const expected = path.endsWith(".data") ? routeData : pageText;
    const compressed = await getResponse(path);
    expect(compressed.status).toBe(200);
    expect(compressed.headers["content-encoding"]).toBe("gzip");
    expect(compressed.headers.vary).toContain("Accept-Encoding");
    if (!path.endsWith(".data")) expect(compressed.headers.vary).toContain("Origin");
    expect(gunzipSync(compressed.body).toString()).toContain(expected);
    for (const encoding of ["identity", "gzip;q=0, identity"]) {
      const plain = await getResponse(path, encoding);
      expect(plain.headers["content-encoding"]).toBeUndefined();
      expect(plain.body.toString()).toContain(expected);
    }
    const head = await getResponse(path, "gzip", "HEAD");
    expect(head.status).toBe(200);
    expect(head.body.byteLength).toBe(0);
    expect(head.headers["content-encoding"]).toBeUndefined();
  },
);

it.each(Object.entries(icons))(
  "serves the real %s at %i pixels",
  async (path, pixels) => {
    const response = await getResponse(path, "identity");
    expect(response.status).toBe(200);
    expect(response.headers["content-type"]).toBe(
      path.endsWith(".ico") ? "image/x-icon" : "image/png",
    );
    expect(response.body.equals(await readFile(join("public", path)))).toBe(true);
    // ICO stores its first image's width in byte 6; PNG stores width at byte 16.
    const width = path.endsWith(".ico")
      ? response.body[6]
      : response.body.readUInt32BE(16);
    expect(width).toBe(pixels);
  },
);

it.each(["gzip", "deflate", "br"])(
  "delivers %s stream chunks before the response finishes",
  async (encoding) => {
    const response = await fetch(`http://127.0.0.1:${port}/responses/stream`, {
      headers: { "Accept-Encoding": encoding },
    });
    try {
      expect(response.headers.get("content-encoding")).toBe(encoding);
      const reader = response.body!.getReader();
      const first = await reader.read();
      expect(new TextDecoder().decode(first.value)).toBe("first chunk");
      releaseStream();
      let rest = "";
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        rest += new TextDecoder().decode(chunk.value);
      }
      expect(rest).toBe("last chunk");
    } finally {
      releaseStream();
    }
  },
);

it("removes the original length when compressing and preserves existing encodings", async () => {
  const compressed = await getResponse("/responses/length");
  expect(compressed.headers["content-encoding"]).toBe("gzip");
  expect(compressed.headers["content-length"]).toBeUndefined();
  expect(gunzipSync(compressed.body).toString()).toBe(pageText);
  const encoded = await getResponse("/responses/encoded");
  expect(encoded.headers["content-encoding"]).toBe("gzip");
  expect(gunzipSync(encoded.body).toString()).toBe(pageText);
});

it.each(["binary", "untransformed"])("leaves %s responses uncompressed", async (kind) => {
  const response = await getResponse(`/responses/${kind}`);
  expect(response.headers["content-encoding"]).toBeUndefined();
  expect(response.body.toString()).toBe(pageText);
});

function refresh(peer: string, headers: Record<string, string>) {
  return new Promise<number | undefined>((resolve, reject) => {
    const request = httpRequest(
      {
        hostname: "127.0.0.1",
        port,
        localAddress: peer,
        path: "/resources/players/%232PP/refresh",
        method: "POST",
        headers: {
          Origin: "https://clashlens.example",
          "Content-Type": "application/x-www-form-urlencoded",
          ...headers,
        },
      },
      (response) => {
        response.resume();
        response.on("end", () => resolve(response.statusCode));
      },
    );
    request.on("error", reject);
    request.end("idempotencyKey=11111111-1111-4111-8111-111111111111");
  });
}

it("uses the real socket peer and rejects forged headers on a direct HTTP connection", async () => {
  for (let index = 0; index < 7; index++) {
    expect(
      await refresh("127.0.0.1", {
        "X-Forwarded-For": `203.0.113.${index + 1}`,
        Forwarded: `for=203.0.113.${index + 1}`,
        "X-Real-IP": `203.0.113.${index + 1}`,
        "CF-Connecting-IP": `203.0.113.${index + 1}`,
      }),
    ).toBe(index < 6 ? 202 : 429);
  }
  // A different socket peer still has its own allowance.
  expect(await refresh("127.0.0.3", {})).toBe(202);
});

it("accepts the visitor header only on the configured proxy connection", async () => {
  for (let index = 0; index < 7; index++) {
    expect(
      await refresh("127.0.0.2", {
        "CF-Connecting-IP": "198.51.100.1",
        "X-Forwarded-For": `203.0.113.${index + 1}`,
      }),
    ).toBe(index < 6 ? 202 : 429);
  }
  expect(await refresh("127.0.0.2", { "CF-Connecting-IP": "198.51.100.2" })).toBe(202);
});

it("serves built assets with caching, compression and HEAD without exposing other files", async () => {
  const url = `http://127.0.0.1:${port}`;
  const asset = await fetch(`${url}/assets/app.js`);
  expect(asset.status).toBe(200);
  expect(asset.headers.get("content-type")).toBe("text/javascript");
  expect(asset.headers.get("cache-control")).toContain("immutable");
  expect(asset.headers.get("content-encoding")).toBe("gzip");
  expect(await asset.text()).toBe("window.example = true;");
  const uncompressed = await fetch(`${url}/assets/app.js`, {
    headers: { "Accept-Encoding": "gzip;q=0.0, identity" },
  });
  expect(uncompressed.headers.get("content-encoding")).toBeNull();
  expect(await uncompressed.text()).toBe("window.example = true;");
  const cached = await fetch(`${url}/assets/app.js`, {
    headers: { "If-None-Match": asset.headers.get("etag")! },
  });
  expect(cached.status).toBe(304);
  const head = await fetch(`${url}/assets/app.js`, { method: "HEAD" });
  expect(head.status).toBe(200);
  expect(await head.text()).toBe("");
  const manifest = await fetch(`${url}/site.webmanifest`);
  expect(manifest.headers.get("content-type")).toBe("application/manifest+json");
  expect(await manifest.json()).toEqual({ name: "Clash Lens" });
  for (const path of ["/.secret", "/%2e%2e/package.json", "/assets/../../package.json"]) {
    expect((await fetch(`${url}${path}`)).status).toBe(404);
  }
});

it("forbids framing on pages, route data, files, refusals and bad requests", async () => {
  const url = `http://127.0.0.1:${port}`;
  for (const path of [
    "/players/%232PP",
    "/players/%232PP.data",
    "/assets/app.js",
    "/.secret",
    "/%E0",
  ]) {
    const response = await fetch(`${url}${path}`);
    expect(response.headers.get("content-security-policy")).toBe(
      "frame-ancestors 'none'",
    );
    expect(response.headers.get("x-frame-options")).toBe("DENY");
  }
});

it("lets only the OBS overlay's preview show inside a Clash Lens page", async () => {
  const url = `http://127.0.0.1:${port}`;
  const preview = await fetch(`${url}/account/obs-overlay/preview?p=2PP`);
  expect(preview.headers.get("content-security-policy")).toBe("frame-ancestors 'self'");
  expect(preview.headers.get("x-frame-options")).toBe("SAMEORIGIN");
  const overlay = await fetch(`${url}/o/AAAAAAAAAAAAAAAAAAAAAA`);
  expect(overlay.headers.get("x-frame-options")).toBe("DENY");
});

it("keeps every host out of search engines while the public origin is the preview", async () => {
  await startServer("https://preview.clashlens.net");
  for (const host of ["preview.clashlens.net", "clashlens.net"]) {
    for (const path of ["/players/%232PP", "/assets/app.js", "/players/%232PP.data"]) {
      const response = await getResponse(path, "identity", "GET", host);
      expect(response.status).toBe(200);
      expect(response.headers["x-robots-tag"]).toBe("noindex, nofollow");
    }
    const robots = await getResponse("/robots.txt", "identity", "GET", host);
    expect(robots.headers["content-type"]).toBe("text/plain; charset=utf-8");
    expect(robots.body.toString()).toBe("User-agent: *\nDisallow: /\n");
  }
});

it.each(["https://clashlens.net", "https://clashlens.net/"])(
  "lets search engines read clashlens.net once %s is the public origin",
  async (publicOrigin) => {
    await startServer(publicOrigin);
    for (const path of ["/players/%232PP", "/assets/app.js"]) {
      const response = await getResponse(path, "identity", "GET", "clashlens.net");
      expect(response.status).toBe(200);
      expect(response.headers["x-robots-tag"]).toBeUndefined();
    }
    const robots = await getResponse("/robots.txt", "identity", "GET", "clashlens.net");
    expect(robots.body.toString()).toContain("Allow: /\n");
    expect(robots.body.toString()).not.toContain("Disallow: /\n");
    expect(robots.body.toString()).toContain("Disallow: /account\n");
    expect(robots.body.toString()).toContain(
      "Sitemap: https://clashlens.net/sitemap.xml\n",
    );
    const head = await getResponse("/robots.txt", "identity", "HEAD", "clashlens.net");
    expect(head.status).toBe(200);
    expect(head.body.byteLength).toBe(0);
    // Only the live host is indexed, whatever else reaches the website.
    const loopback = await getResponse(
      "/players/%232PP",
      "identity",
      "GET",
      "127.0.0.1:3000",
    );
    expect(loopback.headers["x-robots-tag"]).toBe("noindex, nofollow");
    const preview = await getResponse("/x", "identity", "GET", "preview.clashlens.net");
    expect(preview.headers.location).toBe("https://clashlens.net/x");
  },
);

it("moves every preview address to the same address on clashlens.net", async () => {
  const path = "/leaderboards/tracked?view=live&page=2";
  await startServer("https://preview.clashlens.net");
  const page = await getResponse(
    "/players/%232PP",
    "identity",
    "GET",
    "preview.clashlens.net",
  );
  expect(page.status).toBe(200);
  await startServer("https://clashlens.net");
  for (const [request, location] of [
    [path, `https://clashlens.net${path}`],
    ["/players/%232PP", "https://clashlens.net/players/%232PP"],
    ["/assets/app.js", "https://clashlens.net/assets/app.js"],
    ["/robots.txt", "https://clashlens.net/robots.txt"],
    // A path that looks like another host stays a path on clashlens.net.
    ["//example.com/x?y=1", "https://clashlens.net/x?y=1"],
  ]) {
    for (const method of ["GET", "HEAD", "POST"]) {
      const response = await getResponse(
        request,
        "identity",
        method,
        "Preview.ClashLens.net",
      );
      expect(response.status).toBe(301);
      expect(response.headers.location).toBe(location);
    }
  }
});
