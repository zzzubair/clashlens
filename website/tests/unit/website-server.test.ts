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
  const build = {
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
  server = await createWebsiteServer(build, directory);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Missing test port");
  port = address.port;
});

afterEach(async () => {
  releaseStream?.();
  if (server)
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  if (directory) await rm(directory, { recursive: true, force: true });
});

function getResponse(path: string, encoding = "gzip", method = "GET") {
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
        headers: { "Accept-Encoding": encoding },
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
